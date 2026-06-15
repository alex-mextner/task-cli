"""task CLI — argparse + subcommand dispatch (the effectful entrypoint).

The thin entry point (``[project.scripts] task = "tasklib.cli:main"`` and the target of the
``bin/task`` shim). It owns argument parsing, the backend/classify shell-outs, and the
filesystem (sidecar). All pure logic lives in the sibling modules (``model``/``render``/
``policy``/``classify``/``session``/``config``). Heavy/optional imports (yaml via config,
the backends) are lazy so ``task --help`` stays fast and dependency-light.

Subcommands: create · list · read/view · find · change · status · classify · session.
Global flags: --backend, --repo, --config, --json, --yes, and per-gate --skip-<gate>.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .model import Screenshot, State, Ticket

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

    # create
    cp = sub.add_parser("create", parents=[common], help="create a ticket (enforces the policy gates)")
    cp.add_argument("--title", help="ticket title")
    cp.add_argument("--from-message", dest="from_message", metavar="TEXT", help="raw user text → derive title/body")
    cp.add_argument("--what", help="the change (one paragraph)")
    cp.add_argument("--acceptance", action="append", default=[], metavar="CRIT", help="acceptance criterion (repeatable)")
    cp.add_argument("--why", help="motivation")
    cp.add_argument("--impact", help="user impact")
    cp.add_argument("--if-not-done", dest="if_not_done", help="cost of inaction")
    cp.add_argument("--screenshot", action="append", default=[], metavar="PATH", help="screenshot (repeatable)")
    cp.add_argument("--label", action="append", default=[], help="label (repeatable)")
    cp.add_argument("--yes", action="store_true", help="non-interactive; do not prompt")
    _add_skip_flags(cp)

    # list
    lp = sub.add_parser("list", parents=[common], help="list THIS session's tickets (default)")
    lp.add_argument("--all", action="store_true", help="all tickets, not just this session")
    lp.add_argument("--mine", action="store_true", help="only tickets assigned to me")
    lp.add_argument("--state", help="filter by state (todo|in-progress|in-review|done|cancelled)")
    lp.add_argument("--label", action="append", default=[], help="filter by label (repeatable)")
    lp.add_argument("-n", type=int, default=30, dest="limit", help="max results (default 30)")

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
    fp.add_argument("-n", type=int, default=30, dest="limit", help="max results (default 30)")

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
    chp.add_argument("--done", action="store_true", help="close the ticket (runs the on-done gates)")
    _add_skip_flags(chp)

    # status
    stp = sub.add_parser("status", parents=[common], help="read or transition a ticket's state")
    stp.add_argument("id", help="ticket id")
    stp.add_argument("new_state", nargs="?", help="new state (todo|in-progress|in-review|done|cancelled)")
    _add_skip_flags(stp)

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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "create": cmd_create,
        "list": cmd_list,
        "read": cmd_read,
        "view": cmd_read,
        "find": cmd_find,
        "change": cmd_change,
        "status": cmd_status,
        "classify": cmd_classify,
        "session": cmd_session,
        "install-skill": cmd_install_skill,
    }
    try:
        return handlers[args.command](args)
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


def _print_tickets(tickets: list[Ticket], as_json: bool) -> None:
    if as_json:
        import json

        print(json.dumps([_ticket_dict(t) for t in tickets], ensure_ascii=False, indent=2))
        return
    if not tickets:
        print(_dim("(no tickets)"))
        return
    for t in tickets:
        print(_ticket_line(t))


def _ticket_dict(t: Ticket) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "state": t.state.value,
        "url": t.url,
        "labels": t.labels,
        "what": t.what,
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


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend(cfg)
    from .backends import BackendError

    state = _parse_state(args.state) if args.state else None
    try:
        if args.all or args.label:
            tickets = backend.list(labels=args.label or None, state=state, limit=args.limit)
        else:
            session = _detect_session(cfg)
            tickets = backend.session_tickets(session.label, limit=args.limit)
            if state is not None:
                tickets = [t for t in tickets if t.state == state]
            if not args.json:
                print(_dim(f"session {session.id} ({session.source}):"))
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    _print_tickets(tickets, args.json)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend(cfg)
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
    backend = _backend(cfg)
    from .backends import BackendError

    state = _parse_state(args.state) if args.state else None
    try:
        tickets = backend.search(args.query, state=state, limit=args.limit)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    _print_tickets(tickets, args.json)
    return 0


def cmd_change(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend(cfg)
    from .backends import BackendError

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

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
    backend = _backend(cfg)
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


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


if __name__ == "__main__":
    raise SystemExit(main())
