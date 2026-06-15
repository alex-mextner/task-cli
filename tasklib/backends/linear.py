"""Linear backend — calls the Linear GraphQL API directly (stdlib ``urllib``).

Per-repo backend (the hyperide repo → Linear/HYP). Linear has first-class workflow states,
so the normalized :class:`~tasklib.model.State` maps onto the team's workflow states by their
``type`` (``backlog``/``unstarted`` → todo, ``started`` → in-progress, ``done`` → done,
``canceled`` → cancelled), resolved once per backend from the team's state list.

The body is the §5 section template (``render.py``). Labels carry the ``session:<id>`` tag the
same way as GitHub. Credentials come from :mod:`tasklib.credentials`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..model import State, Ticket
from ..render import parse, render
from . import BackendError
from .http import HttpError, request_json

API_URL = "https://api.linear.app/graphql"

# Map a Linear workflow-state ``type`` onto our normalized State.
_TYPE_TO_STATE = {
    "backlog": State.TODO,
    "unstarted": State.TODO,
    "triage": State.TODO,
    "started": State.IN_PROGRESS,
    "completed": State.DONE,
    "canceled": State.CANCELLED,
}
# Which Linear state ``type`` to target when WE want to move to a normalized State.
_STATE_TO_TYPE = {
    State.TODO: "unstarted",
    State.IN_PROGRESS: "started",
    State.IN_REVIEW: "started",  # Linear has no native in-review type; closest is started
    State.DONE: "completed",
    State.CANCELLED: "canceled",
}


@dataclass
class LinearBackend:
    """GraphQL adapter. ``team_key`` (e.g. ``HYP``) scopes creation/listing."""

    api_key: str
    team_key: str
    project: str = ""
    name: str = "linear"
    _team_id: str = ""
    _states_by_type: dict[str, str] = field(default_factory=dict)  # type -> stateId
    _labels: dict[str, str] = field(default_factory=dict)  # name(lower) -> labelId

    @classmethod
    def from_config(cls, config, *, env: dict | None = None) -> "LinearBackend":
        from ..credentials import linear_key

        lin = config.section("linear")
        team = str(lin.get("team", "")).strip()
        if not team:
            raise BackendError("linear backend requires a team key (set linear.team in task.yaml)")
        creds = linear_key(env)
        return cls(api_key=creds.api_key, team_key=team, project=str(lin.get("project", "")))

    # ── GraphQL plumbing ──────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _gql(self, query: str, variables: dict | None = None):
        payload = {"query": query, "variables": variables or {}}
        try:
            result = request_json(API_URL, method="POST", headers=self._headers(), payload=payload)
        except HttpError as exc:
            raise BackendError(f"linear: {exc} {exc.body}".strip()) from exc
        if isinstance(result, dict) and result.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in result["errors"])
            raise BackendError(f"linear GraphQL error: {msgs}")
        return (result or {}).get("data", {})

    def _ensure_team(self) -> None:
        if self._team_id:
            return
        data = self._gql(
            "query($key:String!){teams(filter:{key:{eq:$key}}){nodes{id "
            "states{nodes{id name type}} labels{nodes{id name}}}}}",
            {"key": self.team_key},
        )
        nodes = data.get("teams", {}).get("nodes", [])
        if not nodes:
            raise BackendError(f"linear: no team with key {self.team_key!r}")
        team = nodes[0]
        self._team_id = team["id"]
        for st in team.get("states", {}).get("nodes", []):
            self._states_by_type.setdefault(st["type"], st["id"])
        for lbl in team.get("labels", {}).get("nodes", []):
            self._labels[lbl["name"].lower()] = lbl["id"]

    def _state_id_for(self, state: State) -> str:
        self._ensure_team()
        wanted = _STATE_TO_TYPE[state]
        sid = self._states_by_type.get(wanted)
        if sid:
            return sid
        # fall back to any todo-ish state so creation never hard-fails on an odd workflow
        for fallback in ("unstarted", "backlog", "started"):
            if fallback in self._states_by_type:
                return self._states_by_type[fallback]
        raise BackendError(f"linear: team {self.team_key} has no workflow state for {state.value}")

    def _label_ids(self, names: list[str]) -> list[str]:
        """Resolve label names → ids, CREATING any that don't exist yet.

        Unlike GitHub (which creates labels implicitly on issue create), Linear only accepts
        existing label ids. Session labels like ``session:<id>`` and ``needs-triage`` won't
        pre-exist, so silently dropping them would make ``task list`` (which lists by the
        session label) lose every newly created Linear ticket. We create the missing ones so
        the session label is durable end to end.
        """
        self._ensure_team()
        ids: list[str] = []
        for n in names:
            lid = self._labels.get(n.lower())
            if lid is None:
                lid = self._create_label(n)
            if lid:
                ids.append(lid)
        return ids

    def _create_label(self, name: str) -> str | None:
        """Create a team label and cache its id. Returns ``None`` if creation failed."""
        data = self._gql(
            "mutation($input:IssueLabelCreateInput!){issueLabelCreate(input:$input)"
            "{success issueLabel{id name}}}",
            {"input": {"teamId": self._team_id, "name": name}},
        )
        res = data.get("issueLabelCreate", {})
        label = res.get("issueLabel") if res.get("success") else None
        if label:
            self._labels[label["name"].lower()] = label["id"]
            return label["id"]
        return None

    def _node_to_ticket(self, node: dict) -> Ticket:
        labels = [lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])]
        st_type = (node.get("state") or {}).get("type", "unstarted")
        base = Ticket(
            title=node.get("title", ""),
            labels=labels,
            state=_TYPE_TO_STATE.get(st_type, State.TODO),
            id=node.get("identifier", ""),
            url=node.get("url", ""),
        )
        return parse(node.get("description") or "", base)

    _ISSUE_FIELDS = "id identifier url title description state{type name} labels{nodes{name}}"

    # ── protocol ──────────────────────────────────────────────────────────────────
    def create(self, ticket: Ticket) -> Ticket:
        self._ensure_team()
        variables = {
            "input": {
                "teamId": self._team_id,
                "title": ticket.title,
                "description": render(ticket),
                "stateId": self._state_id_for(ticket.state),
                "labelIds": self._label_ids(ticket.labels),
            }
        }
        if self.project:
            variables["input"]["projectId"] = self.project
        data = self._gql(
            "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{"
            + self._ISSUE_FIELDS
            + "}}}",
            variables,
        )
        res = data.get("issueCreate", {})
        if not res.get("success"):
            raise BackendError("linear: issueCreate returned success=false")
        return self._node_to_ticket(res["issue"])

    def _issue_by_identifier(self, ticket_id: str) -> dict:
        data = self._gql(
            "query($id:String!){issue(id:$id){" + self._ISSUE_FIELDS + "}}",
            {"id": ticket_id},
        )
        node = data.get("issue")
        if not node:
            raise BackendError(f"linear: no issue {ticket_id}")
        return node

    def get(self, ticket_id: str) -> Ticket:
        return self._node_to_ticket(self._issue_by_identifier(ticket_id))

    def update(self, ticket: Ticket) -> Ticket:
        node = self._issue_by_identifier(ticket.id)
        variables = {
            "id": node["id"],
            "input": {
                "title": ticket.title,
                "description": render(ticket),
                "stateId": self._state_id_for(ticket.state),
                "labelIds": self._label_ids(ticket.labels),
            },
        }
        data = self._gql(
            "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input)"
            "{success issue{" + self._ISSUE_FIELDS + "}}}",
            variables,
        )
        res = data.get("issueUpdate", {})
        if not res.get("success"):
            raise BackendError("linear: issueUpdate returned success=false")
        return self._node_to_ticket(res["issue"])

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        self._ensure_team()
        flt: dict = {"team": {"key": {"eq": self.team_key}}}
        if labels:
            flt["labels"] = {"some": {"name": {"in": labels}}}
        data = self._gql(
            "query($filter:IssueFilter,$n:Int){issues(filter:$filter,first:$n,"
            "orderBy:updatedAt){nodes{" + self._ISSUE_FIELDS + "}}}",
            {"filter": flt, "n": min(limit, 100)},
        )
        tickets = [self._node_to_ticket(n) for n in data.get("issues", {}).get("nodes", [])]
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        data = self._gql(
            "query($q:String!,$n:Int){searchIssues(term:$q,first:$n){nodes{"
            + self._ISSUE_FIELDS
            + "}}}",
            {"q": query, "n": min(limit, 100)},
        )
        nodes = data.get("searchIssues", {}).get("nodes", [])
        tickets = [self._node_to_ticket(n) for n in nodes]
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def comment(self, ticket_id: str, body: str) -> None:
        node = self._issue_by_identifier(ticket_id)
        self._gql(
            "mutation($input:CommentCreateInput!){commentCreate(input:$input){success}}",
            {"input": {"issueId": node["id"], "body": body}},
        )

    def attach(self, ticket_id: str, file_path: str) -> str:
        node = self._issue_by_identifier(ticket_id)
        # Linear file upload is a two-step signed-URL dance; for proof auditability we attach
        # the local path as an issue attachment link (the §4 'linear issue attach' intent).
        self._gql(
            "mutation($input:AttachmentCreateInput!){attachmentCreate(input:$input){success}}",
            {"input": {"issueId": node["id"], "title": "screenshot", "url": _file_url(file_path)}},
        )
        return file_path

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)


def _file_url(file_path: str) -> str:
    """Best-effort file ref for an attachment link. Absolute path → file:// URL."""
    if "://" in file_path:
        return file_path
    return "file://" + os.path.abspath(file_path)
