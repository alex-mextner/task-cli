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
- The pid-file is informational; liveness is a real ``kill(pid, 0)`` probe (a stale file whose
  process died reads as "stale", never "running"). One daemon per repo coordinate — the state
  files are keyed by the coordinate so two repos on one machine never collide.
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
    """Where this coordinate's daemon keeps its pid-file, notified-state, and log."""

    pid: Path
    state: Path
    log: Path


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
    """The pid recorded in ``pid_path`` (a positive int), or ``None`` if absent/garbage."""
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.isdigit():
        return None
    pid = int(content)
    return pid if pid > 0 else None


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


def pid_status(pid_path: Path) -> tuple[str, int | None]:
    """Classify the daemon: ``("running"|"stale"|"stopped", pid_or_None)``.

    "stale" = the pid-file exists but its process is gone (a crash/kill left the file behind).
    The pid-file is informational; this liveness probe is the real truth.
    """
    pid = read_pid(pid_path)
    if pid is None:
        return "stopped", None
    return ("running", pid) if is_alive(pid) else ("stale", pid)


def _write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def _clear_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


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
    _write_pid(paths.pid, os.getpid())
    log_event("daemon.start", pid=os.getpid(), interval_s=dcfg.interval_s, due_soon_days=dcfg.due_soon_days)

    stop = _install_stop_handlers()
    ticks = 0
    try:
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
    if status == "stale":
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
    ``"not-running"`` (nothing alive; a stale pid-file is cleared), or ``"timeout"`` (it didn't
    exit within ``timeout_s`` — we still clear the file and report). Sends SIGTERM (clean), never
    SIGKILL: the loop's TERM handler clears its own pid-file and exits the inter-tick sleep at once.
    """
    paths = paths_for(coordinate, env=env)
    status, pid = pid_status(paths.pid)
    if status != "running" or pid is None:
        _clear_pid(paths.pid)
        return "not-running", pid

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(paths.pid)
        return "not-running", pid

    if _wait_gone(pid, timeout_s):
        _clear_pid(paths.pid)
        return "stopped", pid

    # TERM didn't take in time — escalate to SIGKILL so stop never leaves a live daemon behind.
    # A short second wait confirms; if even that fails the process is wedged uninterruptibly
    # (rare), and we report "timeout" honestly rather than pretend it's gone.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        _clear_pid(paths.pid)
        return "stopped", pid
    gone = _wait_gone(pid, 2.0)
    _clear_pid(paths.pid)
    return ("stopped", pid) if gone else ("timeout", pid)


def _wait_gone(pid: int, timeout_s: float) -> bool:
    """Poll until ``pid`` is no longer alive, or ``timeout_s`` elapses. Returns whether it died."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)
