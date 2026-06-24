"""Gantt timeline layout — the pure, testable core of ``task gantt`` (no I/O, no color).

The chart is computed in two passes that this module owns:

1. :func:`compute_window` picks the inclusive date range the axis spans — it auto-fits the
   tickets' own due dates and ALWAYS includes ``today`` (so "now" has a place on the axis even
   when every ticket is in the past or the future). A single-date or empty range degrades to a
   one-day window rather than dividing by zero.
2. :func:`layout` maps each dated ticket onto integer column offsets within a fixed chart width,
   yielding a :class:`GanttRow` per ticket (its bar's start/end column + a status marker) and a
   separate list of *undated* tickets. Undated tickets are returned, never dropped.

``cli.py``/``render``-side code turns a :class:`GanttChart` into ANSI; tests assert the integer
layout here (deterministic), not the rendered escape codes. The bar geometry is the contract:
``bar_start``/``bar_end`` are 0-based inclusive column indices into a chart of ``width`` columns.

A ticket's *span* is ``created → due`` when a created date is available, else a single point at
``due`` (a one-column bar). task-cli's ``Ticket`` carries no created date today, so the common
case is a point bar; the span path is kept so a backend that supplies a start date Just Works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from .textwidth import pad, truncate

if TYPE_CHECKING:
    from .model import Ticket

# A sensible default chart width (columns of the bar area, excluding the label gutter). Narrow
# enough to fit an 80-col terminal next to a label; the renderer may pass a terminal-derived width.
DEFAULT_WIDTH = 40

# The minimum window span in days. A degenerate range (one date, or today == the only due date)
# still gets a 1-day-wide axis so column math never divides by zero.
_MIN_SPAN_DAYS = 1


class GanttStatus(str, Enum):
    """The per-row status marker — the lifecycle state, with ``OVERDUE`` derived from the date.

    ``OVERDUE`` is not a ``State``: it is an open (non-done, non-cancelled) ticket whose due date
    is strictly before ``today``. It is computed at layout time so the chart can flag a slipped
    schedule that the stored state alone can't show.
    """

    TODO = "todo"
    IN_PROGRESS = "in-progress"
    IN_REVIEW = "in-review"
    DONE = "done"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


def status_for(ticket: "Ticket", today: date) -> GanttStatus:
    """The marker for a ticket: its state, escalated to ``OVERDUE`` when an open ticket is late."""
    from .model import State

    if ticket.state in (State.DONE, State.CANCELLED):
        return GanttStatus(ticket.state.value)
    due = ticket.due_date()
    if due is not None and due < today:
        return GanttStatus.OVERDUE
    return GanttStatus(ticket.state.value)


@dataclass(frozen=True)
class DateWindow:
    """The inclusive ``[start, end]`` date range the axis spans, plus its total day span."""

    start: date
    end: date

    @property
    def span_days(self) -> int:
        """Total days between start and end. ``0`` for a single-day window.

        :func:`compute_window` widens any range to at least :data:`_MIN_SPAN_DAYS`, so a window
        built that way is never zero-span; a hand-constructed ``DateWindow(d, d)`` IS zero, which
        :meth:`column_for` guards (``span <= 0`` → column 0) rather than dividing by it.
        """
        return (self.end - self.start).days

    def column_for(self, day: date, width: int) -> int:
        """The 0-based column (0..width-1) a date lands on within ``width`` columns.

        A date before the window clamps to column 0; after it clamps to the last column. The
        mapping is proportional across the inclusive span; a single-day window puts every date
        in column 0. Pure integer arithmetic — deterministic and test-friendly.
        """
        if width <= 1:
            return 0
        span = self.span_days
        if span <= 0:
            return 0
        offset = (day - self.start).days
        if offset <= 0:
            return 0
        if offset >= span:
            return width - 1
        # proportional placement across [0, width-1]
        return round(offset / span * (width - 1))


def compute_window(due_dates: list[date], today: date) -> DateWindow:
    """Auto-fit a :class:`DateWindow` to ``due_dates`` + ``today`` (today is always inside).

    Empty input yields a 1-day window at ``today``. A single distinct date (equal to today or
    not) yields a window from min..max that includes today, widened to at least
    :data:`_MIN_SPAN_DAYS` so the axis is never zero-width.
    """
    points = [*due_dates, today]
    start = min(points)
    end = max(points)
    if (end - start).days < _MIN_SPAN_DAYS:
        end = start + timedelta(days=_MIN_SPAN_DAYS)
    return DateWindow(start=start, end=end)


@dataclass
class GanttRow:
    """One ticket's row in the chart: the ticket, its status marker, and its bar geometry.

    ``bar_start``/``bar_end`` are 0-based inclusive column indices into a chart ``width`` columns
    wide. A point-in-time ticket (no start date) has ``bar_start == bar_end`` (a one-column bar).
    ``due_column`` is where the due date itself sits (== ``bar_end`` for a point bar).
    """

    ticket: "Ticket"
    status: GanttStatus
    bar_start: int
    bar_end: int
    due_column: int


@dataclass
class GanttChart:
    """The laid-out chart: the window, the dated rows, and the undated tickets (shown separately)."""

    window: DateWindow
    width: int
    today_column: int
    rows: list[GanttRow] = field(default_factory=list)
    undated: list["Ticket"] = field(default_factory=list)


def _start_date_for(ticket: "Ticket") -> date | None:
    """The bar's start date (``created`` if a backend supplied one), else ``None`` (point bar).

    task-cli's ``Ticket`` has no created field today, so this is the extension seam: a subclass
    or a future field named ``created``/``start`` (an ISO date string or a ``date``) is honored.
    Anything unparseable yields ``None`` (degrade to a point bar, never crash).
    """
    raw = getattr(ticket, "created", None) or getattr(ticket, "start", None)
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except (ValueError, TypeError):
        return None


def layout(tickets: list["Ticket"], today: date, width: int = DEFAULT_WIDTH) -> GanttChart:
    """Lay tickets onto a date axis: dated tickets become :class:`GanttRow`s, the rest are undated.

    Sorting: dated rows are ordered by due date ascending (earliest/most-urgent first), then by
    id for a stable tie-break, so the chart reads top-to-bottom as a schedule. The window auto-fits
    every due date plus ``today``. ``width`` is the bar-area column count (>= 1).
    """
    width = max(1, width)
    dated: list["Ticket"] = []
    undated: list["Ticket"] = []
    for t in tickets:
        (dated if t.due_date() is not None else undated).append(t)

    # tie-break by id, coercing an empty/None id to "" — other call sites tolerate a missing id
    # (``t.id or '(new)'``), so the sort must not be the one place a None id raises a TypeError.
    dated.sort(key=lambda t: (t.due_date() or today, t.id or ""))
    window = compute_window([t.due_date() for t in dated if t.due_date()], today)

    rows = [_row_for(t, window, width, today) for t in dated]
    return GanttChart(
        window=window,
        width=width,
        today_column=window.column_for(today, width),
        rows=rows,
        undated=undated,
    )


def _row_for(ticket: "Ticket", window: DateWindow, width: int, today: date) -> GanttRow:
    """Build the :class:`GanttRow` for one dated ticket (its bar columns + status marker)."""
    due = ticket.due_date()
    assert due is not None  # callers split dated/undated before this
    due_col = window.column_for(due, width)
    start = _start_date_for(ticket)
    start_col = window.column_for(start, width) if start is not None else due_col
    bar_start, bar_end = min(start_col, due_col), max(start_col, due_col)
    return GanttRow(
        ticket=ticket,
        status=status_for(ticket, today),
        bar_start=bar_start,
        bar_end=bar_end,
        due_column=due_col,
    )


# ── rendering (box-drawing/ASCII; color injected so it stays testable + honors NO_COLOR) ──

# One glyph per status — the marker that sits at the due column of each bar. Box-drawing so it
# works in any UTF-8 terminal; the bar fill itself is a plain block run.
_STATUS_GLYPH: dict[GanttStatus, str] = {
    GanttStatus.TODO: "○",
    GanttStatus.IN_PROGRESS: "◐",
    GanttStatus.IN_REVIEW: "◑",
    GanttStatus.DONE: "●",
    GanttStatus.CANCELLED: "✗",
    GanttStatus.OVERDUE: "!",
}

# ANSI color code per status, applied by the caller's color function (empty = no color).
_STATUS_COLOR: dict[GanttStatus, str] = {
    GanttStatus.TODO: "",  # default fg
    GanttStatus.IN_PROGRESS: "36",  # cyan
    GanttStatus.IN_REVIEW: "34",  # blue
    GanttStatus.DONE: "32",  # green
    GanttStatus.CANCELLED: "2",  # dim
    GanttStatus.OVERDUE: "31",  # red
}

_BAR_FILL = "─"
_BAR_TRACK = "·"  # the empty axis track behind a bar
_TODAY_MARK = "│"  # the "today" gridline (drawn behind bars where they don't cover it)

# How wide the left label gutter is (ticket id + truncated title). Kept fixed so every row's bar
# area starts at the same column — the chart reads as a grid.
LABEL_WIDTH = 34

# Columns the trailing due-date string + separators consume to the right of the bar area; used
# only when fitting the chart to a terminal so the bar area + label + date fit one line.
_TRAILING_WIDTH = 14


def fit_width(terminal_columns: int) -> int:
    """The bar-area width that fits ``terminal_columns`` (label gutter + bar + trailing date).

    Clamps to a sensible band: never narrower than 20 columns (a 1-column-per-day-ish minimum to
    read), never wider than :data:`DEFAULT_WIDTH`. Falls back to the default when the terminal is
    too narrow to fit even the minimum, so a tiny window degrades to a scrollable default rather
    than a 3-column chart.
    """
    avail = terminal_columns - LABEL_WIDTH - _TRAILING_WIDTH
    if avail < 20:
        return DEFAULT_WIDTH
    return max(20, min(DEFAULT_WIDTH, avail))


def _truncate(text: str, width: int) -> str:
    """One-line, truncated label measured in terminal *cells*, not code points.

    The chart is a fixed-width grid: a newline or other control char in a ticket title would shift
    every subsequent column (and an embedded ANSI escape would leak into the terminal). Replacing
    control chars with a space keeps each row exactly one line. Truncation is by display width so a
    CJK/emoji title is cut to ``width`` *cells* (never splitting a wide char) — :func:`truncate`
    owns the cell math; for pure-ASCII text this is byte-identical to the old ``len``/slice path.
    """
    flat = "".join(c if c.isprintable() else " " for c in text)
    return truncate(flat, width)


def bar_cells(row: GanttRow, chart: GanttChart) -> list[str]:
    """The raw (uncolored) glyph for each of the ``chart.width`` columns of one row's bar area.

    Column content, in precedence: the due-status glyph at ``due_column``; a bar-fill glyph across
    ``[bar_start, bar_end]``; the today gridline at ``chart.today_column``; else the empty track.
    Returned as a list so the caller colors per-cell; this is what the renderer and tests share.
    """
    cells: list[str] = []
    for col in range(chart.width):
        if col == row.due_column:
            cells.append(_STATUS_GLYPH[row.status])
        elif row.bar_start <= col <= row.bar_end:
            cells.append(_BAR_FILL)
        elif col == chart.today_column:
            cells.append(_TODAY_MARK)
        else:
            cells.append(_BAR_TRACK)
    return cells


def render(chart: GanttChart, color, today: date) -> str:
    """Render the chart to a single string. ``color(code, s)`` applies ANSI (or is a no-op).

    Layout per row: ``<id+title gutter> │ <bar area>`` then the status marker is colored at the due
    column. A header line shows the date axis (start … today … end); an undated section follows.
    Pure string assembly — the ANSI is entirely the caller's ``color`` function, so a plain-text
    color (identity) renderer is exercised in tests.
    """
    lines: list[str] = []
    lines.append(_axis_header(chart, color, today))
    if not chart.rows and not chart.undated:
        lines.append(color("2", "(no tickets to chart)"))
        return "\n".join(lines)

    for row in chart.rows:
        lines.append(_render_row(row, chart, color))

    if chart.undated:
        lines.append("")
        lines.append(color("2", f"undated ({len(chart.undated)}) — no due date set:"))
        for t in chart.undated:
            marker = _STATUS_GLYPH[status_for(t, today)]
            label = _truncate(f"{t.id or '(new)'} {t.title}", LABEL_WIDTH + chart.width)
            lines.append(f"  {marker} {label}")
    return "\n".join(lines)


def _axis_header(chart: GanttChart, color, today: date) -> str:
    """The top axis line: the window start/end dates with ``today`` (``▼``) marked on the track.

    ``compute_window`` always puts ``today`` inside ``[start, end]``, so ``today_column`` is always
    a valid in-range column; the bounds guard on the marker placement is a defensive belt, not a
    real "off-axis" case (which is why the note just states the date, no off-axis branch).
    """
    w = chart.window
    track = [" "] * chart.width
    if 0 <= chart.today_column < chart.width:
        track[chart.today_column] = "▼"
    gutter = " " * LABEL_WIDTH
    axis = "".join(track)
    head = f"{gutter} {color('2', w.start.isoformat())} {axis} {color('2', w.end.isoformat())}"
    note = color("2", f"(today: {today.isoformat()} ▼)")
    return head + "\n" + " " * LABEL_WIDTH + " " + note


def _render_row(row: GanttRow, chart: GanttChart, color) -> str:
    """One ticket row: the colored label gutter + the colored bar cells."""
    t = row.ticket
    # Right-pad the id by display CELLS too (not the `:>7` format-spec's code-point count) so the
    # id gutter holds even for a non-ASCII id — byte-identical to `:>7` for the normal ASCII id.
    tid = pad(t.id or "(new)", 7, align="right")
    label = _truncate(f"{tid} {t.title}", LABEL_WIDTH)
    label = pad(label, LABEL_WIDTH)  # pad by display CELLS, not code points (CJK/emoji-safe)
    code = _STATUS_COLOR[row.status]
    cells = bar_cells(row, chart)
    painted = []
    for col, ch in enumerate(cells):
        if col == row.due_column or row.bar_start <= col <= row.bar_end:
            painted.append(color(code, ch) if code else ch)
        else:
            painted.append(color("2", ch))
    due = t.due_date()
    due_str = color("2", f" {due.isoformat()}" if due else "")
    return f"{label} {''.join(painted)}{due_str}"


def to_json(chart: GanttChart, today: date) -> dict:
    """The machine-readable timeline shape: window + per-row bar geometry + the undated list.

    Deterministic and stable — this is the ``--json`` contract a script consumes. Column indices
    are 0-based into a ``width``-column chart; dates are ISO strings.
    """
    return {
        "window": {
            "start": chart.window.start.isoformat(),
            "end": chart.window.end.isoformat(),
            "span_days": chart.window.span_days,
            "width": chart.width,
            "today": today.isoformat(),
            "today_column": chart.today_column,
        },
        "rows": [
            {
                "id": r.ticket.id,
                "title": r.ticket.title,
                "state": r.ticket.state.value,
                "status": r.status.value,
                "due": r.ticket.due,
                "bar_start": r.bar_start,
                "bar_end": r.bar_end,
                "due_column": r.due_column,
            }
            for r in chart.rows
        ],
        "undated": [
            {
                "id": t.id,
                "title": t.title,
                "state": t.state.value,
                "status": status_for(t, today).value,
            }
            for t in chart.undated
        ],
    }
