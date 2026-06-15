"""policy.py — the enforcement gates pass/fail + the escape hatch."""

from __future__ import annotations

from tasklib.model import Screenshot, Ticket
from tasklib.policy import (
    EnforceConfig,
    Phase,
    check,
    check_create,
    check_done,
    normalize_skip_gate,
)


def _good_ticket(**over) -> Ticket:
    base = dict(
        title="t",
        what="the change",
        why="because",
        user_impact="users",
        cost_of_inaction="bad things",
        acceptance=["it works"],
    )
    base.update(over)
    return Ticket(**base)


def test_complete_ticket_passes_create():
    assert check_create(_good_ticket(), EnforceConfig()).ok


def test_missing_acceptance_fails():
    res = check_create(_good_ticket(acceptance=[]), EnforceConfig())
    assert not res.ok
    assert any(v.gate == "acceptance-criteria" for v in res.violations)


def test_missing_motivation_fails():
    res = check_create(_good_ticket(why=""), EnforceConfig())
    assert any(v.gate == "motivation" for v in res.violations)


def test_missing_user_impact_and_cost_fail():
    res = check_create(_good_ticket(user_impact="", cost_of_inaction=""), EnforceConfig())
    gates = {v.gate for v in res.violations}
    assert "user-impact" in gates and "cost-of-inaction" in gates


def test_ui_ticket_requires_creation_screenshot():
    t = _good_ticket(labels=["ui"])  # no screenshot
    res = check_create(t, EnforceConfig())
    assert any(v.gate == "screenshots" for v in res.violations)


def test_ui_ticket_with_screenshot_passes_create():
    t = _good_ticket(labels=["ui"], screenshots=[Screenshot(ref="a.png", kind="creation")])
    assert check_create(t, EnforceConfig()).ok


def test_non_ui_ticket_skips_screenshot_gate():
    t = _good_ticket(labels=["backend"])  # not ui/visual
    assert check_create(t, EnforceConfig()).ok


def test_done_phase_demands_implementation_screenshot():
    # a creation shot alone does NOT satisfy the on-done gate — that is why it runs again.
    creation_only = _good_ticket(labels=["visual"], screenshots=[Screenshot(ref="a.png", kind="creation")])
    assert any(v.gate == "screenshots" for v in check_done(creation_only, EnforceConfig()).violations)
    # no screenshots at all is also refused
    bare = _good_ticket(labels=["visual"])
    assert any(v.gate == "screenshots" for v in check_done(bare, EnforceConfig()).violations)
    # an implementation shot satisfies the done gate
    impl = _good_ticket(labels=["visual"], screenshots=[Screenshot(ref="b.png", kind="implementation")])
    assert check_done(impl, EnforceConfig()).ok


def test_escape_hatch_bypasses_a_gate():
    t = _good_ticket(acceptance=[], skips={"acceptance-criteria": "spike, no criteria yet"})
    res = check_create(t, EnforceConfig())
    assert res.ok
    assert "acceptance-criteria" in res.skipped


def test_disabled_gate_in_config_is_not_checked():
    cfg = EnforceConfig(acceptance_criteria=False)
    assert check_create(_good_ticket(acceptance=[]), cfg).ok


def test_formatting_gate_catches_broken_body():
    # an otherwise-complete ticket always renders a valid body, so the format gate passes
    assert not any(v.gate == "formatting" for v in check_create(_good_ticket(), EnforceConfig()).violations)


def test_enforce_config_from_spec_dict():
    cfg = EnforceConfig.from_dict(
        {
            "acceptance_criteria": "required",
            "motivation": "optional",
            "formatting": "strict",
            "screenshots": {
                "on_create": {"required_if_label": ["ui", "visual"]},
                "on_done": {"required_if_label": ["ui"]},
            },
        }
    )
    assert cfg.acceptance_criteria is True
    assert cfg.motivation is False
    assert cfg.formatting is True
    assert "ui" in cfg.screenshot_labels and "visual" in cfg.screenshot_labels


def test_normalize_skip_gate_aliases():
    assert normalize_skip_gate("acceptance") == "acceptance-criteria"
    assert normalize_skip_gate("why") == "motivation"
    assert normalize_skip_gate("impact") == "user-impact"
    assert normalize_skip_gate("if-not-done") == "cost-of-inaction"


def test_normalize_skip_gate_rejects_unknown():
    import pytest

    with pytest.raises(ValueError):
        normalize_skip_gate("nonsense")


def test_check_dispatch_create_vs_done():
    t = _good_ticket()
    assert check(t, EnforceConfig(), Phase.CREATE).ok
    assert check(t, EnforceConfig(), Phase.DONE).ok
