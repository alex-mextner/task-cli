"""task CLI — argparse + subcommand dispatch (the effectful entrypoint).

The thin entry point (``[project.scripts] task = "tasklib.cli:main"`` and the target of the
``bin/task`` shim). It owns argument parsing, the backend/classify shell-outs, and the
filesystem (sidecar). All pure logic lives in the sibling modules (``model``/``render``/
``policy``/``classify``/``session``/``config``). Heavy/optional imports (yaml via config,
the backends) are lazy so ``task --help`` stays fast and dependency-light.

Subcommands: create/new · list · read/view · find · change · status · done · classify · session
· daemon (the due-date reminder watcher: start/stop/status/run).
Global flags: --backend, --repo, --config, --json, --yes, and per-gate --skip-<gate>.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .model import Screenshot, State, Ticket
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
    ("--skip-cost-of-inaction", "cost-of-inaction"),
    ("--skip-screenshots", "screenshots"),
    ("--skip-formatting", "formatting"),
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

    # read / view
    rp = sub.add_parser("read", parents=[common], help="show a full ticket")
    rp.add_argument("id", help="ticket id (#123 or HYP-456)")
    vp = sub.add_parser("view", parents=[common], help="alias of read")
    vp.add_argument("id", help="ticket id")

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
        "read": cmd_read,
        "view": cmd_read,
        "find": cmd_find,
        "change": cmd_change,
        "status": cmd_status,
        "done": cmd_done,
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
    return {
        "id": t.id,
        "title": t.title,
        "state": t.state.value,
        "url": t.url,
        "labels": t.labels,
        "what": t.what,
        "due": t.due,
    }


# ── policy enforcement helper (shared by create + change/close) ─────────────────────


def _enforce_or_die(ticket: Ticket, cfg, phase) -> None:
    from .policy import Phase, check

    result = check(ticket, _enforce_config(cfg), phase)
    if result.skipped:
        print(_warn(f"  skipped gates (justified): {', '.join(result.skipped)}"))
    if not result.ok:
        label = "create" if phase is Phase.CREATE else "close"
        print(_err(f"refusing to {label}: {len(result.violations)} gate(s) unmet"))
        for v in result.violations:
            print(_err(f"  ✗ {v.gate}: {v.message}"))
            if v.hint:
                print(_dim(f"      → {v.hint}  (or --skip-{v.gate} \"<reason>\")"))
        raise _UserError("policy gates not satisfied")


def _attach_screenshots(backend, ticket_id: str, screenshots) -> None:
    """Push each screenshot to the backend's attachment endpoint (durable proof).

    The body already embeds the local ref via render(); ``attach()`` is the durable channel
    (GitHub: a reference comment; Linear: an issue attachment). Best-effort: an attach failure
    must not undo a successful create/update, so a backend error is swallowed (the ref still
    lives in the body). Exercises the ``TicketBackend.attach`` contract rather than leaving it
    a dangling method.
    """
    from .backends import BackendError

    for shot in screenshots:
        try:
            backend.attach(ticket_id, shot.ref)
        except BackendError:
            continue


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
        # derive a title from the first line of the raw message (the hook path)
        first = args.from_message.strip().split("\n", 1)[0]
        title = first[:72]
        what = what or args.from_message.strip()
    if not title:
        raise _UserError("a title is required (--title, or --from-message to derive one)")

    labels = list(dict.fromkeys([*args.label, session.label]))
    screenshots = [Screenshot(ref=p, kind="creation") for p in args.screenshot]
    due = _normalize_due(getattr(args, "due", None)) or ""
    ticket = Ticket(
        title=title,
        what=what,
        why=args.why or "",
        user_impact=args.impact or "",
        cost_of_inaction=args.if_not_done or "",
        acceptance=list(args.acceptance),
        screenshots=screenshots,
        labels=labels,
        links={"Session": session.label},
        skips=_collect_skips(args),
        due=due,
    )

    from .policy import Phase

    _enforce_or_die(ticket, cfg, Phase.CREATE)

    backend = _backend(cfg)
    from .backends import BackendError

    try:
        created = backend.create(ticket)
        _attach_screenshots(backend, created.id, screenshots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .session import record

    record(session.id, created.id, created.title)
    from .logging import log_event

    log_event("ticket.created", ticket_id=created.id, backend=cfg.backend, session=session.id)

    if args.json:
        import json

        print(json.dumps(_ticket_dict(created), ensure_ascii=False, indent=2))
    else:
        print(_ok(f"created {created.id}  {created.url}"))
    return 0


# Limit defaults: a small one when piped/scripted (machine-readable, bounded), a larger one
# in an interactive TTY where the pager scrolls and a 30-row cap would just hide tickets.
_LIMIT_PIPED = 30
_LIMIT_INTERACTIVE = 100


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
        _emit_list(args, [notice, _format_tickets(tickets)])
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
    _emit_list(args, [_dim(f"session {session.id} ({session.source}):"), _format_tickets(tickets)])
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


def cmd_read(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError
    from .render import render

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    if args.json:
        import json

        d = _ticket_dict(ticket)
        d["body"] = render(ticket)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    print(_bold(f"{ticket.id}  {ticket.title}"))
    print(_dim(f"  state: {ticket.state.value}   {ticket.url}"))
    if ticket.labels:
        print(_dim(f"  labels: {', '.join(ticket.labels)}"))
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

    if args.title:
        ticket.title = args.title
    if args.what:
        ticket.what = args.what
    if args.why:
        ticket.why = args.why
    if args.impact:
        ticket.user_impact = args.impact
    if args.if_not_done:
        ticket.cost_of_inaction = args.if_not_done
    for crit in args.acceptance:
        if crit not in ticket.acceptance:
            ticket.acceptance.append(crit)
    if getattr(args, "due", None) is not None:
        # --due passed (incl. --due "" to clear): validate then set. Not passed → leave as-is.
        ticket.due = _normalize_due(args.due)
    new_shots = [Screenshot(ref=path, kind="implementation") for path in args.screenshot]
    ticket.screenshots.extend(new_shots)
    for label in args.label:
        if label not in ticket.labels:
            ticket.labels.append(label)
    ticket.skips.update(_collect_skips(args))

    if args.done:
        ticket.state = State.DONE
        from .policy import Phase

        _enforce_or_die(ticket, cfg, Phase.DONE)

    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, new_shots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    log_event("ticket.changed", ticket_id=updated.id, backend=cfg.backend, closed=args.done)
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
    ticket.state = new_state
    try:
        if new_state is State.DONE:
            from .policy import Phase

            _enforce_or_die(ticket, cfg, Phase.DONE)
            # persist the MUTATED ticket (carries the recorded skip justifications) — using
            # transition() here would re-fetch and drop ticket.skips, silently losing the
            # audit section a gate was waived under. update() writes the body with the skips.
            updated = backend.update(ticket)
        elif skips:
            # a skip recorded on a non-done transition is still an auditable decision → persist.
            updated = backend.update(ticket)
        else:
            updated = backend.transition(args.id, new_state)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    from .logging import log_event

    log_event("ticket.transition", ticket_id=updated.id, state=new_state.value)
    print(_ok(f"{updated.id} → {new_state.value}"))
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
    ticket.state = State.DONE
    _enforce_or_die(ticket, cfg, Phase.DONE)

    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, new_shots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    log_event("ticket.transition", ticket_id=updated.id, state=State.DONE.value)
    print(_ok(f"{updated.id} → {State.DONE.value}"))
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .classify import Verdict, build_prompt, parse_verdict, resolve_chain

    bias = Verdict(cfg.classify_bias) if cfg.classify_bias in ("change", "justAsk") else Verdict.CHANGE
    resolved = resolve_chain(cfg.classify_fallbacks or None)
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
    try:
        candidates = backend.session_tickets(session.label, limit=30)
    except BackendError:
        candidates = []
    match = _best_dedup_match(args.text, candidates)
    if match is not None:
        try:
            backend.comment(match.id, f"(restated) {args.text.strip()}")
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        print(_ok(f"appended to {match.id} (dedup)"))
        return 0

    # create from message: an inbound message can't carry full criteria, so the draft is
    # filled with triage placeholders. Rather than silently bypassing policy, we RUN the gates
    # and record every still-failing gate as an explicit, auditable auto-skip — so the ticket
    # is always policy-clean by construction and the bypass is visible in the body.
    first = args.text.strip().split("\n", 1)[0]
    ticket = Ticket(
        title=first[:72],
        what=args.text.strip(),
        why="(auto-created from an inbound message; needs triage)",
        user_impact="(needs triage)",
        cost_of_inaction="(needs triage)",
        acceptance=["triage this request and fill in the criteria"],
        labels=list(dict.fromkeys([*cfg.section("github").get("default_labels", []), session.label, "needs-triage"])),
        links={"Session": session.label},
    )
    _auto_skip_failing_gates(ticket, cfg)
    try:
        created = backend.create(ticket)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    from .session import record

    record(session.id, created.id, created.title)
    print(_ok(f"created {created.id} from message  {created.url}"))
    return 0


def _auto_skip_failing_gates(ticket: Ticket, cfg) -> None:
    """Run the create gates and record any failure as an auditable auto-skip on the ticket.

    Used by the inbound-classify path, where a message can't satisfy every gate up front. The
    result is a ticket that passes policy by construction, with each waived gate visible in the
    ``Skipped gates`` section — never a silent bypass.
    """
    from .policy import Phase, check

    for v in check(ticket, _enforce_config(cfg), Phase.CREATE).violations:
        ticket.skips.setdefault(v.gate, "auto-created from inbound message; pending triage")


def _classify_append(args: argparse.Namespace, cfg) -> int:
    backend = _backend(cfg)
    from .backends import BackendError

    try:
        backend.comment(args.update, f"(restated) {args.text.strip()}")
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    print(_ok(f"appended to {args.update}"))
    return 0


def _best_dedup_match(text: str, candidates: list[Ticket]) -> Ticket | None:
    """Conservative dedup: pick the candidate whose title is highly similar to the message."""
    import difflib

    target = text.strip().split("\n", 1)[0].lower()
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
        "stale": _warn(f"stale pid-file (pid {pid} is gone)"),
        "stopped": _dim("stopped"),
    }[status]
    print(f"daemon: {line}  for {coordinate}")
    print(_dim(f"  interval={dcfg.interval_s}s  due_soon={dcfg.due_soon_days}d  notifier={' '.join(dcfg.notifier)}"))
    print(_dim(f"  pidfile={paths.pid}"))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


if __name__ == "__main__":
    raise SystemExit(main())
