"""Shared fixtures — a FAKE in-memory backend so tests never hit live GitHub/Linear."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the repo root without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasklib.backends import TicketBackend  # noqa: E402
from tasklib.model import State, Ticket  # noqa: E402
from tasklib.render import parse, render  # noqa: E402


class FakeBackend:
    """An in-memory ``TicketBackend`` — round-trips through render/parse like a real one.

    Implements the full protocol so it is a structural drop-in. ``created`` records the
    sequence of created tickets for assertions; ``comments`` records appended comments.
    """

    name = "fake"

    def __init__(self) -> None:
        self._store: dict[str, Ticket] = {}
        self._next = 1
        self.comments: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []

    def create(self, ticket: Ticket) -> Ticket:
        tid = f"#{self._next}"
        self._next += 1
        # round-trip through the body to mirror a real backend (body is the source of truth)
        stored = parse(render(ticket), ticket)
        stored.id = tid
        stored.url = f"https://fake/{tid.lstrip('#')}"
        stored.labels = list(ticket.labels)
        stored.state = ticket.state
        self._store[tid] = stored
        return stored

    def get(self, ticket_id: str) -> Ticket:
        if ticket_id not in self._store:
            from tasklib.backends import BackendError

            raise BackendError(f"fake: no ticket {ticket_id}")
        return self._store[ticket_id]

    def update(self, ticket: Ticket) -> Ticket:
        stored = parse(render(ticket), ticket)
        stored.id = ticket.id
        stored.url = f"https://fake/{ticket.id.lstrip('#')}"
        stored.labels = list(ticket.labels)
        stored.state = ticket.state
        self._store[ticket.id] = stored
        return stored

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        out = list(self._store.values())
        if labels:
            wanted = set(labels)
            out = [t for t in out if wanted & set(t.labels)]
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        q = query.lower()
        out = [t for t in self._store.values() if q in t.title.lower() or q in render(t).lower()]
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def comment(self, ticket_id: str, body: str) -> None:
        self.get(ticket_id)  # raises if missing
        self.comments.append((ticket_id, body))

    def attach(self, ticket_id: str, file_path: str) -> str:
        self.attachments.append((ticket_id, file_path))
        return file_path

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect the sidecar + config dirs into tmp so tests never touch the real HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # neutralize session detection ambient noise unless a test sets it
    monkeypatch.delenv("TASK_SESSION", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    return tmp_path


def assert_protocol(backend) -> None:
    """A FakeBackend must satisfy the runtime-checkable protocol."""
    assert isinstance(backend, TicketBackend)
