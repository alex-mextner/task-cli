"""Backend protocol — the FakeBackend satisfies it and round-trips correctly.

Also covers the GitHub remote-URL parser (pure) without hitting the network.
"""

from __future__ import annotations

import pytest

from tasklib.backends import BackendError
from tasklib.backends.github_issues import GitHubIssuesBackend, _api_root, _parse_remote
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
    assert fetched.acceptance == ["works"]


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
