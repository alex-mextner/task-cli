"""The due-date reminder daemon — a background poll loop that nudges the CTO.

WHAT this is: the minimal shippable core of the task-cli daemon epic (issues #2/#3). A
long-lived process (`task daemon run`, spawned detached by `task daemon start`) that, on a
fixed interval, queries the backend for OPEN tickets whose due date is due-soon or overdue and
pushes a reminder to the CTO's channel (the `tg` CLI by default). Deferred to later increments:
backend webhooks, tmux-inject completion notifications, recurring/escalating reminders.

HOW it is reached at runtime: `tasklib.cli`'s `daemon` subcommand group dispatches here.
`start` resolves the lifecycle paths, checks liveness (idempotent — never double-spawns), and
re-execs `task daemon run` detached. `run` takes the loop; `stop`/`status` read the pid-file.

INVARIANTS this assumes:
- The pid-file is informational; liveness is a real ``kill(pid, 0)`` probe AND a pid-identity
  check (a stale file whose process died reads as "stale", and a file whose pid was recycled for a
  foreign process reads as "not-ours" — never "running"). ``status``/``start``/``stop`` all key off
  the same identity-aware :func:`pid_status`, so a recycled pid is classified consistently across the
  three commands (#32). One daemon per repo coordinate — the state files are keyed by the coordinate
  so two repos on one machine never collide.
- The loop is FAIL-SOFT end to end: a backend error, a malformed ticket, or a notifier failure
  in one tick is caught, logged, and the loop continues. Nothing a single tick does can wedge
  the daemon — that is the whole point of a watcher you are supposed to forget about.
- De-dupe is keyed on (ticket id → the due date last notified for it). The same ticket+due is
  notified at most once; a CHANGED due date (or first crossing into the window) re-notifies.

PAST BUGS guarded here: a notifier subprocess that hangs would freeze the loop, so the notifier
call carries its own timeout; a tick that raises must not escape ``_tick`` (the bare-except is
deliberate and logged).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging import log_event
from .model import State, Ticket

if TYPE_CHECKING:
    from .config import LoadedConfig

# ── config ──────────────────────────────────────────────────────────────────────────

_DEFAULT_INTERVAL_S = 3600  # poll once an hour by default
_DEFAULT_DUE_SOON_DAYS = 3  # a ticket due within 3 days (or overdue) is "due-soon"
_DEFAULT_NOTIFIER = ["tg", "--tag", "report"]  # the CTO's channel; the message is the last arg
_NOTIFIER_TIMEOUT_S = 30  # a notifier call must never hang the loop
_DEFAULT_QUERY_LIMIT = 100  # tickets fetched per tick (configurable for big/old projects)


@dataclass(frozen=True)
class DaemonConfig:
    """The daemon's tunables, read from the config ``daemon:`` block (all optional)."""

    interval_s: int = _DEFAULT_INTERVAL_S
    due_soon_days: int = _DEFAULT_DUE_SOON_DAYS
    notifier: tuple[str, ...] = tuple(_DEFAULT_NOTIFIER)
    enabled: bool = True
    query_limit: int = _DEFAULT_QUERY_LIMIT

    @classmethod
    def from_config(cls, cfg: "LoadedConfig") -> "DaemonConfig":
        """Build from the ``daemon:`` config section, falling back to the defaults per key."""
        block = cfg.section("daemon")
        return cls(
            interval_s=_pos_int(block.get("interval_s"), _DEFAULT_INTERVAL_S),
            due_soon_days=_pos_int(block.get("due_soon_days"), _DEFAULT_DUE_SOON_DAYS),
            notifier=_notifier_argv(block.get("notifier")),
            enabled=_as_bool(block.get("enabled"), default=True),
            query_limit=_pos_int(block.get("query_limit"), _DEFAULT_QUERY_LIMIT),
        )


def _pos_int(value: Any, default: int) -> int:
    """A positive int, else the default (a 0/negative/garbage value can't make a busy loop)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _as_bool(value: Any, *, default: bool) -> bool:
    """Coerce a config value to bool, treating the string ``"false"``/``"no"``/``"0"`` as False.

    A YAML ``enabled: false`` already parses to a real bool, but a quoted ``"false"`` (or a value
    from another source) would be a truthy non-empty string — ``bool("false") is True`` — and
    silently re-enable a daemon someone meant to disable. Normalize the common falsey strings.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)


def _notifier_argv(value: Any) -> tuple[str, ...]:
    """Resolve the notifier command to an argv tuple.

    Accepts a list (``["tg", "--tag", "report"]``) verbatim, or a string that is split on
    whitespace. The message text is appended as the final argument at notify time. An empty /
    malformed value falls back to the default ``tg`` invocation.
    """
    if isinstance(value, list) and value:
        return tuple(str(x) for x in value)
    if isinstance(value, str) and value.strip():
        return tuple(value.split())
    return tuple(_DEFAULT_NOTIFIER)


# ── lifecycle paths (one daemon per repo coordinate) ────────────────────────────────


@dataclass(frozen=True)
class DaemonPaths:
    """Where this coordinate's daemon keeps its pid-file, notified-state, log, and singleton lock.

    ``lock`` defaults to the pid-file's path with a ``.lock`` suffix so existing constructors that
    pass only ``pid``/``state``/``log`` keep working; the live daemon holds an exclusive flock on
    it for its whole lifetime, which is the race-free singleton guarantee (see :func:`acquire_singleton`).
    """

    pid: Path
    state: Path
    log: Path
    lock: Path | None = None

    @property
    def lock_path(self) -> Path:
        # APPEND ".lock" to the full pid-file name (not with_suffix, which would replace whatever
        # follows the last dot in the stem — fragile if the coordinate key ever contains a dot).
        return self.lock if self.lock is not None else self.pid.parent / (self.pid.name + ".lock")


def _state_dir(env: dict[str, str] | None = None) -> Path:
    """The base dir for daemon state files (``$XDG_STATE_HOME`` → ``~/.local/state``)."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "task-cli" / "daemon"


def _coordinate_key(coordinate: str) -> str:
    """A filesystem-safe key from a repo/team coordinate (``owner/name`` → ``owner__name``)."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in coordinate)
    return safe or "default"


def paths_for(coordinate: str, *, env: dict[str, str] | None = None) -> DaemonPaths:
    """Resolve the per-coordinate state-file paths (created lazily by the writers)."""
    base = _state_dir(env)
    key = _coordinate_key(coordinate)
    return DaemonPaths(
        pid=base / f"{key}.pid",
        state=base / f"{key}.notified.json",
        log=base / f"{key}.log",
    )


# ── pid-file + liveness ─────────────────────────────────────────────────────────────


def read_pid(pid_path: Path) -> int | None:
    """The pid recorded in ``pid_path`` (a positive int), or ``None`` if absent/garbage.

    The pid is the FIRST line; an optional second line records the daemon's launch identity (see
    :func:`read_recorded_identity`). Reading only the first line keeps an older single-line pid-file
    fully compatible.
    """
    try:
        first = pid_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not first.isdigit():
        return None
    pid = int(first)
    return pid if pid > 0 else None


def read_recorded_identity(pid_path: Path) -> str | None:
    """The daemon's recorded launch identity (the pid-file's second line), or ``None`` if absent.

    The running daemon writes its OWN argv signature here at startup (:func:`run_loop`). ``stop`` then
    matches the LIVE pid's argv against this recorded value, which is robust to ANY entrypoint shape
    — a renamed console-script, a frozen/PyInstaller binary — because it compares against what THIS
    daemon actually launched as instead of guessing a pattern.
    """
    try:
        lines = pid_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    recorded = lines[1].strip()
    return recorded or None


def argv_signature(tokens: list[str]) -> str:
    """A normalized, SINGLE-LINE, STABLE signature of an argv token list — argv[0] DROPPED.

    Two normalizations make the signature reproducible across reads of the SAME process:

    - ``argv[0]`` (the interpreter/executable path) is DROPPED entirely. It is NOT stable between
      reads: a venv ``python3`` reads as the symlink path at one moment and the resolved real path
      (or even a different basename like ``Python``) at another, depending on how ``ps`` resolves it
      — so including it would make a live daemon's recorded signature mismatch its OWN later read and
      orphan the daemon. ``argv[1:]`` — ``-m tasklib daemon run -C <coordinate>`` — is the stable AND
      discriminating part: it pins the tool, the subcommand, AND the coordinate (so a recycled pid
      running a DIFFERENT coordinate's daemon still mismatches and is correctly foreign).
    - Internal whitespace (newlines/tabs) inside any remaining token is collapsed to single spaces,
      so the signature is always ONE line — it is stored as the second line of the pid-file, and a
      token with a literal newline would otherwise split the record and truncate the recorded value.
    """
    norm = [" ".join(tok.split()) for tok in tokens if tok.split()]
    return " ".join(norm[1:])  # drop argv[0]; argv[1:] is the stable, discriminating signature


def _self_argv() -> list[str]:
    """This process's OWN argv as :func:`process_cmdline` would read it for another pid.

    Reading our own ``/proc/self``/``ps`` view (not ``sys.argv``) makes the recorded identity match
    what ``stop`` later reads for this pid — same source, same tokenization — so the signatures line
    up exactly. Falls back to ``sys.argv`` if our own cmdline somehow can't be read.
    """
    tokens = process_cmdline(os.getpid())
    return tokens if tokens is not None else list(sys.argv)


def is_alive(pid: int) -> bool:
    """``True`` if ``pid`` is a live process (a ``kill(pid, 0)`` probe; no signal delivered)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # the process exists but we don't own it — for our own daemon this won't happen, but
        # "exists" is the honest answer to a liveness probe.
        return True
    return True


# The substring that identifies OUR daemon in a process's argv. The child is launched as
# `python -m tasklib daemon run …` (see _spawn_detached), so this pair appears in its command
# line and not in an unrelated process that merely happened to be assigned the recycled pid. We
# match the `daemon run` SUBCOMMAND tokens as a CONTIGUOUS pair (a substring-anywhere match would
# treat an innocent `grep "tasklib daemon run"` with a recycled pid as the daemon and SIGKILL it),
# AND require that the token immediately before `daemon` references this tool (`tasklib` for the
# `-m tasklib` module form, or a `task` executable basename for the console-script form). Both
# launch shapes qualify: `python -m tasklib daemon run …` (how _spawn_detached starts it) AND the
# public console-script form `/path/task daemon run …` (systemd unit / Docker ENTRYPOINT) — matching
# only the `-m tasklib` form would mis-classify a console-script daemon as foreign and orphan it.

# The tri-state result of identifying a pid against the daemon's expected argv.
IDENTITY_DAEMON = "daemon"  # the cmdline is readable AND carries the daemon token sequence
IDENTITY_FOREIGN = "foreign"  # the cmdline is readable AND does NOT carry it (a recycled pid)
IDENTITY_UNKNOWN = "unknown"  # the cmdline could not be read at all (ps absent / busybox / timeout)


def process_cmdline(pid: int) -> list[str] | None:
    """The argv of ``pid`` as a token LIST, or ``None`` if it can't be read.

    Prefers Linux's ``/proc/<pid>/cmdline`` (NUL-separated → exact, un-split tokens), which is also
    the reliable path inside the minimal Docker images this project tests in (busybox ``ps`` often
    rejects ``-p``/``-o args=``). Falls back to ``ps -p <pid> -o args=`` (whitespace-split, so a
    token containing a space is approximated) on platforms without ``/proc`` (macOS). ``None`` means
    "could not read" — distinct from "read it and it isn't ours"; the caller MUST keep them apart so
    an unreadable cmdline never orphans a real daemon (see :func:`identify_pid`).
    """
    proc_tokens = _read_proc_cmdline(pid)
    if proc_tokens is not None:
        return proc_tokens
    return _read_ps_cmdline(pid)


def _read_proc_cmdline(pid: int) -> list[str] | None:
    """Linux ``/proc/<pid>/cmdline`` → NUL-separated argv tokens, or ``None`` if unavailable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None  # no /proc (macOS) or the process is gone
    if not raw:
        return None  # a kernel thread / zombie has an empty cmdline — fall back to ps
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def _read_ps_cmdline(pid: int) -> list[str] | None:
    """``ps -ww -p <pid> -o args=`` → whitespace-split argv tokens, or ``None`` if ps can't read it.

    ``-ww`` disables ``ps``'s default command-line TRUNCATION (to ~screen width). Without it a long
    argv is cut off, so the signature derived from a truncated ``ps`` read would not match the one
    derived from the full ``/proc`` read — and a daemon whose identity was recorded from ``/proc`` at
    startup would be mis-classified foreign and ORPHANED when ``stop`` later falls back to ``ps``.
    """
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    line = out.stdout.strip()
    return line.split() if line else None


def argv_is_task_daemon(tokens: list[str]) -> bool:
    """True when an argv ``tokens`` list is recognizably a ``task daemon run`` process.

    Two launch shapes qualify (both contiguous ``daemon run`` + a reference to this tool):

    - ``python -m tasklib daemon run …`` — how :func:`_spawn_detached` starts the child.
    - ``/path/to/task daemon run …`` — the public console-script form (systemd / Docker ENTRYPOINT).

    The ``daemon run`` pair must be contiguous (so a loose ``grep "tasklib daemon run"`` is rejected),
    and the token IMMEDIATELY BEFORE ``daemon`` must be this tool — ``tasklib`` (the ``-m tasklib``
    module form) or a ``task`` executable basename (the console-script form). Anchoring the tool
    reference to the subcommand position rejects an unrelated ``foo daemon run`` and a stray ``task``
    token elsewhere in the argv (``["foo", "task", "daemon", "run"]``).
    """
    for i in range(len(tokens) - 1):
        if tokens[i] == "daemon" and tokens[i + 1] == "run" and i >= 1:
            preceding = tokens[i - 1]
            if preceding == "tasklib" or os.path.basename(preceding) == "task":
                return True
    return False


def identify_pid(pid: int, *, recorded_identity: str | None = None) -> str:
    """Classify ``pid`` against the daemon's expected argv: daemon / foreign / unknown (tri-state).

    The guard against the reused-pid hazard: after a daemon crash the OS can recycle its pid for an
    unrelated process of the same user, which a bare ``kill(pid, 0)`` liveness probe reads as
    "running". We inspect the live pid's argv:

    - ``IDENTITY_DAEMON``  — readable AND either (a) it matches ``recorded_identity`` — the argv
      signature THIS daemon wrote into its pid-file at startup, which makes the check robust to ANY
      entrypoint (a renamed console-script, a frozen/PyInstaller binary) — or (b) it is recognizably
      ``task daemon run`` by shape (:func:`argv_is_task_daemon`), the fallback for an old pid-file
      with no recorded identity.
    - ``IDENTITY_FOREIGN`` — readable AND neither matches → a recycled pid; ``stop`` must NOT signal it.
    - ``IDENTITY_UNKNOWN`` — the cmdline could not be read at all (busybox ``ps``, no ``/proc``, a
      timeout). We deliberately do NOT call this "foreign": refusing to signal on an unreadable
      cmdline would ORPHAN a real daemon in exactly the minimal Docker images this project tests in.
      The caller preserves the pre-guard signalling behavior on ``unknown``.

    ACCEPTED RESIDUAL RISK (legacy pid-files only): with NO ``recorded_identity`` (a single-line
    pid-file written before this version) the shape fallback recognizes ANY ``…daemon run…`` process,
    so a pid recycled by a DIFFERENT coordinate's daemon could be mis-claimed. The window is one
    restart after upgrade — the next daemon start rewrites the pid-file WITH a recorded identity,
    after which the authoritative path closes it. New daemons always record an identity.
    """
    tokens = process_cmdline(pid)
    if tokens is None:
        return IDENTITY_UNKNOWN
    if recorded_identity:
        # When we HAVE a recorded identity, it is AUTHORITATIVE: an exact match → ours, anything
        # else → foreign. We must NOT fall through to the shape matcher here — that recognizes ANY
        # `daemon run` process regardless of coordinate, so a recycled pid running a DIFFERENT
        # coordinate's daemon (`…daemon run -C /other`) would be mis-claimed and killed. The shape
        # matcher is ONLY the fallback for a legacy pid-file that has no recorded identity.
        return IDENTITY_DAEMON if argv_signature(tokens) == recorded_identity else IDENTITY_FOREIGN
    return IDENTITY_DAEMON if argv_is_task_daemon(tokens) else IDENTITY_FOREIGN


def is_task_daemon(pid: int) -> bool:
    """``True`` only when ``pid``'s argv POSITIVELY identifies it as this task daemon.

    Convenience over :func:`identify_pid` — ``True`` for ``IDENTITY_DAEMON`` only. An UNKNOWN cmdline
    (unreadable) is ``False`` here, so callers that need to avoid orphaning a daemon on an unreadable
    cmdline should branch on :func:`identify_pid` (``foreign`` vs ``unknown``), not this boolean.
    """
    return identify_pid(pid) == IDENTITY_DAEMON


def pid_status(pid_path: Path) -> tuple[str, int | None]:
    """Classify the daemon: ``("running"|"not-ours"|"stale"|"stopped", pid_or_None)``.

    - ``"stopped"`` — no pid-file (or garbage): nothing to talk about.
    - ``"stale"`` — the pid-file exists but its process is gone (a crash/kill left the file behind).
    - ``"not-ours"`` — the pid is ALIVE but is POSITIVELY a foreign process (a crash + OS pid-reuse):
      the same identity check ``stop`` makes, threaded through here so ``status``/``start`` agree with
      ``stop`` instead of a bare liveness probe reporting a recycled pid as ``"running"`` (#32).
    - ``"running"`` — the pid is alive AND identifies as our daemon, OR its cmdline can't be read at
      all (``IDENTITY_UNKNOWN`` — busybox ``ps`` / no ``/proc``). The UNKNOWN case stays ``"running"``
      ON PURPOSE: calling an unreadable cmdline "foreign" would mis-report (and, via ``start``, double-
      spawn over) a real daemon in exactly the minimal Docker images this project tests in — the same
      reason ``stop`` preserves its pre-guard signalling on UNKNOWN.

    The pid-file is informational; the liveness probe + identity check are the real truth. Matching
    ``stop``'s recorded-identity-first classification keeps the three commands consistent: a recycled
    pid reads as ``"not-ours"`` everywhere, never ``"running"`` in one command and ``"not-ours"`` in
    another.
    """
    pid = read_pid(pid_path)
    if pid is None:
        return "stopped", None
    if not is_alive(pid):
        return "stale", pid
    recorded = read_recorded_identity(pid_path)
    if identify_pid(pid, recorded_identity=recorded) == IDENTITY_FOREIGN:
        return "not-ours", pid
    return "running", pid


def _write_pid(pid_path: Path, pid: int, *, identity: str | None = None) -> None:
    """Write the pid (line 1) and, when given, the daemon's launch identity (line 2).

    The optional identity is the daemon's own argv signature; ``stop`` matches the live pid's argv
    against it so the daemon is recognized regardless of its entrypoint shape (see :func:`identify_pid`).
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{pid}\n{identity}\n" if identity else str(pid)
    pid_path.write_text(payload, encoding="utf-8")


def _clear_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _clear_pid_if_matches(pid_path: Path, pid: int) -> None:
    """Clear the pid-file ONLY if it still records ``pid`` — never delete a DIFFERENT daemon's file.

    Guards a narrow race in ``stop``: while we waited on a signalled pid, the old daemon could have
    exited, freed the flock, and a fresh ``start`` for the same coordinate could have written a NEW
    pid into the same file. Deleting unconditionally would orphan that live new daemon's pid-file.
    Re-reading and matching the pid first makes the clear safe.
    """
    if read_pid(pid_path) == pid:
        _clear_pid(pid_path)


# ── flock singleton (race-free "never double-starts") ───────────────────────────────


def acquire_singleton(lock_path: Path):  # type: ignore[no-untyped-def]
    """Take an exclusive, non-blocking flock on ``lock_path``. Returns the held handle, or ``None``.

    The race-free singleton primitive behind the docstring's "never double-starts": the live daemon
    holds this lock for its WHOLE lifetime (it is acquired in :func:`run_loop` before the loop). Two
    near-simultaneous ``start``s both pass the liveness check and both spawn — but only ONE child
    wins this flock; the loser gets ``None`` and exits without ticking. The OS lock closes the
    check-then-spawn TOCTOU that the pid-file alone can't.

    Returns the open file object on success (the CALLER must keep it open — closing it, or the
    process exiting, releases the lock). Returns ``None`` when another live daemon already holds it.
    On a platform without ``fcntl`` (non-POSIX) it degrades to "no lock available" → returns the
    open handle UNLOCKED so the daemon still runs (the pid-file liveness check remains the guard).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a", encoding="utf-8")  # noqa: SIM115 - lifetime is the daemon's
    try:
        import fcntl
    except ImportError:
        # non-POSIX (Windows): no flock available. We degrade to "no singleton lock" and rely on the
        # pid-file liveness check (with its narrower TOCTOU). Log it so the lost guarantee isn't silent.
        log_event("daemon.singleton-unavailable", level="WARN", reason="fcntl not available")
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()  # another live daemon holds the lock → we are the loser, release our fd
        return None
    return handle


# ── due-date selection (pure; the unit-tested core) ─────────────────────────────────


def is_open(ticket: Ticket) -> bool:
    """An OPEN ticket is one not in a terminal state — only these get reminders."""
    return ticket.state not in (State.DONE, State.CANCELLED)


def select_due(tickets: list[Ticket], *, today: date, due_soon_days: int) -> list[Ticket]:
    """The open tickets whose due date is overdue or within ``due_soon_days`` of ``today``.

    Pure and side-effect-free so it is exactly testable: a ticket qualifies iff it is open, has
    a parseable due date, and that date is ``<= today + due_soon_days``. An overdue ticket (due
    date in the past) always qualifies. A ticket with no/garbage due date never qualifies (the
    daemon ignores it rather than crashing on it).
    """
    from datetime import timedelta

    horizon = today + timedelta(days=max(due_soon_days, 0))
    selected: list[Ticket] = []
    for ticket in tickets:
        if not is_open(ticket):
            continue
        due = ticket.due_date()
        if due is None:
            continue
        if due <= horizon:
            selected.append(ticket)
    return selected


def _reminder_text(ticket: Ticket, *, today: date) -> str:
    """The human reminder line for one ticket (overdue vs due-in-N-days)."""
    due = ticket.due_date()
    assert due is not None  # select_due only yields tickets with a parseable due date
    delta = (due - today).days
    if delta < 0:
        when = f"OVERDUE by {-delta}d (was due {ticket.due})"
    elif delta == 0:
        when = f"due TODAY ({ticket.due})"
    else:
        when = f"due in {delta}d ({ticket.due})"
    label = ticket.id or "(new)"
    return f"task reminder: {label} {when} — {ticket.title}"


# ── de-dupe state ───────────────────────────────────────────────────────────────────


def load_notified(state_path: Path) -> dict[str, str]:
    """The ``{ticket_id: due_date}`` already-notified map (empty on a missing/corrupt file)."""
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def save_notified(state_path: Path, notified: dict[str, str]) -> None:
    """Persist the notified map atomically (temp + rename; best-effort, never raised).

    A crash mid-write must not leave a truncated JSON file: write to a sibling temp then
    ``os.replace`` (atomic on the same filesystem). ``load_notified`` tolerates a corrupt file
    anyway (returns ``{}``), so the worst case is a duplicate reminder — the atomic write makes
    even that unlikely.
    """
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(notified, sort_keys=True), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as exc:
        log_event("daemon.state-write-failed", level="WARN", error=str(exc))


def needs_notify(ticket: Ticket, notified: dict[str, str]) -> bool:
    """``True`` if this ticket hasn't been notified for its CURRENT due date.

    De-dupe key is (id → due date last notified). The same ticket+due is skipped; a ticket whose
    due date CHANGED since we last notified it re-fires (the deadline moved — the CTO should know).
    """
    return notified.get(ticket.id) != ticket.due


# ── notification ────────────────────────────────────────────────────────────────────


def notify(message: str, notifier: tuple[str, ...]) -> bool:
    """Send ``message`` via the configured notifier command. Returns success; never raises.

    The notifier argv is the configured prefix (default ``tg --tag report``) with the message
    appended as the final argument. A timeout / non-zero exit / missing binary is caught and
    logged — a down notifier must NEVER wedge the loop (the reminder is simply retried next tick
    because it stays un-deduped on failure).
    """
    argv = [*notifier, message]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_NOTIFIER_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        log_event("daemon.notify-failed", level="WARN", reason="notifier not found", cmd=notifier[0])
        return False
    except subprocess.SubprocessError as exc:
        log_event("daemon.notify-failed", level="WARN", reason=str(exc))
        return False
    if result.returncode != 0:
        log_event("daemon.notify-failed", level="WARN", code=result.returncode, stderr=result.stderr.strip()[:200])
        return False
    return True


# ── the tick + loop ─────────────────────────────────────────────────────────────────


def _query_open_due(backend, dcfg: DaemonConfig, *, today: date) -> list[Ticket]:
    """Fetch and select the open due-soon/overdue tickets (the backend half of a tick).

    The fetch is capped at ``dcfg.query_limit`` (default 100). On a very large/old project an
    overdue ticket beyond the cap could be missed in a single tick; raise ``daemon.query_limit``
    in config for such a project. The state filter is applied client-side in :func:`select_due`
    because "open" spans three normalized states and the backends' ``list`` filters one at a time.
    """
    tickets = backend.list(limit=dcfg.query_limit)
    return select_due(tickets, today=today, due_soon_days=dcfg.due_soon_days)


def run_tick(backend, dcfg: DaemonConfig, paths: DaemonPaths, *, today: date | None = None) -> int:
    """One reminder pass: query → select → notify the not-yet-notified → persist de-dupe state.

    Returns the number of reminders actually sent this tick. FAIL-SOFT: any exception (a backend
    error, a malformed row, anything) is caught and logged so the surrounding loop survives. A
    ticket is recorded as notified ONLY after its reminder send succeeds, so a notifier outage
    re-tries it next tick instead of silently dropping the reminder.
    """
    today = today or date.today()
    try:
        due = _query_open_due(backend, dcfg, today=today)
    except Exception as exc:  # noqa: BLE001 - a backend/parse failure must not kill the loop
        log_event("daemon.tick-query-failed", level="WARN", error=str(exc))
        return 0

    notified = load_notified(paths.state)
    # Prune the de-dupe state to the ids still in this tick's due set, so a closed / resolved /
    # no-longer-due ticket drops its record instead of accumulating forever — the file stays
    # bounded by the number of CURRENTLY due tickets, not every ticket ever reminded. A ticket
    # that drops out and later becomes due again will (correctly) re-notify.
    due_ids = {t.id for t in due}
    pruned = {tid: when for tid, when in notified.items() if tid in due_ids}
    changed = pruned != notified
    notified = pruned

    sent = 0
    for ticket in due:
        if not ticket.id:
            # A backend-returned ticket always has an id; an id-less one can't be de-duped
            # (every empty id collides on one state slot) — skip it rather than mis-dedupe.
            continue
        if not needs_notify(ticket, notified):
            continue
        if notify(_reminder_text(ticket, today=today), dcfg.notifier):
            notified[ticket.id] = ticket.due
            sent += 1
            changed = True
    if changed:
        save_notified(paths.state, notified)
    log_event("daemon.tick", due=len(due), sent=sent)
    return sent


def run_loop(cfg: "LoadedConfig", paths: DaemonPaths, *, max_ticks: int | None = None) -> int:
    """The foreground daemon loop: write the pid-file, then tick forever (or ``max_ticks``).

    Installs SIGTERM/SIGINT handlers so ``task daemon stop`` (a TERM) exits cleanly and clears
    the pid-file. ``max_ticks`` bounds the loop for tests; ``None`` runs until signalled. The
    backend is rebuilt each tick from ``cfg`` so a transient construction failure (creds not yet
    present) is just a skipped tick, not a dead daemon.
    """
    from .backends import get_backend

    dcfg = DaemonConfig.from_config(cfg)
    if not dcfg.enabled:
        # A direct `task daemon run` must honor the disable switch too, not only `start` — so a
        # config that turns the daemon off can't be bypassed by invoking the loop directly.
        log_event("daemon.disabled", level="INFO")
        return 0

    # The singleton flock is the race-free guarantee: if another live daemon already holds it, we
    # are a duplicate (two concurrent `start`s raced past the liveness check) — exit at once WITHOUT
    # touching the pid-file, so the winner's pid-file stays authoritative.
    lock_handle = acquire_singleton(paths.lock_path)
    if lock_handle is None:
        log_event("daemon.already-running", level="INFO")
        return 0

    # Everything after a successful acquire is inside try/finally, so the lock is released even if
    # _write_pid / handler-install raises — no leaked flock that would wedge the next start.
    ticks = 0
    try:
        # Record THIS process's own argv signature in the pid-file, so stop() can match the live pid
        # against exactly what we launched as — robust to any entrypoint shape, not a guessed pattern.
        _write_pid(paths.pid, os.getpid(), identity=argv_signature(_self_argv()))
        log_event("daemon.start", pid=os.getpid(), interval_s=dcfg.interval_s, due_soon_days=dcfg.due_soon_days)
        stop = _install_stop_handlers()
        while not stop.requested:
            try:
                backend = get_backend(cfg)
                run_tick(backend, dcfg, paths)
            except Exception as exc:  # noqa: BLE001 - even backend construction must not kill us
                log_event("daemon.tick-failed", level="WARN", error=str(exc))
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            stop.wait(dcfg.interval_s)
    finally:
        _clear_pid(paths.pid)
        lock_handle.close()  # release the singleton lock (also released on process exit)
        log_event("daemon.stop", ticks=ticks)
    return 0


class _StopFlag:
    """A signal-set stop flag with an interruptible wait (so a TERM ends the sleep at once)."""

    def __init__(self) -> None:
        self.requested = False
        import threading

        self._event = threading.Event()

    def request(self, *_: Any) -> None:
        self.requested = True
        self._event.set()

    def wait(self, seconds: float) -> None:
        # Event.wait returns early when .set() fires, so a signal cuts the inter-tick sleep
        # short instead of blocking a full interval before the loop notices the stop request.
        self._event.wait(timeout=seconds)


def _install_stop_handlers() -> _StopFlag:
    flag = _StopFlag()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, flag.request)
        except (ValueError, OSError):
            # signal() only works on the main thread; in a test harness thread we skip it and
            # rely on max_ticks. Not fatal.
            pass
    return flag


# ── start / stop (the detached-spawn + teardown) ────────────────────────────────────


def start(
    coordinate: str,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    child_flags: list[str] | None = None,
) -> tuple[str, int | None]:
    """Idempotently bring up the detached daemon for ``coordinate``.

    Returns ``(outcome, pid)`` where outcome is ``"already-running"`` (no-op, a live daemon was
    found) or ``"started"`` (a fresh detached daemon was spawned). The liveness check BEFORE
    spawning is what makes start idempotent — a second start while one is alive never
    double-spawns. A stale pid-file (crashed daemon) is cleared and a fresh one started. The
    ``daemon.enabled`` switch is enforced by the caller (``cmd_daemon``/``run_loop``), not here —
    ``start`` is the pure spawn primitive.

    ``child_flags`` are the backend-selecting global flags (``--backend``/``--repo``/``--config``)
    the CLI resolved the coordinate WITH; they are forwarded to the spawned ``daemon run`` so the
    child re-resolves to the SAME coordinate. Without them an override that changes resolution
    (e.g. ``--repo X``) would make the child write its pid/state under a different coordinate than
    the one ``start`` checked — breaking idempotency and ``stop``/``status``.
    """
    paths = paths_for(coordinate, env=env)
    status, pid = pid_status(paths.pid)
    if status == "running":
        return "already-running", pid
    # A stale (dead) pid-file OR a "not-ours" recycled-pid pid-file is misleading and must not block
    # a fresh start: clear it and spawn. Before #32, a recycled foreign pid read as "running" here and
    # start wrongly no-op'd as "already-running" — the inconsistency this fix closes. We do NOT signal
    # the foreign pid (that's stop's job); we just stop trusting the pid-file and start our own daemon.
    if status in ("stale", "not-ours"):
        _clear_pid(paths.pid)

    child = _spawn_detached(cwd=cwd, log_path=paths.log, child_flags=child_flags or [])
    # The child writes its OWN pid-file in run_loop; we don't write the launcher's pid here (the
    # launcher exits immediately). Return the spawned child's pid for the caller's report.
    return "started", child


def _spawn_detached(*, cwd: str | None, log_path: Path, child_flags: list[str]) -> int:
    """Spawn ``task daemon run`` fully detached (new session, stdio to the log). Returns its pid.

    Detachment via ``start_new_session`` (setsid) so the daemon outlives the launching shell and
    is not in its process group — a parent exit can't take it down. stdout/stderr go to the log
    file; stdin is closed. We re-invoke THIS interpreter on ``tasklib`` so the spawn works from a
    git checkout (the ``task`` console script may not be on PATH) and from an installed package.
    ``child_flags`` (the backend-selecting global flags) are forwarded so the child resolves the
    same coordinate the launcher did.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "tasklib", "daemon", "run", "-C", cwd or os.getcwd(), *child_flags]
    logf = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child; closed below
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            cwd=cwd or os.getcwd(),
        )
    finally:
        logf.close()
    return proc.pid


def stop(coordinate: str, *, env: dict[str, str] | None = None, timeout_s: float = 5.0) -> tuple[str, int | None]:
    """Stop the running daemon for ``coordinate``: TERM it, wait, then clear the pid-file.

    Returns ``(outcome, pid)`` — ``"stopped"`` (a live daemon was signalled and is gone),
    ``"not-running"`` (nothing alive; a stale pid-file is cleared), ``"not-ours"`` (the pid is
    alive but is NOT the task daemon — a recycled pid; we refuse to signal it and clear the stale
    file), or ``"timeout"`` (it didn't exit within ``timeout_s`` — we still clear the file and
    report). Sends SIGTERM (clean) before SIGKILL: the loop's TERM handler clears its own pid-file
    and exits the inter-tick sleep at once.

    The PID-IDENTITY guard runs BEFORE any signal: after a daemon crash the OS can recycle its pid
    for an unrelated process of the same user, which a bare liveness probe reads as "running". We
    classify the pid (:func:`identify_pid`), matching FIRST against the argv signature the daemon
    recorded in its pid-file at startup — so a daemon launched via ANY entrypoint (a renamed
    console-script, a frozen binary) is still recognized, not just the ``python -m tasklib`` shape —
    and refuse to signal ONLY when the pid is POSITIVELY foreign (a readable argv matching neither
    the recorded identity nor the daemon shape) → ``"not-ours"``. When the argv can't be read at all
    (``IDENTITY_UNKNOWN`` — busybox ``ps`` / no ``/proc``), we do NOT refuse: signalling the recorded
    pid is the pre-guard behavior, and refusing there would ORPHAN a real daemon in the minimal
    Docker images this project tests in. NOTE the guard before the FIRST SIGTERM is best-effort —
    there is a microscopic window between :func:`identify_pid` and ``os.kill`` where the pid could
    die + be recycled (unclosable without ``pidfd``); the identity is RE-CHECKED before SIGKILL,
    which closes the much wider SIGTERM-wait window.
    """
    paths = paths_for(coordinate, env=env)
    status, pid = pid_status(paths.pid)
    if pid is None or status in ("stopped", "stale"):
        # Nothing alive under this pid-file (or no file at all) — clear any stale file and report.
        _clear_pid(paths.pid)
        return "not-running", pid
    if status == "not-ours":
        # The pid is alive but POSITIVELY not OUR daemon (a crash + OS pid-reuse). pid_status already
        # ran the same identity check stop relies on — refuse to signal it and clear the misleading
        # pid-file so a later start/stop doesn't trip on it again. (status/start now agree here too.)
        log_event("daemon.stop-pid-not-ours", level="WARN", pid=pid)
        _clear_pid(paths.pid)
        return "not-ours", pid

    # The pid is alive AND ours (or unreadable→treated as ours): read the recorded launch identity
    # here, on this live path only, and reuse this SAME value for the pre-SIGKILL re-check below (we
    # capture it ONCE for the SIGTERM→wait→SIGKILL sequence rather than re-reading after the wait).
    # That keeps the pre/post-wait checks within stop consistent — the post-wait branch judges "is the
    # pid we SIGTERM'd still ours?" against what we recorded at THIS moment, not a value a racing
    # same-coordinate restart could have rewritten meanwhile. (pid_status did its own read for the
    # earlier not-ours-vs-running classification; in the recycle case both reads agree on FOREIGN, so
    # the brief duplicate read is harmless — the flock the live daemon holds blocks a mid-wait rewrite.)
    recorded = read_recorded_identity(paths.pid)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(paths.pid)
        return "not-running", pid

    if _wait_gone(pid, timeout_s):
        # Clear only if the file still names the pid we stopped — a fresh same-coordinate start
        # during the wait could already own it.
        _clear_pid_if_matches(paths.pid, pid)
        return "stopped", pid

    # TERM didn't take in time — escalate to SIGKILL so stop never leaves a live daemon behind.
    # RE-CHECK identity first (against the SAME `recorded` read before SIGTERM): during the wait the
    # daemon could have died and the OS could have recycled its pid, so a blind SIGKILL might hit an
    # innocent process. A positively-foreign pid here means exactly that — stop signalling, "not-ours".
    if identify_pid(pid, recorded_identity=recorded) == IDENTITY_FOREIGN:
        # Post-wait: the old daemon may have exited and a fresh start re-written the pid-file with a
        # NEW pid — only clear it if it still points at the pid we were stopping, never the new one.
        log_event("daemon.stop-pid-recycled-before-kill", level="WARN", pid=pid)
        _clear_pid_if_matches(paths.pid, pid)
        return "not-ours", pid
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        _clear_pid_if_matches(paths.pid, pid)
        return "stopped", pid
    gone = _wait_gone(pid, 2.0)
    _clear_pid_if_matches(paths.pid, pid)
    return ("stopped", pid) if gone else ("timeout", pid)


def _wait_gone(pid: int, timeout_s: float) -> bool:
    """Poll until ``pid`` is no longer alive, or ``timeout_s`` elapses. Returns whether it died."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)
