"""textwidth.py — terminal display-width helpers (cell-aware width / pad / truncate).

The contract these tests pin:
- ``display_width`` counts terminal *cells*, not code points (CJK/emoji → 2, combining/ZWJ → 0).
- ``pad`` is a cell-aware ``ljust`` (so a CJK label pads to the right column width).
- ``truncate`` cuts to a cell budget without splitting a wide char or stranding a combining mark.
- The ASCII common case is byte-identical to the old ``len`` / ``ljust`` / slice behavior — the
  regression guard that keeps every plain-ASCII chart unchanged.
"""

from __future__ import annotations

import pytest

from tasklib.textwidth import char_width, display_width, pad, truncate

# ── char_width: 0 / 1 / 2 cells ──────────────────────────────────────────────────────

CJK = "全角タイトル日本語"  # all fullwidth (W) → 2 cells each
ROCKET = "\U0001F680"  # 🚀 — single-code-point wide emoji (East_Asian_Width W)
ZWJ = "‍"  # zero-width joiner
VARIATION_SELECTOR = "️"  # emoji variation selector-16
COMBINING_ACUTE = "́"  # combining acute accent (rides the previous base)


def test_char_width_ascii_is_one():
    for ch in "abcXYZ0123 #-_…":
        assert char_width(ch) == 1, ch


def test_char_width_cjk_is_two():
    for ch in CJK:
        assert char_width(ch) == 2, ch


def test_char_width_wide_emoji_is_two():
    assert char_width(ROCKET) == 2


def test_char_width_zero_width_chars_are_zero():
    assert char_width(ZWJ) == 0
    assert char_width(VARIATION_SELECTOR) == 0
    assert char_width(COMBINING_ACUTE) == 0
    assert char_width("​") == 0  # zero-width space
    assert char_width("﻿") == 0  # BOM / zero-width no-break space


# ── display_width: cell totals ───────────────────────────────────────────────────────


def test_display_width_ascii_equals_len():
    for s in ["", "a", "hello world", "#42 fix the thing", "x" * 100, "(none specified)"]:
        assert display_width(s) == len(s), s


def test_display_width_cjk_doubles():
    assert display_width(CJK) == 2 * len(CJK)
    assert display_width("a全b") == 4  # 1 + 2 + 1


def test_display_width_combining_sequence_is_base_width():
    # "e" + combining acute renders as one cell, even though it is two code points.
    assert display_width("e" + COMBINING_ACUTE) == 1
    assert len("e" + COMBINING_ACUTE) == 2  # ... and len() would have miscounted it


def test_display_width_emoji_with_variation_selector():
    # a base emoji + VS-16 occupies the emoji's own cells; the selector adds none.
    assert display_width(ROCKET + VARIATION_SELECTOR) == 2


# ── pad: cell-aware ljust / rjust ────────────────────────────────────────────────────


def test_pad_ascii_matches_ljust_exactly():
    # the regression guard: pure-ASCII padding must be byte-identical to str.ljust.
    for s in ["", "a", "#1 title", "x" * 33, "(new)"]:
        for w in (0, 1, 7, 34, 80):
            assert pad(s, w) == s.ljust(w), (s, w)


def test_pad_cjk_pads_by_cells_not_codepoints():
    # 4 CJK chars = 8 cells; padding to 12 adds 4 spaces (not 8 — the codepoint count).
    s = "全角四字"
    out = pad(s, 12)
    assert display_width(out) == 12
    assert out == s + " " * 4


def test_pad_right_align():
    assert pad("hi", 5, align="right") == "   hi"
    assert pad("全", 5, align="right") == "   全"  # 2 cells + 3 spaces


def test_pad_over_width_returns_unchanged():
    assert pad("全角タイトル", 3) == "全角タイトル"  # already wider than 3 cells → untouched


# ── truncate: cell-budget, no wide-char split ────────────────────────────────────────


def test_truncate_ascii_matches_old_slice_behavior():
    # the regression guard: old gantt truncate was
    #   len<=w -> s ; w<=1 -> s[:w] ; else s[:w-1] + "…"
    for s in ["", "hello", "hello world this is long", "x" * 50]:
        for w in (0, 1, 2, 5, 10, 50):
            if len(s) <= w:
                old = s
            elif w <= 1:
                old = s[:w]
            else:
                old = s[: w - 1] + "…"
            assert truncate(s, w) == old, (s, w)


def test_truncate_short_text_unchanged():
    assert truncate("全角", 10) == "全角"  # 4 cells <= 10 → unchanged, no ellipsis


def test_truncate_cjk_fits_cell_budget_with_ellipsis():
    out = truncate(CJK, 7)
    assert display_width(out) <= 7
    assert out.endswith("…")


def test_truncate_never_splits_a_wide_char():
    # budget 5 = 1 (ellipsis) + 4 content cells = 2 CJK chars; the 3rd would overflow → dropped whole
    out = truncate("全角タイトル", 5)
    assert out == "全角…"
    assert display_width(out) == 5  # exact, no half-cell overrun


def test_truncate_does_not_strand_a_combining_mark():
    # "e" + acute is one grapheme (1 cell); a budget that includes the base must keep its mark.
    text = "abce" + COMBINING_ACUTE + "fghij"
    out = truncate(text, 4)  # budget 3 content + 1 ellipsis
    # the kept content is whole chars; the base+mark sequence is never split mid-cluster.
    assert display_width(out) <= 4
    assert out.endswith("…")


def test_truncate_zero_width_returns_empty():
    assert truncate("anything", 0) == ""


def test_truncate_width_below_ellipsis_drops_ellipsis():
    # width 1 with a 1-cell ellipsis: no room for content + ellipsis → just the clipped content.
    assert truncate("hello", 1) == "h"
    assert truncate("全角", 1) == ""  # one wide char needs 2 cells; 1 cell holds nothing whole


@pytest.mark.parametrize("text", ["", "ascii only", CJK, "mix 全角 and ascii", ROCKET * 3])
def test_truncate_output_never_exceeds_budget(text):
    for w in range(0, 30):
        assert display_width(truncate(text, w)) <= max(0, w)


# ── degenerate / negative widths (boundary anchors) ──────────────────────────────────


def test_pad_negative_width_returns_unchanged():
    # A negative width is a no-op deficit (<= 0) → the string is returned untouched, matching the
    # spirit of str.ljust(-n). Pinned so the boundary can't silently regress.
    assert pad("hi", -5) == "hi"
    assert pad("全角", -1) == "全角"


def test_truncate_negative_width_returns_empty():
    assert truncate("hello", -3) == ""
    assert truncate("全角", -1) == ""


# ── multi-code-point emoji cluster: the documented per-codepoint approximation ────────


def test_grapheme_cluster_width_is_summed_per_codepoint_documented_compromise():
    # A ZWJ family emoji (👨‍👩‍👧 = man + ZWJ + woman + ZWJ + girl) is ONE grapheme but several code
    # points. char_width has no cluster table, so it sums per code point: 2 (man) + 0 (ZWJ) +
    # 2 (woman) + 0 (ZWJ) + 2 (girl) = 6, not the terminal's actual 2. This is the explicitly
    # documented compromise (no wcwidth dep); the anchor pins it so a future "improvement" to
    # grapheme clustering is a conscious, test-visible change — not a silent gantt-grid regression.
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert display_width(family) == 6
