"""The ``Ticket`` dataclass — the in-memory, backend-agnostic shape of a ticket.

Pure data, no I/O. Backends translate provider rows (a GitHub issue JSON, a Linear node)
into a ``Ticket`` and back; ``render.py`` serializes the body section template; ``policy.py``
reads a ``Ticket`` to decide whether the enforcement gates pass. Nothing here imports a
provider SDK, the network, or the filesystem.

The canonical ``State`` enum is the *normalized* lifecycle every backend maps onto its own
states (GitHub issues are open/closed + labels; Linear has its own workflow states). Keeping
one enum here means ``policy.py`` and ``render.py`` never branch on the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date


class State(str, Enum):
    """Normalized ticket lifecycle. Backends map these onto their native states."""

    TODO = "todo"
    IN_PROGRESS = "in-progress"
    IN_REVIEW = "in-review"
    DONE = "done"
    CANCELLED = "cancelled"

    @classmethod
    def parse(cls, value: str) -> "State":
        """Parse a user/string state, tolerant of aliases. Raises ``ValueError`` on junk."""
        norm = value.strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "open": cls.TODO,
            "todo": cls.TODO,
            "backlog": cls.TODO,
            "in-progress": cls.IN_PROGRESS,
            "inprogress": cls.IN_PROGRESS,
            "doing": cls.IN_PROGRESS,
            "started": cls.IN_PROGRESS,
            "in-review": cls.IN_REVIEW,
            "inreview": cls.IN_REVIEW,
            "review": cls.IN_REVIEW,
            "done": cls.DONE,
            "closed": cls.DONE,
            "completed": cls.DONE,
            "cancelled": cls.CANCELLED,
            "canceled": cls.CANCELLED,
            "wontfix": cls.CANCELLED,
        }
        try:
            return aliases[norm]
        except KeyError as exc:
            valid = ", ".join(s.value for s in cls)
            raise ValueError(f"unknown state {value!r} (valid: {valid})") from exc


@dataclass
class Screenshot:
    """A screenshot reference attached to a ticket.

    ``kind`` distinguishes the *creation* proof (what we want to build) from the
    *implementation* proof (the finished result), since the gates require each at a
    different lifecycle point. ``ref`` is a local path (pre-upload) or a hosted URL.
    """

    ref: str
    kind: str = "creation"  # creation | implementation
    caption: str = ""


@dataclass
class Attachment:
    """A backend-native attachment object read back from the tracker (Linear only, for now).

    Distinct from :class:`Screenshot`: a ``Screenshot`` is what THIS process asked to attach
    (a local ref or a hosted URL, part of the create/update request); an ``Attachment`` is what
    the backend actually has recorded, fetched on read (``TicketBackend.get``/``search``/…) so
    ``task read`` can show real durable proof instead of only the local ref embedded in the
    body. ``url`` is the backend's asset/link URL — for a Linear "native" attachment this is a
    ``uploads.linear.app`` asset URL, which requires the SAME ``Authorization`` header as any
    other Linear GraphQL call to fetch (see ``backends/linear.py:fetch_attachment_bytes``); it is
    NOT a bare public URL.
    """

    id: str
    title: str
    url: str
    subtitle: str = ""


@dataclass
class Criterion:
    """One acceptance criterion: its text, whether it is checked, and the proof for the check.

    A criterion may be CHECKED only with a *visual proof* (``proof`` = an image path/URL) — or,
    when a proof is genuinely impractical, with a recorded ``force_reason`` (audited). The
    ``check`` command and the on-done gate enforce this; ``render`` serializes the checkbox line
    (``- [x] text — proof: ![proof](ref)``) and round-trips it. Plain text constructs an
    unchecked criterion (see :meth:`Ticket.__post_init__`'s string coercion).
    """

    text: str
    checked: bool = False
    proof: str = ""  # visual-proof ref (image path/URL) backing a checked criterion
    force_reason: str = ""  # recorded justification when checked WITHOUT a visual proof


@dataclass
class Ticket:
    """A backend-agnostic ticket.

    The ``id`` is the backend's identifier (``#123`` for GitHub, ``HYP-456`` for Linear) and
    is empty for a not-yet-created ticket. Body content lives in the structured fields, NOT
    a single blob — ``render.py`` is the single place that serializes/parses the markdown
    body, so the fields are the source of truth and the body is derived.
    """

    title: str = ""
    what: str = ""
    why: str = ""  # motivation
    user_impact: str = ""
    cost_of_inaction: str = ""
    # Accepts plain strings at construction (coerced to unchecked Criterion in __post_init__) so
    # callers/tests can pass `["a", "b"]`; the stored value is always a list[Criterion].
    acceptance: list[Criterion] = field(default_factory=list)
    screenshots: list[Screenshot] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # URLs only, e.g. {"PR": "https://…", "Issue": "https://…"}
    skips: dict[str, str] = field(default_factory=dict)  # gate -> justification (escape hatch)
    state: State = State.TODO

    # populated only when the ticket exists in a backend
    id: str = ""
    url: str = ""
    raw_body: str = ""  # the verbatim body as the backend returned it (for read/lint)

    # Appended LAST (after the existing fields) on purpose: inserting it earlier would shift the
    # positional order of the dataclass, so any positional ``Ticket(..., State.X)`` call would
    # silently bind the wrong field. Keeping it trailing makes the addition order-safe.
    due: str = ""  # ISO date YYYY-MM-DD the daemon watches; "" = no due date

    # Also appended last (same reasoning as ``due`` above). Populated by the backend from its
    # native "last modified" timestamp (GitHub's ``updated_at``, Linear's ``updatedAt``) as a
    # UTC ISO8601 string; "" means the backend didn't supply one (e.g. a not-yet-created ticket,
    # or the FakeBackend in tests). Read by ``stale_notice.py`` (issue #59) to flag tickets with
    # no backend activity in N hours — never part of the rendered body (operational metadata,
    # not ticket content), so ``render``/``parse`` never touch it.
    updated_at: str = ""

    # Also appended last (same reasoning as ``due`` above). The backend-native identity of
    # whoever FILED the ticket (GitHub issue ``user.login``, Linear issue ``creator.id``); ""
    # means unknown (not-yet-created ticket, FakeBackend in a test that didn't set it, or a
    # backend that can't supply it). task-cli's own ``create()`` never sets a native *assignee*
    # on either backend, so an assignee-based filter would exclude virtually every ticket
    # task-cli itself creates — the reporter/creator is the field that actually identifies "the
    # user who ran task-cli" (issue #59's global stale nudge, review P2: scope the scan to the
    # current user's own tickets, not every ticket in a shared project). Never part of the
    # rendered body — operational metadata, not ticket content.
    reporter: str = ""

    # Also appended last (same reasoning as ``due`` above). Whether the backend's NATIVE
    # open/closed state is closed, independent of the normalized ``state`` above. For GitHub this
    # is ``row["state"] == "closed"``; for Linear it mirrors ``state`` itself (Linear's normalized
    # state IS derived straight from its native workflow state, so there is no separate signal to
    # capture — always consistent by construction). GitHub issues, by contrast, carry a managed
    # ``status:<state>`` LABEL that ``_derive_state`` prioritizes over the native open/closed
    # flag; an issue closed OUTSIDE task-cli (e.g. via the GitHub UI) can keep a stale
    # ``status:todo``/``status:in-progress`` label, so ``state`` alone can lie about whether the
    # ticket is still active. ``stale_notice.stale_tickets`` (issue #59, review P2) checks this
    # field directly rather than trusting the derived ``state`` for that one purpose. Never part
    # of the rendered body — operational metadata, not ticket content.
    provider_closed: bool = False

    # Also appended last (same reasoning as ``due`` above). Backend-native attachment objects
    # read back on ``get``/``list``/``search`` (Linear only, for now — see :class:`Attachment`).
    # Read-only: populated by the backend, never sent back on ``create``/``update`` (attaching
    # is its own op, ``TicketBackend.attach``), and never part of the rendered/parsed body — the
    # body's ``## Screenshots`` section is the local-ref request, this is the tracker's own
    # record of what actually landed there.
    attachments: list[Attachment] = field(default_factory=list)

    # Also appended last (same reasoning as ``due`` above). True when the backend's attachment
    # connection reported more pages than :attr:`attachments` fetched (Linear caps at 250 items
    # per request, not exhaustive cursor pagination — see backends/linear.py's `_ISSUE_FIELDS`
    # comment). Lets a caller (``cli._save_attachments``) surface "there are more, not all were
    # fetched" instead of silently presenting a partial list as the complete set.
    attachments_truncated: bool = False

    def __post_init__(self) -> None:
        # Coerce plain-string criteria to unchecked Criterion objects. This keeps construction
        # ergonomic (`Ticket(acceptance=["a", "b"])`) while the rest of the code — render, parse,
        # the gates, the `check` command — always works in terms of Criterion (text/checked/proof).
        self.acceptance = [c if isinstance(c, Criterion) else Criterion(text=str(c)) for c in self.acceptance]

    @property
    def is_ui(self) -> bool:
        """A UI/visual ticket triggers the screenshot gates (label-gated, see policy)."""
        ui_labels = {"ui", "visual"}
        return any(label.strip().lower() in ui_labels for label in self.labels)

    def unchecked_criteria(self) -> list[Criterion]:
        """The acceptance criteria still unchecked — what blocks a close (the on-done gate)."""
        return [c for c in self.acceptance if not c.checked]

    def session_label(self) -> str | None:
        """The ``session:<id>`` label if present, else ``None``."""
        for label in self.labels:
            if label.startswith("session:"):
                return label
        return None

    def due_date(self) -> "date | None":
        """The ``due`` field parsed to a :class:`datetime.date`, or ``None`` if unset/malformed.

        Tolerant of a malformed value (a hand-edited body): a non-ISO ``due`` yields ``None``
        rather than raising, so one bad ticket can never crash the daemon's selection loop.
        """
        from datetime import date as _date

        if not self.due:
            return None
        try:
            return _date.fromisoformat(self.due.strip())
        except ValueError:
            return None
