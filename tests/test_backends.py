"""Backend protocol — the FakeBackend satisfies it and round-trips correctly.

Also covers the GitHub remote-URL parser (pure) without hitting the network.
"""

from __future__ import annotations

import pytest

from tasklib.backends import AmbiguousBackendError, BackendError, EXIT_AMBIGUOUS
from tasklib.backends.github_issues import GitHubIssuesBackend, _api_root, _parse_remote
from tasklib.backends.http import AmbiguousHttpError
from tasklib.model import State, Ticket

from .conftest import assert_protocol


def _ticket() -> Ticket:
    return Ticket(
        title="thing",
        what="do the thing",
        why="reasons",
        user_impact="users",
        cost_of_inaction="pain",
        acceptance=["works"],
        labels=["session:s1", "agent"],
    )


def test_fake_satisfies_protocol(fake_backend):
    assert_protocol(fake_backend)


def test_create_assigns_id_and_url(fake_backend):
    created = fake_backend.create(_ticket())
    assert created.id == "#1"
    assert created.url.endswith("/1")


def test_get_round_trips_body(fake_backend):
    created = fake_backend.create(_ticket())
    fetched = fake_backend.get(created.id)
    assert fetched.what == "do the thing"
    assert [c.text for c in fetched.acceptance] == ["works"]


def test_update_changes_state(fake_backend):
    created = fake_backend.create(_ticket())
    created.state = State.IN_PROGRESS
    updated = fake_backend.update(created)
    assert updated.state == State.IN_PROGRESS


def test_transition(fake_backend):
    created = fake_backend.create(_ticket())
    done = fake_backend.transition(created.id, State.DONE)
    assert done.state == State.DONE


def test_session_tickets_filters_by_label(fake_backend):
    fake_backend.create(_ticket())
    other = _ticket()
    other.labels = ["session:s2"]
    fake_backend.create(other)
    assert len(fake_backend.session_tickets("session:s1")) == 1


def test_search_matches_title(fake_backend):
    fake_backend.create(_ticket())
    assert len(fake_backend.search("thing")) == 1
    assert len(fake_backend.search("nonexistent")) == 0


def test_comment_on_missing_ticket_raises(fake_backend):
    with pytest.raises(BackendError):
        fake_backend.comment("#999", "hi")


def test_attach_records(fake_backend):
    created = fake_backend.create(_ticket())
    ref = fake_backend.attach(created.id, "shot.png")
    assert ref == "shot.png"
    assert fake_backend.attachments == [(created.id, "shot.png")]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/alex-mextner/task-cli.git", ("alex-mextner", "task-cli")),
        ("https://github.com/alex-mextner/task-cli", ("alex-mextner", "task-cli")),
        ("git@github.com:alex-mextner/task-cli.git", ("alex-mextner", "task-cli")),
    ],
)
def test_parse_github_remote(url, expected):
    assert _parse_remote(url) == expected


def test_parse_github_remote_rejects_garbage():
    with pytest.raises(BackendError):
        _parse_remote("https://gitlab.com/x/y.git")


def test_api_root_defaults_to_public_github(monkeypatch):
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert _api_root() == "https://api.github.com"


def test_api_root_honors_loopback_http_mock(monkeypatch):
    # A hermetic mock server on loopback may be http; the trailing slash is normalized.
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:8771/")
    assert _api_root() == "http://127.0.0.1:8771"


def test_api_root_allows_https_enterprise_host(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    assert _api_root() == "https://ghe.example.com/api/v3"


def test_api_root_rejects_cleartext_nonloopback(monkeypatch):
    # Security: the bearer token rides every request, so a non-loopback http host is refused —
    # an ambient GITHUB_API_URL must not be able to exfiltrate the token over cleartext.
    monkeypatch.setenv("GITHUB_API_URL", "http://ghe.internal/api/v3")
    with pytest.raises(BackendError):
        _api_root()


def test_issues_url_uses_api_root_override(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    assert be._issues_url() == "https://ghe.example.com/api/v3/repos/o/r/issues"
    assert be._issues_url("/1") == "https://ghe.example.com/api/v3/repos/o/r/issues/1"


def test_search_url_uses_api_root_override(monkeypatch):
    # The search() URL is built from _api_root() too; assert the override flows through it.
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    captured = {}

    def fake_call(url, **kw):
        captured["url"] = url
        return {"items": []}

    monkeypatch.setattr(be, "_call", fake_call)
    be.search("hello")
    assert captured["url"].startswith("https://ghe.example.com/api/v3/search/issues?")


def test_github_search_post_filters_by_state(monkeypatch):
    # the GitHub Search API only knows is:open/is:closed; finer states live in labels, so
    # search() must post-filter client-side. Stub _call so no network is touched.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    rows = {
        "items": [
            {"number": 1, "title": "a", "state": "open", "labels": [{"name": "status:in-progress"}]},
            {"number": 2, "title": "b", "state": "open", "labels": [{"name": "status:todo"}]},
        ]
    }
    monkeypatch.setattr(be, "_call", lambda url, **kw: rows)
    out = be.search("x", state=State.IN_PROGRESS)
    assert [t.id for t in out] == ["#1"]  # only the in-progress one survives the post-filter


def test_github_attach_posts_reference_comment(monkeypatch):
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    calls = []
    monkeypatch.setattr(be, "_call", lambda url, **kw: calls.append((url, kw)) or {})
    ref = be.attach("#7", "shot.png")
    assert ref == "shot.png"
    assert any("/7/comments" in url for url, _ in calls)


def test_github_row_to_ticket_maps_updated_at(monkeypatch):
    # issue #59's global stale-ticket nudge reads Ticket.updated_at; GitHub's REST rows carry
    # this natively as `updated_at`, so _row_to_ticket must pass it through.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {
        "number": 1,
        "title": "a",
        "state": "open",
        "labels": [],
        "body": "",
        "html_url": "https://github.com/o/r/issues/1",
        "updated_at": "2026-07-01T12:00:00Z",
    }
    ticket = be._row_to_ticket(row)
    assert ticket.updated_at == "2026-07-01T12:00:00Z"


def test_github_row_to_ticket_defaults_updated_at_to_empty():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).updated_at == ""


def test_github_row_to_ticket_maps_reporter_from_issue_author():
    # issue #59 stale-nudge, review P2: the scan must scope to the current user's OWN tickets.
    # GitHub's row carries the issue author as `user.login` — task-cli's own `create()` never
    # sets a native assignee, so the AUTHOR (not assignee) is the field that actually identifies
    # "the user who filed this via task-cli".
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": "", "user": {"login": "alex"}}
    assert be._row_to_ticket(row).reporter == "alex"


def test_github_row_to_ticket_reporter_defaults_to_empty_when_missing():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).reporter == ""


def test_github_row_to_ticket_marks_provider_closed_when_label_lags_native_state():
    # P2 (review): an issue closed OUTSIDE task-cli can keep a stale `status:todo` label —
    # `_derive_state` still reports `state=TODO` (label wins), so `provider_closed` must be the
    # separate, always-truthful signal the stale-ticket nudge checks instead.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "closed", "labels": [{"name": "status:todo"}], "body": ""}
    ticket = be._row_to_ticket(row)
    assert ticket.state.value == "todo"  # unchanged — label still wins for the derived state
    assert ticket.provider_closed is True


def test_github_row_to_ticket_provider_closed_false_when_open():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).provider_closed is False


def test_github_current_user_returns_login(monkeypatch):
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    seen = {}

    def fake_call(url, **kw):
        seen["url"] = url
        return {"login": "alex"}

    monkeypatch.setattr(be, "_call", fake_call)
    assert be.current_user() == "alex"
    assert seen["url"].endswith("/user")


def test_github_current_user_returns_none_on_backend_error(monkeypatch):
    from tasklib.backends import BackendError
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")

    def raise_err(url, **kw):
        raise BackendError("boom")

    monkeypatch.setattr(be, "_call", raise_err)
    assert be.current_user() is None


def test_issue_number_removeprefix_not_lstrip():
    from tasklib.backends.github_issues import _issue_number

    assert _issue_number("#1") == "1"
    assert _issue_number("##1") == "#1"  # removeprefix drops exactly one '#', not all of them


def test_linear_label_ids_creates_missing_labels(monkeypatch):
    # session:<id> and needs-triage won't pre-exist in a Linear team; _label_ids must CREATE
    # them (not silently drop) so the durable session label survives — else `task list` breaks.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._labels = {"ui": "lbl-ui"}  # ui exists; the session label does not
    created: list[str] = []

    def fake_gql(query, variables=None):
        if "issueLabelCreate" in query:
            name = variables["input"]["name"]
            created.append(name)
            return {"issueLabelCreate": {"success": True, "issueLabel": {"id": f"new-{name}", "name": name}}}
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    ids = be._label_ids(["ui", "session:abc", "needs-triage"])
    assert "lbl-ui" in ids
    assert "new-session:abc" in ids and "new-needs-triage" in ids
    assert created == ["session:abc", "needs-triage"]  # only the missing ones were created


def test_linear_node_to_ticket_seeds_native_due_date(monkeypatch):
    # a ticket created/edited in the Linear UI carries dueDate natively (no body Due section);
    # _node_to_ticket must seed Ticket.due from it so the daemon still sees the date.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "native due",
        "url": "https://linear/HYP-7",
        "description": "",  # no body Due section
        "dueDate": "2026-09-01",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    ticket = be._node_to_ticket(node)
    assert ticket.due == "2026-09-01"


def test_linear_node_to_ticket_maps_updated_at():
    # issue #59's global stale-ticket nudge reads Ticket.updated_at; Linear's node carries this
    # natively as `updatedAt`, so _node_to_ticket must pass it through.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "2026-07-01T12:00:00.000Z",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).updated_at == "2026-07-01T12:00:00.000Z"


def test_linear_list_query_requests_updated_at(monkeypatch):
    # test_linear_node_to_ticket_maps_updated_at above injects `updatedAt` directly into a hand
    # built node, so it would keep passing even if `_ISSUE_FIELDS` stopped requesting the field
    # from Linear — silently losing every Linear stale-ticket warning in production. Drive the
    # real list() query so a dropped `updatedAt` in _ISSUE_FIELDS breaks a test, not just prod.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {
            "issues": {
                "nodes": [
                    {
                        "identifier": "HYP-7",
                        "title": "t",
                        "url": "https://linear/HYP-7",
                        "description": "",
                        "dueDate": None,
                        "updatedAt": "2026-07-01T12:00:00.000Z",
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    }
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    tickets = be.list()
    assert "updatedAt" in seen["query"]
    assert tickets[0].updated_at == "2026-07-01T12:00:00.000Z"


def test_linear_body_due_overrides_native_due_date():
    # when the body carries a ## Due section, it is the source of truth — it overrides the native
    # dueDate (a divergence shouldn't silently prefer the native field)
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket
    from tasklib.render import render

    be = LinearBackend(api_key="k", team_key="HYP")
    body = render(Ticket(title="t", what="w", due="2026-10-10"))  # body Due = 2026-10-10
    node = {
        "identifier": "HYP-8",
        "title": "t",
        "url": "u",
        "description": body,
        "dueDate": "2026-01-01",  # native says something else
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).due == "2026-10-10"


def test_linear_update_clears_native_due_when_empty(monkeypatch):
    # clearing the due date (empty) must send dueDate: null so the Linear UI stays in sync
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._states_by_type = {"unstarted": "st-1"}
    seen: dict = {}

    def fake_gql(query, variables=None):
        if "issue(id" in query:
            return {"issue": {"id": "node-1", "identifier": "HYP-3"}}
        if "issueUpdate" in query:
            seen["input"] = variables["input"]
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {
                        "identifier": "HYP-3",
                        "title": "t",
                        "url": "u",
                        "description": variables["input"]["description"],
                        "dueDate": None,
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    },
                }
            }
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    monkeypatch.setattr(be, "_label_ids", lambda names: [])
    be.update(Ticket(id="HYP-3", title="t", what="w", due=""))  # cleared
    assert seen["input"]["dueDate"] is None


def test_linear_create_mirrors_due_into_native_field(monkeypatch):
    # the body Due section is the portable source of truth; create also echoes it into the
    # native dueDate so it shows in the Linear UI.
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._states_by_type = {"unstarted": "st-1"}
    seen: dict = {}

    def fake_gql(query, variables=None):
        if "issueCreate" in query:
            seen["input"] = variables["input"]
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "identifier": "HYP-9",
                        "title": "t",
                        "url": "u",
                        "description": variables["input"]["description"],
                        "dueDate": variables["input"].get("dueDate"),
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    },
                }
            }
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    monkeypatch.setattr(be, "_label_ids", lambda names: [])
    be.create(Ticket(title="t", what="w", due="2026-09-01"))
    assert seen["input"]["dueDate"] == "2026-09-01"


def test_linear_list_scopes_filter_to_team_and_project(monkeypatch):
    # the IssueFilter must carry the team, and the project too when pinned — otherwise two
    # registry entries on the same team but different projects would list identical issues.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", project="proj-9")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["filter"] = variables["filter"]
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    be.list()
    assert seen["filter"]["team"] == {"key": {"eq": "HYP"}}
    assert seen["filter"]["project"] == {"id": {"eq": "proj-9"}}


def test_linear_search_filters_results_to_team(monkeypatch):
    # searchIssues is workspace-wide; the backend must drop hits from other teams so a
    # cross-project `find` doesn't attribute the whole workspace to this Linear group.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    def fake_gql(query, variables=None):
        return {
            "searchIssues": {
                "nodes": [
                    {"identifier": "HYP-1", "title": "mine", "team": {"key": "HYP"}, "project": None,
                     "state": {"type": "started"}, "labels": {"nodes": []}},
                    {"identifier": "OTH-9", "title": "theirs", "team": {"key": "OTH"}, "project": None,
                     "state": {"type": "started"}, "labels": {"nodes": []}},
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    hits = be.search("anything")
    ids = {t.id for t in hits}
    assert ids == {"HYP-1"}  # the OTH team's hit is scoped out


def test_linear_node_to_ticket_maps_reporter_from_creator():
    # issue #59 stale-nudge, review P2: Linear's node carries the issue creator as `creator.id` —
    # task-cli's own `create()` never sets a native assignee, so the creator identifies "the user
    # who filed this via task-cli".
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
        "creator": {"id": "user-123"},
    }
    assert be._node_to_ticket(node).reporter == "user-123"


def test_linear_node_to_ticket_reporter_defaults_to_empty_when_missing():
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).reporter == ""


def test_linear_list_query_requests_creator(monkeypatch):
    # mirrors test_linear_list_query_requests_updated_at: prove the real list() query asks for
    # `creator`, so a future edit dropping it from _ISSUE_FIELDS breaks a test, not just prod.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {
            "issues": {
                "nodes": [
                    {
                        "identifier": "HYP-7",
                        "title": "t",
                        "url": "https://linear/HYP-7",
                        "description": "",
                        "dueDate": None,
                        "updatedAt": "",
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                        "creator": {"id": "user-123"},
                    }
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    tickets = be.list()
    assert "creator" in seen["query"]
    assert tickets[0].reporter == "user-123"


def test_linear_node_to_ticket_marks_provider_closed_from_completed_state():
    # Linear's normalized state is derived straight from its native workflow state — no separate
    # label-lag risk — so `provider_closed` simply mirrors "state.type is a closed type".
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "completed"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).provider_closed is True


def test_linear_node_to_ticket_provider_closed_false_for_active_state():
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "started"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).provider_closed is False


def test_linear_current_user_returns_viewer_id(monkeypatch):
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    seen = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {"viewer": {"id": "user-123"}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    assert be.current_user() == "user-123"
    assert "viewer" in seen["query"]


def test_linear_current_user_returns_none_on_backend_error(monkeypatch):
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    def raise_err(query, variables=None):
        raise BackendError("boom")

    monkeypatch.setattr(be, "_gql", raise_err)
    assert be.current_user() is None


@pytest.mark.parametrize("bad_response", [None, [], "viewer", 42])
def test_linear_current_user_returns_none_on_non_dict_response(monkeypatch, bad_response):
    """`_gql` is typed to return a dict, but a malformed response or a third-party override
    could hand back something else — mirror the `isinstance(row, dict)` guard the GitHub
    backend already has for its own `/user` call (review finding: `current_user` must never
    raise, per its own docstring, rather than relying on a caller's broad except). Parametrized
    across a few non-dict shapes (not just `None`) so the guard's actual contract — ANY
    non-dict, not one specific falsy value — is locked in (review finding, round 2)."""
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    monkeypatch.setattr(be, "_gql", lambda query, variables=None: bad_response)
    assert be.current_user() is None


@pytest.mark.parametrize("bad_viewer", ["not-a-dict", 42, [], True])
def test_linear_current_user_returns_none_on_non_dict_viewer(monkeypatch, bad_viewer):
    """A dict root response with a truthy non-dict ``viewer`` value (e.g. a string) would crash
    `.get("id")` if only the root were guarded — `"invalid" or {}` evaluates to `"invalid"`,
    not `{}`, since the `or` fallback only fires on a FALSY viewer (review finding, round 3)."""
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    monkeypatch.setattr(be, "_gql", lambda query, variables=None: {"viewer": bad_viewer})
    assert be.current_user() is None


# ── ambiguous read failure (task-cli bug 1: a crash after a successful create) ──────────────


def test_github_call_wraps_ambiguous_http_error(monkeypatch):
    # request_json raising AmbiguousHttpError (the connection dropped mid-read after a 2xx —
    # see test_http.py) must surface through _call as the distinctly-typed AmbiguousBackendError,
    # not the generic BackendError every other failure funnels into.
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    monkeypatch.setattr(
        "tasklib.backends.github_issues.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(AmbiguousHttpError("status 201 was already returned")),
    )
    with pytest.raises(AmbiguousBackendError) as exc:
        be._call("https://api.github.com/repos/o/r/issues", method="POST")
    assert "201" in str(exc.value)


def test_github_call_ambiguous_error_is_not_caught_as_backend_error(monkeypatch):
    # the whole point of the separate type: a bare `except BackendError` (used by nearly every
    # other call site in cli.py) must NOT swallow this — that bucket means "nothing happened".
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    monkeypatch.setattr(
        "tasklib.backends.github_issues.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(AmbiguousHttpError("boom")),
    )
    try:
        be._call("url", method="POST")
    except BackendError:
        pytest.fail("AmbiguousBackendError must not be catchable as BackendError")
    except AmbiguousBackendError:
        pass


def test_linear_gql_wraps_ambiguous_http_error(monkeypatch):
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    monkeypatch.setattr(
        "tasklib.backends.linear.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(AmbiguousHttpError("status 200 was already returned")),
    )
    with pytest.raises(AmbiguousBackendError):
        be._gql("mutation { issueCreate { success } }")


def test_exit_ambiguous_does_not_collide_with_agenttools_errors_contract():
    # unlike EXIT_ILLEGAL_TRANSITION (which deliberately pins to the shared EXIT_USAGE), this
    # code is deliberately NOT aliased to anything in agenttools_errors — review finding: the
    # closest-sounding shared class, EXIT_NETWORK (7), documents itself as the safe-to-retry
    # class ("DNS, timeout, 5xx"), and this exception means the opposite (verify before
    # retrying). Assert no accidental collision with ANY shared code, not just EXIT_NETWORK, so a
    # future contract addition can't silently start meaning "safe to retry" for this value too.
    try:
        from agenttools_errors import EXIT_CODES
    except Exception:  # noqa: BLE001 - shared lib optional in this env
        pytest.skip("agenttools_errors not installed")
    assert EXIT_AMBIGUOUS not in EXIT_CODES
