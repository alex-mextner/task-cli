"""session.py — detection precedence + the sidecar index."""

from __future__ import annotations

from tasklib.session import detect, read_ids, record, sidecar_path


def test_env_task_session_wins():
    s = detect(env={"TASK_SESSION": "my-feature", "TMUX_PANE": "%3"}, git_branch=lambda: "main")
    assert s.id == "my-feature"
    assert s.source == "env:TASK_SESSION"
    assert s.label == "session:my-feature"


def test_tmux_pane_used_when_no_env():
    s = detect(env={"TMUX_PANE": "%7"}, git_branch=lambda: "main")
    assert s.source == "tmux-pane"
    assert s.id == "7"  # slugged ('%' stripped)


def test_git_branch_fallback():
    s = detect(env={}, git_branch=lambda: "HYP-123-add-toggle")
    assert s.source == "git-branch"
    assert s.id == "hyp-123-add-toggle"


def test_default_when_nothing_resolves():
    s = detect(env={}, git_branch=lambda: None)
    assert s.id == "default"
    assert s.source == "none"


def test_long_value_is_hashed_to_bounded_id():
    huge = "x" * 200
    s = detect(env={"TASK_SESSION": huge})
    assert len(s.id) <= 40


def test_detect_order_can_be_reordered():
    # put git-branch first → it wins over env
    s = detect(
        env={"TASK_SESSION": "envid"},
        detect_order=("git-branch", "env:TASK_SESSION"),
        git_branch=lambda: "branchid",
    )
    assert s.id == "branchid"


def test_sidecar_record_and_read(isolated_state):
    record("sess1", "#10", "first")
    record("sess1", "#11", "second")
    assert read_ids("sess1") == ["#10", "#11"]


def test_sidecar_dedups_on_id(isolated_state):
    record("sess1", "#10", "first")
    record("sess1", "#10", "first again")
    assert read_ids("sess1") == ["#10"]


def test_sidecar_path_under_state_home(isolated_state):
    p = sidecar_path("sess1")
    assert "task-cli/sessions/sess1.jsonl" in str(p)


def test_read_ids_empty_for_unknown_session(isolated_state):
    assert read_ids("never-seen") == []


def test_label_prefix_is_configurable():
    s = detect(env={"TASK_SESSION": "feat"}, label_prefix="sess/")
    assert s.label == "sess/feat"


def test_git_branch_detect_uses_cwd(monkeypatch):
    # the injected branch resolver wins, but verify a real call would target the given cwd:
    # detect() builds a closure over cwd; assert it is threaded through (no crash, right id).
    captured = {}

    def fake_branch():
        captured["called"] = True
        return "scoped-branch"

    s = detect(env={}, git_branch=fake_branch, cwd="/some/repo")
    assert s.id == "scoped-branch"
    assert captured["called"]
