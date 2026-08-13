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

    def current_user(self) -> str | None:
        """Best-effort identity (login/id) of the authenticated caller, or ``None`` if it
        can't be determined. A filter hint only (issue #59's personal-scope stale-ticket nudge)
        — never an authorization boundary, and never raises; failures degrade to ``None``."""
        ...


class BackendError(RuntimeError):
    """A backend call failed (HTTP error, auth error, not-found, malformed response)."""


# The structured exit code for an ambiguous backend failure (task-cli bug: a create's response
# read failed AFTER the server already accepted it, so the crash looked identical to "nothing
# happened" and a retry could double-create a ticket). Hard-coded (not imported) so this package
# stays stdlib-only at import time — mirrors ``tasklib.transitions.EXIT_ILLEGAL_TRANSITION``,
# which pins its literal the same way.
#
# Deliberately NOT aliased to any ``agenttools_errors`` class, unlike ``EXIT_ILLEGAL_TRANSITION``
# (review finding): the shared contract's closest-sounding code, ``EXIT_NETWORK`` (7), documents
# itself as "a network/remote operation failed... DNS, timeout, 5xx" — the class a caller is
# TOLD is safe to retry. This exception means the exact opposite (the request may have already
# succeeded — do NOT blindly retry), so reusing that code would tell any exit-code-aware script
# to do precisely the double-create this exception exists to prevent. None of the shared
# contract's other classes (0-8, 127) fit "the outcome is unknown, verify before retrying"
# either, so this is a task-cli-local code with no upstream pin — the next free integer after the
# contract's highest assigned value (8). ``tests/test_backends.py`` asserts it collides with
# nothing in ``agenttools_errors.EXIT_CODES`` when that (optional, not installed here) library is
# present.
EXIT_AMBIGUOUS = 9


class AmbiguousBackendError(RuntimeError):
    """The backend call's outcome is UNKNOWN — the connection dropped while reading the
    response, after the provider already accepted the request (see
    :class:`tasklib.backends.http.AmbiguousHttpError`, which this wraps). The ticket may already
    exist server-side even though this process never saw it.

    Deliberately NOT a :class:`BackendError` subclass. Every ``except BackendError`` call site in
    ``cli.py`` treats that class as "a clean, known failure — nothing happened server-side" (a
    4xx, an auth error, a malformed config) and converts it uniformly into the same exit-2
    ``_UserError``. Subclassing ``BackendError`` would make those call sites silently swallow
    this into that same clean-refusal bucket — indistinguishable from "nothing happened" — which
    is exactly the ambiguity that let a retry double-create a ticket. A caller that must react to
    this specifically (``cmd_create``) catches it by name; everywhere else it propagates
    unhandled, same as before this class existed (no regression — those paths never handled the
    underlying read failure either).
    """

    exit_code: int = EXIT_AMBIGUOUS


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
