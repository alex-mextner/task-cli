"""Legal state-transition validation — ONE source of truth for the close paths.

WHAT THIS FILE IS
    The single legal-transition table + the one validator the three close paths share.
    ``task done``, ``task change --done``, and ``task status <id> done`` all push a ticket to
    ``State.DONE``; before this module each set the state and ran the on-done gates WITHOUT
    checking whether the ticket's CURRENT state legally permits the move. So ``task done`` on
    an already-cancelled ticket silently re-closed it (re-ran update() + re-fired attachments),
    and a double-close re-wrote a ticket already in ``done`` — the audit trail then lied about
    the lifecycle (agent-tools task-cli #10).

HOW IT'S REACHED AT RUNTIME
    Each close handler in ``cli.py`` calls :func:`validate_transition(current, target, force=…)`
    BEFORE mutating the ticket / running the gates. An illegal move raises
    :class:`TransitionError` (a ``_UserError`` carrying a stable, structured ``exit_code``), which
    the top-level CLI handler renders as a clean ``error:`` line and returns as a non-zero exit —
    never a traceback, never a silent re-write.

INVARIANTS / DESIGN
    - **One table, not scattered guards.** :data:`LEGAL_TRANSITIONS` is the explicit, complete
      adjacency map; the three close paths and the general ``status`` transition all consult it
      through the SAME validator (DRY — issue #10 acceptance criterion 3).
    - **Stdlib-only at import time** (repo hard rule): only ``tasklib.model`` (pure data). The
      structured exit code is the literal ``2`` — which IS the shared ``agenttools_errors``
      ``EXIT_USAGE`` contract value ("the request is invalid"); we hard-code the value rather than
      import the library at module load so ``task --help`` never pulls a non-stdlib import. The
      value is pinned by :func:`test_exit_code_matches_agenttools_errors_contract` so a future
      renumber in the shared lib can't drift this silently.
    - **Re-entering the same state is a clean error, not a no-op-rewrite.** ``done → done`` /
      ``cancelled → cancelled`` are rejected so a double-close can't re-fire side effects.
    - **A deliberate dead-end stays dead unless forced.** ``cancelled → done`` (resurrecting a
      cancelled ticket) is refused unless the caller passes ``force=True`` (the ``--force`` flag).
"""

from __future__ import annotations

from .model import State

# The structured exit code for an illegal transition. This is the shared ``agenttools_errors``
# ``EXIT_USAGE`` contract value (2: "the request is invalid given current state") — the SAME class
# the repo's ``_UserError`` already uses. It is hard-coded (not imported from agenttools_errors) so
# this module — and therefore ``task --help`` — pulls no non-stdlib import at load time (the repo's
# stdlib-only-at-import rule). The pin to the shared contract is asserted by a test, so a future
# renumber in agenttools_errors surfaces as a failing test rather than silent drift.
EXIT_ILLEGAL_TRANSITION = 2

# ── the legal-transition table (the single source of truth) ────────────────────────────
# For each source state, the set of states it may legally move TO. Re-entering the same state
# is deliberately ABSENT from every row (a same-state move is rejected as a no-op re-write, not
# silently allowed). ``cancelled`` is a deliberate DEAD-END (empty set): a deliberately-cancelled
# ticket leaves ``cancelled`` only with --force, so it is never silently revived to ANY state —
# the core of issue #10. ``done`` may be reopened to an active state (work was reopened) but a
# re-close to ``done`` is the same-state no-op that is rejected.
LEGAL_TRANSITIONS: dict[State, frozenset[State]] = {
    State.TODO: frozenset({State.IN_PROGRESS, State.IN_REVIEW, State.DONE, State.CANCELLED}),
    State.IN_PROGRESS: frozenset({State.TODO, State.IN_REVIEW, State.DONE, State.CANCELLED}),
    State.IN_REVIEW: frozenset({State.TODO, State.IN_PROGRESS, State.DONE, State.CANCELLED}),
    State.DONE: frozenset({State.TODO, State.IN_PROGRESS, State.IN_REVIEW, State.CANCELLED}),
    State.CANCELLED: frozenset(),
}


class TransitionError(Exception):
    """An illegal state transition was requested → a clean ``error:`` line + a non-zero exit.

    Carries a stable, structured ``exit_code`` (the ``agenttools_errors`` usage class, 2) so a
    calling script can branch on "illegal transition" the same way across the ecosystem. The CLI
    handler treats it like ``_UserError`` (prints ``error: <message>``, no traceback) but reads
    ``exit_code`` so the close paths can signal the failure class explicitly.
    """

    exit_code: int = EXIT_ILLEGAL_TRANSITION

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _illegal_message(current: State, target: State) -> str:
    """The three-part (what / why / fix) message for an illegal ``current → target`` move."""
    cur, tgt = current.value, target.value
    if current is target:
        return (
            f"already {cur}: re-running the close is a no-op re-write, not a transition\n"
            f"  why: the ticket is already in `{cur}`; re-closing would re-fire side effects "
            "(attachments, log events) and misrepresent the lifecycle\n"
            f"  fix: nothing to do — it is already {cur}"
        )
    if current is State.CANCELLED:
        return (
            f"illegal transition {cur} → {tgt}: a cancelled ticket cannot be reopened or closed\n"
            f"  why: `{cur}` is a deliberate dead-end; moving it to `{tgt}` would silently revive "
            "deliberately-cancelled work and re-fire side effects\n"
            f"  fix: pass --force to override, or create a fresh ticket instead of reviving this one"
        )
    # Generic fallback. With the CURRENT table this branch is unreachable: the only illegal
    # moves are a same-state re-entry (handled above) and any move out of `cancelled` (handled
    # above) — every active state legally reaches every other. It is kept as a defensive default
    # so that if the table ever GAINS a non-cancelled illegal edge, the message is still sane
    # rather than a KeyError/blank.
    return (
        f"illegal transition {cur} → {tgt}\n"
        f"  why: `{tgt}` is not a legal destination from `{cur}`\n"
        f"  fix: pass --force to override"
    )


def validate_transition(current: State, target: State, *, force: bool = False) -> None:
    """Raise :class:`TransitionError` if ``current → target`` is not a legal move.

    The single guard the three close paths (and the general ``status`` transition) call BEFORE
    mutating the ticket. ``force=True`` bypasses the check entirely (the ``--force`` escape hatch
    the acceptance criteria require), so an operator can deliberately override — e.g. re-open a
    cancelled ticket — while the default path refuses it.

    Consults the one :data:`LEGAL_TRANSITIONS` table; a same-state move and a move out of
    ``cancelled`` are both illegal by table construction (the same-state pair is absent from every
    row; ``cancelled`` only legally moves to the active states, never to ``done``).
    """
    if force:
        return
    if target in LEGAL_TRANSITIONS.get(current, frozenset()):
        return
    raise TransitionError(_illegal_message(current, target))
