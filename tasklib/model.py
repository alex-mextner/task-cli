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
    acceptance: list[str] = field(default_factory=list)  # checkbox items (text only)
    screenshots: list[Screenshot] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # e.g. {"PR": "...", "Session": "session:abc"}
    skips: dict[str, str] = field(default_factory=dict)  # gate -> justification (escape hatch)
    state: State = State.TODO

    # populated only when the ticket exists in a backend
    id: str = ""
    url: str = ""
    raw_body: str = ""  # the verbatim body as the backend returned it (for read/lint)

    @property
    def is_ui(self) -> bool:
        """A UI/visual ticket triggers the screenshot gates (label-gated, see policy)."""
        ui_labels = {"ui", "visual"}
        return any(label.strip().lower() in ui_labels for label in self.labels)

    def acceptance_checkboxes(self) -> list[str]:
        """The acceptance criteria rendered as GitHub-flavored markdown checkbox lines."""
        return [f"- [ ] {item}" for item in self.acceptance]

    def session_label(self) -> str | None:
        """The ``session:<id>`` label if present, else ``None``."""
        for label in self.labels:
            if label.startswith("session:"):
                return label
        return None
