"""GitHub Issues backend — calls the GitHub REST API directly (stdlib ``urllib``).

The default backend (§1). State is mapped onto GitHub's open/closed + a small set of managed
``status:<state>`` labels, since GitHub issues have only two native states. The body is the
§5 section template (``render.py``), so a GitHub issue and its PR speak one shape.

``repo`` defaults to ``auto`` = the ``origin`` owner/name resolved from git. Credentials come
from :mod:`tasklib.credentials` (never re-prompted, never logged).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ..model import State, Ticket
from ..render import parse, render
from . import BackendError
from .http import HttpError, request_json

API_ROOT = "https://api.github.com"

# GitHub issues are open/closed; we carry the finer lifecycle as managed labels.
_STATE_LABEL = {
    State.TODO: "status:todo",
    State.IN_PROGRESS: "status:in-progress",
    State.IN_REVIEW: "status:in-review",
    State.DONE: "status:done",
    State.CANCELLED: "status:cancelled",
}
_LABEL_STATE = {v: k for k, v in _STATE_LABEL.items()}


@dataclass
class GitHubIssuesBackend:
    """REST adapter. ``owner``/``repo`` target the issues; ``token`` authenticates."""

    owner: str
    repo: str
    token: str
    default_labels: tuple[str, ...] = ()
    name: str = "github-issues"

    # ── construction ──────────────────────────────────────────────────────────────
    @classmethod
    def from_config(cls, config, *, env: dict | None = None) -> "GitHubIssuesBackend":
        from ..credentials import github_token

        gh = config.section("github")
        repo_spec = str(gh.get("repo", "auto"))
        owner, repo = _resolve_repo(repo_spec, config.repo_root)
        creds = github_token(env)
        labels = tuple(str(x) for x in (gh.get("default_labels") or []))
        return cls(owner=owner, repo=repo, token=creds.token, default_labels=labels)

    # ── helpers ───────────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _issues_url(self, suffix: str = "") -> str:
        return f"{API_ROOT}/repos/{self.owner}/{self.repo}/issues{suffix}"

    def _call(self, url: str, *, method: str = "GET", payload=None):
        try:
            return request_json(url, method=method, headers=self._headers(), payload=payload)
        except HttpError as exc:
            raise BackendError(f"github: {exc} {exc.body}".strip()) from exc

    def _all_labels(self, ticket: Ticket) -> list[str]:
        labels = list(dict.fromkeys([*self.default_labels, *ticket.labels]))
        labels.append(_STATE_LABEL[ticket.state])
        return list(dict.fromkeys(labels))

    def _row_to_ticket(self, row: dict) -> Ticket:
        labels = [lbl["name"] for lbl in row.get("labels", []) if isinstance(lbl, dict)]
        state = _derive_state(row, labels)
        base = Ticket(
            title=row.get("title", ""),
            labels=[lbl for lbl in labels if not lbl.startswith("status:")],
            state=state,
            id=f"#{row.get('number')}",
            url=row.get("html_url", ""),
        )
        body = row.get("body") or ""
        return parse(body, base)

    # ── protocol ──────────────────────────────────────────────────────────────────
    def create(self, ticket: Ticket) -> Ticket:
        payload = {"title": ticket.title, "body": render(ticket), "labels": self._all_labels(ticket)}
        row = self._call(self._issues_url(), method="POST", payload=payload)
        return self._row_to_ticket(row)

    def get(self, ticket_id: str) -> Ticket:
        num = _issue_number(ticket_id)
        row = self._call(self._issues_url(f"/{num}"))
        return self._row_to_ticket(row)

    def update(self, ticket: Ticket) -> Ticket:
        num = _issue_number(ticket.id)
        payload = {
            "title": ticket.title,
            "body": render(ticket),
            "labels": self._all_labels(ticket),
            "state": "closed" if ticket.state in (State.DONE, State.CANCELLED) else "open",
        }
        row = self._call(self._issues_url(f"/{num}"), method="PATCH", payload=payload)
        return self._row_to_ticket(row)

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        params = [f"per_page={min(limit, 100)}", "state=all"]
        if labels:
            params.append("labels=" + ",".join(labels))
        rows = self._call(self._issues_url("?" + "&".join(params))) or []
        tickets = [self._row_to_ticket(r) for r in rows if "pull_request" not in r]
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        import urllib.parse

        q = f"repo:{self.owner}/{self.repo} is:issue {query}"
        if state in (State.DONE, State.CANCELLED):
            q += " is:closed"
        elif state is not None:
            q += " is:open"
        url = f"{API_ROOT}/search/issues?q={urllib.parse.quote(q)}&per_page={min(limit, 100)}"
        result = self._call(url) or {}
        rows = result.get("items", []) if isinstance(result, dict) else []
        tickets = [self._row_to_ticket(r) for r in rows]
        # GitHub search only knows is:open/is:closed; our finer states (in-progress/in-review)
        # live in labels, so post-filter client-side exactly like list() and the Linear backend
        # — otherwise `find --state in-progress` would return every open issue.
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def comment(self, ticket_id: str, body: str) -> None:
        num = _issue_number(ticket_id)
        self._call(self._issues_url(f"/{num}/comments"), method="POST", payload={"body": body})

    def attach(self, ticket_id: str, file_path: str) -> str:
        # GitHub has no issue-attachment REST endpoint; the upload path is the web UI only.
        # We record the local path as a reference comment so the proof is auditable. The
        # body's Screenshots section embeds the same ref via render().
        self.comment(ticket_id, f"screenshot: {file_path}")
        return file_path

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)


# ── module helpers ──────────────────────────────────────────────────────────────────


def _issue_number(ticket_id: str) -> str:
    # removeprefix, NOT lstrip("#") — lstrip strips ALL leading '#' (e.g. "##1" -> "1"),
    # mangling an id; removeprefix drops exactly one leading marker.
    return ticket_id.removeprefix("#").strip()


def _derive_state(row: dict, labels: list[str]) -> State:
    for lbl in labels:
        if lbl in _LABEL_STATE:
            return _LABEL_STATE[lbl]
    return State.DONE if row.get("state") == "closed" else State.TODO


def _resolve_repo(repo_spec: str, repo_root) -> tuple[str, str]:
    """Resolve ``owner/repo``. ``auto`` reads the git ``origin`` remote of ``repo_root``."""
    if repo_spec and repo_spec != "auto":
        if "/" not in repo_spec:
            raise BackendError(f"github.repo must be 'owner/name' or 'auto', got {repo_spec!r}")
        owner, repo = repo_spec.split("/", 1)
        return owner, repo
    return _git_origin_owner_repo(repo_root)


def _git_origin_owner_repo(repo_root) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError("cannot resolve github repo: git not available") from exc
    if out.returncode != 0:
        raise BackendError("cannot resolve github repo: no 'origin' remote (set github.repo in task.yaml)")
    url = out.stdout.strip()
    return _parse_remote(url)


def _parse_remote(url: str) -> tuple[str, str]:
    """Parse ``owner/repo`` from an https or ssh GitHub remote URL."""
    cleaned = url.removesuffix(".git")
    if cleaned.startswith("git@") and ":" in cleaned:
        path = cleaned.split(":", 1)[1]
    elif "github.com/" in cleaned:
        path = cleaned.split("github.com/", 1)[1]
    else:
        raise BackendError(f"unrecognized github remote URL: {url}")
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise BackendError(f"cannot parse owner/repo from remote: {url}")
    return parts[0], parts[1]
