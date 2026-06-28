"""references.py — the un-linked-reference scanner (rule 1: related entities must be links)."""

from __future__ import annotations

from tasklib.references import find_unlinked_references


def _kinds(text: str) -> set[str]:
    return {r.kind for r in find_unlinked_references(text)}


def _tokens(text: str) -> set[str]:
    return {r.text for r in find_unlinked_references(text)}


def test_bare_tracker_id_is_flagged():
    assert "HYP-789" in _tokens("blocked by HYP-789 until merged")
    assert "tracker id" in _kinds("blocked by HYP-789")


def test_bare_issue_ref_is_flagged():
    assert "#123" in _tokens("fixes #123 on mobile")


def test_bare_commit_sha_is_flagged():
    assert "6103ec8" in _tokens("regressed in 6103ec8")
    # an all-letter hex word that is real English is NOT mistaken for a sha
    assert _tokens("the wall was defaced overnight") == set()


def test_repo_and_path_slugs_flagged_but_english_pairs_are_not():
    assert "alex-mextner/task-cli" in _tokens("see alex-mextner/task-cli for context")
    assert "tasklib/cli.py" in _tokens("the bug is in tasklib/cli.py")
    # common english/jargon pairs must NOT trip the gate (broad, but not absurd)
    assert _tokens("support read/write and input/output and TS/TSX") == set()


def test_all_letter_owner_repo_is_a_deliberate_false_negative():
    # A two-segment, all-LETTER single-slash slug (facebook/react) is intentionally NOT flagged:
    # it is indistinguishable in prose from an English pair (read/write) without a word-list, so
    # the gate stays quiet rather than fire on ordinary sentences. Documented in references.py.
    assert _tokens("built on facebook/react and django/django") == set()
    # ...but the moment it carries a signal char (-, ., digit) or a host, it IS flagged:
    assert "facebook/react-dom" in _tokens("see facebook/react-dom")


def test_already_linked_references_are_not_flagged():
    # a proper markdown link or a full URL satisfies the rule → nothing flagged
    assert find_unlinked_references("see [HYP-789](https://linear.app/x/HYP-789)") == []
    assert find_unlinked_references("fixed in https://github.com/o/r/commit/6103ec8deadbeef") == []
    assert find_unlinked_references("[#123](https://github.com/o/r/issues/123)") == []


def test_references_are_deduplicated():
    refs = find_unlinked_references("HYP-1 and HYP-1 again")
    assert [r.text for r in refs] == ["HYP-1"]


def test_pure_numbers_are_not_shas():
    # a long decimal number is NOT a commit sha (sha needs both a hex letter and a digit)
    assert _tokens("the service handles 1000000 requests per second") == set()


def test_common_acronyms_are_not_tracker_ids():
    for token in ("UTF-8", "SHA-256", "ISO-8601", "RFC-7231", "COVID-19"):
        assert _tokens(f"encoded as {token} in the payload") == set(), token


def test_numeric_dates_and_ratios_are_not_slugs():
    assert _tokens("due 06/28 with 24/7 coverage and the 9/11 retro") == set()


def test_clean_text_yields_nothing():
    assert find_unlinked_references("a perfectly ordinary sentence about users and their goals") == []
