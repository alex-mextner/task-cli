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
        ["list"],
        ["list", "--all", "--state", "todo"],
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
        "users",
        "--if-not-done",
        "pain",
        "--acceptance",
        "it works",
    ]
    for k, v in over.items():
        argv += [k, v]
    return argv


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
        "create", "--title", "t", "--what", "c", "--impact", "u", "--if-not-done", "p",
        "--acceptance", "works", "--skip-motivation", "spike, no motivation needed",
    ]
    rc = main(argv)
    assert rc == 0
    assert "skipped gates (justified): motivation" in capsys.readouterr().out


def test_create_from_message_derives_title(capsys, _inject_fake):
    rc = main(
        [
            "create", "--from-message", "fix the broken header on mobile", "--why", "b",
            "--impact", "u", "--if-not-done", "p", "--acceptance", "works",
        ]
    )
    assert rc == 0
    created = _inject_fake.list()[0]
    assert created.title.startswith("fix the broken header")


def test_create_records_session_sidecar(_inject_fake):
    from tasklib.session import read_ids

    main(_create_argv())
    assert read_ids("testsess") == ["#1"]


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
    assert "also handles edge case" in fetched.acceptance


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
    capsys.readouterr()
    rc = main(["status", "#1", "done"])
    assert rc == 0
    assert "→ done" in capsys.readouterr().out


def test_status_done_persists_skip_justification(_inject_fake):
    # closing a UI ticket via `status done --skip-screenshots` must RECORD the waiver in the
    # body (not lose it to a re-fetching transition()). Regression for the Codex/Opus P1.
    main(_create_argv() + ["--label", "ui", "--skip-screenshots", "no proof at create"])
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
    capsys.readouterr()
    rc = main(["change", "#1", "--screenshot", "after.png", "--done"])
    assert rc == 0


def test_change_close_recorded_waiver_persists(capsys, _inject_fake):
    # a recorded screenshots waiver is an auditable decision and legitimately carries through.
    main(_create_argv() + ["--label", "ui", "--skip-screenshots", "config-only, not real UI"])
    capsys.readouterr()
    rc = main(["change", "#1", "--done"])
    assert rc == 0


# ── classify ─────────────────────────────────────────────────────────────────────────


def test_classify_change_creates_ticket(capsys, monkeypatch, _inject_fake):
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda fallbacks=None, env=None: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
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
        lambda fallbacks=None, env=None: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
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
        lambda fallbacks=None, env=None: __import__("tasklib.classify", fromlist=["ResolvedModel"]).ResolvedModel(
            "anthropic", "claude:claude-haiku-4-5"
        ),
    )
    rc = main(["classify", "what does this function do?", "--create"])
    assert rc == 0
    assert "justAsk" in capsys.readouterr().out
    assert _inject_fake.list() == []


def test_classify_no_provider_biases(capsys, monkeypatch, _inject_fake):
    monkeypatch.setattr("tasklib.classify.resolve_chain", lambda fallbacks=None, env=None: None)
    rc = main(["classify", "ambiguous message"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no classifier provider available" in out
    assert "change" in out


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
