"""Assess the *quality* of a ticket's user-impact text — the plain-language rule.

Pure, stdlib-only. The ``user-impact`` section must be written for someone only weakly familiar
with the product, in the user's-world terms (their tasks/goals/what they see), not in
implementation/jargon terms. A one-word "users" or a jargon-only "refactor the serializer"
tells a reader nothing. :func:`assess_user_impact` returns the quality problems (empty = good)
that the ``user-impact-quality`` gate (``policy.py``) turns into a refusal.

The heuristic is intentionally coarse — it catches the two failure modes that recur (too
thin, and no user-world framing) without pretending to grade prose. The escape hatch
(``--skip-user-impact-quality`` / ``--force "<reason>"``) covers a genuinely-N/A impact.
"""

from __future__ import annotations

import re

# A user-impact worth the name says something concrete; below this it is a placeholder, not impact.
_MIN_WORDS = 10

# Words that signal the text is framed around the USER and their world (people, what they do/see,
# the surfaces they touch). Presence of any one is the cheap "this talks about users" signal.
_USER_CUES = frozenset(
    {
        "user", "users", "customer", "customers", "people", "person", "they", "them", "you",
        "someone", "anyone", "see", "sees", "seeing", "view", "read", "reads", "click", "clicks",
        "tap", "taps", "navigate", "page", "pages", "screen", "button", "menu", "form", "load",
        "loads", "open", "opens", "unable", "can", "cannot", "without", "instead", "expect",
        "expects", "experience", "workflow", "report", "reports", "dashboard", "notice", "confused",
        "lost", "blocked", "wait", "waiting", "mistake", "fails", "lose", "loses", "their", "able",
    }
)

_WORD_RE = re.compile(r"[A-Za-z']+")


def assess_user_impact(text: str) -> list[str]:
    """Return the user-impact quality problems (empty = acceptable)."""
    problems: list[str] = []
    words = _WORD_RE.findall(text)
    if len(words) < _MIN_WORDS:
        problems.append(
            f"too thin ({len(words)} word(s)): describe, in plain language and full context, what "
            f"actually changes for someone weakly familiar with the product — their tasks/goals, "
            f"not the implementation"
        )
        return problems  # too short to judge framing meaningfully

    if not _has_user_cue(text):
        problems.append(
            "reads in implementation/jargon terms with no user-world context: say who is affected "
            "and what they can or can't do, in their terms, not the system's"
        )
    return problems


def _has_user_cue(text: str) -> bool:
    """True if the text uses at least one user-world word (it talks about people, not internals)."""
    tokens = {w.lower() for w in _WORD_RE.findall(text)}
    return bool(tokens & _USER_CUES)
