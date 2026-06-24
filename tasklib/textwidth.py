"""Terminal display-width helpers — column alignment by CELL width, not code-point count.

Why this exists: every fixed-width grid in task-cli (the ``gantt`` chart's label gutter and
bar area; any padded/truncated column) lined up its columns with ``len(s)`` / ``str.ljust`` /
slicing. Those count Unicode *code points*. A terminal lays text out in *cells*: a CJK
ideograph (全角) or an emoji occupies **two** cells but is one code point, while a combining
mark or a zero-width joiner occupies **zero** cells yet still counts as one. So a title with
wide or combining characters over- or under-fills its column and shifts every column after it,
breaking the grid.

The fix is to measure and pad/truncate by *display width*. This is pure, stdlib-only string
work — ``unicodedata.east_asian_width`` (W/F → 2) plus a zero-width rule for combining marks,
the zero-width joiner, and the variation selectors. No ``wcwidth`` dependency: task-cli ships
only ``pyyaml``, and a perfect emoji-cluster width table is not worth a new dep for label
alignment. The approximation is deliberate and documented in :func:`char_width`.

ASCII is the common case and MUST stay byte-identical to the old ``len``/``ljust``/slice path:
every ASCII char has width 1, so :func:`display_width` == ``len`` and :func:`pad` == ``ljust``
for any pure-ASCII string (regression-guarded in the tests).
"""

from __future__ import annotations

import unicodedata

# The Unicode east-asian-width classes that take two terminal cells. ``W`` (Wide) and ``F``
# (Fullwidth) are the unambiguous double-width ranges (CJK ideographs, fullwidth forms, most
# emoji that carry an East_Asian_Width of W). ``A`` (Ambiguous) is intentionally treated as
# width 1: it is single-width in a Western locale terminal (the task-cli target) and only
# doubles under a CJK locale — guessing 2 would break far more (Greek, Cyrillic, box-drawing)
# than it fixes.
_WIDE_EAW = frozenset({"W", "F"})

# Zero-width: combining marks (Mn/Me — they stack onto the previous cell), the zero-width
# joiner / non-joiner, and the variation selectors (which only modify the preceding glyph).
# A standalone control char is handled by the caller (the gantt label flattens controls to a
# space first); here a control simply falls through to width 1, which is harmless.
_ZERO_WIDTH_CODEPOINTS = frozenset({0x200B, 0x200C, 0x200D, 0xFEFF})


def char_width(ch: str) -> int:
    """The number of terminal cells a single character occupies: 0, 1, or 2.

    - 0 for a combining mark (``Mn``/``Me``), the zero-width (non-)joiner, the BOM, and the
      variation selectors (U+FE00–U+FE0F): they render onto the *previous* cell, adding none.
    - 2 for East_Asian_Width ``W``/``F`` (CJK ideographs, fullwidth forms, wide emoji).
    - 1 otherwise (the ASCII / Western common case).

    This is an approximation, not a perfect grapheme-cluster width: a multi-code-point emoji ZWJ
    sequence (e.g. a flag, or 👨‍👩‍👧) is summed per code point rather than measured as one cluster,
    so an exotic emoji label may still drift by a cell. Correct alignment for CJK text and
    single-code-point emoji — the cases that actually show up in ticket titles — is covered.
    """
    code = ord(ch)
    if code in _ZERO_WIDTH_CODEPOINTS or 0xFE00 <= code <= 0xFE0F:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in _WIDE_EAW:
        return 2
    return 1


def display_width(text: str) -> int:
    """Total terminal-cell width of ``text`` (sum of :func:`char_width` over its characters).

    Equals ``len(text)`` for any pure-ASCII string (the regression-critical common case).
    """
    return sum(char_width(ch) for ch in text)


def pad(text: str, width: int, *, align: str = "left") -> str:
    """Pad ``text`` with spaces to a *display* width of ``width`` (the cell-aware ``ljust``).

    ``align`` is ``"left"`` (default, like ``ljust``) or ``"right"`` (like ``rjust``). A string
    already at or over ``width`` display cells is returned unchanged — same as ``str.ljust``,
    which never truncates. Pad amount is computed from the missing *cells*, so a CJK/emoji
    string pads by the right number of spaces to align the column.
    """
    deficit = width - display_width(text)
    if deficit <= 0:
        return text
    spaces = " " * deficit
    return spaces + text if align == "right" else text + spaces


def truncate(text: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate ``text`` to at most ``width`` *display* cells, appending ``ellipsis`` if cut.

    Never splits a wide character across the boundary and never strands a combining mark on a
    dropped base: characters are taken whole until the next one would exceed the budget. When
    truncation happens the result ends with ``ellipsis`` and the *total* (kept text + ellipsis)
    fits within ``width`` cells. For pure-ASCII text with a 1-cell ellipsis this is byte-for-byte
    the old ``s[: width - 1] + "…"`` behavior.

    Degenerate widths: ``width <= 0`` → empty string. When ``width`` is too small to hold even
    one cell of content beside the ellipsis (``width <= ellipsis width``), the ellipsis is
    *dropped* and the result is just the content clipped to ``width`` cells — there is no room to
    signal "more follows", and this keeps the pure-ASCII width-1 case byte-identical to the old
    ``flat[:width]`` slice (a regression guard).
    """
    if width <= 0:
        return ""
    total = display_width(text)
    if total <= width:
        return text

    ell_w = display_width(ellipsis)
    # No room for content beside the ellipsis → drop it; clip the content to the raw budget.
    if width <= ell_w:
        return _take_cells(text, width)

    budget = width - ell_w
    return _take_cells(text, budget) + ellipsis


def _take_cells(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose display width is ``<= budget`` (whole chars only).

    Combining marks (width 0) ride along with the base char before them at no cost, so a base +
    accent sequence is never split. A wide char that would overflow the budget is dropped whole
    rather than half-rendered.
    """
    out: list[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out)
