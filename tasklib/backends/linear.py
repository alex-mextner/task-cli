"""Linear backend — calls the Linear GraphQL API directly (stdlib ``urllib``).

Per-repo backend (the hyperide repo → Linear/HYP). Linear has first-class workflow states,
so the normalized :class:`~tasklib.model.State` maps onto the team's workflow states by their
``type`` (``backlog``/``unstarted`` → todo, ``started`` → in-progress, ``done`` → done,
``canceled`` → cancelled), resolved once per backend from the team's state list.

The body is the §5 section template (``render.py``). Labels carry the ``session:<id>`` tag the
same way as GitHub. Credentials come from :mod:`tasklib.credentials`.

## Attachments and the ``uploads.linear.app`` 401 (tg#11652)

A prior investigation (hyperide HYP-1237/HYP-1248) found that a RAW, UNAUTHENTICATED GET
against ``uploads.linear.app`` (the asset domain behind both a browser-pasted editor image AND
this backend's own uploaded attachments) returns ``401 {"error":"unauthorized","message":
"Please provide authorization header compatible with Linear GraphQL API..."}`` — verified live
here again against a real, freshly-uploaded attachment (scratch ticket HYP-1254/1255, deleted
after verification): a bare ``curl``/urllib GET 401s, and adding the SAME ``Authorization:
<api-key>`` header this backend already sends on every GraphQL call turns that into a clean
``200 image/png``. So the asset domain is NOT browser-session-scoped in the sense that matters
for an API consumer — it is auth-header-scoped, and this backend already holds a working header.
That is the actual "no 401" fix ``attachment_mode: native`` below relies on for both writing
(``attach``) and reading (``fetch_attachment_bytes``) an attachment's bytes.

What genuinely CANNOT be fixed this way — and is NOT what ``native`` mode does — is a raw
``![img](https://uploads.linear.app/...)`` markdown reference embedded directly in a ticket
BODY (the browser-paste case HYP-1248 actually hit): a third-party markdown renderer (GitHub,
Slack, a plain ``<img>`` tag) fetches that URL unauthenticated and always 401s, because it has
no way to attach Linear's API key to an ordinary image fetch. ``native`` mode never emits that
kind of raw body reference — it registers the upload as a proper ``Attachment`` object via
``attachmentCreate``, which Linear's OWN ticket UI renders through its own authenticated
fetch (same as any other attachment thumbnail), not a bare ``<img src>``. ``attachment_mode:
link`` (hyperide's default, HYP-1248) sidesteps the whole domain: it only ever registers a
URL the CALLER already hosted somewhere else (a GitHub PR asset, a PR's ``#screenshots``
anchor) — never a ``uploads.linear.app`` URL — and never re-uploads a local file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..model import Attachment, State, Ticket
from ..render import parse, render
from . import AmbiguousBackendError, BackendError
from .http import AmbiguousHttpError, HttpError, request_bytes, request_json

API_URL = "https://api.linear.app/graphql"
# The ONLY hosts `fetch_attachment_bytes` will send the Linear API key to (review finding: never
# attach auth to an arbitrary attachment URL — see that method's docstring).
_LINEAR_ASSET_HOSTS = frozenset({"uploads.linear.app"})


def _safe_urlparse(url: str):
    """``urlparse(url)``, or ``None`` on a malformed url. review finding: an attachment's ``url``
    is tracker-controlled data — a value like ``"https://[bad"`` (invalid IPv6-bracket syntax)
    makes ``urlparse`` itself raise ``ValueError``, which would otherwise escape every caller
    that treats URL classification as infallible (``is_native_attachment_url``,
    ``fetch_attachment_bytes``) and crash a command instead of cleanly refusing one bad
    attachment."""
    try:
        return urlparse(url)
    except ValueError:
        return None

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
    """GraphQL adapter. ``team_key`` (e.g. ``HYP``) scopes creation/listing.

    ``attachment_mode`` selects how :meth:`attach` handles a LOCAL file ref (already-hosted
    URLs are always registered as-is regardless of mode — see :meth:`attach`'s docstring):
    ``"native"`` uploads the bytes through Linear's own signed-URL flow; ``"link"`` refuses a
    bare local path (nothing to link to) and no-ops. See :data:`tasklib.config.VALID_ATTACHMENT_MODES`.
    """

    api_key: str
    team_key: str
    project: str = ""
    attachment_mode: str = "native"
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
        return cls(
            api_key=creds.api_key,
            team_key=team,
            project=str(lin.get("project", "")),
            attachment_mode=str(lin.get("attachment_mode", "native")),
        )

    # ── GraphQL plumbing ──────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _gql(self, query: str, variables: dict | None = None):
        payload = {"query": query, "variables": variables or {}}
        try:
            result = request_json(API_URL, method="POST", headers=self._headers(), payload=payload)
        except AmbiguousHttpError as exc:
            # see the matching comment in github_issues.py: this must stay its own, distinctly-
            # typed wrapper rather than falling through to the generic BackendError below.
            raise AmbiguousBackendError(f"linear: {exc}") from exc
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
        normalized_state = _TYPE_TO_STATE.get(st_type, State.TODO)
        # review finding: `node.get("attachments", {})`'s default only fires when the KEY is
        # absent — a key present with an explicit `null` (a possible, if unusual, GraphQL
        # response shape) returns None, and `.get("nodes")`/`.get("pageInfo")` on that would
        # raise AttributeError. `or {}` catches both "absent" and "present-but-null".
        attachments_node = node.get("attachments") or {}
        attachments = [
            Attachment(
                id=str(a.get("id", "")),
                title=str(a.get("title", "")),
                url=str(a.get("url", "")),
                subtitle=str(a.get("subtitle") or ""),
            )
            for a in (attachments_node.get("nodes") or [])
            if isinstance(a, dict)  # review finding: a null ENTRY inside `nodes` (same "unusual
            # but possible GraphQL shape" class the container-level `or {}` guards defend
            # against) would otherwise raise AttributeError on `a.get(...)`.
        ]
        base = Ticket(
            title=node.get("title", ""),
            labels=labels,
            state=normalized_state,
            id=node.get("identifier", ""),
            url=node.get("url", ""),
            # Seed the native dueDate so a ticket created/edited in the Linear UI (no body
            # section) still carries its due date; parse() lets the body's Due section override.
            due=str(node.get("dueDate") or "").strip(),
            updated_at=str(node.get("updatedAt") or ""),
            reporter=str((node.get("creator") or {}).get("id") or ""),
            # Unlike GitHub, Linear's normalized state IS derived straight from its native
            # workflow state (no separate managed label to lag behind it), so there is no
            # divergence to guard against — this simply mirrors the same derivation.
            provider_closed=normalized_state in (State.DONE, State.CANCELLED),
            attachments=attachments,
            # review finding: `first:250` is a cap, not exhaustive pagination — surface the
            # truncation explicitly (via `pageInfo.hasNextPage`) rather than silently presenting
            # a partial list as "all of the ticket's attachments". See `cli._save_attachments`.
            attachments_truncated=bool((attachments_node.get("pageInfo") or {}).get("hasNextPage")),
        )
        return parse(node.get("description") or "", base)

    _ISSUE_FIELDS_BASE = (
        "id identifier url title description dueDate updatedAt state{type name} labels{nodes{name}} creator{id}"
    )
    # review finding: Linear's `attachments` connection defaults to a 50-item page — an issue
    # with 51+ attachments would silently lose everything past the first page. `first: 250` is
    # not exhaustive cursor-based pagination (a genuinely unbounded connection would still need
    # `pageInfo`/`after`), but it is far beyond anything a screenshot/proof workflow produces on
    # one ticket, and is a one-line fix vs. the complexity of full pagination for this case.
    #
    # A SEPARATE field set from `_ISSUE_FIELDS_BASE` (review finding, 2nd round): attachments are
    # only requested for a SINGLE-ticket fetch (`get`/`update`/`transition`, via
    # `_issue_by_identifier`) — `list`/`search` (the bulk, N-tickets-at-once paths) use the base
    # set. Pulling up to 250 attachment sub-objects PER ticket into every `task list`/`find` call
    # would be a real payload/latency regression for a field almost no list consumer needs.
    _ISSUE_FIELDS = _ISSUE_FIELDS_BASE + " attachments(first:250){nodes{id title url subtitle} pageInfo{hasNextPage}}"

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
        if ticket.due.strip():
            # Mirror the body's Due section into the native dueDate (TimelessDate = YYYY-MM-DD)
            # so the date is visible in the Linear UI too. The body section stays the portable
            # source of truth; this is the best-effort native echo for the github-less backend.
            variables["input"]["dueDate"] = ticket.due.strip()
        if self.project:
            variables["input"]["projectId"] = self.project
        data = self._gql(
            # base fields — a just-created issue has no attachments yet, nothing to fetch.
            "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{"
            + self._ISSUE_FIELDS_BASE
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
                # Echo into the native dueDate on update too; an empty due clears it (null) so a
                # removed due date doesn't linger in the Linear UI out of sync with the body.
                "dueDate": ticket.due.strip() or None,
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
        if self.project:
            # When a project id is pinned, scope the list to it — otherwise two registry entries
            # for the same team but different projects would list the SAME team-wide issues, so
            # the cross-project grouped view would double-count them under each group.
            flt["project"] = {"id": {"eq": self.project}}
        data = self._gql(
            # base fields — the bulk N-tickets-at-once path; see _ISSUE_FIELDS's own comment.
            "query($filter:IssueFilter,$n:Int){issues(filter:$filter,first:$n,"
            "orderBy:updatedAt){nodes{" + self._ISSUE_FIELDS_BASE + " team{key} project{id}}}}",
            {"filter": flt, "n": min(limit, 100)},
        )
        tickets = [self._node_to_ticket(n) for n in data.get("issues", {}).get("nodes", [])]
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        # searchIssues has no team/project filter argument, so scope the results client-side to
        # THIS backend's team (and project, if pinned). Without this a cross-project `find` would
        # return workspace-wide hits and attribute the whole workspace to each Linear group.
        # NOTE: ``limit`` bounds the rows FETCHED from Linear; the team/project filter then runs
        # client-side, so the returned count can be < limit (matches are a subset of the fetch).
        data = self._gql(
            # base fields — same bulk-path reasoning as list().
            "query($q:String!,$n:Int){searchIssues(term:$q,first:$n){nodes{"
            + self._ISSUE_FIELDS_BASE
            + " team{key} project{id}}}}",
            {"q": query, "n": min(limit, 100)},
        )
        nodes = data.get("searchIssues", {}).get("nodes", [])
        nodes = [n for n in nodes if (n.get("team") or {}).get("key") == self.team_key]
        if self.project:
            nodes = [n for n in nodes if (n.get("project") or {}).get("id") == self.project]
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
        """Attach ``file_path`` (a local path OR an already-hosted URL) to ``ticket_id``.

        An already-hosted URL (``"://" in file_path`` — e.g. a GitHub PR asset URL, or a PR's
        ``#screenshots`` anchor) is ALWAYS registered as-is via ``attachmentCreate``, regardless
        of ``attachment_mode``: there is nothing to upload, and linking to an external resource
        the caller already hosts is exactly what both modes want. Only a bare LOCAL path
        branches on ``attachment_mode``:
        - ``"native"``: upload the bytes through Linear's own API (see
          :meth:`_upload_and_attach`) and register the result as a real attachment.
        - ``"link"``: a local path has no hosted URL to link to — raise :class:`BackendError`
          (the caller, ``_attach_screenshots``, already swallows this as best-effort so the
          local ref stays visible in the body even though it wasn't durably attached).
        """
        node = self._issue_by_identifier(ticket_id)
        issue_id = node["id"]
        if "://" in file_path:
            self._register_attachment(issue_id, title=os.path.basename(file_path) or "attachment", url=file_path)
            return file_path
        if self.attachment_mode == "link":
            raise BackendError(
                f"linear: attachment_mode=link cannot attach local path {file_path!r} "
                "(nothing hosted to link to) — pre-host the file and pass a URL, or switch to "
                "attachment_mode: native"
            )
        return self._upload_and_attach(issue_id, file_path)

    def _register_attachment(self, issue_id: str, *, title: str, url: str) -> None:
        data = self._gql(
            "mutation($input:AttachmentCreateInput!){attachmentCreate(input:$input){success}}",
            {"input": {"issueId": issue_id, "title": title, "url": url}},
        )
        # review finding: a `success: false` response was previously ignored, so `attach()`
        # reported the ref as durably attached (and, in native mode, left an orphaned uploaded
        # asset with nothing pointing at it) even when Linear rejected the registration.
        # `or {}` (not `.get(..., {})`): a PRESENT-but-null `attachmentCreate` must not raise a
        # bare AttributeError from `.get("success")` on None (review finding, same class as the
        # read-path `attachments`/`pageInfo` guards above).
        if not (data.get("attachmentCreate") or {}).get("success"):
            raise BackendError(f"linear: attachmentCreate returned success=false for issue {issue_id}")

    def _upload_and_attach(self, issue_id: str, file_path: str) -> str:
        """The real native-upload path: Linear's ``fileUpload`` mutation returns a short-lived
        signed PUT URL + the persistent ``assetUrl`` the bytes will live at; PUT the file to the
        signed URL with EXACTLY the headers Linear specified, then register ``assetUrl`` as a
        real attachment via :meth:`_register_attachment`. ``assetUrl`` lives on
        ``uploads.linear.app`` and 401s an unauthenticated fetch — see the module docstring —
        which is why :meth:`fetch_attachment_bytes` exists for the READ side.
        """
        # review finding: `_attach_screenshots` (cli.py) only swallows `BackendError`/
        # `AmbiguousBackendError` as best-effort — a bare OSError (missing file, permission
        # denied) from `getsize`/`open` would escape past that guard and crash a command whose
        # underlying create/update already succeeded. Wrap the whole local-file read window.
        try:
            content_type = _detect_content_type(file_path)
            with open(file_path, "rb") as f:
                body = f.read()
        except OSError as exc:
            raise BackendError(f"linear: cannot read {file_path} for upload: {exc}") from exc

        data = self._gql(
            "mutation($contentType:String!,$filename:String!,$size:Int!){"
            "fileUpload(contentType:$contentType,filename:$filename,size:$size){"
            "success uploadFile{assetUrl uploadUrl headers{key value}}}}",
            {"contentType": content_type, "filename": os.path.basename(file_path), "size": len(body)},
        )
        # `or {}`: a present-but-null `fileUpload` must not raise a bare AttributeError on the
        # `.get("success")` below (review finding, same class as the guards above).
        res = data.get("fileUpload") or {}
        # review finding: `success: true` does NOT guarantee a well-formed `uploadFile` — blindly
        # indexing `res["uploadFile"]["uploadUrl"]` etc. would raise a bare KeyError/TypeError
        # that escapes `_attach_screenshots`'s BackendError-only best-effort catch, crashing a
        # command whose create/update already succeeded. Validate the whole shape up front.
        upload_file = res.get("uploadFile") if res.get("success") else None
        if not isinstance(upload_file, dict) or not upload_file.get("uploadUrl") or not upload_file.get("assetUrl"):
            raise BackendError(f"linear: fileUpload returned a malformed response: {res!r}")
        put_headers = _parse_upload_headers(upload_file.get("headers"))
        if put_headers is None:
            raise BackendError(f"linear: fileUpload returned malformed upload headers: {upload_file.get('headers')!r}")
        put_headers["Content-Type"] = content_type
        try:
            # review finding: same_host_redirects_only=True here too, for consistency with
            # fetch_attachment_bytes — no Authorization header rides on this PUT (the signed URL
            # itself is the credential), but a compromised/misbehaving signed-URL response
            # redirecting the file BYTES to an attacker-controlled host is still worth refusing
            # (the screenshot content itself could be sensitive).
            request_bytes(
                upload_file["uploadUrl"], method="PUT", headers=put_headers, data=body, same_host_redirects_only=True
            )
        except AmbiguousHttpError as exc:
            raise AmbiguousBackendError(f"linear: upload PUT for {file_path}: {exc}") from exc
        except HttpError as exc:
            raise BackendError(f"linear: upload PUT for {file_path} failed: {exc} {exc.body}".strip()) from exc
        asset_url = upload_file["assetUrl"]
        self._register_attachment(issue_id, title=os.path.basename(file_path), url=asset_url)
        return asset_url

    def is_native_attachment_url(self, url: str) -> bool:
        """Whether ``url`` is one of THIS backend's own uploaded assets (``uploads.linear.app``,
        https only) — as opposed to an external URL a ``link``-mode attachment merely points at
        (a GitHub PR asset, say). Used by callers (``cli._save_attachments``) that want to fetch
        ONLY assets this process/tracker actually controls, not follow an arbitrary tracker-data
        URL onto the open network (SSRF surface — review finding)."""
        parsed = _safe_urlparse(url)
        if parsed is None or parsed.scheme != "https":
            return False
        try:
            # review finding: `.netloc` includes the port and preserves case — a genuine (if
            # unusual) asset url like `https://uploads.linear.app:443/...` or a differently-cased
            # host would compare unequal to the frozenset and get wrongly classified as
            # non-native (safe direction — under-authenticates rather than over-authenticates —
            # but still a real misclassification). `.hostname` is lowercased and port-stripped.
            # It's a lazily-computed property that can ITSELF raise ValueError on some malformed
            # inputs `urlparse()` alone doesn't catch — hence the same defensive try/except
            # `_safe_urlparse` already applies to the initial parse.
            hostname = parsed.hostname
        except ValueError:
            return False
        return hostname in _LINEAR_ASSET_HOSTS

    def fetch_attachment_bytes(self, url: str) -> bytes:
        """Read back an attachment's raw bytes — the READ half of ``native`` mode.

        A ``uploads.linear.app`` asset URL (whether pasted-in-editor or uploaded by
        :meth:`_upload_and_attach`) 401s an unauthenticated GET; this sends the SAME
        ``Authorization`` header every GraphQL call already uses, which is all it takes (see the
        module docstring — verified live, not theoretical).

        The ``Authorization`` header (the Linear API key) is attached ONLY when the URL's host
        is Linear's own asset domain (``_LINEAR_ASSET_HOSTS``) — review finding: this method
        takes an arbitrary attachment URL (which can be an external ``link``-mode URL, e.g. a
        GitHub PR asset), and sending the API key to a host this process doesn't control would
        leak it. An external URL is fetched plain/unauthenticated instead (works fine for a
        public GitHub asset; a private one is the caller's own auth problem, not this backend's).
        ``same_host_redirects_only=True`` additionally refuses a redirect off Linear's host mid
        request, so a compromised/misbehaving response can't redirect the authenticated request
        elsewhere. KNOWN TRADE-OFF (review finding): if Linear's asset host ever starts
        redirecting a GET to a DIFFERENT host (a CDN/object-store handoff, say), this guard
        would turn that into a clean ``HttpError`` instead of following it — observed behavior
        across many live verification runs during this feature's development was a direct
        ``200 image/png``, never a redirect, but that is not a guarantee about Linear's future
        behavior. Deliberately kept strict rather than loosened to "same-host-or-strip-auth":
        a fetch failing loudly is the correct failure mode for an auth-carrying request whose
        target moved unexpectedly, not a silent credential leak to a redirect target.
        """
        parsed = _safe_urlparse(url)
        # review finding: an attachment's `url` is tracker-controlled data, not a value this
        # process constructed — `urlopen` also understands `file:`/`ftp:`/`data:` schemes, so
        # without a scheme allowlist a `file:///etc/passwd`-shaped attachment url would read a
        # LOCAL file instead of fetching anything. Reject outright before any network/file call.
        # Also review finding: urlparse() itself can raise ValueError on a malformed url (e.g.
        # invalid IPv6-bracket syntax) — `_safe_urlparse` never raises, so THIS is where that
        # gets converted into the same clean BackendError as any other refusal.
        if parsed is None:
            raise BackendError(f"linear: refusing to fetch attachment with an unparseable url {url!r}")
        if parsed.scheme not in ("http", "https"):
            raise BackendError(f"linear: refusing to fetch attachment with scheme {parsed.scheme!r} ({url!r})")
        is_linear_asset = self.is_native_attachment_url(url)
        headers = self._headers() if is_linear_asset else {}
        try:
            return request_bytes(url, method="GET", headers=headers, same_host_redirects_only=is_linear_asset)
        except AmbiguousHttpError as exc:
            raise AmbiguousBackendError(f"linear: fetch attachment {url}: {exc}") from exc
        except HttpError as exc:
            raise BackendError(f"linear: fetch attachment {url} failed: {exc} {exc.body}".strip()) from exc

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)

    def current_user(self) -> str | None:
        """Best-effort id of this API key's owner (issue #59's stale-nudge personal-scope
        filter). ``None`` on any failure — never an authorization boundary, just a filter hint;
        the caller (``cli.py``) falls back to an unfiltered scan when it can't be determined."""
        try:
            data = self._gql("query{viewer{id}}")
        except BackendError:
            return None
        if not isinstance(data, dict):
            return None
        viewer = data.get("viewer")
        viewer_id = viewer.get("id") if isinstance(viewer, dict) else None
        return viewer_id if isinstance(viewer_id, str) and viewer_id else None


_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
}


def _detect_content_type(file_path: str) -> str:
    """The MIME type for a local file, by extension (falls back to
    ``application/octet-stream``) — ``fileUpload`` requires it up front.

    Deliberately does NOT also return a size via a separate ``os.path.getsize`` call (review
    finding, TOCTOU): the caller reads the file's bytes anyway, and ``len(body)`` from that SAME
    read is the authoritative size — a second, independent stat can observe a DIFFERENT size if
    the file changes between the two syscalls, and Linear's signed PUT URL is scoped to an exact
    ``x-goog-content-length-range`` from whatever size ``fileUpload`` was told, so a stale stat
    would just fail the PUT with a range mismatch rather than silently upload wrong bytes.
    """
    ext = os.path.splitext(file_path)[1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _parse_upload_headers(raw) -> dict[str, str] | None:
    """Validate ``fileUpload``'s ``uploadFile.headers`` shape (a list of ``{key, value}``
    objects — Linear's own signed-URL headers, e.g. ``x-goog-content-length-range``) into a
    plain ``dict``. Returns ``None`` on ANY malformed shape (missing/None, not a list, an entry
    that isn't a dict, or missing/non-string ``key``/``value``) — review finding: the previous
    dict-comprehension blindly indexed ``h["key"]``/``h["value"]`` and iterated whatever
    ``.get("headers", [])`` returned, so a ``None`` headers value (a key present but null, which
    ``dict.get``'s default does NOT catch) or a malformed entry raised a raw TypeError/KeyError
    past ``_attach_screenshots``'s ``BackendError``-only best-effort catch. An EMPTY list is
    valid (some upload targets need no extra headers) and returns ``{}``, not ``None``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, list):
        return None
    headers: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        key, value = entry.get("key"), entry.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        headers[key] = value
    return headers
