"""CLI dispatch — argparse front-end + end-to-end flows against the FakeBackend.

The backend is injected by monkeypatching ``tasklib.backends.get_backend``; the classify
shell-out is monkeypatched too. No network, no gh/linear/review subprocess.
"""

from __future__ import annotations

import pytest

from tasklib import cli
from tasklib.cli import _mutation_message, build_parser, main
from tasklib.model import Ticket


@pytest.fixture(autouse=True)
def _inject_fake(monkeypatch, fake_backend, isolated_state):
    """Every CLI test gets the in-memory fake backend and an isolated state dir."""
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: fake_backend)
    # force git-branch session detection to a stable id (no ambient tmux/env)
    monkeypatch.setenv("TASK_SESSION", "testsess")
    return fake_backend


# ── arg parsing: every subcommand parses ────────────────────────────────────────────


def test_help_runs(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "enforced ticket interface" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    rc = main([])
    assert rc == 0
    assert "enforced ticket interface" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "--title", "t"],
        ["new", "--title", "t"],
        ["done", "#1"],
        ["done", "#1", "--screenshot", "after.png"],
        ["list"],
        ["list", "--all", "--state", "todo"],
        ["gantt"],
        ["gantt", "--all", "--width", "60"],
        ["gantt", "--state", "todo", "--label", "ui", "--json"],
        ["read", "#1"],
        ["view", "#1"],
        ["find", "query"],
        ["change", "#1", "--title", "x"],
        ["status", "#1"],
        ["status", "#1", "done"],
        ["classify", "some text"],
        ["session"],
        ["session", "bind", "#1"],
    ],
)
def test_subcommand_parses(argv):
    # parsing must not raise SystemExit (which argparse does on a bad arg spec)
    parser = build_parser()
    ns = parser.parse_args(argv)
    assert ns.command == argv[0]


# ── create flow + enforcement ───────────────────────────────────────────────────────


_GOOD_IMPACT = (
    "Users on the dashboard can finally see their report load, so they no longer give up and "
    "leave thinking the page is broken"
)


def _create_argv(**over):
    argv = [
        "create",
        "--title",
        "Add a thing",
        "--what",
        "the change",
        "--why",
        "because",
        "--impact",
        _GOOD_IMPACT,
        "--if-not-done",
        "pain",
        "--acceptance",
        "it works",
        "--acceptance",
        "it also handles the empty case",
    ]
    for k, v in over.items():
        argv += [k, v]
    return argv


def _ready_to_close(fake, ticket_id="#1"):
    """Tick every acceptance criterion (with a dummy proof) so a ticket can pass the on-done
    'all criteria checked' gate (rule 2). Returns nothing — the backend is mutated in place."""
    count = len(fake.get(ticket_id).acceptance)
    for i in range(1, count + 1):
        main(["check", ticket_id, str(i), "--proof", "proof.png"])


def test_create_complete_ticket_succeeds(capsys, _inject_fake):
    rc = main(_create_argv())
    assert rc == 0
    out = capsys.readouterr().out
    assert "created #1" in out
    assert "active tasks in this session" not in out
    assert len(_inject_fake.list()) == 1


def test_create_refuses_when_gate_unmet(capsys):
    # drop --why → motivation gate fails
    argv = [
        "create", "--title", "t", "--what", "c", "--impact", "u", "--if-not-done", "p",
        "--acceptance", "works",
    ]
    rc = main(argv)
    assert rc == 2
    out = capsys.readouterr().out
    assert "refusing to create" in out
    assert "motivation" in out


def test_create_escape_hatch_allows_skip(capsys, _inject_fake):
    argv = [
        "create", "--title", "t", "--what", "c", "--impact", _GOOD_IMPACT, "--if-not-done", "p",
        "--acceptance", "works", "--acceptance", "also handles the empty input",
        "--skip-motivation", "spike, no motivation needed",
    ]
    rc = main(argv)
    assert rc == 0
    # the skipped-gates summary is a diagnostic → stderr, keeping stdout clean for --json
    assert "skipped gates (justified): motivation" in capsys.readouterr().err


def test_create_from_message_derives_title(capsys, _inject_fake):
    rc = main(
        [
            "create", "--from-message", "fix the broken header on mobile", "--why", "b",
            "--impact", _GOOD_IMPACT, "--if-not-done", "p",
            "--acceptance", "works", "--acceptance", "also covers the empty input",
        ]
    )
    assert rc == 0
    created = _inject_fake.list()[0]
    assert created.title.startswith("fix the broken header")


def test_create_from_message_strips_tg_inbound_wrap_from_title(capsys, _inject_fake):
    # task-cli#45 review finding: --from-message is the documented tg-cli hook path (see
    # tasklib.msgrefs's module docstring) -- an agent forwarding an inbound message passes the
    # WHOLE wrapped text, `[TG from Alex tg#1234] <message>`. The wrap's own tg#1234 must not
    # end up in the derived TITLE (it would trip the non-skippable msgref-title gate on a
    # message that legitimately quotes itself); it must still reach `what` untouched.
    rc = main(
        [
            "create", "--from-message", "[TG from Alex tg#1234] fix the broken header on mobile",
            "--why", "b", "--impact", _GOOD_IMPACT, "--if-not-done", "p",
            "--acceptance", "works", "--acceptance", "also covers the empty input",
        ]
    )
    assert rc == 0
    created = _inject_fake.list()[0]
    assert created.title == "fix the broken header on mobile"
    assert "tg#1234" not in created.title
    assert "[TG from Alex tg#1234]" in created.what


def test_create_records_session_sidecar(_inject_fake):
    from tasklib.session import read_ids

    main(_create_argv())
    assert read_ids("testsess") == ["#1"]


def test_create_puts_session_in_labels_not_links(_inject_fake):
    # Regression (issue #54): `create` must NOT duplicate the session id into the Links section.
    # The session lives in `labels` only (the value session_tickets() queries by); a non-URL
    # "Session: session:<id>" entry under `## Links` surfaced as junk in the rendered ticket.
    from tasklib.render import render

    main(_create_argv())
    created = _inject_fake.list()[0]
    assert created.links == {}
    assert "session:testsess" in created.labels
    # and the rendered body shows an empty Links section, not the fake session "link".
    # Bound the slice to the Links section itself (up to the next `## ` heading, if any) so the
    # assertions measure that section rather than the rest of the document.
    body = render(created)
    links_section = body.split("## Links", 1)[1].split("\n## ", 1)[0]
    assert "session:testsess" not in links_section
    assert "- (none)" in links_section


# ── ambiguous backend failure after create (task-cli bug 1) ─────────────────────────


def test_create_ambiguous_backend_error_prints_distinct_message_and_exit_code(capsys, monkeypatch, _inject_fake):
    # a connection drop mid-read AFTER the fake provider "accepted" the create must NOT look
    # like a bare traceback, and must NOT collide with the clean-refusal exit code (2) that
    # _UserError / TransitionError already use — a caller needs to tell the two apart.
    from tasklib.backends import AmbiguousBackendError, EXIT_AMBIGUOUS

    def raise_ambiguous(ticket):
        raise AmbiguousBackendError("github: connection interrupted while reading the response for POST url (status 201 was already returned)")

    monkeypatch.setattr(_inject_fake, "create", raise_ambiguous)
    rc = main(_create_argv())
    out = capsys.readouterr().out
    assert rc == EXIT_AMBIGUOUS
    assert rc != 2
    assert "ambiguous" in out
    assert "duplicate" in out
    assert len(_inject_fake.list()) == 0  # the fake never actually stored anything


# ── create-time dedup (task-cli bug 2: `create` had no duplicate guard) ─────────────
#
# `_classify_create` (the inbound-message hook) already dedups against the session via
# `_best_dedup_match`/`session_tickets()` before creating; `cmd_create`/`task new` had no such
# guard, so a caller retrying after ANY ambiguous result (e.g. the AmbiguousBackendError tested
# above) could silently create a genuine duplicate ticket. These pin the fix.


def test_create_blocks_close_duplicate_title_without_force(capsys, _inject_fake):
    rc1 = main(_create_argv())
    assert rc1 == 0
    capsys.readouterr()

    rc2 = main(_create_argv())  # identical title, same session
    out = capsys.readouterr().out

    assert rc2 == 2  # a clean, no-network-call-made refusal — the _UserError class
    assert "#1" in out  # identifies the matching existing ticket
    assert "duplicate" in out
    assert len(_inject_fake.list()) == 1  # no second ticket was created


def test_create_force_bypasses_duplicate_block(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()

    rc = main(_create_argv(**{"--force": "intentional duplicate, needed for X"}))
    assert rc == 0
    assert len(_inject_fake.list()) == 2  # --force let the second one through


def test_create_distinct_title_is_not_flagged_as_duplicate(capsys, _inject_fake):
    main(_create_argv())  # "Add a thing"
    capsys.readouterr()

    rc = main(_create_argv(**{"--title": "Completely unrelated feature request for the sidebar"}))
    assert rc == 0
    assert len(_inject_fake.list()) == 2  # a genuinely distinct title is never blocked


def test_create_dedup_scoped_to_session_not_global(capsys, monkeypatch, _inject_fake):
    # a same-title ticket from a DIFFERENT session must not block this one — dedup mirrors
    # `_classify_create`'s scoping (session_tickets(), not a global search).
    main(_create_argv())
    capsys.readouterr()

    monkeypatch.setenv("TASK_SESSION", "othersess")
    rc = main(_create_argv())
    assert rc == 0
    assert len(_inject_fake.list()) == 2


# ── `new` alias + `done` close verb (CTO-requested ergonomics, issue #8) ─────────────


def test_new_is_an_alias_of_create(capsys, _inject_fake):
    # `new` takes the identical argument set and creates a ticket just like `create`.
    argv = _create_argv()
    argv[0] = "new"
    rc = main(argv)
    assert rc == 0
    assert "created #1" in capsys.readouterr().out
    assert len(_inject_fake.list()) == 1


def test_new_enforces_the_create_gates(capsys, _inject_fake):
    # the alias is not an escape hatch: a missing motivation still refuses.
    rc = main(["new", "--title", "t", "--what", "c", "--impact", "u", "--if-not-done", "p", "--acceptance", "w"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "refusing to create" in out and "motivation" in out


def test_done_closes_a_non_ui_ticket(capsys, _inject_fake):
    main(_create_argv())
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["done", "#1"])
    assert rc == 0
    assert "→ done" in capsys.readouterr().out
    assert _inject_fake.get("#1").state.value == "done"


def test_done_runs_the_on_done_gates(capsys, _inject_fake):
    # a UI ticket with only a creation screenshot cannot be closed via `done` — the on-done
    # gate demands the implementation proof (same enforcement as `change --done`/`status done`).
    main(_create_argv() + ["--label", "ui", "--screenshot", "creation.png"])
    capsys.readouterr()
    rc = main(["done", "#1"])
    assert rc == 2
    assert "implementation" in capsys.readouterr().out


def test_done_with_implementation_screenshot_closes_and_attaches(_inject_fake):
    main(_create_argv() + ["--label", "ui", "--screenshot", "creation.png"])
    _ready_to_close(_inject_fake)
    rc = main(["done", "#1", "--screenshot", "after.png"])
    assert rc == 0
    assert ("#1", "after.png") in _inject_fake.attachments


def test_done_persists_skip_justification(_inject_fake):
    # `done --skip-screenshots` records the waiver in the body, not lost to a re-fetch.
    main(_create_argv() + ["--label", "ui", "--skip-screenshots", "no proof at create"])
    _ready_to_close(_inject_fake)
    rc = main(["done", "#1", "--skip-screenshots", "config-only change, no UI"])
    assert rc == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "## Skipped gates" in body and "config-only change, no UI" in body


def test_done_skip_links_waives_the_close_phase_links_gate(capsys, _inject_fake):
    # the close-command --skip-links flag is wired and waives the now-active close-phase links
    # gate: a close-ready ticket carrying a bare reference (e.g. one edited in the web UI) closes
    # with a recorded reason instead of being stuck. Inject the bare ref into the stored ticket to
    # simulate a body that never passed the create gate.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    _inject_fake.get("#1").what = "regressed by HYP-789"  # a bare ref the create gate never saw
    capsys.readouterr()
    # without the skip, the close is refused on links
    assert main(["done", "#1"]) == 2
    assert "links" in capsys.readouterr().out
    # with --skip-links on the close command, it closes and records the waiver in the body
    rc = main(["done", "#1", "--skip-links", "legacy ref edited in the GitHub UI"])
    assert rc == 0
    from tasklib.render import render

    t = _inject_fake.get("#1")
    assert t.state.value == "done"
    assert "## Skipped gates" in render(t) and "legacy ref edited in the GitHub UI" in render(t)


def test_done_skip_user_impact_quality_waives_the_close_phase_quality_gate(capsys, _inject_fake):
    # symmetric to the links case: the close-command --skip-user-impact-quality flag is wired and
    # waives the now-active close-phase quality gate, so a close-ready ticket whose impact was
    # thinned (e.g. edited in the web UI) closes with a recorded reason instead of being stuck.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    _inject_fake.get("#1").user_impact = "users"  # a thin impact the create gate never graded
    capsys.readouterr()
    assert main(["done", "#1"]) == 2
    assert "user-impact-quality" in capsys.readouterr().out
    rc = main(["done", "#1", "--skip-user-impact-quality", "internal tool, no end user"])
    assert rc == 0
    assert _inject_fake.get("#1").state.value == "done"


def test_create_force_links_waiver_persists_through_close(capsys, _inject_fake):
    # the documented migration: a bare reference waived at create with --force is recorded in the
    # body's Skipped gates, so the ticket closes WITHOUT re-specifying the skip — the close-phase
    # links gate honors the persisted waiver via the render/parse round-trip.
    main(_create_argv() + ["--what", "follow-up to HYP-789", "--force", "legacy ref, pre-link era"])
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["done", "#1"])
    assert rc == 0
    assert _inject_fake.get("#1").state.value == "done"


def test_done_on_unknown_id_is_clean_error_not_traceback(capsys, _inject_fake):
    # a backend lookup miss surfaces as a clean `error:` (exit 2), never a traceback.
    rc = main(["done", "#999"])
    assert rc == 2
    assert "error:" in capsys.readouterr().out


# ── list session-scoping ────────────────────────────────────────────────────────────


def test_list_defaults_to_session(capsys, _inject_fake):
    main(_create_argv())  # creates #1 labelled session:testsess
    # a second ticket in a different session should NOT show in the default list
    other = _inject_fake.create(
        type(_inject_fake.list()[0])(title="other", labels=["session:elsewhere"])
    )
    capsys.readouterr()
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out
    assert other.id not in out


def test_list_all_shows_everything(capsys, _inject_fake):
    main(_create_argv())
    _inject_fake.create(type(_inject_fake.list()[0])(title="other", labels=["session:elsewhere"]))
    capsys.readouterr()
    main(["list", "--all"])
    out = capsys.readouterr().out
    assert "#1" in out and "#2" in out


def test_list_label_filters_session_view(capsys, _inject_fake):
    # --label narrows the SESSION list too (not only the --all path): a session ticket WITHOUT
    # the requested label is excluded. Regression for the codex finding that --label was ignored
    # whenever the current session had tickets.
    main(_create_argv())  # #1: session:testsess, no extra label
    # a distinct title so the create-time dedup guard doesn't fold this into a comment on #1
    main(_create_argv(**{"--title": "Add a second, unrelated thing"}) + ["--label", "urgent"])  # #2
    capsys.readouterr()
    rc = main(["list", "--label", "urgent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#2" in out
    assert "#1" not in out


def test_list_filter_excludes_all_session_tickets_does_not_fall_back(capsys, _inject_fake):
    # a session that HAS tickets but none match the filter is a legitimately-empty FILTERED view
    # — it must NOT fall back to all tasks (which would spill other sessions' tickets). Regression
    # for the codex P1 (fallback decided on filtered result leaked cross-session tickets).
    main(_create_argv())  # #1: this session, state=todo
    _inject_fake.create(type(_inject_fake.list()[0])(title="other-session", labels=["session:elsewhere"]))
    capsys.readouterr()
    rc = main(["list", "--label", "nonexistent-label"])
    out = capsys.readouterr().out
    assert rc == 0
    # no fallback line, and the OTHER session's ticket must not appear
    assert "showing all project tasks" not in out
    assert "other-session" not in out and "#2" not in out


def _write_session_touch(session_id: str, ticket_id: str, title: str, ts: int) -> None:
    import json

    from tasklib.session import sidecar_path

    path = sidecar_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": ticket_id, "title": title, "ts": ts}) + "\n", encoding="utf-8")


def _write_raw_session_line(session_id: str, payload: object) -> None:
    import json

    from tasklib.session import sidecar_path

    path = sidecar_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_list_warns_about_stale_active_session_task(capsys, _inject_fake):
    stale = Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS)
    _inject_fake.create(stale)
    _write_session_touch("testsess", "#1", "Old active work", 1)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "active tasks in this session may need attention" in out
    assert "#1 [in-progress] Old active work" in out
    assert "parallel" in out


def test_list_recent_active_task_prompts_continue_or_deprioritize(capsys, _inject_fake, monkeypatch):
    import time

    monkeypatch.setenv("TASK_RECENT_WARNING_SECONDS", "999999")
    _inject_fake.create(Ticket(title="Started just now", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _inject_fake.create(Ticket(title="New priority", labels=["session:testsess"], state=cli.State.TODO))
    _write_session_touch("testsess", "#1", "Started just now", int(time.time()))

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "recently touched active task" in out
    assert "ask whether to continue it or park it" in out


def test_list_legacy_sidecar_without_timestamp_does_not_warn(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Old sidecar format", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_raw_session_line("testsess", {"id": "#1", "title": "Old sidecar format"})

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "active tasks in this session may need attention" not in out
    assert "#1" in out


def test_list_ignores_non_object_sidecar_lines(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_raw_session_line("testsess", [])

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "#1" in out


def test_list_ignores_sidecar_lines_with_non_string_ids(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_raw_session_line("testsess", {"id": ["#1"], "title": "bad id", "ts": 1})

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "#1" in out


def test_list_future_sidecar_timestamp_is_not_recent(capsys, _inject_fake, monkeypatch):
    import time

    monkeypatch.setenv("TASK_RECENT_WARNING_SECONDS", "999999")
    _inject_fake.create(Ticket(title="Future timestamp", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _inject_fake.create(Ticket(title="Other priority", labels=["session:testsess"], state=cli.State.TODO))
    _write_session_touch("testsess", "#1", "Future timestamp", int(time.time()) + 3600)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "recently touched active task" not in out


def test_list_json_omits_stale_task_warning(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", 1)

    rc = main(["list", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "active tasks in this session" not in out
    import json

    payload = json.loads(out)
    assert payload[0]["id"] == "#1"


# ── issue #59: global stale-ticket nudge (not scoped to THIS session) ──────────────


def _iso_hours_ago(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_list_warns_about_globally_stale_ticket_outside_session(capsys, _inject_fake):
    """A ticket THIS session never touched (no session label at all) is invisible to
    `_session_attention_notice` (session-scoped only). It must still surface here once it has
    gone >48h without a backend update while active — the gap issue #59 closes."""
    # keep `session_tickets` non-empty so `_list_session_scoped` takes the session-scoped branch,
    # not the "no session tickets -> show everything" fallback — proving the global check looks
    # beyond this session's own tickets, not just reusing the fallback's already-broad query.
    _inject_fake.create(Ticket(title="Session work", labels=["session:testsess"], state=cli.State.TODO))
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()
    assert "#2" in out
    assert "Forgotten ticket" in out
    # pins _stale_line's age-rendering unit: stale_tickets() returns age in SECONDS, and
    # _stale_line feeds it straight into _age_text (also seconds) — 50h ago must render as
    # "2d ago" (50h // 24 = 2), not a nonsense value from a unit mismatch (review finding).
    assert "(updated 2d ago)" in out


def test_list_does_not_warn_about_recently_updated_active_ticket(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Session work", labels=["session:testsess"], state=cli.State.TODO))
    fresh = _inject_fake.create(Ticket(title="Fresh ticket", state=cli.State.IN_PROGRESS))
    fresh.updated_at = _iso_hours_ago(1)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" not in out.lower()


def test_list_global_stale_warning_does_not_repeat_within_rate_limit_window(capsys, _inject_fake):
    """Rate-limited: an immediate second `task list` must not re-nag (a local timestamp cache,
    not per-process memory, since every CLI invocation is a fresh process)."""
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)

    rc1 = main(["list"])
    out1 = capsys.readouterr().out
    rc2 = main(["list"])
    out2 = capsys.readouterr().out

    assert rc1 == 0 and rc2 == 0
    assert "stale" in out1.lower()
    assert "stale" not in out2.lower()


def test_list_json_omits_global_stale_warning(capsys, _inject_fake):
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)

    rc = main(["list", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" not in out.lower()


def test_list_global_stale_check_failure_is_best_effort(capsys, _inject_fake, monkeypatch):
    """A backend hiccup in the global stale check must never break `task list`'s own output —
    it's a bonus nudge, not the command's job (see _global_stale_notice_block's fail-soft try)."""
    from tasklib.backends import BackendError

    _inject_fake.create(Ticket(title="Session work", labels=["session:testsess"], state=cli.State.TODO))
    original_list = _inject_fake.list

    def flaky_list(*args, **kwargs):
        if kwargs.get("limit") == cli._LIMIT_INTERACTIVE:  # the global check's own fetch
            raise BackendError("boom")
        return original_list(*args, **kwargs)

    monkeypatch.setattr(_inject_fake, "list", flaky_list)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Session work" in out
    assert "stale" not in out.lower()


def test_list_global_stale_check_failure_leaves_rate_limit_window_open(capsys, _inject_fake, monkeypatch):
    """A transient backend failure must not consume the rate-limit window — mark_checked only
    runs after a successful scan (see _global_stale_notice_block), so the NEXT invocation gets
    a real retry instead of being silently rate-limited for the failed one."""
    from tasklib.backends import BackendError

    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)
    original_list = _inject_fake.list
    calls = {"n": 0}

    def flaky_once(*args, **kwargs):
        if kwargs.get("limit") == cli._LIMIT_INTERACTIVE:
            calls["n"] += 1
            if calls["n"] == 1:
                raise BackendError("boom")
        return original_list(*args, **kwargs)

    monkeypatch.setattr(_inject_fake, "list", flaky_once)

    rc1 = main(["list"])
    out1 = capsys.readouterr().out
    rc2 = main(["list"])
    out2 = capsys.readouterr().out

    assert rc1 == 0 and rc2 == 0
    assert "stale" not in out1.lower()  # the flaky first call swallowed the check
    assert "stale" in out2.lower()  # window was NOT consumed — the retry actually ran


def test_list_global_stale_notice_reflects_custom_threshold(capsys, _inject_fake, monkeypatch):
    monkeypatch.setenv("TASK_GLOBAL_STALE_SECONDS", str(2 * 3600))
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(3)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "2h+" in out


def test_list_global_stale_notice_formats_sub_hour_threshold_precisely(capsys, _inject_fake, monkeypatch):
    # a naive `seconds // 3600` would print "1h+" for 90 minutes and "0h+" for any sub-hour
    # value — both misleading (review finding: 5400s must read as "1.5h+", not floor to "1h+").
    monkeypatch.setenv("TASK_GLOBAL_STALE_SECONDS", "5400")  # 90 minutes
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(2)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "1.5h+" in out
    assert "0h+" not in out and "1h+" not in out


def test_list_global_stale_check_ignores_tickets_reported_by_someone_else(capsys, _inject_fake):
    """P2 (review): an unqualified `backend.list()` scans EVERY ticket in a shared GitHub/Linear
    project, not just the current user's — contradicting issue #59's stated scope ("check ALL of
    THE USER'S open tickets"). A stale ticket someone else filed must never surface in my nudge,
    but MY stale ticket must still surface."""
    _inject_fake.current_user_result = "alex"
    _inject_fake.create(Ticket(title="Session work", labels=["session:testsess"], state=cli.State.TODO))
    mine = _inject_fake.create(Ticket(title="My forgotten ticket", state=cli.State.IN_PROGRESS))
    mine.updated_at = _iso_hours_ago(50)
    mine.reporter = "alex"
    theirs = _inject_fake.create(Ticket(title="Coworker forgotten ticket", state=cli.State.IN_PROGRESS))
    theirs.updated_at = _iso_hours_ago(50)
    theirs.reporter = "bob"

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()
    assert "My forgotten ticket" in out
    assert "Coworker forgotten ticket" not in out


def test_list_global_stale_check_keeps_scanning_when_identity_is_unknown(capsys, _inject_fake):
    """When the backend can't determine "who am I" (current_user() -> None, e.g. an
    unauthenticated-for-that-endpoint token or a transient hiccup), the reporter filter must fail
    OPEN — degrade to the prior unfiltered behavior — rather than silently disabling the whole
    nudge. Worse-case parity with before this fix, never worse."""
    _inject_fake.current_user_result = None
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)
    stale.reporter = "someone"

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()
    assert "Forgotten ticket" in out


def test_list_global_stale_check_keeps_scanning_when_identity_lookup_raises(capsys, _inject_fake, monkeypatch):
    """P1 (review, found independently by two reviewers): `backend.current_user()` can RAISE
    (a network hiccup a backend forgot to wrap, or any other unexpected error) instead of just
    returning None. Before this fix that exception propagated out of `_scope_to_current_user`
    into `_global_stale_notice_block`'s single broad `except Exception`, which swallows the
    ENTIRE nudge — worse than not scoping at all, because a real stale ticket that has nothing
    to do with the identity lookup goes unreported, and `mark_checked` never runs either. The
    reporter filter must fail OPEN on a raise exactly like it does on a None return."""

    def raise_boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(_inject_fake, "current_user", raise_boom)
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)
    stale.reporter = "someone"

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()
    assert "Forgotten ticket" in out


def test_list_global_stale_check_keeps_scanning_when_current_user_is_missing(capsys, _inject_fake, monkeypatch):
    """P1 (review): same fail-open contract when `current_user()` is missing entirely (a minimal
    third-party ``TicketBackend`` — the protocol is structural, not ABC-enforced — that never
    implements it), which raises ``AttributeError`` rather than a "normal" exception. Must
    degrade the same way as a raise or a None return, not swallow the whole nudge."""
    monkeypatch.delattr(type(_inject_fake), "current_user")
    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)
    stale.reporter = "someone"

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()
    assert "Forgotten ticket" in out


def test_list_global_stale_notice_survives_unwritable_cache(capsys, _inject_fake, monkeypatch):
    """A failure to persist the rate-limit marker must not discard an already-computed finding
    (see _global_stale_notice_block's separate try/except around mark_checked)."""
    from tasklib import stale_notice

    stale = _inject_fake.create(Ticket(title="Forgotten ticket", state=cli.State.IN_PROGRESS))
    stale.updated_at = _iso_hours_ago(50)

    def raise_mark(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(stale_notice, "mark_checked", raise_mark)

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "stale" in out.lower()


def test_create_warns_about_stale_active_session_task(capsys, _inject_fake):
    stale = Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS)
    _inject_fake.create(stale)
    _write_session_touch("testsess", "#1", "Old active work", 1)

    rc = main(_create_argv())
    out = capsys.readouterr().out

    assert rc == 0
    assert "created #2" in out
    assert "active tasks in this session may need attention" in out
    assert "#1 [in-progress] Old active work" in out
    assert "parallel" in out


def test_create_warns_about_recent_active_session_task(capsys, _inject_fake):
    import time

    _inject_fake.create(Ticket(title="Started work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Started work", int(time.time()))

    rc = main(_create_argv())
    out = capsys.readouterr().out

    assert rc == 0
    assert "created #2" in out
    assert "#1 [in-progress] Started work" in out
    assert "ask whether to continue it or park it" in out


def test_create_json_omits_stale_task_warning(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", 1)

    rc = main(_create_argv() + ["--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "active tasks in this session" not in out
    import json

    payload = json.loads(out)
    assert payload["id"] == "#2"


def test_create_attention_notice_failure_is_best_effort(capsys, monkeypatch):
    def _raise_notice(*_args, **_kwargs):
        raise RuntimeError("broken notice")

    monkeypatch.setattr(cli, "_session_attention_notice", _raise_notice)

    rc = main(_create_argv())
    out = capsys.readouterr().out

    assert rc == 0
    assert "created #1" in out


def test_status_to_in_progress_warns_after_priority_change(capsys, _inject_fake, monkeypatch):
    import time

    monkeypatch.setenv("TASK_RECENT_WARNING_SECONDS", "999999")
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", int(time.time()))
    _inject_fake.create(Ticket(title="New priority", labels=["session:testsess"], state=cli.State.TODO))

    rc = main(["status", "#2", "in-progress"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "#2" in out and "in-progress" in out
    assert "#1 [in-progress] Old active work" in out
    assert "New priority" not in out
    assert "ask whether to continue it or park it" in out


def test_change_metadata_does_not_emit_priority_attention(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", 1)
    _inject_fake.create(Ticket(title="Metadata edit", labels=["session:testsess"], state=cli.State.TODO))

    rc = main(["change", "#2", "--title", "Renamed metadata edit"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "updated #2" in out
    assert "active tasks in this session" not in out


def test_status_transition_refreshes_session_touch(capsys, _inject_fake):
    from tasklib.session import read_entries

    _inject_fake.create(Ticket(title="Touch me", labels=["session:testsess"], state=cli.State.TODO))
    _write_session_touch("testsess", "#1", "Touch me", 1)

    rc = main(["status", "#1", "in-progress"])
    capsys.readouterr()

    assert rc == 0
    entries = read_entries("testsess")
    assert entries[-1].id == "#1"
    assert entries[-1].ts > 1


def test_status_transition_does_not_record_ticket_from_another_session(capsys, _inject_fake):
    from tasklib.session import read_entries

    _inject_fake.create(Ticket(title="Other session", labels=["session:elsewhere"], state=cli.State.TODO))

    rc = main(["status", "#1", "in-progress"])
    capsys.readouterr()

    assert rc == 0
    assert read_entries("testsess") == []


# ── gantt (read-only due-date timeline) ──────────────────────────────────────────────


def test_gantt_renders_dated_and_undated(capsys, _inject_fake):
    # #1: dated, in this session;  #2: undated, in this session (distinct title so the
    # create-time dedup guard doesn't fold it into a comment on #1)
    main(_create_argv() + ["--due", "2026-07-01"])
    main(_create_argv(**{"--title": "Add a second, unrelated thing"}))
    capsys.readouterr()
    rc = main(["gantt", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out
    assert "undated" in out  # the no-due ticket is shown in its own section, not hidden


def test_gantt_json_timeline_shape(capsys, _inject_fake):
    # distinct titles so the create-time dedup guard doesn't fold #2 into a comment on #1
    main(_create_argv() + ["--due", "2026-07-01"])
    main(_create_argv(**{"--title": "Add a second, unrelated thing"}))  # undated
    capsys.readouterr()
    rc = main(["gantt", "--json", "--width", "30"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    payload = json.loads(out)
    assert set(payload) == {"window", "rows", "undated"}
    assert payload["window"]["width"] == 30
    ids = [r["id"] for r in payload["rows"]]
    assert "#1" in ids  # the dated ticket charted as a row
    assert [u["id"] for u in payload["undated"]] == ["#2"]


def test_gantt_empty_is_clean(capsys, _inject_fake):
    # no tickets at all → a message, exit 0, no traceback
    rc = main(["gantt", "--all", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no tickets" in out.lower()


def test_gantt_does_not_mutate(_inject_fake):
    main(_create_argv() + ["--due", "2026-07-01"])
    before = _inject_fake.get("#1").state
    main(["gantt", "--no-pager"])
    main(["gantt", "--json"])
    # read-only: state untouched, no comments/attachments written
    assert _inject_fake.get("#1").state == before
    assert _inject_fake.comments == []
    assert _inject_fake.attachments == []


def test_gantt_session_filter_excluding_all_does_not_fall_back(capsys, _inject_fake):
    # parity with `list`: a session that HAS tickets but none match --label must NOT spill other
    # sessions' tickets via the all-tasks fallback (the same regression `list` guards).
    main(_create_argv() + ["--due", "2026-07-01"])  # #1: this session
    _inject_fake.create(type(_inject_fake.list()[0])(title="other-session", labels=["session:elsewhere"]))
    capsys.readouterr()
    rc = main(["gantt", "--label", "nonexistent-label", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    payload = json.loads(out)
    # nothing from the other session leaked in (neither as a row nor undated)
    all_ids = [r["id"] for r in payload["rows"]] + [u["id"] for u in payload["undated"]]
    assert "#2" not in all_ids


def test_gantt_width_zero_clamps_not_crash(capsys, _inject_fake):
    main(_create_argv() + ["--due", "2026-07-01"])
    capsys.readouterr()
    rc = main(["gantt", "--width", "0", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    assert json.loads(out)["window"]["width"] == 1  # floored to 1, no divide-by-zero


# ── read / change / status ──────────────────────────────────────────────────────────


def test_read_shows_sections(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    main(["read", "#1"])
    out = capsys.readouterr().out
    assert "## What" in out and "## Acceptance criteria" in out


def test_change_adds_acceptance(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    main(["change", "#1", "--acceptance", "also handles edge case"])
    fetched = _inject_fake.get("#1")
    assert "also handles edge case" in [c.text for c in fetched.acceptance]


def test_status_read_only(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    main(["status", "#1"])
    out = capsys.readouterr().out
    assert "#1" in out and "todo" in out


def test_status_transition_to_done_enforces(capsys, _inject_fake):
    # a UI ticket with only a CREATION screenshot cannot be closed via `status done`:
    # the on-done gate demands the implementation proof, which status can't supply.
    argv = _create_argv() + ["--label", "ui", "--screenshot", "creation.png"]
    main(argv)
    capsys.readouterr()
    rc = main(["status", "#1", "done"])
    assert rc == 2
    assert "implementation" in capsys.readouterr().out


def test_status_transition_done_non_ui_succeeds(capsys, _inject_fake):
    # a non-UI ticket has no screenshot gate → status done just transitions.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["status", "#1", "done"])
    assert rc == 0
    assert "→ done" in capsys.readouterr().out


def test_status_done_does_not_emit_priority_attention(capsys, _inject_fake):
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", 1)
    main(_create_argv())
    _ready_to_close(_inject_fake, "#2")
    capsys.readouterr()

    rc = main(["status", "#2", "done"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "→ done" in out
    assert "active tasks in this session" not in out


def test_status_done_persists_skip_justification(_inject_fake):
    # closing a UI ticket via `status done --skip-screenshots` must RECORD the waiver in the
    # body (not lose it to a re-fetching transition()). Regression for the Codex/Opus P1.
    main(_create_argv() + ["--label", "ui", "--skip-screenshots", "no proof at create"])
    _ready_to_close(_inject_fake)
    rc = main(["status", "#1", "done", "--skip-screenshots", "config-only change, no UI"])
    assert rc == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "## Skipped gates" in body
    assert "config-only change, no UI" in body


def test_bad_state_is_clean_error_not_traceback(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["status", "#1", "bogus-state"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out and "unknown state" in out


def test_list_bad_state_is_clean_error(capsys, _inject_fake):
    rc = main(["list", "--all", "--state", "nonsense"])
    assert rc == 2
    assert "error:" in capsys.readouterr().out


def test_create_calls_attach_for_screenshots(_inject_fake):
    main(_create_argv() + ["--label", "ui", "--screenshot", "mock.png"])
    assert ("#1", "mock.png") in _inject_fake.attachments


def test_change_close_refuses_ui_without_implementation_screenshot(capsys, _inject_fake):
    # create a UI ticket WITH a creation screenshot (passes create) but NO recorded waiver.
    main(_create_argv() + ["--label", "ui", "--screenshot", "creation.png"])
    capsys.readouterr()
    # closing demands the IMPLEMENTATION proof specifically — a creation shot is not enough.
    rc = main(["change", "#1", "--done"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "implementation" in out


def test_change_close_allowed_with_implementation_screenshot(capsys, _inject_fake):
    main(_create_argv() + ["--label", "ui", "--screenshot", "creation.png"])
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["change", "#1", "--screenshot", "after.png", "--done"])
    assert rc == 0


def test_change_close_recorded_waiver_persists(capsys, _inject_fake):
    # a recorded screenshots waiver is an auditable decision and legitimately carries through.
    main(_create_argv() + ["--label", "ui", "--skip-screenshots", "config-only, not real UI"])
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["change", "#1", "--done"])
    assert rc == 0


# ── close-path transition legality (issue #10) ───────────────────────────────────────


def _force_state(backend, ticket_id, state):
    """Put a stored ticket into ``state`` directly (bypassing the close paths under test)."""
    from tasklib.model import State

    backend.get(ticket_id).state = State(state) if not isinstance(state, State) else state


# Every (close-verb argv) the three close paths reach DONE through. Parametrizing over these
# proves the SHARED validator gates all three, not just one. ``#1`` is the ticket each test seeds.
_CLOSE_PATHS = [
    pytest.param(["done", "#1"], id="done"),
    pytest.param(["change", "#1", "--done"], id="change--done"),
    pytest.param(["status", "#1", "done"], id="status-done"),
]


@pytest.mark.parametrize("close_argv", _CLOSE_PATHS)
def test_close_on_cancelled_ticket_refuses(capsys, _inject_fake, close_argv):
    # a CANCELLED ticket must NOT be silently resurrected to done by any close path.
    main(_create_argv())
    _force_state(_inject_fake, "#1", "cancelled")
    capsys.readouterr()
    rc = main(close_argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out
    assert "cancelled" in out and "illegal transition" in out
    # the refusal must NOT have re-written the ticket to done.
    assert _inject_fake.get("#1").state.value == "cancelled"


@pytest.mark.parametrize("close_argv", _CLOSE_PATHS)
def test_close_on_already_done_ticket_refuses(capsys, _inject_fake, close_argv):
    # a re-close of an already-DONE ticket is a no-op re-write → clean error, not a silent rerun.
    main(_create_argv())
    _force_state(_inject_fake, "#1", "done")
    _inject_fake.attachments.clear()
    capsys.readouterr()
    rc = main(close_argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out and "already done" in out
    # the no-op refusal must not re-fire side effects (attachments).
    assert _inject_fake.attachments == []


@pytest.mark.parametrize("close_argv", _CLOSE_PATHS)
def test_close_on_open_ticket_still_works(capsys, _inject_fake, close_argv):
    # the legal todo → done path is UNCHANGED: a fresh ticket still closes cleanly via every verb.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(close_argv)
    out = capsys.readouterr().out
    assert rc == 0
    # done/status print "→ done"; change prints "updated ..." — either way it reached done.
    assert "→ done" in out or "updated" in out
    assert _inject_fake.get("#1").state.value == "done"


def test_force_reopens_a_cancelled_ticket_via_status(capsys, _inject_fake):
    # --force is the explicit override the acceptance criteria require: it bypasses the legality
    # check so an operator can deliberately move a cancelled ticket back into an active state.
    main(_create_argv())
    _force_state(_inject_fake, "#1", "cancelled")
    capsys.readouterr()
    rc = main(["status", "#1", "in-progress", "--force"])
    assert rc == 0
    assert _inject_fake.get("#1").state.value == "in-progress"


def test_status_illegal_non_done_transition_refuses(capsys, _inject_fake):
    # the validator guards the GENERAL status transition too, not only the close-to-done path:
    # cancelled → in-review is illegal without --force.
    main(_create_argv())
    _force_state(_inject_fake, "#1", "cancelled")
    capsys.readouterr()
    rc = main(["status", "#1", "in-review"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "illegal transition" in out
    assert _inject_fake.get("#1").state.value == "cancelled"


def test_status_read_only_never_validates(capsys, _inject_fake):
    # `task status <id>` with NO new state is a pure read — it must short-circuit BEFORE the
    # validator (a None target would otherwise crash). A cancelled ticket still reads cleanly.
    main(_create_argv())
    _force_state(_inject_fake, "#1", "cancelled")
    capsys.readouterr()
    rc = main(["status", "#1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out and "cancelled" in out


def test_done_force_overrides_on_cancelled(_inject_fake):
    # `task done --force` is the explicit override: it re-closes a cancelled ticket to done.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    _force_state(_inject_fake, "#1", "cancelled")
    rc = main(["done", "#1", "--force"])
    assert rc == 0
    assert _inject_fake.get("#1").state.value == "done"


def test_change_done_force_overrides_on_done(_inject_fake):
    # `task change --done --force` re-closes an already-done ticket (the no-op block is bypassed).
    main(_create_argv())
    _ready_to_close(_inject_fake)
    _force_state(_inject_fake, "#1", "done")
    rc = main(["change", "#1", "--done", "--force"])
    assert rc == 0
    assert _inject_fake.get("#1").state.value == "done"


def test_illegal_change_done_with_screenshot_fires_no_attachment(capsys, _inject_fake):
    # the core of #10: an illegal `change --done --screenshot --title --label` on a cancelled
    # ticket must refuse BEFORE any side effect OR edit — no attachment uploaded, no re-write, and
    # the fetched ticket object left UNDIRTIED (validation precedes both update() and the edits).
    main(_create_argv())
    _force_state(_inject_fake, "#1", "cancelled")
    _inject_fake.attachments.clear()
    stored = _inject_fake.get("#1")
    shots_before = list(stored.screenshots)
    labels_before = list(stored.labels)
    title_before = stored.title
    capsys.readouterr()
    rc = main(["change", "#1", "--done", "--screenshot", "after.png", "--title", "HIJACK", "--label", "x"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "illegal transition" in out
    assert _inject_fake.attachments == []
    # the refusal must not have dirtied the live ticket object with the would-be edits.
    fetched = _inject_fake.get("#1")
    assert fetched.state.value == "cancelled"
    assert fetched.screenshots == shots_before
    assert fetched.labels == labels_before
    assert fetched.title == title_before


# ── rule 1: related entities must be links (create + edit) ───────────────────────────


def test_create_blocks_unlinked_reference(capsys, _inject_fake):
    rc = main(_create_argv() + ["--what", "blocked by HYP-789 until it lands"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "links" in out and "HYP-789" in out


def test_create_force_overrides_unlinked_reference(capsys, _inject_fake):
    rc = main(_create_argv() + ["--what", "the SKU HYP-789 ships", "--force", "HYP-789 is a product SKU, not a ticket"])
    assert rc == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "## Skipped gates" in body and "product SKU" in body


def test_change_edit_blocks_unlinked_reference(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["change", "#1", "--what", "now also see tasklib/cli.py"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "links" in out


def test_change_edit_links_skippable(_inject_fake):
    main(_create_argv())
    rc = main(["change", "#1", "--what", "see HYP-9", "--skip-links", "HYP-9 is a SKU"])
    assert rc == 0
    # the waiver is an auditable decision and persists in the body even on a non-closing edit
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "## Skipped gates" in body and "HYP-9 is a SKU" in body


def test_create_force_overrides_links_and_impact_together(_inject_fake):
    rc = main(_create_argv() + ["--what", "see HYP-1", "--impact", "n/a", "--force", "spike: SKU + internal tool"])
    assert rc == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "links" in body and "user-impact-quality" in body and "spike: SKU + internal tool" in body


def test_force_records_only_the_gate_that_failed(_inject_fake):
    # --force only waives the gate that actually fired: a links-only force must NOT silently
    # record a user-impact-quality skip when the impact is perfectly fine.
    rc = main(_create_argv() + ["--what", "see HYP-1", "--force", "HYP-1 is a SKU"])
    assert rc == 0
    skips = _inject_fake.get("#1").skips
    assert "links" in skips and "user-impact-quality" not in skips


def test_unlinked_reference_in_criterion_blocks_create(capsys, _inject_fake):
    # the links scan covers acceptance-criterion text too, not only --what
    rc = main(_create_argv() + ["--acceptance", "fixes #123 on mobile"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "links" in out and "#123" in out


def test_change_impact_edit_enforces_quality(capsys, _inject_fake):
    # editing the impact to something thin (non-closing edit) re-runs the quality gate
    main(_create_argv())
    capsys.readouterr()
    rc = main(["change", "#1", "--impact", "users"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "user-impact-quality" in out


def test_change_done_cannot_smuggle_an_unlinked_reference(capsys, _inject_fake):
    # a close-ready ticket: `change --what "…HYP-789…" --done` is an EDIT plus a close, so the
    # links edit-gate must fire on the touched text BEFORE the close — not be bypassed by --done.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["change", "#1", "--what", "now blocked by HYP-789", "--done"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "links" in out
    assert _inject_fake.get("#1").state.value != "done"  # the close did NOT go through


def test_change_done_cannot_smuggle_a_thinned_impact(capsys, _inject_fake):
    # same hole on rule 5: closing while thinning the impact must still trip user-impact-quality.
    main(_create_argv())
    _ready_to_close(_inject_fake)
    capsys.readouterr()
    rc = main(["change", "#1", "--impact", "users", "--done"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "user-impact-quality" in out
    assert _inject_fake.get("#1").state.value != "done"


def test_metadata_only_edit_does_not_rescan_links(_inject_fake):
    # a ticket whose body already carries a bare ref (e.g. created in the web UI) — a pure
    # metadata edit (--due) must NOT be re-blocked by the links gate (regression guard).
    from tasklib.model import Ticket

    _inject_fake.create(
        Ticket(
            title="web-created",
            what="blocked by HYP-1",
            why="x",
            user_impact="Users can read the page without the menu covering the text, so they stay",
            cost_of_inaction="y",
            acceptance=["a", "b"],
        )
    )
    rc = main(["change", "#1", "--due", "2026-07-01"])
    assert rc == 0


def test_create_force_overrides_thin_impact(_inject_fake):
    rc = main(_create_argv() + ["--impact", "n/a", "--force", "internal tool, no end user"])
    assert rc == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "user-impact-quality" in body and "internal tool" in body


# ── tg#<id> message references (task 6109): title guard + local-history quote expansion ──


def _write_tg_history(isolated_tmp_path, records: list[dict]) -> None:
    """Write a fake tg-cli history JSONL into the isolated ``$XDG_CONFIG_HOME`` (see the
    ``isolated_state`` fixture) so :func:`tasklib.msgrefs.load_history` finds it via glob."""
    import json

    config_dir = isolated_tmp_path / "config" / "tg-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "tg-ctl.999.history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_create_title_with_msgref_refuses(capsys, _inject_fake):
    argv = [
        "create", "--title", "see tg#5900", "--what", "c", "--why", "b",
        "--impact", _GOOD_IMPACT, "--if-not-done", "p", "--acceptance", "works", "--acceptance", "also empty",
    ]
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert "msgref-title" in out and "tg#5900" in out


def test_create_body_msgref_does_not_trip_the_title_gate(capsys, _inject_fake):
    rc = main(_create_argv() + ["--what", "per tg#5900 do X"])
    assert rc == 0


def test_create_expands_msgref_quote_from_local_history(_inject_fake, isolated_state):
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "fix the props bug", "pane": "%0"}],
    )
    rc = main(_create_argv() + ["--what", "per tg#42 do X"])
    assert rc == 0
    stored = _inject_fake.get("#1")
    assert "per tg#42 do X" in stored.what
    assert "fix the props bug" in stored.what
    assert "Alex" in stored.what


def test_create_msgref_not_found_in_history_still_succeeds(_inject_fake, isolated_state):
    # no history file written at all — the reference is unresolvable, but creation must still
    # succeed (never block a ticket on tg-cli being absent/empty), with a plain "not found" note.
    rc = main(_create_argv() + ["--what", "per tg#999999 do X"])
    assert rc == 0
    stored = _inject_fake.get("#1")
    assert "not found" in stored.what


def test_quoted_message_carrying_a_bare_reference_does_not_trip_the_links_gate(_inject_fake, isolated_state):
    # the quoted message's text is arbitrary — it happens to mention "HYP-999" here. Without the
    # blockquote-line exclusion in policy._scanned_text, this would wrongly refuse creation on
    # text the author never wrote (task-cli#45 review finding). It must succeed.
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "blocked by HYP-999", "pane": None}],
    )
    rc = main(_create_argv() + ["--what", "per tg#42 do X"])
    assert rc == 0


def test_stored_quote_does_not_trip_links_gate_on_a_later_UNRELATED_edit(capsys, _inject_fake, isolated_state):
    # task-cli#45 review finding (the main one): an earlier version protected only the command
    # that DID the expanding — the quote, once stored, was re-scanned (and could refuse) on any
    # LATER command. Create with a quote carrying a bare reference, then edit a DIFFERENT field
    # (--why) — the stored `what` (with its embedded "HYP-999") must not block this edit.
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "blocked by HYP-999", "pane": None}],
    )
    main(_create_argv() + ["--what", "per tg#42 do X"])
    capsys.readouterr()
    rc = main(["change", "#1", "--why", "updated motivation"])
    out = capsys.readouterr().out
    assert rc == 0, out


def test_stored_quote_does_not_block_close(_inject_fake, isolated_state):
    # same finding, on the close path: `change --done` re-runs the FULL create-shaped gate set
    # (links + impact-quality) against the stored ticket — a bare reference or thin-looking text
    # buried in an already-stored quote must not block closing a ticket that is otherwise ready.
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "blocked by HYP-999", "pane": None}],
    )
    main(_create_argv() + ["--what", "per tg#42 do X"])
    _ready_to_close(_inject_fake, "#1")
    rc = main(["change", "#1", "--done"])
    assert rc == 0


def test_acceptance_criterion_msgref_stays_bare_not_expanded(_inject_fake, isolated_state):
    # acceptance criteria are excluded from expansion (single-line checkbox format can't carry a
    # multi-line quote — see the msgrefs.py module docstring). The mention survives verbatim.
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "quoted text", "pane": None}],
    )
    rc = main(_create_argv() + ["--acceptance", "verify per tg#42"])
    assert rc == 0
    stored = _inject_fake.get("#1")
    texts = [c.text for c in stored.acceptance]
    assert "verify per tg#42" in texts
    assert not any("quoted text" in t for t in texts)


def test_change_expands_msgref_and_repeat_change_is_not_duplicated(_inject_fake, isolated_state):
    main(_create_argv())
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "fix the props bug", "pane": None}],
    )
    rc = main(["change", "#1", "--what", "per tg#42 do X"])
    assert rc == 0
    what_after_first = _inject_fake.get("#1").what
    assert what_after_first.count("tg#42") <= 2  # the mention itself + the quote header, never more
    # re-applying the SAME edit must not pile up a second quote block underneath the first.
    rc = main(["change", "#1", "--what", "per tg#42 do X"])
    assert rc == 0
    what_after_second = _inject_fake.get("#1").what
    assert what_after_second == what_after_first


def test_change_resubmitting_the_full_already_expanded_value_is_not_duplicated(_inject_fake, isolated_state):
    # task-cli#45 review finding: a read-modify-write flow (read the CURRENT what, tweak it,
    # write the whole thing back) resubmits a value that already contains the quote block --
    # not just the bare mention like the test above. That must not pile up a second quote either.
    main(_create_argv())
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "fix the props bug", "pane": None}],
    )
    main(["change", "#1", "--what", "per tg#42 do X"])
    current_what = _inject_fake.get("#1").what
    rc = main(["change", "#1", "--what", current_what])
    assert rc == 0
    assert _inject_fake.get("#1").what == current_what


def test_change_title_with_msgref_refuses(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["change", "#1", "--title", "see tg#5900"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "msgref-title" in out


def test_change_not_touching_title_does_not_trigger_the_title_gate(_inject_fake):
    # a pre-existing tg#<id> in the title (created before this gate existed, or edited in the
    # web UI) must not block an UNRELATED edit that leaves the title alone.
    main(_create_argv())
    _inject_fake.get("#1").title = "see tg#5900"  # simulate a legacy/web-UI-edited title
    rc = main(["change", "#1", "--why", "updated motivation"])
    assert rc == 0


# ── rule 4 + rule 5 at create ────────────────────────────────────────────────────────


def test_create_blocks_single_criterion(capsys, _inject_fake):
    argv = [
        "create", "--title", "t", "--what", "c", "--why", "b", "--impact", _GOOD_IMPACT,
        "--if-not-done", "p", "--acceptance", "only one",
    ]
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert "at least 2" in out


def test_create_blocks_thin_impact(capsys, _inject_fake):
    rc = main(_create_argv() + ["--impact", "users"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "user-impact-quality" in out


# ── rule 3: checking a criterion needs a visual proof ────────────────────────────────


def test_check_requires_a_visual_proof(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["check", "#1", "1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "visual proof" in out
    assert not _inject_fake.get("#1").acceptance[0].checked


def test_check_with_proof_marks_and_attaches(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["check", "#1", "1", "--proof", "shot.png"])
    assert rc == 0
    crit = _inject_fake.get("#1").acceptance[0]
    assert crit.checked and crit.proof == "shot.png"
    assert ("#1", "shot.png") in _inject_fake.attachments


def test_check_force_records_reason_without_proof(_inject_fake):
    main(_create_argv())
    rc = main(["check", "#1", "1", "--force", "this is a backend invariant, no UI to shoot"])
    assert rc == 0
    crit = _inject_fake.get("#1").acceptance[0]
    assert crit.checked and crit.proof == "" and "backend invariant" in crit.force_reason


def test_check_screenshot_is_an_alias_of_proof(_inject_fake):
    # --screenshot writes into the same dest as --proof, so it satisfies the proof requirement
    main(_create_argv())
    rc = main(["check", "#1", "1", "--screenshot", "shot.png"])
    assert rc == 0
    crit = _inject_fake.get("#1").acceptance[0]
    assert crit.checked and crit.proof == "shot.png"
    assert ("#1", "shot.png") in _inject_fake.attachments


def test_check_by_text_selector(_inject_fake):
    main(_create_argv())
    rc = main(["check", "#1", "empty", "--proof", "shot.png"])
    assert rc == 0
    # the substring 'empty' matches the second criterion ("it also handles the empty case")
    assert _inject_fake.get("#1").acceptance[1].checked


def test_check_ambiguous_text_selector_is_clean_error(capsys, _inject_fake):
    # both default criteria ("it works" / "it also handles the empty case") contain "it" —
    # an ambiguous substring must refuse and ask for an index, not silently pick one.
    main(_create_argv())
    capsys.readouterr()
    rc = main(["check", "#1", "it", "--proof", "shot.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "match" in out and "disambiguate" in out
    # neither criterion was checked by the ambiguous attempt
    assert not any(c.checked for c in _inject_fake.get("#1").acceptance)


def test_check_bad_index_is_clean_error(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["check", "#1", "9", "--proof", "shot.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "out of range" in out


def test_check_on_ticket_without_criteria_is_clean_error(capsys, _inject_fake):
    # a ticket created with the acceptance gate skipped has no criteria → a clean error, no crash
    main(
        [
            "create", "--title", "spike", "--what", "c", "--why", "b", "--impact", _GOOD_IMPACT,
            "--if-not-done", "p", "--skip-acceptance", "spike, criteria pending",
        ]
    )
    capsys.readouterr()
    rc = main(["check", "#1", "1", "--proof", "shot.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no acceptance criteria" in out


# ── rule 2: a ticket cannot close while a criterion is unchecked ──────────────────────


def test_done_blocked_until_all_criteria_checked(capsys, _inject_fake):
    main(_create_argv())
    main(["check", "#1", "1", "--proof", "shot.png"])  # only the first
    capsys.readouterr()
    rc = main(["done", "#1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "unchecked" in out
    assert _inject_fake.get("#1").state.value != "done"
    # check the remaining one → close now succeeds
    main(["check", "#1", "2", "--proof", "shot.png"])
    assert main(["done", "#1"]) == 0
    assert _inject_fake.get("#1").state.value == "done"


# ── classify ─────────────────────────────────────────────────────────────────────────


def test_classify_change_creates_ticket(capsys, monkeypatch, _inject_fake):
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "please add a logout button", "--create"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "change" in out
    assert "created" in out


def test_classify_create_puts_session_in_labels_not_links(monkeypatch, _inject_fake):
    # Regression (issue #54): the classify --create triage path must also keep the session id
    # in `labels` only, never duplicated into `links` (mirrors the `create` path).
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "please add a logout button", "--create"])
    assert rc == 0
    created = _inject_fake.list()[0]
    assert created.links == {}
    assert "session:testsess" in created.labels


def test_classify_create_omits_priority_attention_for_hook_output(capsys, monkeypatch, _inject_fake):
    _inject_fake.create(Ticket(title="Old active work", labels=["session:testsess"], state=cli.State.IN_PROGRESS))
    _write_session_touch("testsess", "#1", "Old active work", 1)
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )

    rc = main(["classify", "please add a logout button", "--create"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "created #2" in out
    assert "active tasks in this session" not in out


def test_classify_update_success_not_masked_by_refetch_failure(capsys, monkeypatch, _inject_fake):
    from tasklib.backends import BackendError

    main(_create_argv())
    capsys.readouterr()
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )

    def _comment(ticket_id: str, body: str) -> None:
        _inject_fake.comments.append((ticket_id, body))

    monkeypatch.setattr(_inject_fake, "comment", _comment)
    monkeypatch.setattr(_inject_fake, "get", lambda _ticket_id: (_ for _ in ()).throw(BackendError("temporary get failed")))

    rc = main(["classify", "append this context", "--update", "#1"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "appended to #1" in out
    assert _inject_fake.comments == [("#1", "(restated) append this context")]


def test_classify_create_dedup_matches_against_wrap_stripped_title(capsys, monkeypatch, _inject_fake):
    # task-cli#45 review finding: the stored ticket's title is ALWAYS wrap-stripped (every
    # ticket this path creates goes through _derive_title_from_message). A repeat inbound
    # message wrapped the SAME way must dedup-comment on that existing ticket, not create a
    # second one just because the raw wrapped first line scores low similarity against the
    # already-clean stored title.
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    main(["classify", "[TG from Alex tg#100] please add a dark mode toggle to settings", "--create"])
    assert len(_inject_fake.list()) == 1
    _write_session_touch("testsess", "#1", "please add a dark mode toggle to settings", 1)
    capsys.readouterr()
    rc = main(["classify", "[TG from Alex tg#200] please add a dark mode toggle to settings", "--create"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dedup" in out
    assert len(_inject_fake.list()) == 1  # still one ticket — the repeat was a comment, not a create
    from tasklib.session import read_entries

    assert read_entries("testsess")[-1].ts > 1


def test_classify_create_makes_policy_clean_draft(capsys, monkeypatch, _inject_fake):
    # the inbound-create path runs the gates and records every failing one as an auditable
    # auto-skip, so the draft is policy-clean by construction (no silent bypass).
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "VERDICT: change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "make the sidebar collapsible", "--create"])
    assert rc == 0
    created = _inject_fake.list()[0]
    from tasklib.policy import EnforceConfig, check_create

    # the stored draft passes the create gates (failing ones were waived with a recorded skip)
    assert check_create(created, EnforceConfig()).ok


def test_classify_create_expands_msgref_quote_in_what(capsys, monkeypatch, _inject_fake, isolated_state):
    # task-cli#45 review finding: classify --create IS a tg-cli hook entry point (an agent runs
    # `task classify "<inbound msg>" --create`) and must expand tg#<id> in `what` the same way
    # cmd_create's --from-message path does -- it must not be the one path that silently drops
    # the quote a reader would otherwise get everywhere else.
    _write_tg_history(
        isolated_state,
        [{"ts": 1700000000, "message_id": 42, "direction": "user", "from": "Alex", "text": "fix the props bug", "pane": None}],
    )
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    # the tg#<id> reference is on a SECOND line so the derived title (first line only) stays
    # clean and the msgref-title gate doesn't fire -- this test is about `what` expansion.
    rc = main(["classify", "add the login page redesign\nper tg#42 do X", "--create"])
    assert rc == 0
    created = _inject_fake.list()[0]
    assert "fix the props bug" in created.what


def test_classify_create_strips_tg_inbound_wrap_from_title(capsys, monkeypatch, _inject_fake):
    # task-cli#45 review finding: `classify --create` is ALSO a tg-cli hook entry point (an
    # inbound message routed straight through classify) and must derive its title the SAME way
    # cmd_create's --from-message path does -- otherwise this path silently creates a ticket
    # whose title carries the wrap's own tg#<id>, exactly what msgref-title exists to prevent.
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "[TG from Alex tg#4242] please add a logout button", "--create"])
    assert rc == 0
    created = _inject_fake.list()[0]
    assert "tg#4242" not in created.title
    assert created.title.startswith("please add a logout button")


def test_classify_create_refuses_rather_than_silently_bypass_a_non_skippable_gate(capsys, monkeypatch, _inject_fake):
    # the user's OWN message (not the wrap) starts with a tg#<id> mention that survives wrap-
    # stripping -- the title is still non-compliant. _auto_skip_failing_gates must NOT silently
    # auto-skip msgref-title (it is non-skippable); _classify_create must refuse instead of
    # persisting a ticket that violates a rule with no legitimate exception.
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "tg#99 is broken, please fix it", "--create"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "msgref-title" in out
    assert _inject_fake.list() == []


def test_classify_create_refuses_a_wrap_only_message_with_no_derivable_title(capsys, monkeypatch, _inject_fake):
    # task-cli#45 review finding: a wrap-only inbound message (no content after the wrap --
    # a malformed/truncated hook call) derives an EMPTY title. cmd_create's --from-message path
    # already refuses this (`if not title: raise`); classify --create must refuse the same way
    # rather than silently create a titleless ticket.
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "[TG from Alex tg#1234]", "--create"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no title" in out
    assert _inject_fake.list() == []


def test_classify_just_ask_creates_nothing(capsys, monkeypatch, _inject_fake):
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "justAsk")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None, **_kw: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "what does this function do?", "--create"])
    assert rc == 0
    assert "justAsk" in capsys.readouterr().out
    assert _inject_fake.list() == []


def test_classify_no_provider_biases(capsys, monkeypatch, _inject_fake):
    monkeypatch.setattr("tasklib.classify.resolve_chain", lambda fallbacks=None, env=None, **_kw: None)
    rc = main(["classify", "ambiguous message"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no classifier provider available" in out
    assert "change" in out


def test_classify_passes_configured_capability_to_resolve_chain(monkeypatch, _inject_fake):
    # cmd_classify must forward cfg.classify_capability into resolve_chain (rig#8 wiring) — a
    # capability configured in task.yaml has to actually reach the resolver, not be dropped.
    seen = {}

    def _spy(fallbacks=None, env=None, *, capability=None):
        seen["capability"] = capability
        return None  # bias-decide; we only care that capability arrived

    monkeypatch.setattr("tasklib.classify.resolve_chain", _spy)
    monkeypatch.setattr(
        "tasklib.config.LoadedConfig.classify_capability", property(lambda self: "reasoning")
    )
    rc = main(["classify", "anything"])
    assert rc == 0
    assert seen["capability"] == "reasoning"


def test_classify_empty_capability_passes_none(monkeypatch, _inject_fake):
    # the `cfg.classify_capability or None` wiring: an empty config capability must reach
    # resolve_chain as None (no manifest lookup), not as "".
    seen = {}

    def _spy(fallbacks=None, env=None, *, capability=None):
        seen["capability"] = capability
        return None

    monkeypatch.setattr("tasklib.classify.resolve_chain", _spy)
    monkeypatch.setattr(
        "tasklib.config.LoadedConfig.classify_capability", property(lambda self: "")
    )
    rc = main(["classify", "anything"])
    assert rc == 0
    assert seen["capability"] is None


# ── session ──────────────────────────────────────────────────────────────────────────


def test_session_show(capsys, _inject_fake):
    rc = main(["session"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "session: testsess" in out


def test_session_bind_records(capsys, _inject_fake):
    from tasklib.session import read_ids

    rc = main(["session", "bind", "#5"])
    assert rc == 0
    assert "#5" in read_ids("testsess")


# ── --due field + daemon dispatch ────────────────────────────────────────────────────


def test_create_with_due_roundtrips(capsys, _inject_fake):
    rc = main(_create_argv() + ["--due", "2026-07-01"])
    assert rc == 0
    created = _inject_fake.list()[0]
    # the fake backend round-trips through the body, exactly like a real one → due survives
    assert created.due == "2026-07-01"


def test_create_rejects_malformed_due(capsys):
    rc = main(_create_argv() + ["--due", "not-a-date"])
    assert rc == 2
    assert "ISO date" in capsys.readouterr().out


def test_change_sets_and_clears_due(capsys, _inject_fake):
    main(_create_argv() + ["--due", "2026-07-01"])
    # change to a new due date
    rc = main(["change", "#1", "--due", "2026-08-15"])
    assert rc == 0
    assert _inject_fake.get("#1").due == "2026-08-15"
    # clear it with an empty string
    rc = main(["change", "#1", "--due", ""])
    assert rc == 0
    assert _inject_fake.get("#1").due == ""


def test_create_without_due_has_empty_due(_inject_fake):
    main(_create_argv())
    assert _inject_fake.list()[0].due == ""


def test_change_rejects_malformed_due(capsys, _inject_fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["change", "#1", "--due", "31-12-2026"])  # not ISO
    assert rc == 2
    assert "ISO date" in capsys.readouterr().out


def test_daemon_run_honors_disabled_via_cli(capsys, _inject_fake, tmp_path, monkeypatch):
    # `task daemon run` with a disabled config returns 0 immediately, writing no pid-file
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from tasklib import daemon as _d

    monkeypatch.setattr(
        _d.DaemonConfig, "from_config", classmethod(lambda cls, cfg: _d.DaemonConfig(enabled=False))
    )
    rc = main(["daemon", "run"])
    assert rc == 0


def test_daemon_stop_when_nothing_running(capsys, _inject_fake, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = main(["daemon", "stop"])
    assert rc == 0
    assert "no running daemon" in capsys.readouterr().out


def test_daemon_stop_reports_not_ours_pid(capsys, _inject_fake, tmp_path, monkeypatch):
    # the new "not-ours" outcome (a live but recycled pid) must print a distinct warning, not be
    # silently reported as a clean stop — and still return 0
    from tasklib import daemon as _d

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(_d, "stop", lambda *a, **k: ("not-ours", 4242))
    rc = main(["daemon", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "4242" in out
    assert "not the task daemon" in out


def test_read_json_includes_due(capsys, _inject_fake):
    import json

    main(_create_argv() + ["--due", "2026-07-01"])
    capsys.readouterr()  # drain the create output so only the read's JSON remains
    rc = main(["read", "#1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["due"] == "2026-07-01"


def test_daemon_status_reports_stopped(capsys, _inject_fake, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = main(["daemon", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "daemon:" in out
    assert "stopped" in out


def test_daemon_status_json(capsys, _inject_fake, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stopped"
    assert payload["interval_s"] == 3600
    assert payload["notifier"] == ["tg", "--tag", "report"]


def test_daemon_status_reports_not_ours(capsys, _inject_fake, tmp_path, monkeypatch):
    # #32: a recycled foreign pid must surface in `status` as a recycled/foreign warning (not
    # "running") — the consistency fix with stop's identity guard.
    from tasklib import daemon as _d

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(_d, "pid_status", lambda _p: ("not-ours", 4242))
    rc = main(["daemon", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "4242" in out
    assert "recycled" in out and "running (pid" not in out


def test_daemon_status_json_not_ours(capsys, _inject_fake, tmp_path, monkeypatch):
    import json

    from tasklib import daemon as _d

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(_d, "pid_status", lambda _p: ("not-ours", 4242))
    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not-ours"
    assert payload["pid"] == 4242


def test_daemon_start_is_idempotent_no_double_spawn(capsys, _inject_fake, tmp_path, monkeypatch):
    import os
    from argparse import Namespace

    from tasklib import daemon as _d
    from tasklib.cli import _daemon_coordinate, _load

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    spawned: list = []
    monkeypatch.setattr(_d, "_spawn_detached", lambda *a, **k: spawned.append(1) or 4242)
    rc = main(["daemon", "start"])
    assert rc == 0
    assert "daemon started" in capsys.readouterr().out
    assert spawned == [1]

    # a second start while OUR daemon is alive must not spawn again. Stamp the pid-file with THIS
    # live process AND make its cmdline read as the daemon (pid_status is identity-aware now — #32 —
    # so a bare-liveness "running" requires a daemon-shaped argv), then assert the second start no-ops.
    cfg = _load(Namespace(cwd=".", backend=None, repo=None, config=None))
    paths = _d.paths_for(_daemon_coordinate(cfg))
    _d._write_pid(paths.pid, os.getpid())
    monkeypatch.setattr(_d, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])
    spawned.clear()
    rc = main(["daemon", "start"])
    assert rc == 0
    assert "already running" in capsys.readouterr().out
    assert spawned == []


def test_daemon_disabled_does_not_start(capsys, _inject_fake, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from tasklib import daemon as _d

    monkeypatch.setattr(_d.DaemonConfig, "from_config", classmethod(lambda cls, cfg: _d.DaemonConfig(enabled=False)))
    spawned: list = []
    monkeypatch.setattr(_d, "_spawn_detached", lambda *a, **k: spawned.append(1))
    rc = main(["daemon", "start"])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out
    assert spawned == []


# ── mutation notifications (TG hook) ────────────────────────────────────────────────


def _patch_notify(monkeypatch):
    """Capture daemon.notify calls; return the list of (message, notifier) tuples."""
    from tasklib import daemon as _d

    calls: list[tuple[str, tuple]] = []

    def _fake_notify(msg, notifier):
        calls.append((msg, notifier))
        return True

    monkeypatch.setattr(_d, "notify", _fake_notify)
    return calls


def test_create_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    rc = main(_create_argv())
    assert rc == 0
    assert len(calls) == 1
    msg, notifier = calls[0]
    assert '<a href="https://fake/1">#1</a>' in msg
    assert "created" in msg
    assert "Add a thing" in msg
    assert "tg" in notifier[0]
    assert "--format" in notifier and "html" in notifier


def test_done_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    _ready_to_close(_inject_fake)
    rc = main(["done", "#1"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "done" in msg and "#1" in msg
    assert '<a href="https://fake/1">#1</a>' in msg


def test_change_done_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    _ready_to_close(_inject_fake)
    rc = main(["change", "#1", "--done"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "done" in msg and "#1" in msg
    assert '<a href="https://fake/1">#1</a>' in msg


def test_change_update_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    rc = main(["change", "#1", "--title", "Updated title"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "changed" in msg and "#1" in msg
    assert '<a href="https://fake/1">#1</a>' in msg


def test_status_transition_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    rc = main(["status", "#1", "in-progress"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "changed" in msg and "#1" in msg
    assert '<a href="https://fake/1">#1</a>' in msg


def test_list_does_not_send_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    rc = main(["list"])
    assert rc == 0
    assert calls == [], "list must not trigger a notification"


def test_notification_disabled_by_config(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    # Inject notifications.on_mutation: false into the loaded config
    from tasklib import cli as _cli

    orig_load = _cli._load

    def _patched_load(args):
        cfg = orig_load(args)
        cfg.data.setdefault("notifications", {})["on_mutation"] = False
        return cfg

    monkeypatch.setattr(_cli, "_load", _patched_load)
    rc = main(_create_argv())
    assert rc == 0
    assert calls == [], "notification must be suppressed when on_mutation: false"


def test_notification_failure_does_not_fail_ticket_op(monkeypatch, _inject_fake, capsys):
    from tasklib import daemon as _d

    monkeypatch.setattr(_d, "notify", lambda msg, notifier: False)
    rc = main(_create_argv())
    assert rc == 0
    assert "created #1" in capsys.readouterr().out


def test_notification_html_escapes_title(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    rc = main(_create_argv() + ["--title", "Fix <preview> & links"])
    assert rc == 0
    msg, _ = calls[0]
    assert "Fix &lt;preview&gt; &amp; links" in msg
    assert "<preview>" not in msg


def test_notification_custom_notifier_stays_plain(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    from tasklib import daemon as _d

    monkeypatch.setattr(
        _d.DaemonConfig,
        "from_config",
        classmethod(lambda cls, cfg: _d.DaemonConfig(notifier=("mynotify", "--quiet"))),
    )
    rc = main(_create_argv())
    assert rc == 0
    msg, notifier = calls[0]
    assert notifier == ("mynotify", "--quiet")
    assert msg.startswith("[task created] #1: Add a thing [todo]")
    assert "\nhttps://fake/1" in msg


def test_notification_tg_format_plain_override_is_not_mutated(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    from tasklib import daemon as _d

    monkeypatch.setattr(
        _d.DaemonConfig,
        "from_config",
        classmethod(lambda cls, cfg: _d.DaemonConfig(notifier=("tg", "--format", "plain"))),
    )
    rc = main(_create_argv())
    assert rc == 0
    msg, notifier = calls[0]
    assert notifier == ("tg", "--format", "plain")
    assert '<a href="https://fake/1">#1</a>' not in msg
    assert msg.startswith("[task created] #1: Add a thing [todo]")


def test_notification_tg_format_equals_html_is_not_duplicated(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    from tasklib import daemon as _d

    monkeypatch.setattr(
        _d.DaemonConfig,
        "from_config",
        classmethod(lambda cls, cfg: _d.DaemonConfig(notifier=("tg", "--format=html"))),
    )
    rc = main(_create_argv())
    assert rc == 0
    msg, notifier = calls[0]
    assert notifier == ("tg", "--format", "html")
    assert '<a href="https://fake/1">#1</a>' in msg


def test_mutation_message_escapes_href_attributes():
    msg = _mutation_message(
        Ticket(id="HYP-903", url='https://linear.example/issue/HYP-903?a=1&b="two"', title="Fix"),
        "created",
        "todo",
        html_mode=True,
    )
    assert 'href="https://linear.example/issue/HYP-903?a=1&amp;b=&quot;two&quot;"' in msg


# ── Links-URL gate + tg#<id> quote warning routing (tg#9179 / tg#9161) ────────────────


def test_change_metadata_only_edit_is_exempt_from_links_url(capsys, _inject_fake):
    # a --due/--label edit touches no scanned field → the links-url gate does not fire, so a
    # legacy junk link (Session: session:2) can't block an unrelated metadata edit.
    main(_create_argv())
    _inject_fake.get("#1").links["Session"] = "session:2"  # legacy junk a real backend read back
    capsys.readouterr()
    assert main(["change", "#1", "--due", "2026-12-31"]) == 0


def test_change_touching_text_enforces_links_url(capsys, _inject_fake):
    # editing a scanned field re-validates the Links field: the junk value now blocks the edit,
    # and --skip-links-url waives it (proving the new skip flag is wired).
    main(_create_argv())
    _inject_fake.get("#1").links["Session"] = "session:2"
    capsys.readouterr()
    assert main(["change", "#1", "--what", "a real edit"]) == 2
    assert "links-url" in capsys.readouterr().out
    assert main(["change", "#1", "--what", "a real edit", "--skip-links-url", "legacy junk"]) == 0


def test_report_and_die_prints_warnings_to_stderr_only(capsys):
    # an unresolvable msgref-quote warning must NOT reach stdout: a non-fatal enforce returns and
    # the caller may then print a --json payload to stdout that a warning line would corrupt.
    from tasklib.policy import PolicyResult, Violation

    result = PolicyResult(warnings=[Violation("msgref-quote", "the body references tg#9 but no quote could be attached")])
    cli._report_and_die(result, "update")  # no violations → does not raise
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "msgref-quote" in cap.err


def test_skip_msgref_quote_flag_parses_and_records():
    ns = build_parser().parse_args([*_create_argv(), "--skip-msgref-quote", "false positive"])
    assert ns.skip_msgref_quote == "false positive"


def _fake_history(msg_id=42, text="the referenced message"):
    from tasklib.msgrefs import HistoryRecord

    return {msg_id: HistoryRecord(ts=1700000000, message_id=msg_id, direction="user", from_="Alex", text=text, pane=None)}


def test_msgref_quote_deny_at_change_when_resolvable(capsys, _inject_fake, monkeypatch):
    # end-to-end: a backend-authored ticket carries a bare tg#42 (no quote); the message IS in
    # local history, so touching a text field re-scans the body and the close/edit is refused.
    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X"  # a bare ref the create gate never expanded
    monkeypatch.setattr("tasklib.msgrefs.load_history", lambda env=None: _fake_history(42))
    capsys.readouterr()
    assert main(["change", "#1", "--title", "New title"]) == 2
    assert "msgref-quote" in capsys.readouterr().out
    # the new skip flag waives it through the full CLI path
    assert main(["change", "#1", "--title", "New title", "--skip-msgref-quote", "version tag"]) == 0


def test_msgref_quote_warn_at_change_preserves_json_stdout(capsys, _inject_fake, monkeypatch):
    # an UNRESOLVABLE reference only warns (to stderr) and never blocks — and the --json payload
    # on stdout stays parseable (the whole point of routing the warning off stdout).
    import json

    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X"
    monkeypatch.setattr("tasklib.msgrefs.load_history", lambda env=None: {})  # not in history → warn
    capsys.readouterr()
    assert main(["change", "#1", "--title", "New title", "--json"]) == 0
    cap = capsys.readouterr()
    json.loads(cap.out)  # stdout is clean JSON, not corrupted by the warning
    assert "msgref-quote" in cap.err


def test_msgref_quote_disabled_does_not_read_history(capsys, _inject_fake, monkeypatch):
    # enforce.msgref_quote: false must not touch the (potentially large, private) history log.
    from tasklib.policy import EnforceConfig

    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X"
    monkeypatch.setattr(cli, "_enforce_config", lambda cfg: EnforceConfig(msgref_quote=False))

    def _boom(env=None):
        raise AssertionError("history must not be read when the gate is disabled")

    monkeypatch.setattr("tasklib.msgrefs.load_history", _boom)
    capsys.readouterr()
    assert main(["change", "#1", "--title", "New title"]) == 0


def test_msgref_quote_no_refs_does_not_read_history(capsys, _inject_fake, monkeypatch):
    # a ticket with no tg#<id> reference must never touch the history log.
    main(_create_argv())

    def _boom(env=None):
        raise AssertionError("history must not be read for a reference-free ticket")

    monkeypatch.setattr("tasklib.msgrefs.load_history", _boom)
    capsys.readouterr()
    assert main(["change", "#1", "--title", "New title"]) == 0


def test_msgref_quote_already_skipped_does_not_read_history(capsys, _inject_fake, monkeypatch):
    # once --skip-msgref-quote is recorded, the gate can't fire, so history is not read.
    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X"

    def _boom(env=None):
        raise AssertionError("history must not be read when the gate is already waived")

    monkeypatch.setattr("tasklib.msgrefs.load_history", _boom)
    capsys.readouterr()
    assert main(["change", "#1", "--title", "New title", "--skip-msgref-quote", "version tag"]) == 0


def test_create_with_active_session_puts_no_link_in_the_links_field(_inject_fake):
    # #55 moved the session id to labels only; the links-url gate assumes Links is URL-only, so a
    # create must never plant a non-URL Session link (which would then fail the gate it just passed).
    main(_create_argv())
    t = _inject_fake.get("#1")
    assert t.links == {}
    assert any(lbl.startswith("session:") for lbl in t.labels)


def test_skip_summary_and_warning_keep_json_stdout_clean(capsys, _inject_fake, monkeypatch):
    # a --json command that BOTH waives a gate (skipped summary) AND carries an untouched
    # unresolvable reference (warning) must still emit parseable JSON on stdout — both diagnostics
    # go to stderr. The reference and the bare HYP ref sit in an UNtouched field so they genuinely
    # fire (a --title edit re-scans the whole body).
    import json

    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X, blocked by HYP-789"  # untouched: unresolvable ref + bare ref
    monkeypatch.setattr("tasklib.msgrefs.load_history", lambda env=None: {})  # tg#42 unresolvable → warn
    capsys.readouterr()
    rc = main(["change", "#1", "--title", "New title", "--skip-links", "legacy bare ref", "--json"])
    assert rc == 0
    cap = capsys.readouterr()
    json.loads(cap.out)  # stdout is clean JSON, not corrupted by either diagnostic
    assert "msgref-quote" in cap.err  # the warning went to stderr
    assert "skipped gates (justified)" in cap.err  # the skip summary went to stderr


def test_skip_msgref_quote_shows_in_the_audit_trail_via_cli(capsys, _inject_fake, monkeypatch):
    # a waived msgref-quote must appear in the "skipped gates (justified)" summary like every other
    # skippable gate — even though the CLI skips loading history for it.
    main(_create_argv())
    _inject_fake.get("#1").what = "per tg#42 do X"
    monkeypatch.setattr("tasklib.msgrefs.load_history", lambda env=None: (_ for _ in ()).throw(AssertionError("no read")))
    capsys.readouterr()
    rc = main(["change", "#1", "--title", "New title", "--skip-msgref-quote", "version tag"])
    assert rc == 0
    assert "skipped gates (justified)" in capsys.readouterr().err
    from tasklib.render import render

    assert "msgref-quote" in render(_inject_fake.get("#1"))  # recorded in the Skipped gates section


def test_skip_msgref_quote_at_create_suppresses_expansion_and_history(capsys, _inject_fake, monkeypatch):
    # a waived tg#<id> must NOT be expanded into an (unrelated, possibly private) quote, and the
    # history log must not be read at all — the skip governs expansion, not just the gate.
    def _boom(env=None):
        raise AssertionError("history must not be read when msgref-quote is skipped")

    monkeypatch.setattr("tasklib.msgrefs.load_history", _boom)
    argv = [
        "create", "--title", "Bump", "--what", "bump to tg#2", "--why", "because",
        "--impact", _GOOD_IMPACT, "--if-not-done", "pain",
        "--acceptance", "it works", "--acceptance", "empty case",
        "--skip-msgref-quote", "version tag, not a message ref",
    ]
    assert main(argv) == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "bump to tg#2" in body  # the raw text is preserved
    assert "msgref-quote:2" not in body  # no quote block was attached
    assert "msgref-quote" in body  # recorded in the Skipped gates section


def test_persisted_msgref_skip_does_not_disable_expansion_of_a_new_reference(capsys, _inject_fake, monkeypatch):
    # a ticket waived once (msgref-quote in its skips) must still expand a genuinely NEW reference
    # added by a later change — the waiver is not a permanent expansion kill-switch.
    main(_create_argv())
    _inject_fake.get("#1").skips["msgref-quote"] = "waived earlier for a version tag"
    monkeypatch.setattr("tasklib.msgrefs.load_history", lambda env=None: _fake_history(99, "the new referenced message"))
    capsys.readouterr()
    assert main(["change", "#1", "--what", "now genuinely per tg#99"]) == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "the new referenced message" in body  # tg#99 WAS expanded despite the old waiver


def test_disabling_msgref_quote_config_stops_expansion_and_history(capsys, _inject_fake, monkeypatch):
    # enforce.msgref_quote: false is the master switch: a tg#<id> in a touched field is neither
    # expanded nor resolved against history — the whole feature is off.
    from tasklib.policy import EnforceConfig

    monkeypatch.setattr(cli, "_enforce_config", lambda cfg: EnforceConfig(msgref_quote=False))

    def _boom(env=None):
        raise AssertionError("history must not be read when msgref_quote is disabled")

    monkeypatch.setattr("tasklib.msgrefs.load_history", _boom)
    argv = [
        "create", "--title", "Ref", "--what", "per tg#2 do the thing", "--why", "because",
        "--impact", _GOOD_IMPACT, "--if-not-done", "pain",
        "--acceptance", "it works", "--acceptance", "empty case",
    ]
    assert main(argv) == 0
    from tasklib.render import render

    body = render(_inject_fake.get("#1"))
    assert "per tg#2 do the thing" in body  # raw text, no quote attached
    assert "msgref-quote:2" not in body
