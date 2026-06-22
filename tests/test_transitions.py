"""Unit tests for the shared legal-transition table + validator (issue #10).

These exercise ``tasklib.transitions`` directly — the ONE table and ONE validator the three
close paths share — so the legality rules are pinned independently of the CLI wiring.
"""

from __future__ import annotations

import pytest

from tasklib.model import State
from tasklib.transitions import (
    EXIT_ILLEGAL_TRANSITION,
    LEGAL_TRANSITIONS,
    TransitionError,
    validate_transition,
)


def test_legal_to_done_from_active_states_pass():
    for src in (State.TODO, State.IN_PROGRESS, State.IN_REVIEW):
        validate_transition(src, State.DONE)  # no raise


def test_cancelled_to_done_is_illegal():
    with pytest.raises(TransitionError) as exc:
        validate_transition(State.CANCELLED, State.DONE)
    assert "cancelled" in str(exc.value) and "illegal transition" in str(exc.value)


def test_same_state_is_illegal_for_every_state():
    # re-entering the same state is a no-op re-write, rejected for ALL states (none lists itself).
    for st in State:
        assert st not in LEGAL_TRANSITIONS[st]
        with pytest.raises(TransitionError) as exc:
            validate_transition(st, st)
        assert "already" in str(exc.value)


def test_force_bypasses_any_illegal_move():
    # the --force escape hatch: even cancelled → done passes when forced.
    validate_transition(State.CANCELLED, State.DONE, force=True)  # no raise
    validate_transition(State.DONE, State.DONE, force=True)  # no raise


def test_transition_error_carries_structured_exit_code():
    err = TransitionError("x")
    assert err.exit_code == EXIT_ILLEGAL_TRANSITION
    # the structured exit is the agenttools_errors USAGE class (2) — non-zero, and the same
    # "invalid request" class the rest of the CLI uses (NOT a transition-only code).
    assert EXIT_ILLEGAL_TRANSITION == 2
    assert EXIT_ILLEGAL_TRANSITION != 0


def test_exit_code_matches_agenttools_errors_contract():
    # the literal 2 is hard-coded (so import-time stays stdlib-only); pin it to the shared
    # agenttools_errors EXIT_USAGE contract IF that library is installed, so a future renumber
    # there fails this test instead of drifting silently. When the lib is absent, the literal
    # contract value (2) is the source of truth.
    try:
        from agenttools_errors import EXIT_USAGE
    except Exception:  # noqa: BLE001 - shared lib optional in this env; literal 2 is the contract
        pytest.skip("agenttools_errors not installed; EXIT_ILLEGAL_TRANSITION pinned to literal 2")
    assert EXIT_ILLEGAL_TRANSITION == EXIT_USAGE


def test_table_covers_every_state():
    # the table is exhaustive — a new State without a row would be a silent gap.
    assert set(LEGAL_TRANSITIONS) == set(State)
    for dests in LEGAL_TRANSITIONS.values():
        assert all(isinstance(d, State) for d in dests)
