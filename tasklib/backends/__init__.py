"""Backend abstraction — the ``TicketBackend`` protocol + the adapter selector.

Adapters call the provider API **directly** (stdlib ``urllib``, no per-call subprocess):
``github_issues`` → GitHub REST, ``linear`` → Linear GraphQL. The rest of the tool never
touches a provider; it speaks only this protocol, so a fake backend (tests) is a drop-in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model import State, Ticket


@runtime_checkable
class TicketBackend(Protocol):
    """The contract every backend implements. Methods raise ``BackendError`` on failure."""

    name: str

    def create(self, ticket: Ticket) -> Ticket:
        """Create a ticket from a fully-populated, policy-passed ``Ticket``. Returns it with
        ``id``/``url`` filled in."""
        ...

    def get(self, ticket_id: str) -> Ticket:
        """Fetch a single ticket by id, body parsed back into structured fields."""
        ...

    def update(self, ticket: Ticket) -> Ticket:
        """Update an existing ticket (``ticket.id`` set). Returns the updated ``Ticket``."""
        ...

    def list(self, *, labels: list[str] | None = None, state: State | None = None, limit: int = 30) -> list[Ticket]:
        """List tickets, optionally filtered by labels/state."""
        ...

    def search(self, query: str, *, state: State | None = None, limit: int = 30) -> list[Ticket]:
        """Full-text search over title+body."""
        ...

    def comment(self, ticket_id: str, body: str) -> None:
        """Append a comment to a ticket."""
        ...

    def attach(self, ticket_id: str, file_path: str) -> str:
        """Attach a file (e.g. a screenshot). Returns a reference (URL or marker)."""
        ...

    def transition(self, ticket_id: str, state: State) -> Ticket:
        """Move a ticket to a new normalized state."""
        ...

    def session_tickets(self, session_label: str, *, limit: int = 30) -> list[Ticket]:
        """List tickets carrying a ``session:<id>`` label (the backend-side session view)."""
        ...


class BackendError(RuntimeError):
    """A backend call failed (HTTP error, auth error, not-found, malformed response)."""


def get_backend(config, *, env: dict | None = None):
    """Construct the configured backend from a ``LoadedConfig``. Effectful (harvests creds).

    Imported lazily by the entrypoint; importing this package must stay dependency-light.
    """
    backend = config.backend
    if backend == "github-issues":
        from .github_issues import GitHubIssuesBackend

        return GitHubIssuesBackend.from_config(config, env=env)
    if backend == "linear":
        from .linear import LinearBackend

        return LinearBackend.from_config(config, env=env)
    raise BackendError(f"unknown backend {backend!r}")
