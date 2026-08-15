"""task CLI — argparse + subcommand dispatch (the effectful entrypoint).

The thin entry point (``[project.scripts] task = "tasklib.cli:main"`` and the target of the
``bin/task`` shim). It owns argument parsing, the backend/classify shell-outs, and the
filesystem (sidecar). All pure logic lives in the sibling modules (``model``/``render``/
``policy``/``classify``/``session``/``config``). Heavy/optional imports (yaml via config,
the backends) are lazy so ``task --help`` stays fast and dependency-light.

Subcommands: create/new · list · gantt (read-only due-date timeline) · read/view · find · change
· status · done · classify · session · daemon (the due-date reminder watcher: start/stop/status/run).
Global flags: --backend, --repo, --config, --json, --yes, and per-gate --skip-<gate>.
"""

from __future__ import annotations

import argparse
import html
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .model import Criterion, Screenshot, State, Ticket
from .transitions import TransitionError, validate_transition

# ── tiny output helpers (no color dep; honor NO_COLOR) ──────────────────────────────
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _ok(s: str) -> str:
    return _c("32", s)


def _warn(s: str) -> str:
    return _c("33", s)


def _err(s: str) -> str:
    return _c("31", s)


def _dim(s: str) -> str:
    return _c("2", s)


def _bold(s: str) -> str:
    return _c("1", s)


# Per-gate escape-hatch flags: --skip-<gate> "<reason>".
_SKIP_FLAGS = (
    ("--skip-acceptance", "acceptance-criteria"),
    ("--skip-motivation", "motivation"),
    ("--skip-user-impact", "user-impact"),
    ("--skip-user-impact-quality", "user-impact-quality"),
    ("--skip-cost-of-inaction", "cost-of-inaction"),
    ("--skip-screenshots", "screenshots"),
    ("--skip-formatting", "formatting"),
    ("--skip-links", "links"),
    ("--skip-links-url", "links-url"),
    ("--skip-msgref-quote", "msgref-quote"),
)


def _add_skip_flags(p: argparse.ArgumentParser) -> None:
    for flag, gate in _SKIP_FLAGS:
        dest = "skip_" + gate.replace("-", "_")
        p.add_argument(flag, dest=dest, metavar="REASON", help=f"skip the {gate} gate with a recorded justification")


def _add_create_args(p: argparse.ArgumentParser) -> None:
    """The ticket-creation argument set, shared by `create` and its `new` alias."""
    p.add_argument("--title", help="ticket title")
    p.add_argument("--from-message", dest="from_message", metavar="TEXT", help="raw user text → derive title/body")
    p.add_argument("--what", help="the change (one paragraph)")
    p.add_argument("--acceptance", action="append", default=[], metavar="CRIT", help="acceptance criterion (repeatable)")
    p.add_argument("--why", help="motivation")
    p.add_argument("--impact", help="user impact")
    p.add_argument("--if-not-done", dest="if_not_done", help="cost of inaction")
    p.add_argument("--screenshot", action="append", default=[], metavar="PATH", help="screenshot (repeatable)")
    p.add_argument("--label", action="append", default=[], help="label (repeatable)")
    p.add_argument("--due", metavar="YYYY-MM-DD", help="due date (the daemon reminds before/at it)")
    p.add_argument("--yes", action="store_true", help="non-interactive; do not prompt")
    p.add_argument(
        "--force",
        dest="force_reason",
        metavar="REASON",
        help="override the links / user-impact-quality gates, or a detected close-duplicate "
        "ticket, with a recorded reason (a false-positive link match, a genuinely-N/A impact, "
        "or an intentional duplicate)",
    )
    _add_skip_flags(p)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="task",
        description="task — the enforced ticket interface. Every request becomes a "
        "well-formed ticket (GitHub Issues / Linear); ticket quality is enforced by the tool.",
    )
    p.add_argument("--version", action="version", version=f"task {__version__}")
    # global flags live on the top parser AND each subparser via parents
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")
    common.add_argument("--backend", help="override the configured backend (github-issues|linear)")
    common.add_argument("--repo", help="override github repo (owner/name)")
    common.add_argument("--config", help="explicit config file (default: ./task.yaml + global)")
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")

    sub = p.add_subparsers(dest="command", metavar="<command>")

    # create (+ `new` alias — same arguments, same handler)
    for verb, blurb in (
        ("create", "create a ticket (enforces the policy gates)"),
        ("new", "alias of create (enforces the policy gates)"),
    ):
        cp = sub.add_parser(verb, parents=[common], help=blurb)
        _add_create_args(cp)

    # list
    lp = sub.add_parser("list", parents=[common], help="list THIS session's tickets (default)")
    lp.add_argument("--all", action="store_true", help="all tickets, not just this session")
    lp.add_argument("--mine", action="store_true", help="only tickets assigned to me")
    lp.add_argument("--state", help="filter by state (todo|in-progress|in-review|done|cancelled)")
    lp.add_argument("--label", action="append", default=[], help="filter by label (repeatable)")
    # default=None so the limit is chosen by interactivity: 100 in a TTY (the pager scrolls),
    # 30 when piped/scripted. An explicit -n always wins (see _effective_limit).
    lp.add_argument("-n", type=int, default=None, dest="limit", help="max results (default 100 interactive, 30 piped)")
    lp.add_argument("--no-pager", action="store_true", dest="no_pager", help="never page output (also honors NO_PAGER, $PAGER='')")

    # gantt — read-only due-date timeline (charts `list`'s tickets, same scoping)
    gp = sub.add_parser("gantt", parents=[common], help="render tickets on a due-date timeline (read-only)")
    gp.add_argument("--all", action="store_true", help="all tickets, not just this session")
    gp.add_argument("--state", help="filter by state (todo|in-progress|in-review|done|cancelled)")
    gp.add_argument("--label", action="append", default=[], help="filter by label (repeatable)")
    gp.add_argument("-n", type=int, default=None, dest="limit", help="max tickets (default 100 interactive, 30 piped)")
    gp.add_argument("--width", type=int, default=None, help="bar-area width in columns (default: auto from terminal)")
    gp.add_argument("--no-pager", action="store_true", dest="no_pager", help="never page output (also honors NO_PAGER, $PAGER='')")

    # read / view
    rp = sub.add_parser("read", parents=[common], help="show a full ticket")
    rp.add_argument("id", help="ticket id (#123 or HYP-456)")
    rp.add_argument(
        "--save-attachments",
        metavar="DIR",
        help="download each native attachment's bytes into DIR (Linear only; "
        "authenticates the fetch so a uploads.linear.app asset doesn't 401 — see "
        "backends/linear.py:fetch_attachment_bytes)",
    )
    vp = sub.add_parser("view", parents=[common], help="alias of read")
    vp.add_argument("id", help="ticket id")
    vp.add_argument("--save-attachments", metavar="DIR", help="see `task read --help`")

    # find
    fp = sub.add_parser("find", parents=[common], help="search tickets (title+body)")
    fp.add_argument("query", help="search query")
    fp.add_argument("--state", help="filter by state")
    fp.add_argument("--all", action="store_true", help="(reserved) include all; search is global by default")
    fp.add_argument("-n", type=int, default=None, dest="limit", help="max results (default 100 interactive, 30 piped)")
    fp.add_argument("--no-pager", action="store_true", dest="no_pager", help="never page output (also honors NO_PAGER, $PAGER='')")

    # change
    chp = sub.add_parser("change", parents=[common], help="update a ticket (enforces on-done gates when closing)")
    chp.add_argument("id", help="ticket id")
    chp.add_argument("--title", help="new title")
    chp.add_argument("--what", help="replace the What section")
    chp.add_argument("--acceptance", action="append", default=[], metavar="CRIT", help="add acceptance criterion")
    chp.add_argument("--why", help="set motivation")
    chp.add_argument("--impact", help="set user impact")
    chp.add_argument("--if-not-done", dest="if_not_done", help="set cost of inaction")
    chp.add_argument("--screenshot", action="append", default=[], metavar="PATH", help="add implementation screenshot")
    chp.add_argument("--label", action="append", default=[], help="add label")
    chp.add_argument("--due", metavar="YYYY-MM-DD", help="set/replace the due date (empty string clears it)")
    chp.add_argument("--done", action="store_true", help="close the ticket (runs the on-done gates)")
    chp.add_argument("--force", action="store_true", help="override the legal-transition check (e.g. re-close a cancelled ticket)")
    _add_skip_flags(chp)

    # status
    stp = sub.add_parser("status", parents=[common], help="read or transition a ticket's state")
    stp.add_argument("id", help="ticket id")
    stp.add_argument("new_state", nargs="?", help="new state (todo|in-progress|in-review|done|cancelled)")
    stp.add_argument("--force", action="store_true", help="override the legal-transition check (e.g. reopen a cancelled ticket)")
    _add_skip_flags(stp)

    # done — close a ticket by id (the on-done gates run; the close-verb the CTO reaches for)
    dnp = sub.add_parser("done", parents=[common], help="close a ticket (runs the on-done gates)")
    dnp.add_argument("id", help="ticket id (#123 or HYP-456)")
    dnp.add_argument("--screenshot", action="append", default=[], metavar="PATH", help="add implementation screenshot")
    dnp.add_argument("--force", action="store_true", help="override the legal-transition check (re-close a cancelled/done ticket)")
    _add_skip_flags(dnp)

    # check — tick an acceptance criterion off, with the visual proof the box demands
    ckp = sub.add_parser("check", parents=[common], help="check an acceptance criterion (requires a visual proof)")
    ckp.add_argument("id", help="ticket id (#123 or HYP-456)")
    ckp.add_argument("selector", help="which criterion: a 1-based index, or a text substring")
    ckp.add_argument(
        "--proof", action="append", default=[], metavar="PATH", help="visual proof (screenshot/image) for this criterion (repeatable)"
    )
    ckp.add_argument("--screenshot", action="append", default=[], dest="proof", help="alias of --proof")
    ckp.add_argument(
        "--force",
        dest="force_reason",
        metavar="REASON",
        help="check WITHOUT a visual proof, recording why one is impossible/impractical",
    )

    # classify
    clp = sub.add_parser("classify", parents=[common], help="classify a message change|justAsk (the tg hook entry)")
    clp.add_argument("text", help="the message text")
    clp.add_argument("--create", action="store_true", help="on a `change` verdict, create/dedup a ticket")
    clp.add_argument("--update", metavar="ID", help="on a `change` verdict, append to this ticket")

    # session
    sep = sub.add_parser("session", parents=[common], help="show/bind the current session and its tickets")
    sep.add_argument("action", nargs="?", choices=["show", "bind"], default="show", help="show (default) | bind")
    sep.add_argument("bind_id", nargs="?", metavar="ID", help="ticket id to bind (with `bind`)")

    sub.add_parser("install-skill", help="register the task agent skill with harnesses")

    # daemon — the due-date reminder watcher (start/stop/status/run lifecycle)
    dp = sub.add_parser("daemon", parents=[common], help="due-date reminder daemon (start|stop|status|run)")
    dp.add_argument(
        "action",
        choices=["start", "stop", "status", "run"],
        help="start (spawn detached) | stop | status | run (foreground loop)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "create": cmd_create,
        "new": cmd_create,  # `new` is an alias of `create` (CTO-requested verb)
        "list": cmd_list,
        "gantt": cmd_gantt,
        "read": cmd_read,
        "view": cmd_read,
        "find": cmd_find,
        "change": cmd_change,
        "status": cmd_status,
        "done": cmd_done,
        "check": cmd_check,
        "classify": cmd_classify,
        "session": cmd_session,
        "install-skill": cmd_install_skill,
        "daemon": cmd_daemon,
    }
    try:
        return handlers[args.command](args)
    except TransitionError as exc:
        # an illegal state transition (issue #10): clean error, no traceback. It returns the
        # structured exit code the TransitionError pins — the agenttools_errors USAGE class (2),
        # the same "the request is invalid" class _UserError already uses, NOT a transition-only
        # code (a script sees "usage error", not "illegal transition specifically").
        print(_err(f"error: {exc}"))
        return exc.exit_code
    except _UserError as exc:
        print(_err(f"error: {exc}"))
        return 2


class _UserError(Exception):
    """A user-facing error → printed as ``error: ...`` and exit 2 (no traceback)."""


# ── shared plumbing ─────────────────────────────────────────────────────────────────


def _load(args: argparse.Namespace):
    """Load config for the -C repo, applying --backend/--repo/--config overrides."""
    from .config import ConfigError, load

    repo_root = Path(args.cwd).resolve()
    explicit = None
    if getattr(args, "config", None):
        cp = Path(args.config)
        explicit = cp if cp.is_absolute() else repo_root / cp
    try:
        cfg = load(repo_root, explicit_config=explicit)
    except ConfigError as exc:
        raise _UserError(str(exc)) from exc
    if getattr(args, "backend", None):
        cfg.data["backend"] = args.backend
        try:
            from .config import validate

            validate(cfg.data)
        except ConfigError as exc:
            raise _UserError(str(exc)) from exc
    if getattr(args, "repo", None):
        cfg.data.setdefault("github", {})["repo"] = args.repo
    return cfg


def _backend(cfg):
    from .backends import BackendError, get_backend
    from .credentials import CredentialError

    try:
        return get_backend(cfg)
    except (BackendError, CredentialError) as exc:
        raise _UserError(str(exc)) from exc


# ── repo presence + project resolution (the outside-a-repo / cross-repo machinery) ──


def _in_git_repo(repo_root) -> bool:
    """``True`` if ``repo_root`` is inside a git work tree (cheap, no network)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _current_project_overlay(cfg) -> tuple[str, dict[str, Any]] | None:
    """The single project to scope the cwd to (name + config overlay), or ``None``.

    This is the "do I have ONE concrete target?" predicate that routes every command between
    the single-project path and the cross-project/outside-a-repo path. It returns a target when
    the coordinate is pinned — an explicit ``github.repo``/``linear.team`` in config (works even
    without git) — or when ``github.repo: auto`` resolves a git ``origin`` (inside a repo).
    ``None`` means no single target (genuinely outside a git work tree), which sends
    ``list``/``find`` to the grouped registry view and makes ``create`` emit its 3-part error.
    The overlay is what :func:`tasklib.backends.get_backend` resolves unchanged.

    Crucially, ``None`` is returned ONLY for the real outside-a-repo case (no git work tree). A
    repo with a broken/unsupported ``origin`` is NOT silently demoted to "outside a repo" — the
    backend's resolution error surfaces normally, rather than being masked as "no projects".
    """
    backend = cfg.backend
    if backend == "linear":
        team = str(cfg.section("linear").get("team", "")).strip()
        if not team:
            # A teamless `backend: linear` INSIDE a repo is a real, actionable misconfig — surface
            # the backend's "requires a team key" error (mirrors the github `repo: auto` branch
            # below) instead of masking it as "outside a repo". Only genuinely outside a work tree
            # do we return None to route to the grouped/registry view.
            if not _in_git_repo(cfg.repo_root):
                return None
            _backend(cfg)  # NoReturn here: raises _UserError("linear backend requires a team key")
            raise AssertionError("unreachable: _backend raises on a teamless linear config")
        project = str(cfg.section("linear").get("project", "")).strip()
        name = f"{team}/{project}" if project else team
        return name, {"backend": "linear", "linear": {"team": team, "project": project}}

    # github-issues
    gh = cfg.section("github")
    repo_spec = str(gh.get("repo", "auto")).strip()
    if repo_spec and repo_spec != "auto" and "/" in repo_spec:
        return repo_spec, {"backend": "github-issues", "github": {"repo": repo_spec}}
    # repo: auto → no git work tree is the genuine "outside a repo" signal. INSIDE a work tree
    # we resolve the origin and let any resolution error surface (a broken remote is not the
    # same as being outside a repo — don't mask it as "no projects").
    if not _in_git_repo(cfg.repo_root):
        return None
    from .backends import BackendError
    from .backends.github_issues import _resolve_repo

    try:
        owner, repo = _resolve_repo("auto", cfg.repo_root)
    except BackendError as exc:
        # inside a repo, but the origin is broken/unsupported → a real, user-facing error
        # (not "outside a repo"). Surface it as a clean _UserError, never a traceback.
        raise _UserError(str(exc)) from exc
    full = f"{owner}/{repo}"
    return full, {"backend": "github-issues", "github": {"repo": full}}


def _config_for_overlay(cfg, overlay: dict):
    """Clone ``cfg`` with ``overlay`` deep-merged over its data (so a project resolves).

    Returns a fresh ``LoadedConfig`` — the base ``cfg`` is never mutated, so aggregating
    across projects can't leak one project's coordinates into the next.
    """
    return cfg.with_overlay(overlay)


def _known_projects(cfg) -> tuple[list, str | None]:
    """Return ``(projects, current_coordinate)`` — the groups + which one is the cwd's repo.

    The registry projects, plus (when inside a repo) the synthetic current-repo project if its
    coordinate isn't already registered. ``current_coordinate`` is the cwd repo's coordinate (or
    ``None`` outside a repo) — currentness is tracked by COORDINATE, kept separate from registry
    explicitness, so the cwd repo is flagged ``(current)`` even when it IS in the registry.
    """
    from .projects import current_repo_project, projects_from_config

    projects = projects_from_config(cfg.data)
    current = _current_project_overlay(cfg)
    current_coordinate: str | None = None
    if current is not None:
        name, overlay = current
        cur_backend = str(overlay.get("backend", cfg.backend))
        cur = current_repo_project(name, cur_backend, overlay)
        current_coordinate = cur.coordinate
        # match by COORDINATE (repo/team), not display name: a registry entry for the same repo
        # under a different label is still the same project — append the synthetic one only when
        # the coordinate isn't already registered (the registry entry keeps its own label).
        if not any(p.coordinate == cur.coordinate for p in projects):
            projects.append(cur)
    return projects, current_coordinate


def _backend_for_id(cfg, ticket_id: str):
    """Resolve the backend that owns ``ticket_id`` — works inside a repo AND outside one.

    Inside a repo (or with a pinned coordinate / ``--repo``) the cwd's backend is used. Outside
    a repo the ticket is routed to a registered project: a Linear id (``HYP-456``) by its team
    prefix; a GitHub id (``#123``) when exactly one GitHub project is registered. An ambiguous
    or unroutable id fails with a 3-part error rather than a cryptic backend failure.
    """
    if _current_project_overlay(cfg) is not None:
        return _backend(cfg)

    projects, _ = _known_projects(cfg)
    candidates = [p for p in projects if p.explicit]
    chosen = _route_id_to_project(ticket_id, candidates)
    if chosen is None:
        raise _UserError(_unroutable_id_error(ticket_id, candidates))
    pcfg = _config_for_overlay(cfg, chosen.overlay)
    return _backend(pcfg)


def _route_id_to_project(ticket_id: str, projects: list):
    """Pick the registered project that owns ``ticket_id``, or ``None`` if ambiguous/none."""
    tid = ticket_id.strip()
    if tid.startswith("#") or tid.lstrip("#").isdigit():
        gh = [p for p in projects if p.backend == "github-issues"]
        return gh[0] if len(gh) == 1 else None
    # Linear-shaped id: TEAM-123 → route by the team prefix. A Linear identifier is team-scoped,
    # and get/update resolve by the identifier alone (they ignore the project), so ANY registered
    # project on that team can fetch it — pick the first deterministically rather than calling two
    # same-team projects "ambiguous" (they'd resolve the same issue either way).
    prefix = tid.partition("-")[0].upper() if "-" in tid else ""
    if prefix:
        matches = [p for p in projects if p.backend == "linear" and _linear_team_of(p) == prefix]
        if matches:
            return matches[0]
    return None


def _linear_team_of(project) -> str:
    return str(project.overlay.get("linear", {}).get("team", "")).strip().upper()


def _unroutable_id_error(ticket_id: str, projects: list) -> str:
    names = ", ".join(p.name for p in projects) or "(none registered)"
    return (
        f"cannot resolve which project ticket {ticket_id!r} belongs to (you are outside a git repo).\n"
        f"  why: the id maps to no single known project. registered projects: {names}.\n"
        "  fix: run inside the repo, or pass --repo owner/name (GitHub), "
        "or register the project under `projects:` in ~/.config/task-cli/config.yaml."
    )


def _create_without_repo_error(cfg) -> str:
    """The honest 3-part (WHAT/WHY/HOW) error for `task create` run outside a repo.

    The HOW must not lie: it points at the real escape hatches that exist — ``--repo`` (which
    pins the GitHub coordinate), a Linear team in config, or simply running inside the repo.
    """
    backend = cfg.backend
    if backend == "linear":
        how = (
            "  fix: run inside the repo whose `task.yaml` sets `linear.team`, "
            "or set `linear: {team: KEY}` in ~/.config/task-cli/config.yaml."
        )
    else:
        how = (
            "  fix: run inside the target git repo, or pass `--repo owner/name` "
            "to pin the GitHub project explicitly."
        )
    return (
        "cannot create a ticket: no project context (you are outside a git repo).\n"
        "  why: `create` is repo-bound — it writes the ticket into ONE specific project, "
        "so it must know which backend/repo to target; `github.repo: auto` needs a git origin.\n"
        f"{how}"
    )


def _enforce_config(cfg):
    from .policy import EnforceConfig

    return EnforceConfig.from_dict(cfg.enforce)


def _detect_session(cfg):
    from .session import detect

    # root git-branch detection at the same repo the backend resolves (cfg.repo_root), not the
    # shell's cwd, so `task -C /other/repo` is consistent between session scope and backend.
    return detect(
        detect_order=cfg.session_detect,
        cwd=str(cfg.repo_root),
        label_prefix=cfg.session_label_prefix,
    )


def _parse_state(value: str):
    """Parse a state string, turning the ValueError into a clean ``_UserError`` (exit 2)."""
    try:
        return State.parse(value)
    except ValueError as exc:
        raise _UserError(str(exc)) from exc


def _normalize_due(value: str | None) -> str | None:
    """Validate a ``--due`` value to a canonical ``YYYY-MM-DD`` string (``None`` = not passed).

    An empty string is a deliberate "clear the due date" signal (returns ``""``). Any other
    value must be an ISO date; a malformed one is a clean ``_UserError`` (exit 2), never stored
    as-is — a daemon that watches due dates must not be fed un-parseable junk at the front door.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return ""
    from datetime import date

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise _UserError(f"--due must be an ISO date YYYY-MM-DD (got {value!r})") from exc


def _collect_skips(args: argparse.Namespace) -> dict[str, str]:
    """Gather the recorded escape-hatch justifications from --skip-<gate> flags."""
    skips: dict[str, str] = {}
    for _flag, gate in _SKIP_FLAGS:
        dest = "skip_" + gate.replace("-", "_")
        reason = getattr(args, dest, None)
        if reason:
            skips[gate] = reason
    return skips


def _ticket_line(t: Ticket) -> str:
    first_para = (t.what or t.raw_body or "").strip().split("\n", 1)[0][:80]
    state = _dim(f"[{t.state.value}]")
    sep = f" — {first_para}" if first_para else ""
    return f"{_bold(t.id or '(new)')} {state} {t.title}{sep}"


def _format_tickets(tickets: list[Ticket]) -> str:
    """The flat (non-grouped) human view as a single string — fed to the pager by the caller."""
    if not tickets:
        return _dim("(no tickets)")
    return "\n".join(_ticket_line(t) for t in tickets)


def _print_tickets_json(tickets: list[Ticket]) -> None:
    """The flat machine-readable view — straight to stdout, never paged (must stay parseable)."""
    import json

    print(json.dumps([_ticket_dict(t) for t in tickets], ensure_ascii=False, indent=2))


def _ticket_dict(t: Ticket) -> dict:
    d = {
        "id": t.id,
        "title": t.title,
        "state": t.state.value,
        "url": t.url,
        "labels": t.labels,
        "what": t.what,
        "due": t.due,
    }
    # Only emitted when the backend actually returned any (Linear; GitHub never populates this)
    # — keeps the JSON shape unchanged for every existing consumer that doesn't care about it.
    if t.attachments:
        d["attachments"] = [{"id": a.id, "title": a.title, "url": a.url, "subtitle": a.subtitle} for a in t.attachments]
        # review finding: human output already warns about the 250-item cap — a JSON consumer
        # (`--json`) must get the SAME signal, not silently treat a capped list as complete.
        if t.attachments_truncated:
            d["attachments_truncated"] = True
    return d


# ── policy enforcement helper (shared by create + change/close) ─────────────────────


def _msgref_resolvable_ids(ticket, cfg_e) -> set[int] | None:
    """The ``tg#<id>`` ids resolvable in local tg-cli history — loaded ONCE, and ONLY when it can
    actually matter: the ``msgref-quote`` gate is enabled AND the ticket's prose carries an
    UNQUOTED reference. A gate disabled via ``enforce.msgref_quote: false``, or a fully-expanded /
    reference-free ticket, never touches the (potentially large, private) history log (review
    finding). Feeds the gate's deny-vs-warn split (:func:`tasklib.policy.check`); returns ``None``
    when there is nothing to assess (the gate then no-ops), else the resolvable-id set. Uses the
    gate's own :func:`tasklib.policy.unquoted_body_ids` so the "should I load history?" question
    can never diverge from "what does the gate look at?". ``load_history`` is contractually
    best-effort (it swallows a missing dir / absent tg-cli / corrupt line and returns ``{}``), so
    this advisory path never breaks a create/close even where tg-cli was never installed."""
    from .policy import GATE_MSGREF_QUOTE, unquoted_body_ids

    # Skip the (potentially large, private) history read entirely when it can't change the
    # outcome: the gate is disabled, already waived on this ticket, or there is nothing unquoted.
    if not cfg_e.msgref_quote or GATE_MSGREF_QUOTE in ticket.skips:
        return None
    if not unquoted_body_ids(ticket):
        return None
    from .msgrefs import load_history

    return set(load_history())


def _enforce_or_die(ticket: Ticket, cfg, phase) -> None:
    from .policy import Phase, check

    cfg_e = _enforce_config(cfg)
    result = check(ticket, cfg_e, phase, resolvable_ids=_msgref_resolvable_ids(ticket, cfg_e))
    _report_and_die(result, "create" if phase is Phase.CREATE else "close")


def _report_and_die(result, label: str) -> None:
    """Print the skipped/violation summary for a PolicyResult and refuse if any gate is unmet.

    Stream contract: on the SUCCESS path (no violation) the advisory warnings (``result.warnings``)
    and the "skipped gates (justified)" summary are diagnostics, not data — they go to STDERR so a
    machine-readable ``--json`` payload the caller then prints to stdout is never corrupted (review
    finding). The FATAL refusal report below prints to stdout and then raises ``_UserError`` — that
    is pre-existing behavior shared with every other command's refusal (and asserted by existing
    tests); a failing command exits non-zero without printing ``--json``, so it does not corrupt a
    success-path parse. Routing the refusal report to stderr too is a worthwhile follow-up but a
    cross-cutting change beyond this gate's scope."""
    import sys

    for w in result.warnings:
        print(_warn(f"  ⚠ {w.gate}: {w.message}"), file=sys.stderr)
    if result.skipped:
        print(_warn(f"  skipped gates (justified): {', '.join(result.skipped)}"), file=sys.stderr)
    if not result.ok:
        from .policy import NON_SKIPPABLE_GATES

        print(_err(f"refusing to {label}: {len(result.violations)} gate(s) unmet"))
        for v in result.violations:
            print(_err(f"  ✗ {v.gate}: {v.message}"))
            if v.hint:
                # A NON_SKIPPABLE gate (e.g. acceptance-checked, msgref-title) has no
                # --skip-<gate> flag at all -- suggesting one is actively misleading (task-cli#45
                # review finding: the new msgref-title gate inherited this pre-existing wart from
                # acceptance-checked). Only append the skip hint for a gate that actually has one.
                skip_hint = "" if v.gate in NON_SKIPPABLE_GATES else f'  (or --skip-{v.gate} "<reason>")'
                print(_dim(f"      → {v.hint}{skip_hint}"))
        raise _UserError("policy gates not satisfied")


def _apply_force(ticket: Ticket, cfg, phase, reason: str | None, forceable: set[str]) -> None:
    """Record ``reason`` as the skip justification for whichever ``forceable`` gates actually fire.

    This is how ``--force "<reason>"`` overrides the new content gates (links / user-impact-
    quality): the reason is written onto the ticket's ``skips``, so it lands — audited — in the
    body's ``Skipped gates`` section, exactly like ``--skip-<gate>``. Only gates that genuinely
    fail are recorded, so a force never pollutes the audit trail with gates that passed.
    """
    if not reason:
        return
    from .policy import check

    for v in check(ticket, _enforce_config(cfg), phase).violations:
        if v.gate in forceable:
            ticket.skips.setdefault(v.gate, reason)


def _session_dedup_candidates(backend, session) -> list[Ticket]:
    """This session's tickets for a dedup scan, degrading to `[]` on ANY backend hiccup.

    Shared by `_refuse_if_duplicate` and `_classify_create` so both dedup checks fail open the
    same way. Catches BOTH `BackendError` and `AmbiguousBackendError`: this is a read-only GET,
    so an ambiguous outcome here is harmless (nothing was mutated either way) — unlike
    `AmbiguousBackendError` from a WRITE, which must never be silently swallowed. Missing
    `AmbiguousBackendError` here was itself a review-caught bug: `session_tickets()` funnels
    through the same `_call`/`_gql` wrappers as every other request, so a mid-read drop during
    THIS lookup used to crash `create` with a raw traceback before the create was even attempted
    — defeating the very guard meant to protect a retry after a flaky connection.
    """
    from .backends import AmbiguousBackendError, BackendError

    try:
        return backend.session_tickets(session.label, limit=30)
    except (BackendError, AmbiguousBackendError):
        return []


def _refuse_if_duplicate(backend, session, force_reason: str | None, ticket: Ticket) -> None:
    """Block `create` when a close-duplicate ticket already exists in this session.

    Reuses `_best_dedup_match` — the SAME conservative title-similarity check `_classify_create`
    already runs before creating from an inbound message — so `task new`/`create` gets the
    identical guard. Without this, a caller retrying after ANY ambiguous result (e.g.
    `AmbiguousBackendError`) could silently create a genuine duplicate ticket (task-cli review
    finding).

    `force_reason` (`--force "<reason>"`) overrides the block; the reason is recorded under
    `policy.GATE_DUPLICATE` in the ticket's `skips` (renders in the `Skipped gates` section) ONLY
    when it actually overrode a detected match — a force that found nothing to override never
    pollutes the audit trail, mirroring `_apply_force`'s same rule for the policy gates.
    """
    from .policy import GATE_DUPLICATE

    candidates = _session_dedup_candidates(backend, session)
    match = _best_dedup_match(ticket.title, candidates)
    if match is None:
        return
    if force_reason:
        ticket.skips.setdefault(GATE_DUPLICATE, force_reason)
        return
    # Quote the id (`'#1'`, not bare `#1`): pasted bare into a shell, `#1` is swallowed as a
    # comment (`#` starts a comment wherever a new word may begin, e.g. `bash -c 'echo hi #1'`
    # prints only "hi") — quoting keeps these suggestions actually copy-paste-safe while
    # task-cli still receives the identical plain `#1` argument once the shell strips the quotes.
    raise _UserError(
        f"a close-duplicate ticket already exists: {match.id}  {match.title}\n"
        f"  url: {match.url}\n"
        f"  next steps:\n"
        f"    task read '{match.id}'                    view it\n"
        f"    task change '{match.id}' ...               edit it instead of creating a new one\n"
        f'    task new ... --force "<reason>"          create anyway (records the override)'
    )


def _derive_title_from_message(text: str) -> str:
    """Derive a ticket title from raw inbound text (the ``--from-message`` / ``classify
    --create`` hook path): first line, truncated to 72 chars — but with a leading tg-cli inbound-
    inject wrap (``[TG from <name> tg#<id>]``) stripped FIRST, so the title never inherits the
    wrap's own ``tg#<id>`` and trips the non-skippable ``msgref-title`` gate (task-cli#45 review
    finding: this is the documented hook path tasklib.msgrefs's module docstring names as the
    whole feature's motivating case — it must not be the one path that breaks). Shared by
    :func:`cmd_create` and :func:`_classify_create` so both derive a title the same way."""
    from .msgrefs import strip_inbound_wrap

    unwrapped = strip_inbound_wrap(text.strip())
    first = unwrapped.split("\n", 1)[0]
    return first[:72]


def _expand_msgrefs(text: str, history=None):
    """Expand every ``tg#<id>`` mention in ``text`` into itself plus a quote pulled from
    tg-cli's local message history (task 6109 / HYP-897) — see :mod:`tasklib.msgrefs`. A no-op
    when ``text`` carries no reference (or ``text`` is falsy), so calling this on every prose
    field is always safe. ``history`` lets a caller load the (potentially large, append-only)
    history log ONCE per command via :func:`_load_msgref_history` and reuse it across fields,
    instead of re-reading it from disk for every field that happens to carry a reference."""
    if not text:
        return text
    from .msgrefs import render_msgref_quotes

    return render_msgref_quotes(text, history=history)


def _msgref_expand_enabled(skips, cfg) -> bool:
    """Whether tg#<id> auto-expansion should run for a ticket carrying these skips under ``cfg``.
    OFF when either (a) ``enforce.msgref_quote: false`` — that flag is the single master switch for
    the whole tg#<id> feature, so disabling the gate also stops the quote machinery and its
    private-history read (review finding); or (b) a ``--skip-msgref-quote`` is recorded — the
    author declared the reference a false positive, so it must not be quoted at all. The skip
    governs expansion, not merely the enforcement gate."""
    from .policy import GATE_MSGREF_QUOTE

    if not _enforce_config(cfg).msgref_quote:
        return False
    return GATE_MSGREF_QUOTE not in skips


def _load_msgref_history_if_needed(*texts: str):
    """Load tg-cli's local message history ONCE per command — but only when at least one of
    ``texts`` actually names a ``tg#<id>`` reference. A metadata-only edit, or a ticket that
    never mentions one, never touches disk for this (the log is append-only and can grow large;
    task-cli#45 review finding)."""
    from .msgrefs import detect_msgrefs, load_history

    if not any(detect_msgrefs(t) for t in texts if t):
        return None
    return load_history()


def _enforce_edit_or_die(ticket: Ticket, cfg, *, check_links: bool, check_impact: bool, check_title: bool = False) -> None:
    """The edit-time gates (rule 1 links, and rule 5 impact-quality when impact was touched).

    A plain ``change`` (not a close) doesn't re-run the full create gates — an edit may be a
    partial step. The links rule runs only when the edit TOUCHED a scanned text field, so a
    metadata-only edit (``--due``/``--label``) on a ticket whose body (e.g. one created in the
    GitHub/Linear web UI) already carries a bare reference is never blocked. The impact-quality
    rule runs only when the edit sets/changes the impact. ``check_title`` runs the (non-skippable)
    ``msgref-title`` gate only when this edit is SETTING the title (``--title``) — an edit that
    leaves the title alone can't be blocked by a title it didn't touch.

    Note the links scan is a FULL re-scan of the ticket text, not only the changed field: a
    clean ``--title`` edit on a ticket that already carries a bare reference in another section
    (e.g. ``why``) is blocked on that pre-existing reference — deliberately, as a nudge to fix
    it. On this edit path the waiver is ``--skip-links``/``--skip-user-impact-quality`` (the
    ``--force "<reason>"`` shortcut is a create/new-only flag — on ``change`` ``--force`` is the
    boolean transition-legality override, not a reason). Only a metadata-only edit, which
    touches no scanned field, is exempt.
    """
    from .policy import (
        apply_skips,
        impact_quality_violation,
        links_url_violation,
        links_violation,
        msgref_quote_reports,
        msgref_title_violation,
    )

    cfg_e = _enforce_config(cfg)
    raw = []
    warnings = []
    if check_links:
        # `check_links` means this edit touched a scanned content field. Re-run the same content
        # gates the create phase would, over the WHOLE (conservatively re-scanned) body: the prose
        # bare-reference gate, the Links-field URL gate, and the body tg#<id> quote gate. A pure
        # metadata edit (--due/--label) leaves check_links False and is exempt from all three, so
        # a legacy junk link / unquoted reference never blocks an unrelated metadata edit — it is
        # still caught at create and at close (where these gates run unconditionally via check()).
        v = links_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
        v = links_url_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
        # Auto-expansion runs on this same command before enforcement, so a freshly set --what
        # carrying a reference is already quoted; this catches a pre-existing unquoted reference
        # in an untouched field — the backstop for a web-UI-authored ticket.
        deny, warn = msgref_quote_reports(ticket, cfg_e, _msgref_resolvable_ids(ticket, cfg_e))
        if deny is not None:
            raw.append(deny)
        if warn is not None:
            warnings.append(warn)
    if check_impact:
        v = impact_quality_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
    if check_title:
        v = msgref_title_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
    if raw or warnings:
        _report_and_die(apply_skips(raw, ticket, warnings), "update")


def _notify_mutation(cfg, ticket, action: str) -> None:
    """Send a TG notification after a ticket mutation (create/update/done/transition).

    Uses the daemon's notifier config (default: tg --tag report) and the daemon's
    notifications.on_mutation enable flag. Best-effort: a notifier failure never fails the
    ticket op. Read-only commands (list/find/read/session) never call this.
    """
    # Config gate: notifications.on_mutation (default True). Set to false to silence.
    notif_section = cfg.section("notifications")
    if not _as_bool_cfg(notif_section.get("on_mutation"), default=True):
        return
    from . import daemon as _daemon

    dcfg = _daemon.DaemonConfig.from_config(cfg)
    notifier, html_mode = _resolve_notification_notifier(dcfg.notifier)
    state_str = ticket.state.value if ticket.state else "unknown"
    msg = _mutation_message(ticket, action, state_str, html_mode=html_mode)
    _daemon.notify(msg, notifier)


def _mutation_message(ticket: Ticket, action: str, state_str: str, *, html_mode: bool) -> str:
    """Render the one-line mutation notice in plain text or Telegram HTML."""
    ticket_id = ticket.id or "(new)"
    if not html_mode:
        url_part = f"\n{ticket.url}" if ticket.url else ""
        return f"[task {action}] {ticket_id}: {ticket.title} [{state_str}]{url_part}"

    safe_action = html.escape(action, quote=False)
    safe_title = html.escape(ticket.title or "", quote=False)
    safe_state = html.escape(state_str, quote=False)
    display_id = html.escape(ticket_id, quote=False)
    if ticket.url:
        display_id = f'<a href="{html.escape(ticket.url, quote=True)}">{display_id}</a>'
    return f"[task {safe_action}] {display_id}: {safe_title} [{safe_state}]"


def _resolve_notification_notifier(notifier: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    if not _is_tg_notifier(notifier):
        return notifier, False
    notifier, fmt = _normalize_format_arg(notifier)
    if fmt is None:
        return (*notifier, "--format", "html"), True
    return notifier, fmt.strip().lower() == "html"


def _is_tg_notifier(notifier: tuple[str, ...]) -> bool:
    return bool(notifier) and Path(notifier[0]).name == "tg"


def _normalize_format_arg(notifier: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
    normalized: list[str] = []
    fmt: str | None = None
    idx = 0
    while idx < len(notifier):
        arg = notifier[idx]
        if arg == "--format":
            normalized.append(arg)
            if idx + 1 < len(notifier):
                fmt = notifier[idx + 1]
                normalized.append(fmt)
                idx += 2
            else:
                fmt = ""
                idx += 1
            continue
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
            normalized.extend(["--format", fmt])
            idx += 1
            continue
        normalized.append(arg)
        idx += 1
    return tuple(normalized), fmt


def _as_bool_cfg(value, *, default: bool) -> bool:
    """Coerce a config value to bool (mirrors daemon._as_bool without importing daemon)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)


def _attach_screenshots(backend, ticket_id: str, screenshots) -> None:
    """Push each screenshot to the backend's attachment endpoint (durable proof).

    The body already embeds the local ref via render(); ``attach()`` is the durable channel
    (GitHub: a reference comment; Linear: an issue attachment). Best-effort: an attach failure
    must not undo a successful create/update, so a backend error is swallowed (the ref still
    lives in the body). Exercises the ``TicketBackend.attach`` contract rather than leaving it
    a dangling method.

    Catches ``AmbiguousBackendError`` too, not just ``BackendError`` (review finding): the
    ticket/comment this attaches to already exists by the time this runs, so an ambiguous attach
    outcome carries none of the "did this create a duplicate?" risk the type exists to flag — it
    is exactly the harmless-to-swallow case this function's own docstring already describes.
    Missing this was itself a bug: a caller wrapping ``backend.create()`` + this in one ``try``
    (``cmd_create``) would misreport a definitely-created ticket as merely "may have been
    created" and never print its id, because this raised past its own best-effort contract.
    """
    from .backends import AmbiguousBackendError, BackendError

    for shot in screenshots:
        try:
            backend.attach(ticket_id, shot.ref)
        except (BackendError, AmbiguousBackendError):
            continue


def _create_ticket_or_ambiguous(backend, ticket: Ticket) -> tuple[Ticket | None, int | None]:
    """Create ``ticket`` via ``backend``. Returns ``(created, None)`` on success, or
    ``(None, exit_code)`` when the outcome is ambiguous — the caller returns ``exit_code``
    immediately without further output (the message is already printed here).

    Shared by ``cmd_create`` and ``_classify_create`` — the two paths that create a NEW ticket
    and so share the exact same duplicate-on-retry hazard (review finding: only ``cmd_create``
    handled ``AmbiguousBackendError``, leaving the inbound-message hook path — which is ALSO a
    creation path — to crash with a raw traceback on the identical scenario).
    """
    from .backends import AmbiguousBackendError, BackendError

    try:
        return backend.create(ticket), None
    except AmbiguousBackendError as exc:
        # the connection dropped AFTER the backend already accepted the request (e.g. GitHub
        # returned 201 and created the issue, then the read itself failed) — this is NOT the
        # same as a clean, nothing-happened refusal (that's the BackendError/_UserError/exit-2
        # path below), so it gets its own message and its own exit code rather than either a
        # bare traceback or a misleadingly-clean "error:" line.
        print(_err(f"ambiguous: {exc}"))
        print(
            _warn(
                "  the ticket may have already been created — run `task list` (or check the "
                "backend) before retrying, to avoid creating a duplicate."
            )
        )
        return None, exc.exit_code
    except BackendError as exc:
        raise _UserError(str(exc)) from exc


# ── commands ────────────────────────────────────────────────────────────────────────


def cmd_create(args: argparse.Namespace) -> int:
    cfg = _load(args)
    # create is the one repo-BOUND op: it writes a ticket into a specific project, so it needs
    # to know which one. Outside a repo (and with no pinned coordinate) fail with an honest
    # 3-part error — never the cryptic "no 'origin' remote" the backend would otherwise throw.
    if _current_project_overlay(cfg) is None:
        raise _UserError(_create_without_repo_error(cfg))
    session = _detect_session(cfg)

    title = args.title
    what = args.what or ""
    if args.from_message and not title:
        # derive a title from the first line of the raw message (the hook path) — see
        # _derive_title_from_message for why this strips a leading tg-cli inbound wrap first.
        title = _derive_title_from_message(args.from_message)
        what = what or args.from_message.strip()
    if not title:
        raise _UserError("a title is required (--title, or --from-message to derive one)")

    # Expand tg#<id> mentions in each prose field, once history is loaded (a single read of
    # tg-cli's local history log, reused across all four fields — see _load_msgref_history).
    # Safe to do BEFORE the gates run: `links`/`user-impact-quality` ignore any `>` blockquote
    # line (policy._strip_quoted_blocks), which is exactly the shape the appended quote takes, so
    # the quoted Telegram message — arbitrary text the ticket's author didn't write — can never
    # itself cause a refusal, no matter which command later re-scans the stored field
    # (task-cli#45 review finding: an earlier "expand only after THIS command's gates" version
    # protected only the expanding command itself — a later edit or close re-scanned the already-
    # expanded stored text and reintroduced the same failure).
    #
    # Acceptance criteria are deliberately EXCLUDED: render.py serializes each criterion as ONE
    # markdown checkbox line (`- [ ] <text>`), and _parse_criteria reads it back line-by-line —
    # a criterion text containing the quote's embedded newlines renders as loose markdown BELOW
    # the checkbox and is silently dropped on the next parse (verified empirically: render→parse
    # loses everything after the first line). A tg#<id> in a criterion stays a bare, un-expanded
    # mention — harmless, since the links gate already excludes tg#<id> from its bare-reference
    # scan.
    # A recorded --skip-msgref-quote means the author declares the tg#<id> a false positive (a
    # version tag, not a message ref), so NEITHER expand it (attaching an unrelated, possibly
    # private Telegram message) NOR read history at all — the skip must govern expansion, not only
    # the gate, or the waiver would still leak the very quote it waives (review finding).
    skips = _collect_skips(args)
    expand = _msgref_expand_enabled(skips, cfg)
    history = _load_msgref_history_if_needed(what, args.why or "", args.impact or "", args.if_not_done or "") if expand else None
    labels = list(dict.fromkeys([*args.label, session.label]))
    screenshots = [Screenshot(ref=p, kind="creation") for p in args.screenshot]
    due = _normalize_due(getattr(args, "due", None)) or ""
    ticket = Ticket(
        title=title,
        what=_expand_msgrefs(what, history) if expand else what,
        why=_expand_msgrefs(args.why or "", history) if expand else (args.why or ""),
        user_impact=_expand_msgrefs(args.impact or "", history) if expand else (args.impact or ""),
        cost_of_inaction=_expand_msgrefs(args.if_not_done or "", history) if expand else (args.if_not_done or ""),
        acceptance=list(args.acceptance),
        screenshots=screenshots,
        labels=labels,
        # NOTE: the session id already lives in `labels` (the value `session_tickets()` queries
        # by) — it must NOT also go into `links`. The Links section renders as `- key: value`
        # with no URL requirement, so a "Session: session:2" entry silently escaped the links
        # gate (which only scans prose fields, see policy._scanned_text) and showed up in the
        # ticket as a fake, non-URL "link". Reported by the CTO as junk (2026-07-17).
        skips=skips,
        due=due,
    )

    from .policy import GATE_LINKS, GATE_USER_IMPACT_QUALITY, Phase

    force_reason = getattr(args, "force_reason", None)
    _apply_force(ticket, cfg, Phase.CREATE, force_reason, {GATE_LINKS, GATE_USER_IMPACT_QUALITY})
    _enforce_or_die(ticket, cfg, Phase.CREATE)

    backend = _backend(cfg)
    _refuse_if_duplicate(backend, session, force_reason, ticket)

    created, ambiguous_exit = _create_ticket_or_ambiguous(backend, ticket)
    if ambiguous_exit is not None:
        return ambiguous_exit
    # `created` is set (not None) whenever ambiguous_exit is None — attach is best-effort and
    # runs AFTER, in its own scope, so an attach hiccup (even an ambiguous one — see
    # _attach_screenshots) can never suppress the fact that the ticket itself was created.
    _attach_screenshots(backend, created.id, screenshots)

    from .session import record

    record(session.id, created.id, created.title)
    from .logging import log_event

    log_event("ticket.created", ticket_id=created.id, backend=cfg.backend, session=session.id)
    _notify_mutation(cfg, created, "created")

    if args.json:
        import json

        print(json.dumps(_ticket_dict(created), ensure_ascii=False, indent=2))
    else:
        print(_ok(f"created {created.id}  {created.url}"))
        _print_recent_tickets_after_create(session)
        _print_session_attention_after_mutation(args, cfg, backend, current_id=created.id)
    return 0


# Limit defaults: a small one when piped/scripted (machine-readable, bounded), a larger one
# in an interactive TTY where the pager scrolls and a 30-row cap would just hide tickets.
_LIMIT_PIPED = 30
_LIMIT_INTERACTIVE = 100
_STALE_WARNING_SECONDS = 4 * 60 * 60
_RECENT_WARNING_SECONDS = 30 * 60
_ATTENTION_STATES = {State.TODO, State.IN_PROGRESS, State.IN_REVIEW}
_RECENT_PRIORITY_STATES = {State.IN_PROGRESS, State.IN_REVIEW}
_RECENT_TICKETS_LIMIT = 5
_RECENT_TICKETS_WINDOW_SECONDS = 60 * 60


def _effective_limit(args: argparse.Namespace) -> int:
    """The result cap: an explicit ``-n`` wins; otherwise pick by interactivity.

    Without ``-n``, a TTY gets the higher cap (the pager handles scrolling) and a pipe gets the
    small one (scriptable, bounded). Decided off the real stdout so a piped run is deterministic.
    """
    if getattr(args, "limit", None) is not None:
        return args.limit
    return _LIMIT_INTERACTIVE if sys.stdout.isatty() else _LIMIT_PIPED


def _emit_list(args: argparse.Namespace, blocks: list[str]) -> None:
    """Join the human-readable list output and route it through the pager (interactive only).

    ``blocks`` are the already-rendered sections (e.g. the "showing all project tasks" notice
    then the ticket/group body). Empty blocks are dropped so we don't emit blank separators.
    """
    from .pager import page

    text = "\n".join(b for b in blocks if b)
    page(text, no_pager_flag=getattr(args, "no_pager", False))


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _age_text(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _attention_line(ticket: Ticket, age: int | None) -> str:
    suffix = f" (touched {_age_text(age)})" if age is not None else ""
    return f"{ticket.id} [{ticket.state.value}] {ticket.title}{suffix}"


def _stale_line(ticket: Ticket, age: int) -> str:
    return f"{ticket.id} [{ticket.state.value}] {ticket.title} (updated {_age_text(age)})"


def _format_duration_hours(seconds: int) -> str:
    """A duration in hours for display, without a naive ``// 3600`` flooring any realistic
    sub-hour value straight to a misleading "0h".

    ``TASK_GLOBAL_STALE_SECONDS`` accepts any positive value, not just whole hours — integer
    division would print "0h+" for a configured 30-minute threshold and silently round 90
    minutes down to "1h+". One decimal place is enough precision for a nudge message; a
    threshold under ~3 minutes still rounds to "0h+" (a config value that extreme is not a
    realistic staleness window for this feature, so this stays a display nicety, not a
    correctness guarantee for arbitrarily tiny thresholds).
    """
    hours = round(seconds / 3600, 1)
    return f"{hours:g}h"


def _session_attention_notice(session, tickets: list[Ticket], *, priority_change: bool = False) -> str:
    """Human-only warning for active session tasks that are easy to forget.

    Backend timestamps are not portable across GitHub/Linear, but the session sidecar records
    when task-cli last touched a ticket in this session. Use that as a conservative signal:
    old active work suggests parallel follow-up; recently touched in-progress work suggests the
    agent should ask whether to continue it before switching priority.
    """
    if session.source == "none":
        return ""
    active = [t for t in tickets if t.state in _ATTENTION_STATES]
    if not active:
        return ""

    from .session import read_entries

    entries = {entry.id: entry for entry in read_entries(session.id)}
    now = int(time.time())
    stale_after = _env_seconds("TASK_STALE_WARNING_SECONDS", _STALE_WARNING_SECONDS)
    recent_before = _env_seconds("TASK_RECENT_WARNING_SECONDS", _RECENT_WARNING_SECONDS)
    stale: list[tuple[Ticket, int | None]] = []
    recent: list[tuple[Ticket, int | None]] = []
    for ticket in active:
        entry = entries.get(ticket.id)
        age = (now - entry.ts) if (entry and entry.ts > 0) else None
        if age is not None and stale_after and age >= stale_after:
            stale.append((ticket, age))
            continue
        if (
            (len(active) > 1 or priority_change)
            and ticket.state in _RECENT_PRIORITY_STATES
            and age is not None
            and age >= 0
            and recent_before
            and age <= recent_before
        ):
            recent.append((ticket, age))

    if not stale and not recent:
        return ""

    lines = [_warn("warning: active tasks in this session may need attention:")]
    if stale:
        lines.append(_dim("  forgotten/old: consider continuing in parallel or explicitly parking:"))
        lines.extend(_dim(f"    - {_attention_line(ticket, age)}") for ticket, age in stale[:3])
    if recent:
        lines.append(_dim("  recently touched active task: ask whether to continue it or park it before switching priorities:"))
        lines.extend(_dim(f"    - {_attention_line(ticket, age)}") for ticket, age in recent[:3])
    return "\n".join(lines)


def _recent_tickets_block(session) -> str:
    """The 'Recent tickets (last hour)' block printed after a successful `task new`/`create`
    (Alex, tg#10993/#10995): up to 5 tickets created in this session within the last hour,
    newest first, the just-created one included.

    Reuses the session sidecar (:mod:`tasklib.session`) — the same local, offline record
    `_refuse_if_duplicate` and `_session_attention_notice` already read — rather than a fresh
    backend query, so this costs no extra network round-trip. Note the same caveat
    `_session_attention_notice` already documents for this sidecar: ``SessionEntry.ts`` is when
    task-cli last TOUCHED the ticket in this session, not a pure creation timestamp. In practice
    an entry is written once, at `record()`-after-create time, and only moves if a later
    `change`/`done`/`check` in the SAME session touches that SAME ticket again — an accepted
    approximation, not a new one invented for this feature.

    ``session.source == "none"`` (no `TASK_SESSION`/tmux pane/git branch resolved) short-
    circuits to ``""``, mirroring `_session_attention_notice`'s own guard: an unresolved
    session always collapses to the SAME ``id="default"`` sidecar file, shared by every repo
    where detection fails — without this guard, "recent tickets" could leak a completely
    unrelated project's tickets into this one's `create` output.

    Bounded by BOTH the count (5) AND a genuinely fixed 1-hour window — deliberately NOT
    configurable (an earlier `TASK_RECENT_TICKETS_WINDOW_SECONDS` override made the hardcoded
    "(last hour)" header lie under a different value; Alex's ask was a fixed hour, so the
    header just stays honest instead). Returns ``""`` when nothing falls in the window (never
    happens right after a successful create: the ticket just created is always inside its own
    window).
    """
    if session.source == "none":
        return ""
    from .session import read_entries

    entries = read_entries(session.id)
    now = int(time.time())
    in_window = [e for e in entries if e.ts > 0 and 0 <= now - e.ts <= _RECENT_TICKETS_WINDOW_SECONDS]
    # `entries` is oldest-appended-first (session.py: "newest-last"). At 1-second `ts`
    # resolution, two touches in the same second are a real case (a fast create batch, or the
    # just-created ticket landing in the same second as a seeded one) — a plain
    # `sorted(..., reverse=True)` is stable and would keep ties in oldest-appended-first order,
    # silently violating "newest first". Break ties by original (append) position instead.
    by_recency = sorted(enumerate(in_window), key=lambda pair: (pair[1].ts, pair[0]), reverse=True)
    recent = [entry for _, entry in by_recency][:_RECENT_TICKETS_LIMIT]
    if not recent:
        return ""
    lines = [_dim("Recent tickets (last hour):")]
    lines.extend(_dim(f"  {e.id}  {e.title}") for e in recent)
    return "\n".join(lines)


def _print_recent_tickets_after_create(session) -> None:
    """Best-effort wrapper around `_recent_tickets_block`: a sidecar hiccup must never turn a
    successful `create` into a failure — mirrors `_print_session_attention_after_mutation`'s
    same fail-soft contract for the sibling session-sidecar-backed notice."""
    try:
        block = _recent_tickets_block(session)
    except Exception as exc:
        from .logging import log_event

        log_event("recent_tickets.skipped", error=type(exc).__name__)
        return
    if block:
        print(block)


def _record_session_touch(cfg, ticket: Ticket) -> None:
    """Refresh this session's sidecar timestamp for a ticket mutation."""
    if not ticket.id:
        return
    session = _detect_session(cfg)
    if session.source == "none":
        return
    if session.label not in ticket.labels:
        return
    from .session import record

    record(session.id, ticket.id, ticket.title)


def _print_session_attention_after_mutation(args: argparse.Namespace, cfg, backend, *, current_id: str | None = None) -> None:
    """Best-effort human warning after commands that can switch task context."""
    if args.json:
        return
    try:
        session = _detect_session(cfg)
        if session.source == "none":
            return
        tickets = backend.session_tickets(session.label, limit=_LIMIT_INTERACTIVE)
        if current_id is not None:
            tickets = [ticket for ticket in tickets if ticket.id != current_id]
        notice = _session_attention_notice(session, tickets, priority_change=True)
    except Exception as exc:
        from .logging import log_event

        log_event("session.attention.skipped", error=type(exc).__name__)
        return
    if notice:
        print(notice)


def _scope_to_current_user(backend, tickets: list) -> list:
    """Keep only tickets the current user actually reported/filed (issue #59, review P2): an
    unqualified scan would nag about a COWORKER's stale ticket in a shared GitHub/Linear
    project, contradicting this feature's "the user's own open tickets" framing. Reporter
    (issue author / Linear creator), not assignee: `create()` never sets a native assignee on
    either backend, so an assignee-based filter would exclude virtually every ticket task-cli
    itself creates — the reporter is who actually ran task-cli.

    Fails OPEN: if the backend can't determine "who am I" (`current_user()` -> None — a
    transient hiccup, or simply unsupported), keep the unfiltered set rather than dropping every
    ticket — parity with the pre-fix behavior, never worse.

    Also fails OPEN if `current_user()` *raises* — a network hiccup a backend forgot to wrap in
    `BackendError`, or `AttributeError` from a third-party backend that never implements it (the
    protocol is structural, not ABC-enforced) — rather than a "normal" `None` return (review P1,
    found independently by two reviewers). Left uncaught, that exception would propagate into
    `_global_stale_notice_block`'s single broad `except Exception`, which swallows the ENTIRE
    nudge (and skips `mark_checked`) — silently hiding a real stale ticket that has nothing to do
    with the identity lookup. Catching it HERE, at the scope-lookup's own boundary, keeps that
    failure mode contained to "don't scope" instead of "don't nudge at all".
    """
    try:
        me = backend.current_user()
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.identity_lookup_failed", error=type(exc).__name__)
        return tickets
    if not me:
        return tickets
    return [t for t in tickets if t.reporter and t.reporter == me]


def _global_stale_notice_block(args: argparse.Namespace, backend, coordinate: str) -> str:
    """Rate-limited nudge: ANY of the user's own active tickets stale >48h, not just this
    session's (issue #59 — `_session_attention_notice` above only ever sees session-touched
    tickets). Fail-soft end to end: a backend hiccup or a malformed cache file here must never
    break `task list`'s primary output, it's a bonus notice, not the command's job.

    ALWAYS fetches its OWN unfiltered (by `--state`/`--label`) ticket set — never a caller's
    already-filtered `tickets`. A caller in the all-tasks fallback branch already has a ticket
    list in hand, but reusing it silently narrows "every open ticket" to whatever the user
    filtered for (e.g. `task list --state done` would only ever see done tickets and could
    never flag a stale active one). The extra backend round-trip this costs is bounded by the
    rate limit — at most once per window, not once per invocation. It IS narrowed by
    :func:`_scope_to_current_user` right after the fetch, to the current user's own tickets
    (issue #59, review P2) — a shared GitHub/Linear project must not nag one user about a
    coworker's stale ticket.

    Like `daemon.py`'s own polling loop, this reads at most `_LIMIT_INTERACTIVE` (100) tickets
    per backend page — a project with more than that many open tickets won't have the tail
    examined. Matches the daemon's existing `query_limit` precedent rather than adding
    pagination for what's meant to stay a lightweight nudge.
    """
    if args.json:
        return ""
    from . import stale_notice

    try:
        if not stale_notice.should_check(coordinate):
            return ""
        tickets = backend.list(limit=_LIMIT_INTERACTIVE)
        tickets = _scope_to_current_user(backend, tickets)
        stale = stale_notice.stale_tickets(tickets)
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.skipped", error=type(exc).__name__)
        return ""

    # Mark AFTER a completed check (never inside the try above — a transient backend failure
    # must not consume the rate-limit window, see should_check's docstring) and in its OWN
    # try/except: an unwritable cache must not discard an already-computed finding — worst case
    # we merely check again sooner than the window intends, never worse than that.
    try:
        stale_notice.mark_checked(coordinate)
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.mark_failed", error=type(exc).__name__)

    if not stale:
        return ""
    threshold = _format_duration_hours(stale_notice.stale_after_seconds())
    lines = [_warn(f"warning: {len(stale)} open ticket(s) look stale (no backend update in {threshold}+):")]
    lines.extend(_dim(f"    - {_stale_line(ticket, age)}") for ticket, age in stale[:3])
    return "\n".join(lines)


def cmd_list(args: argparse.Namespace) -> int:
    """List tickets. Three shapes, chosen by where you are and what you ask for:

    - **Session scope** (default, inside a repo, in an agent session with tickets) — this
      session's tickets in the current repo.
    - **All-tasks fallback** — same as session scope BUT when there's no agent session, or the
      session has no tickets: fall back to ALL tickets in the current repo, and SAY SO.
    - **Cross-project grouped** — ``--all``, or run OUTSIDE any repo: every known project's
      tickets, grouped under a heading per project. A project whose backend errors shows a
      degraded group; it never aborts the whole aggregation.

    Human output (non-``--json``) is paged through ``less`` when stdout is an interactive TTY
    and the user hasn't opted out (``--no-pager`` / ``NO_PAGER`` / ``$PAGER=''``); a piped run
    prints plain text so it stays scriptable.
    """
    cfg = _load(args)
    state = _parse_state(args.state) if args.state else None
    current = _current_project_overlay(cfg)

    # Cross-project grouped view: an explicit --all, or there's no current repo to scope to.
    if args.all or current is None:
        return _list_grouped(args, cfg, state, outside_repo=current is None)

    # Inside a repo, no --all → session scope, with the all-tasks fallback.
    return _list_session_scoped(args, cfg, state, current)


def _list_session_scoped(args, cfg, state, current) -> int:
    """The default in-repo view: this session's tickets, falling back to all repo tasks."""
    from .backends import BackendError

    backend = _backend(cfg)
    session = _detect_session(cfg)
    no_session = session.source == "none"
    want_labels = set(args.label or [])
    limit = _effective_limit(args)
    try:
        # Whether to fall back is decided on the UNFILTERED session result: a session that HAS
        # tickets but none match --state/--label is a legitimately-empty FILTERED view, NOT a
        # reason to spill every other session's tickets. Only a truly empty session falls back.
        session_tickets = [] if no_session else backend.session_tickets(session.label, limit=limit)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    if no_session or not session_tickets:
        # Fallback: no agent session, or the session has NO tickets at all → show ALL repo tasks
        # and SAY SO, so the user understands why they're seeing everything (not just theirs).
        try:
            tickets = backend.list(labels=args.label or None, state=state, limit=limit)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        if args.json:
            _print_tickets_json(tickets)
            return 0
        notice = _dim("showing all project tasks (`task list` defaults to tasks created in the agent session)")
        # deliberately NOT reusing `tickets` here — it may be --state/--label filtered, and the
        # global check needs its OWN unfiltered view (see _global_stale_notice_block's docstring).
        stale_notice = _global_stale_notice_block(args, backend, current[0])
        _emit_list(args, [notice, stale_notice, _format_tickets(tickets)])
        return 0

    # session HAS tickets → scope to it, then apply the --state/--label filters within that view.
    tickets = session_tickets
    if state is not None:
        tickets = [t for t in tickets if t.state == state]
    if want_labels:
        tickets = [t for t in tickets if want_labels <= set(t.labels)]
    if args.json:
        _print_tickets_json(tickets)
        return 0
    # session_tickets is scoped to THIS session, so the global check needs its own (rate-limited,
    # best-effort) fetch of every project ticket — see _global_stale_notice_block.
    stale_notice = _global_stale_notice_block(args, backend, current[0])
    _emit_list(args, [
        _session_attention_notice(session, session_tickets),
        stale_notice,
        _dim(f"session {session.id} ({session.source}):"),
        _format_tickets(tickets),
    ])
    return 0


def _list_grouped(args, cfg, state, *, outside_repo: bool) -> int:
    """Cross-project view: query every known project and group tickets under its heading."""
    projects, current_coordinate = _known_projects(cfg)
    if not projects:
        # No registry and no current repo: there is nothing to aggregate. Guide the user to
        # register a project rather than printing a bare empty list (informative > silent).
        raise _UserError(
            "no projects to list. You are outside a git repo and no projects are registered.\n"
            "  why: `task list` aggregates across known projects; none are configured.\n"
            "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml "
            "(e.g. `projects: [{repo: owner/name}]`), or run inside a repo."
        )

    groups = _aggregate_projects(
        cfg, projects, labels=args.label or None, state=state, limit=_effective_limit(args),
        current_coordinate=current_coordinate,
    )

    if args.json:
        _print_groups_json(groups)
        return 0

    # The session-vs-all line: an implicit aggregate (outside a repo) must explain itself; an
    # explicit `--all` was asked for, so no apology is needed there.
    notice = (
        _dim("showing all project tasks (`task list` defaults to tasks created in the agent session)")
        if outside_repo
        else ""
    )
    _emit_list(args, [notice, _format_groups(groups)])
    return 0


@dataclass
class _ProjectGroup:
    """One project's slice of the aggregated list: its tickets, or a degraded error."""

    name: str
    backend: str
    tickets: list[Ticket]
    error: str | None = None
    current: bool = False  # True for the repo the user is currently inside


def _aggregate_projects(base_cfg, projects, *, labels, state, limit, current_coordinate=None) -> list:
    """Query each project's backend; a failing one becomes a degraded group, not a hard stop.

    Each project's config is the base config with the project's overlay deep-merged in, so the
    existing :func:`tasklib.backends.get_backend` resolves it unchanged. The aggregation is
    best-effort by design: one unreachable/unauthed/empty project must not sink the rest of the
    cross-repo view (the "never aborts the whole aggregation" rule).
    """
    return _query_projects(
        base_cfg, projects, lambda b: b.list(labels=labels, state=state, limit=limit), current_coordinate
    )


def _query_projects(base_cfg, projects, call, current_coordinate=None) -> list:
    """Run ``call(backend)`` for each project's backend, capturing a failure as a degraded group.

    The single best-effort fan-out used by both the grouped ``list`` and ``find`` — ``call`` is
    the per-backend query (``.list`` or ``.search``). One project's error never aborts the rest.
    ``current_coordinate`` flags the cwd's repo (by coordinate, so it works whether or not that
    repo is also in the registry).
    """
    from .backends import BackendError, get_backend
    from .credentials import CredentialError

    groups: list[_ProjectGroup] = []
    for proj in projects:
        cur = current_coordinate is not None and proj.coordinate == current_coordinate
        try:
            backend = get_backend(_config_for_overlay(base_cfg, proj.overlay))
            tickets = call(backend)
            groups.append(_ProjectGroup(name=proj.name, backend=proj.backend, tickets=tickets, current=cur))
        except (BackendError, CredentialError) as exc:
            groups.append(_ProjectGroup(name=proj.name, backend=proj.backend, tickets=[], error=str(exc), current=cur))
    return groups


def _format_groups(groups) -> str:
    """Render the grouped, cross-project list as a string — a heading per project, tickets beneath.

    Heading shape: ``<name> · <backend> · <N> (current)`` — informative at a glance (which
    backend, how many, is this the repo I'm in). A degraded project shows its one-line error.
    Returned as one string so the caller can route it through the pager (interactive only).
    """
    total = sum(len(g.tickets) for g in groups)
    if total == 0 and all(g.error is None for g in groups):
        return _dim("(no tickets)")
    sections: list[str] = []
    for g in groups:
        lines = []
        marker = _dim(" (current)") if g.current else ""
        if g.error is not None:
            head = f"{g.backend} · " + _err("degraded")
        else:
            head = f"{g.backend} · {len(g.tickets)}"
        lines.append(_bold(g.name) + _dim(" · ") + head + marker)
        if g.error is not None:
            lines.append(_dim(f"  ! {g.error.splitlines()[0]}"))
        elif not g.tickets:
            lines.append(_dim("  (none)"))
        else:
            lines.extend("  " + _ticket_line(t) for t in g.tickets)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)  # blank line BETWEEN project groups (was the per-iter print())


def _print_groups_json(groups) -> None:
    """The machine-readable grouped shape: ``[{project, backend, current, error, tickets}]``."""
    import json

    payload = [
        {
            "project": g.name,
            "backend": g.backend,
            "current": g.current,
            "error": g.error,
            "tickets": [_ticket_dict(t) for t in g.tickets],
        }
        for g in groups
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ── gantt (read-only due-date timeline) ─────────────────────────────────────────────


def _gantt_tickets(args, cfg, state) -> list[Ticket]:
    """Collect the tickets to chart, with the SAME scoping `list` uses (session / all / grouped).

    Returns a flat ticket list — the chart is one timeline, so a cross-project run flattens every
    project's tickets into a single axis (the project shows up via the ticket id prefix). Reuses
    the existing backend queries so `gantt` and `list` never drift on what "this session" means.
    """
    from .backends import BackendError

    current = _current_project_overlay(cfg)
    limit = _effective_limit(args)

    # Cross-project: an explicit --all, or outside any repo → flatten every known project's tickets.
    if args.all or current is None:
        projects, current_coordinate = _known_projects(cfg)
        if not projects:
            raise _UserError(
                "no projects to chart. You are outside a git repo and no projects are registered.\n"
                "  why: `task gantt` charts known projects; none are configured.\n"
                "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml, or run inside a repo."
            )
        groups = _aggregate_projects(
            cfg, projects, labels=args.label or None, state=state, limit=limit,
            current_coordinate=current_coordinate,
        )
        return [t for g in groups for t in g.tickets]

    # In-repo: session scope with the all-tasks fallback, mirroring `list`.
    backend = _backend(cfg)
    session = _detect_session(cfg)
    no_session = session.source == "none"
    want_labels = set(args.label or [])
    try:
        session_tickets = [] if no_session else backend.session_tickets(session.label, limit=limit)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    if no_session or not session_tickets:
        try:
            return backend.list(labels=args.label or None, state=state, limit=limit)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc

    tickets = session_tickets
    if state is not None:
        tickets = [t for t in tickets if t.state == state]
    if want_labels:
        tickets = [t for t in tickets if want_labels <= set(t.labels)]
    return tickets


def _gantt_width(args) -> int:
    """Resolve the bar-area width: an explicit --width wins; else fit the terminal, with a floor."""
    from . import gantt as _g

    if getattr(args, "width", None) is not None:
        return max(1, args.width)
    import shutil

    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    return _g.fit_width(cols)


def cmd_gantt(args: argparse.Namespace) -> int:
    """Render open tickets on a due-date timeline (read-only). Scoping mirrors `task list`.

    Human output is a box-drawing chart (one row per dated ticket, bars on a date axis, a status
    marker, and a clearly-labeled undated section), paged like `list`. `--json` emits the same
    timeline as a deterministic structure. No mutation — this is purely a view.
    """
    from datetime import date

    from . import gantt as _g

    cfg = _load(args)
    state = _parse_state(args.state) if args.state else None
    tickets = _gantt_tickets(args, cfg, state)
    today = date.today()
    chart = _g.layout(tickets, today, width=_gantt_width(args))

    if args.json:
        import json

        print(json.dumps(_g.to_json(chart, today), ensure_ascii=False, indent=2))
        return 0

    text = _g.render(chart, color=_c, today=today)
    _emit_list(args, [text])
    return 0


def _sanitize_attachment_basename(title: str) -> str:
    """Strip ``title`` (tracker-owned, untrusted data) down to a safe, bare filename component
    — no path traversal possible, on ANY platform, into ``--save-attachments DIR``.

    review finding: a naive ``title.replace(os.sep, "_")`` only strips the CURRENT platform's
    separator — on POSIX that's ``/`` (safe), but on Windows ``os.sep`` is ``\\`` while ``/`` is
    ALSO a valid separator there (``os.altsep``), so a title like ``../outside.txt`` would
    survive untouched and ``os.path.join(dest_dir, "../outside.txt")`` would escape ``dest_dir``.
    Fixed by hardcoding BOTH slash characters (not deriving from the running platform's
    ``os.sep``/``os.altsep`` — the title could theoretically be crafted on one platform and read
    back on another, e.g. a synced dotfiles-style config) and by rejecting the bare
    ``.``/``..``/empty results a slash-strip alone can still produce (a title of exactly ``".."``
    has no separator to strip, but would still resolve to ``dest_dir``'s PARENT as a path).

    Also strips NUL and other C0 control characters (review finding, 2nd round): a title like
    ``"\\0"`` isn't a traversal attempt, but NUL is illegal in a POSIX filename and ``os.link``
    raises ``ValueError`` (not ``OSError``) for it — a value ``_write_attachment_exclusive``'s
    caller specifically wasn't catching, so it would crash the command instead of being reported
    as one failed attachment.
    """
    stripped = title.replace("/", "_").replace("\\", "_").strip()
    stripped = "".join(c for c in stripped if ord(c) >= 0x20 and c != "\x7f")
    if stripped in ("", ".", ".."):
        return ""
    return stripped


def _unique_attachment_filenames(attachments, existing: frozenset = frozenset()) -> dict:
    """``{id(attachment): filename}`` with a ``-2``/``-3``/… suffix on any REPEATED title
    (review finding: two attachments can share a title — e.g. two screenshots both named
    ``screenshot.png`` — and writing both to the same bare filename silently drops the first).

    Guards against a SECOND review finding on top of that: a naive "count occurrences of this
    title" scheme can itself collide when a later attachment's OWN title happens to equal an
    earlier DEDUPED name (titles in order ``screenshot.png``, ``screenshot-2.png``,
    ``screenshot.png`` would naively produce ``screenshot.png``, ``screenshot-2.png``,
    ``screenshot-2.png`` — the 2nd and 3rd collide). Fixed by tracking the set of names ACTUALLY
    assigned so far and bumping the suffix until the candidate is free, not just counting how
    many times the original title has been seen.

    ``existing`` (review finding, 3rd round): filenames ALREADY present in the destination
    directory before this run — a tracker-controlled attachment title matching a pre-existing
    file in the user's chosen directory must not silently overwrite it. Those names are seeded
    into the same "already taken" set, so a colliding title gets a ``-N`` suffix exactly like an
    in-batch collision, rather than reusing the exact target name.

    Keyed by ``id()`` rather than the ``Attachment`` object itself: it's a plain (unfrozen)
    dataclass, so it's unhashable — this is only ever looked up against the SAME list within one
    ``_save_attachments`` call, so identity is a safe, cheap key.
    """
    import os

    assigned: set[str] = set(existing)
    names: dict = {}
    for a in attachments:
        # review finding: `a.id` is ALSO tracker-controlled data (the same trust boundary as
        # `a.title`) — falling back to it raw would bypass the sanitizer entirely and reopen the
        # exact traversal hole it exists to close.
        base = _sanitize_attachment_basename(a.title) or _sanitize_attachment_basename(a.id) or "attachment"
        stem, ext = os.path.splitext(base)
        candidate = base
        n = 1
        while candidate in assigned:
            n += 1
            candidate = f"{stem}-{n}{ext}"
        assigned.add(candidate)
        names[id(a)] = candidate
    return names


def _write_attachment_exclusive(dest_dir: str, dest_path: str, data: bytes) -> None:
    """Publish ``data`` at ``dest_path`` atomically: write it COMPLETE to a temp file in
    ``dest_dir`` first, then hard-link the temp file to ``dest_path`` (fails with
    ``FileExistsError`` if anything — a file, a symlink, a race since the caller's
    pre-existing-name scan — already sits at that name; never overwrites, never follows a
    symlink), and always remove the temp file afterward.

    review finding: writing straight to ``dest_path`` with ``open(..., "xb")`` means a write
    that fails PARTWAY (e.g. disk-full mid-write) leaves a truncated, corrupt file already
    claiming ``dest_path``'s name — a retry would then treat that name as "already taken" and
    save the real bytes under a spurious ``-2`` name instead of the intended one. Publishing via
    a hard link only after the FULL write succeeded means a failed write leaves no trace at
    ``dest_path`` at all.
    """
    import os
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".attach-")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
        os.link(tmp_path, dest_path)
    finally:
        os.unlink(tmp_path)


def _save_attachments(backend, ticket, dest_dir: str) -> list[str]:
    """Download each of ``ticket.attachments`` into ``dest_dir``, via the backend's own
    authenticated fetch (Linear's ``fetch_attachment_bytes`` — the actual "read" half of
    ``attachment_mode: native": a plain unauthenticated GET against a ``uploads.linear.app``
    asset 401s, see ``backends/linear.py``'s module docstring). Returns one status line per
    attachment (success or failure) for the caller to print; best-effort per-file, like
    ``_attach_screenshots`` on the write side — one bad attachment (a fetch failure OR a local
    filesystem error: permission denied, full disk) must not abort the rest.

    Backends that don't implement the method (GitHub — no native-attachment dichotomy there)
    are skipped with an explicit line rather than an ``AttributeError``.

    Only fetches attachments the backend confirms as its OWN native asset
    (``is_native_attachment_url``): an attachment's ``url`` is tracker-owned data, not something
    this process constructed, so blindly fetching every attachment url would let a
    malicious/compromised ticket make this CLI probe arbitrary hosts (or, without the scheme
    check in `fetch_attachment_bytes`, read a local file). An external (``link``-mode)
    attachment is reported, not fetched — the URL is already right there in the printed line for
    the user to open directly. review finding (2nd round): a backend that implements
    ``fetch_attachment_bytes`` but NOT ``is_native_attachment_url`` must NOT default to "trust
    everything" (that inverts the whole point of the capability check) — it fails CLOSED
    (nothing gets auto-fetched) until that backend explicitly opts in.
    """
    fetch = getattr(backend, "fetch_attachment_bytes", None)
    if fetch is None:
        return [f"  (backend {backend.name!r} has no native attachment fetch — nothing to save)"]
    is_native = getattr(backend, "is_native_attachment_url", None)

    import os

    from .backends import AmbiguousBackendError, BackendError

    try:
        os.makedirs(dest_dir, exist_ok=True)
        # review finding: reserve names already on disk too, so a tracker-controlled attachment
        # title matching a pre-existing file in the user's chosen directory doesn't overwrite it.
        existing = frozenset(os.listdir(dest_dir))
    except OSError as exc:
        return [f"  ✗ cannot create/list {dest_dir}: {exc}"]

    lines = []
    to_fetch = []
    for a in ticket.attachments:
        if is_native is None or not is_native(a.url):
            lines.append(f"  (skipped, external/unconfirmed link — open directly) {a.title}: {a.url}")
        else:
            to_fetch.append(a)
    # review finding: naming ALL attachments (including skipped ones) let a skipped external
    # attachment's title consume a name slot, giving a later NATIVE attachment a spurious `-2`
    # suffix for no reason the user could see. Only the ones actually being fetched compete for
    # names.
    filenames = _unique_attachment_filenames(to_fetch, existing)
    for a in to_fetch:
        dest_path = os.path.join(dest_dir, filenames[id(a)])
        try:
            data = fetch(a.url)
            _write_attachment_exclusive(dest_dir, dest_path, data)
        # ValueError alongside OSError: review finding — os.link/open can raise ValueError (not
        # OSError) for some illegal-filename byte sequences (a NUL that survived sanitization on
        # a platform-specific edge case, say); belt-and-suspenders on top of
        # `_sanitize_attachment_basename`'s own stripping, so a filesystem-level rejection is
        # still reported as one failed attachment rather than crashing the whole command.
        except (BackendError, AmbiguousBackendError, OSError, ValueError) as exc:
            lines.append(f"  ✗ {a.title}: {exc}")
            continue
        lines.append(f"  ✓ {a.title} -> {dest_path} ({len(data)} bytes)")
    # Deliberately NOT appending a truncation note here (review finding: it duplicated the one
    # `cmd_read` already prints in the attachments listing) — `ticket.attachments_truncated` is
    # the single source of truth, surfaced once in human output (cmd_read) and once in --json
    # (`_ticket_dict`); a caller of this function directly can check the same field itself.
    return lines


def cmd_read(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError
    from .render import render

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    save_dir = getattr(args, "save_attachments", None)
    saved_lines = _save_attachments(backend, ticket, save_dir) if save_dir else []

    if args.json:
        import json

        d = _ticket_dict(ticket)
        d["body"] = render(ticket)
        if save_dir:
            d["saved_attachments"] = saved_lines
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    print(_bold(f"{ticket.id}  {ticket.title}"))
    print(_dim(f"  state: {ticket.state.value}   {ticket.url}"))
    if ticket.labels:
        print(_dim(f"  labels: {', '.join(ticket.labels)}"))
    if ticket.attachments:
        # Real backend-native attachments (Linear "native" mode) — distinct from the body's
        # `## Screenshots` section, which is only the local ref that was REQUESTED at attach
        # time. `native` mode's own asset URL needs the same Authorization header any GraphQL
        # call uses to fetch (see backends/linear.py's module docstring); a raw browser open of
        # it will 401 like any other uploads.linear.app URL — that's expected, not a bug. Use
        # `--save-attachments DIR` to fetch the real bytes through the authenticated path.
        count_note = "+" if ticket.attachments_truncated else ""
        print(_dim(f"  attachments ({len(ticket.attachments)}{count_note}):"))
        for a in ticket.attachments:
            print(_dim(f"    - {a.title}: {a.url}"))
        if ticket.attachments_truncated:
            print(_dim("    (more attachments exist beyond the 250-item cap — not all are listed)"))
    if saved_lines:
        print(_dim("  saved attachments:"))
        for line in saved_lines:
            print(_dim(line))
    print()
    print(render(ticket))
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .backends import BackendError

    state = _parse_state(args.state) if args.state else None

    limit = _effective_limit(args)
    # Inside a repo: search the cwd's backend (current behavior). Outside one: search across
    # every known project and group the hits, so `find` is a true global op anywhere.
    if _current_project_overlay(cfg) is None:
        projects, current_coordinate = _known_projects(cfg)
        if not projects:
            raise _UserError(
                "no projects to search. You are outside a git repo and no projects are registered.\n"
                "  why: `task find` searches known projects; none are configured.\n"
                "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml, or run inside a repo."
            )
        groups = _search_projects(
            cfg, projects, args.query, state=state, limit=limit, current_coordinate=current_coordinate
        )
        if args.json:
            _print_groups_json(groups)
            return 0
        _emit_list(args, [_format_groups(groups)])
        return 0

    backend = _backend(cfg)
    try:
        tickets = backend.search(args.query, state=state, limit=limit)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    if args.json:
        _print_tickets_json(tickets)
        return 0
    _emit_list(args, [_format_tickets(tickets)])
    return 0


def _search_projects(base_cfg, projects, query, *, state, limit, current_coordinate=None) -> list:
    """Cross-project search — the search analogue of :func:`_aggregate_projects`."""
    return _query_projects(
        base_cfg, projects, lambda b: b.search(query, state=state, limit=limit), current_coordinate
    )


def cmd_change(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    # legality FIRST, before any edit mutates the fetched ticket: an illegal `change --done` on a
    # cancelled/already-done ticket must refuse without touching title/labels/screenshots/skips —
    # so a backend that hands back a live/cached Ticket object isn't left dirtied (#10).
    if args.done:
        validate_transition(ticket.state, State.DONE, force=args.force)

    # A --skip-msgref-quote passed on THIS edit turns expansion off for this command — the skip
    # governs the quote machinery, not only the gate (review finding), so a waived false positive
    # isn't expanded into an unrelated Telegram quote. Gate on THIS command's skips only, NOT a
    # skip persisted from a past create: a historical waiver must not permanently disable
    # expansion of a genuinely NEW reference added later (review finding). The enforcement gate
    # below still honours the persisted skip (it lives in ticket.skips) — only expansion is scoped
    # to the current command's intent.
    new_skips = _collect_skips(args)
    expand = _msgref_expand_enabled(new_skips, cfg)
    # Load tg-cli's local history ONCE for this command, and only if a touched field actually
    # names a reference (see _load_msgref_history_if_needed) — a metadata-only edit (--due/
    # --label), an edit naming no tg#<id>, or a msgref-quote-waived edit never touches the
    # (potentially large) history log.
    history = (
        _load_msgref_history_if_needed(args.what or "", args.why or "", args.impact or "", args.if_not_done or "")
        if expand
        else None
    )
    if args.title:
        ticket.title = args.title
    if args.what:
        ticket.what = _expand_msgrefs(args.what, history) if expand else args.what
    if args.why:
        ticket.why = _expand_msgrefs(args.why, history) if expand else args.why
    if args.impact:
        ticket.user_impact = _expand_msgrefs(args.impact, history) if expand else args.impact
    if args.if_not_done:
        ticket.cost_of_inaction = _expand_msgrefs(args.if_not_done, history) if expand else args.if_not_done
    # Acceptance criteria are NEVER msgref-expanded (see the cmd_create comment on why: the
    # single-line checkbox format can't carry a multi-line quote), so this dedup stays plain
    # equality against the stored (always-raw) text — unchanged from before this feature.
    existing_texts = {c.text for c in ticket.acceptance}
    for crit in args.acceptance:
        if crit not in existing_texts:
            ticket.acceptance.append(Criterion(text=crit))
            existing_texts.add(crit)
    if getattr(args, "due", None) is not None:
        # --due passed (incl. --due "" to clear): validate then set. Not passed → leave as-is.
        ticket.due = _normalize_due(args.due)
    new_shots = [Screenshot(ref=path, kind="implementation") for path in args.screenshot]
    ticket.screenshots.extend(new_shots)
    for label in args.label:
        if label not in ticket.labels:
            ticket.labels.append(label)
    ticket.skips.update(new_skips)

    # An edit that TOUCHES a text field is re-scanned for bare references (rule 1) and re-graded
    # on impact quality (rule 5) — whether or not this same command ALSO closes the ticket, so
    # `change --what "…HYP-789…" --done` cannot smuggle a bare reference (or a thinned impact)
    # past the edit gates on its way to DONE. The scan is conservative (it re-validates the whole
    # body, not only the new bytes); a pure metadata edit (--due/--label) touches no scanned
    # field and is exempt entirely (see _enforce_edit_or_die).
    touched_text = any(
        [args.title, args.what, args.why, args.impact, args.if_not_done, args.acceptance]
    )
    _enforce_edit_or_die(
        ticket, cfg, check_links=touched_text, check_impact=bool(args.impact), check_title=bool(args.title)
    )

    if args.done:
        from .policy import Phase

        # enforce the close gates BEFORE the state flip so a refusal doesn't dirty the fetched
        # ticket (#10).
        _enforce_or_die(ticket, cfg, Phase.DONE)
        ticket.state = State.DONE

    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, new_shots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    _record_session_touch(cfg, updated)
    log_event("ticket.changed", ticket_id=updated.id, backend=cfg.backend, closed=args.done)
    _notify_mutation(cfg, updated, "done" if args.done else "changed")
    if args.json:
        import json

        print(json.dumps(_ticket_dict(updated), ensure_ascii=False, indent=2))
    else:
        print(_ok(f"updated {updated.id}  {updated.url}"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    if not args.new_state:
        try:
            ticket = backend.get(args.id)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        print(f"{ticket.id} {_dim('[' + ticket.state.value + ']')} {ticket.title}")
        return 0

    new_state = _parse_state(args.new_state)
    skips = _collect_skips(args)
    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    # legality first: status guards EVERY transition (not only close-to-done) so a cancelled
    # ticket can't be silently revived and a same-state re-write is rejected (#10).
    validate_transition(ticket.state, new_state, force=args.force)
    ticket.skips.update(skips)
    try:
        if new_state is State.DONE:
            from .policy import Phase

            # enforce BEFORE the state flip so a refusal doesn't dirty the fetched ticket (#10).
            _enforce_or_die(ticket, cfg, Phase.DONE)
            ticket.state = new_state
            # persist the MUTATED ticket (carries the recorded skip justifications) — using
            # transition() here would re-fetch and drop ticket.skips, silently losing the
            # audit section a gate was waived under. update() writes the body with the skips.
            updated = backend.update(ticket)
        elif skips:
            # a skip recorded on a non-done transition is still an auditable decision → persist.
            ticket.state = new_state
            updated = backend.update(ticket)
        else:
            updated = backend.transition(args.id, new_state)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    from .logging import log_event

    _record_session_touch(cfg, updated)
    log_event("ticket.transition", ticket_id=updated.id, state=new_state.value)
    _notify_mutation(cfg, updated, "done" if new_state is State.DONE else "changed")
    print(_ok(f"{updated.id} → {new_state.value}"))
    if new_state in _RECENT_PRIORITY_STATES:
        _print_session_attention_after_mutation(
            args,
            cfg,
            backend,
            current_id=updated.id,
        )
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """Close a ticket by id — the dedicated close verb. Runs the on-done gates (and the
    `--skip-<gate>` hatches), and accepts `--screenshot` for the implementation proof a
    UI ticket needs to close. The same close path `change --done` takes, minus the edits.
    """
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError
    from .policy import Phase

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    # legality first: refuse re-closing a cancelled/already-done ticket before any mutation (#10).
    validate_transition(ticket.state, State.DONE, force=args.force)
    new_shots = [Screenshot(ref=path, kind="implementation") for path in args.screenshot]
    ticket.screenshots.extend(new_shots)
    ticket.skips.update(_collect_skips(args))
    # enforce BEFORE mutating state (the DONE phase is passed explicitly, not read off the
    # ticket), so a gate refusal leaves the fetched ticket undirtied — same rule as #10.
    _enforce_or_die(ticket, cfg, Phase.DONE)
    ticket.state = State.DONE

    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, new_shots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    _record_session_touch(cfg, updated)
    log_event("ticket.transition", ticket_id=updated.id, state=State.DONE.value)
    _notify_mutation(cfg, updated, "done")
    print(_ok(f"{updated.id} → {State.DONE.value}"))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Check an acceptance criterion off — but only WITH a visual proof (rule 3).

    A checkbox cannot be ticked without a screenshot/image backing it (``--proof <path>``).
    When a visual proof is genuinely impossible, ``--force "<reason>"`` records the reason on
    the criterion (audited). The checked state + proof live in the body, so the on-done gate
    (rule 2) can refuse a close while any criterion is still unchecked.
    """
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    crit = _select_criterion(ticket, args.selector)
    proofs = list(args.proof or [])
    force_reason = getattr(args, "force_reason", None)
    if not proofs and not force_reason:
        raise _UserError(
            f"refusing to check {crit.text!r}: a visual proof is required.\n"
            "  why: a criterion is only 'done' when there's a screenshot/image proving it.\n"
            '  fix: task check <id> <selector> --proof <path>   (or --force "<reason>" if a '
            "proof is genuinely impossible)."
        )

    crit.checked = True
    if proofs:
        crit.proof = proofs[0]
        crit.force_reason = ""
    else:
        crit.proof = ""
        crit.force_reason = force_reason or ""

    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, [Screenshot(ref=p, kind="implementation") for p in proofs])
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    _record_session_touch(cfg, updated)
    log_event("criterion.checked", ticket_id=updated.id, forced=bool(force_reason and not proofs))
    proof_note = f"proof {proofs[0]}" if proofs else f"forced ({force_reason})"
    remaining = len(updated.unchecked_criteria())
    print(_ok(f"checked {crit.text!r} on {updated.id}  ({proof_note}; {remaining} left)"))
    return 0


def _select_criterion(ticket: Ticket, selector: str) -> Criterion:
    """Resolve a criterion by 1-based index or by a unique text substring; raise on miss/ambiguity."""
    crits = ticket.acceptance
    if not crits:
        raise _UserError(f"ticket {ticket.id or '(unknown)'} has no acceptance criteria to check")
    sel = selector.strip()
    if sel.isdigit():
        idx = int(sel)
        if not 1 <= idx <= len(crits):
            raise _UserError(f"criterion index {idx} out of range (1..{len(crits)})")
        return crits[idx - 1]
    matches = [c for c in crits if sel.lower() in c.text.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _UserError(f"no acceptance criterion matches {selector!r}; use an index (1..{len(crits)})")
    raise _UserError(f"{len(matches)} criteria match {selector!r}; use an index (1..{len(crits)}) to disambiguate")


def cmd_classify(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .classify import Verdict, build_prompt, parse_verdict, resolve_chain

    bias = Verdict(cfg.classify_bias) if cfg.classify_bias in ("change", "justAsk") else Verdict.CHANGE
    resolved = resolve_chain(cfg.classify_fallbacks or None, capability=cfg.classify_capability or None)
    if resolved is None:
        # no provider reachable → bias-decide, still observable
        verdict = bias
        print(_warn(f"  no classifier provider available; biasing to {verdict.value}"))
    else:
        output = _run_review_just_ask(resolved.model_arg, build_prompt(args.text))
        verdict = parse_verdict(output, bias=bias)

    from .logging import log_event

    log_event("classify", verdict=verdict.value, model=(resolved.model_arg if resolved else "none"))

    if args.json:
        import json

        print(json.dumps({"verdict": verdict.value}, ensure_ascii=False))
    else:
        print(f"{verdict.value}")

    if verdict is Verdict.JUST_ASK:
        return 0
    # verdict == change
    if args.update:
        return _classify_append(args, cfg)
    if args.create:
        return _classify_create(args, cfg)
    return 0


def _classify_create(args: argparse.Namespace, cfg) -> int:
    """`change` + --create: dedup against the session, else create from the message."""
    backend = _backend(cfg)
    session = _detect_session(cfg)
    from .backends import BackendError

    # dedup: same session + high title similarity
    candidates = _session_dedup_candidates(backend, session)
    match = _best_dedup_match(args.text, candidates)
    if match is not None:
        try:
            backend.comment(match.id, f"(restated) {args.text.strip()}")
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        _record_session_touch(cfg, match)
        print(_ok(f"appended to {match.id} (dedup)"))
        return 0

    # create from message: an inbound message can't carry full criteria, so the draft is
    # filled with triage placeholders. Rather than silently bypassing policy, we RUN the gates
    # and record every still-failing SKIPPABLE gate as an explicit, auditable auto-skip — so the
    # ticket is always policy-clean by construction and the bypass is visible in the body. The
    # title is derived the SAME way cmd_create's --from-message path does (strip a leading tg-cli
    # inbound wrap first) — this is, after all, the exact hook this classify path exists for
    # (task-cli#45 review finding: the two paths must not derive a title differently).
    what_raw = args.text.strip()
    title = _derive_title_from_message(args.text)
    if not title:
        # A wrap-only inbound message (`[TG from Alex tg#1234]`, no content after it — a
        # malformed or truncated hook call) derives an EMPTY title. cmd_create's --from-message
        # path already refuses this (`if not title: raise _UserError(...)`); this path must
        # refuse the same way rather than silently create a titleless ticket (task-cli#45
        # review finding: the two paths must not diverge on this either).
        raise _UserError("cannot auto-create from this message — no title could be derived from it")
    ticket = Ticket(
        title=title,
        what=what_raw,
        why="(auto-created from an inbound message; needs triage)",
        user_impact="(needs triage)",
        cost_of_inaction="(needs triage)",
        acceptance=[Criterion(text="triage this request and fill in the criteria")],
        labels=list(dict.fromkeys([*cfg.section("github").get("default_labels", []), session.label, "needs-triage"])),
        # See the matching NOTE in cmd_create: the session id belongs in `labels` only, never
        # duplicated into `links` (which is for real URL references like a PR link, not session
        # metadata — the Links renderer does not enforce URLs, so junk there slips through).
    )
    blocking = _auto_skip_failing_gates(ticket, cfg)
    if blocking:
        # A NON-skippable gate still fails after auto-skipping everything that CAN be skipped —
        # e.g. msgref-title, if the title-derivation fix above somehow still let a tg#<id>
        # through. Refuse loudly rather than silently persist a ticket that violates a rule with
        # no legitimate exception (task-cli#45 review finding: this is exactly the scenario the
        # non-skippable gate exists to prevent, and it must never be bypassed here either).
        names = "; ".join(f"{v.gate}: {v.message}" for v in blocking)
        raise _UserError(f"cannot auto-create from this message — non-skippable gate(s) failed: {names}")
    # Expand tg#<id> mentions in `what` the SAME way cmd_create's --from-message path does — this
    # classify path is ALSO a tg-cli hook entry point (it IS the `task classify "<msg>" --create`
    # call an agent makes from an inbound message), so it must not be the one path that silently
    # drops the quote a reader would otherwise get everywhere else (task-cli#45 review finding).
    # Honours the same master switch as the other paths: no expansion (nor history read) when
    # `enforce.msgref_quote: false` or the reference is waived (review finding).
    if _msgref_expand_enabled(ticket.skips, cfg):
        ticket.what = _expand_msgrefs(ticket.what, _load_msgref_history_if_needed(what_raw))

    created, ambiguous_exit = _create_ticket_or_ambiguous(backend, ticket)
    if ambiguous_exit is not None:
        return ambiguous_exit
    from .session import record

    record(session.id, created.id, created.title)
    # classify --create is the tg-cli inbound hook path; unlike cmd_create, it must not append
    # a session-attention notice because the hook consumes stdout programmatically.
    print(_ok(f"created {created.id} from message  {created.url}"))
    return 0


def _auto_skip_failing_gates(ticket: Ticket, cfg) -> list:
    """Run the create gates and record any failing SKIPPABLE gate as an auditable auto-skip.

    Used by the inbound-classify path, where a message can't satisfy every gate up front. A
    skippable gate's waiver is recorded, visible in the ``Skipped gates`` section — never a
    silent bypass. Returns any violations that are NOT skippable (see
    ``policy.NON_SKIPPABLE_GATES``): those are NEVER auto-skipped here — a gate with no
    legitimate exception (e.g. ``msgref-title``) must not be silently waived just because this
    path can't fill in every field up front (task-cli#45 review finding). The caller must refuse
    the create when this returns anything.
    """
    from .policy import NON_SKIPPABLE_GATES, Phase, check

    blocking = []
    for v in check(ticket, _enforce_config(cfg), Phase.CREATE).violations:
        if v.gate in NON_SKIPPABLE_GATES:
            blocking.append(v)
        else:
            ticket.skips.setdefault(v.gate, "auto-created from inbound message; pending triage")
    return blocking


def _classify_append(args: argparse.Namespace, cfg) -> int:
    backend = _backend(cfg)
    from .backends import BackendError

    try:
        backend.comment(args.update, f"(restated) {args.text.strip()}")
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    try:
        ticket = backend.get(args.update)
    except BackendError:
        ticket = None
    if ticket is not None:
        _record_session_touch(cfg, ticket)
    print(_ok(f"appended to {args.update}"))
    return 0


def _best_dedup_match(text: str, candidates: list[Ticket]) -> Ticket | None:
    """Conservative dedup: pick the candidate whose title is highly similar to the message.

    ``text`` is matched via the SAME title derivation :func:`_derive_title_from_message` uses
    (strip a leading tg-cli inbound wrap, first line, truncate) — not the raw first line — so a
    wrapped inbound message (``[TG from Alex tg#1234] <message>``) is compared against a stored
    candidate's title on equal terms. A stored title is ALREADY wrap-stripped (every ticket this
    path creates goes through the same derivation); comparing it against the raw wrapped first
    line would score a genuine repeat-message low on similarity and create a duplicate ticket
    instead of dedup-commenting on the existing one (task-cli#45 review finding).
    """
    import difflib

    target = _derive_title_from_message(text).lower()
    best: tuple[float, Ticket] | None = None
    for t in candidates:
        if t.state in (State.DONE, State.CANCELLED):
            continue
        ratio = difflib.SequenceMatcher(None, target, t.title.lower()).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, t)
    if best and best[0] >= 0.7:  # high-similarity threshold (conservative; §12b)
        return best[1]
    return None


def _run_review_just_ask(model_arg: str, prompt: str) -> str:
    """Shell out to ``review just-ask "<prompt>" -m <model> --pool 1``. Returns stdout.

    The single classification shell-out. Modes in review-cli are SUBCOMMANDS (``review
    just-ask …``), not flags. ``review`` resolves/calls the model; ``--pool 1`` is one
    fast/cheap model, no panel. A failure returns empty output (the caller biases).
    """
    review = _which_review()
    if not review:
        return ""
    try:
        out = subprocess.run(
            [review, "just-ask", prompt, "-m", model_arg, "--pool", "1"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def _which_review() -> str | None:
    import shutil

    return shutil.which("review")


def cmd_session(args: argparse.Namespace) -> int:
    cfg = _load(args)
    session = _detect_session(cfg)
    if args.action == "bind":
        if not args.bind_id:
            raise _UserError("`task session bind` needs a ticket id")
        from .session import record

        record(session.id, args.bind_id, "")
        print(_ok(f"bound {args.bind_id} to session {session.id}"))
        return 0

    # show
    from .session import read_ids

    ids = read_ids(session.id)
    if args.json:
        import json

        print(json.dumps({"id": session.id, "source": session.source, "label": session.label, "tickets": ids}))
        return 0
    print(_bold(f"session: {session.id}") + _dim(f"  (source: {session.source})"))
    print(_dim(f"  label: {session.label}"))
    if ids:
        print(_dim(f"  tickets ({len(ids)}): {', '.join(ids)}"))
    else:
        print(_dim("  no tickets recorded yet"))
    return 0


def _daemon_coordinate(cfg) -> str:
    """The repo/team coordinate that keys this daemon's state files (one daemon per project).

    Reuses the same single-target resolution every command uses. A daemon is repo-bound — it
    watches ONE project's tickets — so running it outside a repo (no pinned coordinate) is a
    clean 3-part error, never a cryptic backend failure.
    """
    current = _current_project_overlay(cfg)
    if current is None:
        raise _UserError(
            "cannot run the daemon: no project context (you are outside a git repo).\n"
            "  why: the daemon watches ONE project's due dates, so it must know which backend/repo.\n"
            "  fix: run inside the target git repo, or pass `--repo owner/name` / set `linear.team`."
        )
    name, _overlay = current
    return name


def cmd_daemon(args: argparse.Namespace) -> int:
    """Dispatch the daemon lifecycle action (start | stop | status | run)."""
    cfg = _load(args)
    coordinate = _daemon_coordinate(cfg)
    from . import daemon

    if args.action == "run":
        paths = daemon.paths_for(coordinate)
        return daemon.run_loop(cfg, paths)
    if args.action == "start":
        return _daemon_start(daemon, coordinate, cfg, args)
    if args.action == "stop":
        return _daemon_stop(daemon, coordinate)
    return _daemon_status(daemon, coordinate, cfg, args)


def _daemon_child_flags(args: argparse.Namespace) -> list[str]:
    """The backend-selecting global flags to forward to the spawned ``daemon run`` child.

    These are exactly the flags that change coordinate/backend resolution — so the child resolves
    the SAME coordinate the launcher checked (otherwise start isn't idempotent and stop/status
    miss the daemon). ``-C`` is added separately by the spawn.
    """
    flags: list[str] = []
    if getattr(args, "backend", None):
        flags += ["--backend", args.backend]
    if getattr(args, "repo", None):
        flags += ["--repo", args.repo]
    if getattr(args, "config", None):
        flags += ["--config", args.config]
    return flags


def _daemon_start(daemon, coordinate: str, cfg, args: argparse.Namespace) -> int:
    dcfg = daemon.DaemonConfig.from_config(cfg)
    if not dcfg.enabled:
        print(_warn("daemon is disabled in config (daemon.enabled: false) — not starting"))
        return 0
    outcome, pid = daemon.start(coordinate, cwd=str(cfg.repo_root), child_flags=_daemon_child_flags(args))
    if outcome == "already-running":
        print(_dim(f"daemon already running (pid {pid}) for {coordinate}"))
    else:
        print(_ok(f"daemon started (pid {pid}) for {coordinate}  interval={dcfg.interval_s}s"))
    return 0


def _daemon_stop(daemon, coordinate: str) -> int:
    outcome, pid = daemon.stop(coordinate)
    if outcome == "stopped":
        print(_ok(f"daemon stopped (was pid {pid})"))
    elif outcome == "timeout":
        print(_warn(f"daemon (pid {pid}) did not exit in time; pid-file cleared"))
    elif outcome == "not-ours":
        # the recorded pid is alive but is NOT our daemon (a crash + OS pid-reuse) — we refused to
        # signal it; the stale pid-file is cleared. Surface this so it isn't mistaken for a clean stop.
        print(_warn(f"pid {pid} is alive but is not the task daemon (recycled pid); not signalled, pid-file cleared"))
    else:
        print(_dim("no running daemon"))
    return 0


def _daemon_status(daemon, coordinate: str, cfg, args: argparse.Namespace) -> int:
    paths = daemon.paths_for(coordinate)
    status, pid = daemon.pid_status(paths.pid)
    dcfg = daemon.DaemonConfig.from_config(cfg)
    if getattr(args, "json", False):
        import json

        print(
            json.dumps(
                {
                    "status": status,
                    "pid": pid,
                    "coordinate": coordinate,
                    "interval_s": dcfg.interval_s,
                    "due_soon_days": dcfg.due_soon_days,
                    "notifier": list(dcfg.notifier),
                    "pidfile": str(paths.pid),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    line = {
        "running": _ok(f"running (pid {pid})"),
        "not-ours": _warn(f"recycled pid-file (pid {pid} is alive but is a foreign, reused process)"),
        "stale": _warn(f"stale pid-file (pid {pid} is gone)"),
        "stopped": _dim("stopped"),
    }.get(status, _warn(f"{status} (pid {pid})"))
    print(f"daemon: {line}  for {coordinate}")
    print(_dim(f"  interval={dcfg.interval_s}s  due_soon={dcfg.due_soon_days}d  notifier={' '.join(dcfg.notifier)}"))
    print(_dim(f"  pidfile={paths.pid}"))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


if __name__ == "__main__":
    raise SystemExit(main())
