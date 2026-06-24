"""gantt.py — the pure timeline layout + render. Tests assert integer geometry, not ANSI.

The renderer takes a ``color(code, s)`` callback; an identity callback (returns ``s``) gives a
plain-text render that's deterministic to assert against. The layout math (window auto-fit, bar
column offsets, overdue derivation, undated split) is the contract the chart is built on.
"""

from __future__ import annotations

from datetime import date

from tasklib.gantt import (
    DEFAULT_WIDTH,
    LABEL_WIDTH,
    DateWindow,
    GanttStatus,
    bar_cells,
    compute_window,
    fit_width,
    layout,
    render,
    status_for,
    to_json,
)
from tasklib.model import State, Ticket
from tasklib.textwidth import display_width

TODAY = date(2026, 6, 24)


def _plain(_code, text):  # identity color fn → plain text, no ANSI in assertions
    return text


def _t(tid, title, due="", state=State.TODO, labels=None):
    return Ticket(id=tid, title=title, due=due, state=state, labels=labels or [])


# ── window auto-fit ──────────────────────────────────────────────────────────────────


def test_window_includes_today_with_no_dates():
    w = compute_window([], TODAY)
    assert w.start <= TODAY <= w.end
    assert w.span_days >= 1  # never zero-width


def test_window_autofits_date_range_and_today():
    dates = [date(2026, 6, 20), date(2026, 7, 10)]
    w = compute_window(dates, TODAY)
    assert w.start == date(2026, 6, 20)
    assert w.end == date(2026, 7, 10)
    assert w.start <= TODAY <= w.end


def test_window_future_only_still_includes_today():
    w = compute_window([date(2026, 8, 1)], TODAY)
    assert w.start == TODAY  # today is the floor when every due date is later
    assert w.end == date(2026, 8, 1)


def test_window_single_date_degenerate_does_not_collapse():
    # a single due date equal to today → a 1-day window, no divide-by-zero downstream
    w = compute_window([TODAY], TODAY)
    assert w.span_days >= 1
    assert w.column_for(TODAY, 40) == 0  # no exception


def test_column_for_clamps_and_is_proportional():
    w = DateWindow(start=date(2026, 6, 1), end=date(2026, 6, 11))  # 10-day span
    assert w.column_for(date(2026, 5, 1), 11) == 0  # before → col 0
    assert w.column_for(date(2026, 6, 1), 11) == 0  # start → col 0
    assert w.column_for(date(2026, 6, 11), 11) == 10  # end → last col
    assert w.column_for(date(2026, 7, 1), 11) == 10  # after → clamps to last
    assert w.column_for(date(2026, 6, 6), 11) == 5  # midpoint → middle


def test_column_for_single_day_window_is_zero():
    w = DateWindow(start=TODAY, end=TODAY)
    assert w.column_for(TODAY, 40) == 0


# ── layout: rows, ordering, undated split ────────────────────────────────────────────


def test_layout_splits_dated_and_undated():
    tickets = [
        _t("#1", "has due", due="2026-07-01"),
        _t("#2", "no due"),
        _t("#3", "also dated", due="2026-06-25"),
    ]
    chart = layout(tickets, TODAY, width=40)
    assert [r.ticket.id for r in chart.rows] == ["#3", "#1"]  # sorted by due ascending
    assert [t.id for t in chart.undated] == ["#2"]


def test_layout_undated_never_dropped():
    tickets = [_t("#1", "a"), _t("#2", "b"), _t("#3", "c")]  # all undated
    chart = layout(tickets, TODAY, width=40)
    assert chart.rows == []
    assert len(chart.undated) == 3


def test_layout_bar_positions_match_window():
    # two tickets spanning a known window; assert their due columns are at the right ends
    tickets = [_t("#early", "x", due="2026-06-24"), _t("#late", "y", due="2026-07-24")]
    chart = layout(tickets, TODAY, width=31)
    early, late = chart.rows
    assert early.due_column == 0  # earliest due == window start
    assert late.due_column == chart.width - 1  # latest due == window end
    # point bars (no start date): bar is one column wide at the due column
    assert early.bar_start == early.bar_end == early.due_column
    assert late.bar_start == late.bar_end == late.due_column


def test_layout_today_column_on_axis():
    tickets = [_t("#1", "x", due="2026-06-24"), _t("#2", "y", due="2026-07-24")]
    chart = layout(tickets, TODAY, width=31)
    # today == window start here → column 0
    assert chart.today_column == 0


def test_layout_empty_is_safe():
    chart = layout([], TODAY, width=40)
    assert chart.rows == []
    assert chart.undated == []
    assert chart.window.span_days >= 1


def test_layout_zero_width_does_not_crash():
    chart = layout([_t("#1", "x", due="2026-07-01")], TODAY, width=0)
    assert chart.width == 1  # floored
    assert chart.rows[0].due_column == 0


# ── status / overdue derivation ──────────────────────────────────────────────────────


def test_overdue_derived_for_open_past_due():
    t = _t("#1", "late", due="2026-06-01", state=State.IN_PROGRESS)
    assert status_for(t, TODAY) == GanttStatus.OVERDUE


def test_done_past_due_is_not_overdue():
    t = _t("#1", "shipped late but done", due="2026-06-01", state=State.DONE)
    assert status_for(t, TODAY) == GanttStatus.DONE


def test_future_due_keeps_state():
    t = _t("#1", "soon", due="2026-07-01", state=State.IN_PROGRESS)
    assert status_for(t, TODAY) == GanttStatus.IN_PROGRESS


def test_undated_open_keeps_state():
    t = _t("#1", "no date", state=State.TODO)
    assert status_for(t, TODAY) == GanttStatus.TODO


def test_every_state_maps_to_a_gantt_status():
    # status_for() builds GanttStatus(state.value); a State value with no GanttStatus member would
    # raise ValueError and sink the whole chart. Pin the mapping so adding a State without the
    # matching GanttStatus fails HERE (loudly, at dev time), not at render time on a user's chart.
    for state in State:
        t = _t("#1", "x", due="2026-07-01", state=state)
        assert isinstance(status_for(t, TODAY), GanttStatus)


# ── bar_cells: the per-column glyph contract ─────────────────────────────────────────


def test_bar_cells_marks_due_column_with_status_glyph():
    chart = layout([_t("#1", "x", due="2026-07-24")], TODAY, width=31)
    row = chart.rows[0]
    cells = bar_cells(row, chart)
    assert len(cells) == chart.width
    # the due column carries a non-track glyph; the rest are track/today
    assert cells[row.due_column] not in ("·",)


# ── render (plain-text via identity color) ───────────────────────────────────────────


def test_render_lists_dated_and_undated_sections():
    tickets = [_t("#1", "dated one", due="2026-07-01"), _t("#2", "no due here")]
    out = render(layout(tickets, TODAY, width=40), color=_plain, today=TODAY)
    assert "#1" in out
    assert "dated one" in out
    assert "undated" in out
    assert "no due here" in out


def test_render_empty_says_so_not_crash():
    out = render(layout([], TODAY, width=40), color=_plain, today=TODAY)
    assert "no tickets" in out.lower()


def test_render_includes_window_dates():
    chart = layout([_t("#1", "x", due="2026-07-01")], TODAY, width=40)
    out = render(chart, color=_plain, today=TODAY)
    assert chart.window.start.isoformat() in out
    assert chart.window.end.isoformat() in out


def test_render_glyph_sits_at_due_column():
    # the status glyph in a rendered row must land at exactly the column layout computed (no drift
    # between the geometry and what's drawn) — assert on the plain (no-ANSI) bar cells.
    chart = layout([_t("#1", "x", due="2026-07-24")], TODAY, width=31)
    row = chart.rows[0]
    cells = bar_cells(row, chart)
    glyph = cells[row.due_column]
    # the glyph appears once, at the due column, and nowhere else in the track
    assert cells.count(glyph) == 1
    assert cells.index(glyph) == row.due_column


def test_render_overdue_glyph_in_drawn_row():
    # the overdue `!` must reach the actual rendered line at the right column (not only the unit
    # status_for / JSON paths). Assert on the plain row line.
    chart = layout([_t("#1", "late", due="2026-06-01", state=State.TODO)], TODAY, width=31)
    row = chart.rows[0]
    assert row.status == GanttStatus.OVERDUE
    out = render(chart, color=_plain, today=TODAY)
    row_line = next(ln for ln in out.splitlines() if "#1" in ln)
    assert "!" in row_line  # the overdue marker is drawn


def test_render_title_with_newline_stays_one_line():
    # a malicious/multiline title must NOT break the fixed-width grid: the row stays one line.
    chart = layout([_t("#1", "line one\nline two\ttabbed", due="2026-07-01")], TODAY, width=40)
    out = render(chart, color=_plain, today=TODAY)
    row_line = [ln for ln in out.splitlines() if "#1" in ln]
    assert len(row_line) == 1  # the ticket renders on exactly one line
    assert "line two" in row_line[0]  # content preserved, just flattened onto one line


# ── display-width column alignment (CJK / emoji titles) ──────────────────────────────


def _bar_area_column(row_line: str) -> int:
    """The display-cell column where the bar-area separator ``│`` begins on a rendered row."""
    idx = row_line.index("│")
    return display_width(row_line[:idx])


def test_cjk_title_row_aligns_to_same_bar_column_as_ascii():
    # The bug: len()-based padding counted a CJK ideograph (2 cells) as 1, so a CJK-titled row's
    # bar area started at the wrong terminal column and the grid sheared. Every row's bar area must
    # begin at the SAME display column regardless of the title's character widths.
    tickets = [
        _t("#1", "ascii title here", due="2026-06-25"),
        _t("#2", "全角タイトル日本語", due="2026-07-01"),  # all wide
        _t("#3", "mix 全角 and ascii", due="2026-07-10"),  # mixed
        _t("#4", "short", due="2026-07-15"),
    ]
    out = render(layout(tickets, TODAY, width=20), color=_plain, today=TODAY)
    row_lines = [ln for ln in out.splitlines() if ln.strip().startswith("#")]
    assert len(row_lines) == 4
    columns = {_bar_area_column(ln) for ln in row_lines}
    assert len(columns) == 1, f"bar areas misaligned across rows: {columns}"


def test_cjk_label_gutter_is_exactly_label_width_cells():
    # The label gutter (id + title, padded) must be exactly LABEL_WIDTH *display cells* wide so it
    # lines up with the axis header's `" " * LABEL_WIDTH` gutter — even for a wide-char title.
    chart = layout([_t("#42", "全角タイトル日本語テキスト", due="2026-07-01")], TODAY, width=20)
    out = render(chart, color=_plain, today=TODAY)
    row_line = next(ln for ln in out.splitlines() if ln.strip().startswith("#"))
    gutter = row_line[: row_line.index("│") - 1]  # drop the single space before the bar area
    assert display_width(gutter) == LABEL_WIDTH


def test_long_cjk_title_truncates_to_cell_width_without_splitting():
    # A title far wider than the gutter is cut to LABEL_WIDTH cells with an ellipsis, never leaving
    # a half-rendered wide char (which would still over/under-fill the column).
    chart = layout([_t("#1", "日" * 60, due="2026-07-01")], TODAY, width=20)
    out = render(chart, color=_plain, today=TODAY)
    row_line = next(ln for ln in out.splitlines() if ln.strip().startswith("#"))
    gutter = row_line[: row_line.index("│") - 1]
    assert display_width(gutter) == LABEL_WIDTH
    assert "…" in gutter  # truncation was signaled


def test_ascii_chart_is_byte_identical_regression_guard():
    # The whole render of an ASCII-only chart must be unchanged by the display-width rework. This
    # is the no-regression guard: routing through cell-aware pad/truncate must equal the old
    # len/ljust/slice output for the common (ASCII) case.
    tickets = [
        _t("#1", "first ticket", due="2026-06-25"),
        _t("#2", "a much longer ticket title that will get truncated by the gutter", due="2026-07-01"),
        _t("#3", "third", due="2026-07-10", state=State.IN_PROGRESS),
        _t("#noid", "undated one"),
    ]
    out = render(layout(tickets, TODAY, width=20), color=_plain, today=TODAY)
    expected = (
        "                                   2026-06-24 ▼                    2026-07-10\n"
        "                                   (today: 2026-06-24 ▼)\n"
        "     #1 first ticket               │○·················· 2026-06-25\n"
        "     #2 a much longer ticket titl… │·······○··········· 2026-07-01\n"
        "     #3 third                      │··················◐ 2026-07-10\n"
        "\n"
        "undated (1) — no due date set:\n"
        "  ○ #noid undated one"
    )
    assert out == expected


# ── --json timeline shape ────────────────────────────────────────────────────────────


def test_to_json_shape_is_deterministic():
    tickets = [
        _t("#1", "dated", due="2026-07-01", state=State.IN_PROGRESS),
        _t("#2", "undated"),
        _t("#3", "overdue", due="2026-06-01", state=State.TODO),
    ]
    payload = to_json(layout(tickets, TODAY, width=31), TODAY)
    assert set(payload) == {"window", "rows", "undated"}
    win = payload["window"]
    assert set(win) >= {"start", "end", "span_days", "width", "today", "today_column"}
    # rows are sorted by due ascending → overdue (#3) first
    assert [r["id"] for r in payload["rows"]] == ["#3", "#1"]
    overdue_row = payload["rows"][0]
    assert overdue_row["status"] == "overdue"
    assert overdue_row["state"] == "todo"  # state is preserved alongside derived status
    assert {"bar_start", "bar_end", "due_column"} <= set(overdue_row)
    assert [u["id"] for u in payload["undated"]] == ["#2"]


def test_to_json_empty():
    payload = to_json(layout([], TODAY, width=40), TODAY)
    assert payload["rows"] == []
    assert payload["undated"] == []
    assert payload["window"]["span_days"] >= 1


# ── fit_width (terminal fitting) ─────────────────────────────────────────────────────


def test_fit_width_clamps_to_band():
    assert fit_width(200) == DEFAULT_WIDTH  # wide terminal → capped at default
    assert fit_width(20) == DEFAULT_WIDTH  # too narrow → fall back to default (scrolls)
    mid = fit_width(80)
    assert 20 <= mid <= DEFAULT_WIDTH
