"""quality.py — user-impact plain-language assessment (rule 5)."""

from __future__ import annotations

from tasklib.quality import assess_user_impact


def test_one_word_impact_is_too_thin():
    problems = assess_user_impact("users")
    assert problems
    assert "thin" in problems[0]


def test_jargon_only_impact_has_no_user_context():
    # length is fine, but it is framed in implementation terms with no user-world cue
    problems = assess_user_impact(
        "refactor the serializer and the orm payload so the async middleware compiles cleanly again"
    )
    assert problems
    assert "user-world context" in problems[0]


def test_plain_language_user_framed_impact_passes():
    good = (
        "Users on the checkout page can finally complete a purchase instead of seeing a blank "
        "screen, so they stop abandoning their carts"
    )
    assert assess_user_impact(good) == []


def test_detailed_impact_mentioning_people_passes():
    assert assess_user_impact("People who open the report no longer wait forever for it to load") == []
