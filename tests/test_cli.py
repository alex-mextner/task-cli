"""CLI dispatch — argparse front-end + end-to-end flows against the FakeBackend.

The backend is injected by monkeypatching ``tasklib.backends.get_backend``; the classify
shell-out is monkeypatched too. No network, no gh/linear/review subprocess.
"""

from __future__ import annotations

import pytest

from tasklib import cli
from tasklib.cli import build_parser, main


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
    assert "skipped gates (justified): motivation" in capsys.readouterr().out


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


def test_create_records_session_sidecar(_inject_fake):
    from tasklib.session import read_ids

    main(_create_argv())
    assert read_ids("testsess") == ["#1"]


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
    main(_create_argv() + ["--label", "urgent"])  # #2: session:testsess + urgent
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


# ── gantt (read-only due-date timeline) ──────────────────────────────────────────────


def test_gantt_renders_dated_and_undated(capsys, _inject_fake):
    # #1: dated, in this session;  #2: undated, in this session
    main(_create_argv() + ["--due", "2026-07-01"])
    main(_create_argv())
    capsys.readouterr()
    rc = main(["gantt", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out
    assert "undated" in out  # the no-due ticket is shown in its own section, not hidden


def test_gantt_json_timeline_shape(capsys, _inject_fake):
    main(_create_argv() + ["--due", "2026-07-01"])
    main(_create_argv())  # undated
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
    assert "#1" in msg
    assert "created" in msg
    assert "Add a thing" in msg
    assert "tg" in notifier[0]


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


def test_change_update_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    rc = main(["change", "#1", "--title", "Updated title"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "changed" in msg and "#1" in msg


def test_status_transition_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    main(_create_argv())
    calls.clear()
    rc = main(["status", "#1", "in-progress"])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "changed" in msg and "#1" in msg


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
    from tasklib.config import LoadedConfig

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
