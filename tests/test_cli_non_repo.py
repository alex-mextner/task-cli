"""CLI flows for the OUTSIDE-a-repo / cross-project / session-fallback behavior.

Exercises production code (``cmd_list``/``cmd_find``/``cmd_read``/``cmd_status``/``cmd_create``)
end to end against the FakeBackend. Repo presence is controlled two ways:
  - real: ``-C <tmp_path>`` is a directory outside any git work tree, so ``github.repo: auto``
    cannot resolve an origin → the genuine "outside a repo" path (no monkeypatching).
  - pinned: a per-project FakeBackend keyed by coordinate, injected via ``get_backend`` —
    the same seam the rest of the suite uses.
"""

from __future__ import annotations

import pytest

from tasklib import cli
from tasklib.cli import main
from tasklib.model import State, Ticket


@pytest.fixture(autouse=True)
def _isolate(isolated_state):
    """Isolated HOME/XDG + no ambient session, so every test starts from a clean slate."""
    return isolated_state


def _gh_config(tmp_path, projects: str) -> str:
    """Write a global config with a ``projects:`` registry; return its XDG_CONFIG_HOME."""
    cfg_home = tmp_path / "config"
    d = cfg_home / "task-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        "version: 1\nbackend: github-issues\n" + projects, encoding="utf-8"
    )
    return str(cfg_home)


# ── outside a repo, no registry → 3-part errors, never a crash ──────────────────────


def test_list_outside_repo_no_registry_errors_with_guidance(capsys, tmp_path, monkeypatch):
    rc = main(["list", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no projects to list" in out
    assert "outside a git repo" in out
    assert "projects:" in out  # the fix points at the registry


def test_create_outside_repo_gives_3part_error(capsys, tmp_path):
    rc = main(
        ["create", "-C", str(tmp_path), "--title", "t", "--why", "w", "--impact", "i",
         "--if-not-done", "c", "--acceptance", "a"]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "no project context" in out
    assert "repo-bound" in out  # WHY
    assert "--repo owner/name" in out  # HOW names a real flag (must not lie)


def test_read_outside_repo_unroutable_id_errors(capsys, tmp_path):
    rc = main(["read", "#5", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "cannot resolve which project" in out


def test_create_outside_repo_linear_backend_gives_linear_3part_error(capsys, tmp_path, monkeypatch):
    # with the global backend set to linear but no team resolvable, `create` outside a repo
    # must give the LINEAR-flavored HOW (not the GitHub --repo one). Covers the linear branch.
    cfg_home = tmp_path / "config"
    d = cfg_home / "task-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("version: 1\nbackend: linear\nlinear: {team: ''}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    rc = main(
        ["create", "-C", str(tmp_path), "--title", "t", "--why", "w", "--impact", "i",
         "--if-not-done", "c", "--acceptance", "a"]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "no project context" in out
    assert "linear.team" in out  # the linear HOW, not the github --repo one


def test_list_in_repo_with_broken_remote_surfaces_error_not_outside_repo(capsys, tmp_path):
    # INSIDE a git repo but the origin is an unsupported URL: this is a real error, NOT
    # "outside a repo" / "no projects". It must surface (exit 2, clean message), never be masked
    # as the empty cross-project view. Regression for the codex P2 (BackendError swallowed).
    import subprocess

    repo = tmp_path / "weird"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/not-github"],
        check=True,
    )
    rc = main(["list", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    # the genuine remote error, not the outside-a-repo guidance
    assert "no projects to list" not in out
    assert "remote" in out.lower() or "github" in out.lower()


def test_list_in_repo_linear_missing_team_surfaces_config_error_not_outside_repo(capsys, tmp_path):
    # INSIDE a git repo whose task.yaml selects `backend: linear` but omits `linear.team`: this
    # is a real, actionable in-repo misconfiguration. It must surface the backend's "requires a
    # team key" error (exit 2), NOT be demoted to the "outside a repo" / "no projects" path.
    # Regression for the codex P2 (a teamless in-repo linear backend masked as outside-a-repo).
    import subprocess

    repo = tmp_path / "lin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "task.yaml").write_text("version: 1\nbackend: linear\n", encoding="utf-8")
    rc = main(["list", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no projects to list" not in out
    assert "outside a git repo" not in out
    # the actionable backend error, anchored so a reworded message can't pass on a stray "team"
    assert "requires a team key" in out


# ── outside a repo, WITH a registry → grouped cross-project list ─────────────────────


class _ByCoordBackend:
    """A FakeBackend whose results depend on the project coordinate it was built for."""

    def __init__(self, store):
        # exactly one coord per instance — the factory builds a fresh backend per project, so
        # the store always holds a single coordinate's tickets (encode that invariant explicitly).
        assert len(store) == 1, "a per-project backend holds exactly one coordinate"
        self._store = store  # {coord: list[Ticket]}

    @classmethod
    def factory(cls, store):
        from tasklib.backends import BackendError

        def get_backend(cfg, env=None):
            if cfg.backend == "linear":
                coord = str(cfg.section("linear").get("team", ""))
            else:
                coord = str(cfg.section("github").get("repo", ""))
            value = store.get(coord)
            if isinstance(value, BackendError):
                # a backend that raises on construction (auth) — degraded group
                raise value
            return cls({coord: value or []})

        return get_backend

    def _coord_tickets(self):
        return next(iter(self._store.values()))

    def list(self, *, labels=None, state=None, limit=30):
        out = list(self._coord_tickets())
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def search(self, query, *, state=None, limit=30):
        q = query.lower()
        return [t for t in self._coord_tickets() if q in t.title.lower()]

    def session_tickets(self, label, *, limit=30):
        return []


def test_list_outside_repo_groups_by_project(capsys, tmp_path, monkeypatch):
    from tasklib.backends import BackendError

    store = {
        "acme/frontend": [Ticket(title="Fix header", state=State.IN_PROGRESS, id="#12")],
        "acme/backend": [
            Ticket(title="DB migration", state=State.TODO, id="#7"),
            Ticket(title="Rate limit", state=State.DONE, id="#3"),
        ],
        "HYP": BackendError("linear: no team with key 'HYP'"),
    }
    monkeypatch.setattr("tasklib.backends.get_backend", _ByCoordBackend.factory(store))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(
            tmp_path,
            "projects:\n"
            "  - {name: acme/frontend, repo: acme/frontend}\n"
            "  - {name: acme/backend, repo: acme/backend}\n"
            "  - {name: HYP, backend: linear, team: HYP}\n",
        ),
    )

    rc = main(["list", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    # session-vs-all messaging present (implicit aggregate must explain itself)
    assert "showing all project tasks" in out
    # a heading per project, tickets grouped beneath
    assert "acme/frontend" in out and "#12" in out
    assert "acme/backend" in out and "#7" in out and "#3" in out
    # a failing project is a DEGRADED group, not a hard stop — the others still rendered
    assert "HYP" in out
    assert "no team with key" in out


def test_list_outside_repo_json_is_structured(capsys, tmp_path, monkeypatch):
    store = {"acme/frontend": [Ticket(title="Fix header", state=State.TODO, id="#12")]}
    monkeypatch.setattr("tasklib.backends.get_backend", _ByCoordBackend.factory(store))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME", _gh_config(tmp_path, "projects:\n  - {name: acme/frontend, repo: acme/frontend}\n")
    )
    rc = main(["list", "-C", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    payload = json.loads(out)
    assert payload[0]["project"] == "acme/frontend"
    assert payload[0]["backend"] == "github-issues"
    assert payload[0]["error"] is None
    assert payload[0]["current"] is False  # a registry project, not the cwd
    assert payload[0]["tickets"][0]["id"] == "#12"


def test_find_outside_repo_searches_all_projects_grouped(capsys, tmp_path, monkeypatch):
    store = {
        "acme/frontend": [Ticket(title="header bug", state=State.TODO, id="#1")],
        "acme/backend": [Ticket(title="header crash", state=State.TODO, id="#2")],
    }
    monkeypatch.setattr("tasklib.backends.get_backend", _ByCoordBackend.factory(store))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(
            tmp_path,
            "projects:\n  - {repo: acme/frontend}\n  - {repo: acme/backend}\n",
        ),
    )
    rc = main(["find", "header", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "acme/frontend" in out and "#1" in out
    assert "acme/backend" in out and "#2" in out


def test_grouped_list_is_paged_on_tty(capsys, tmp_path, monkeypatch):
    # the cross-project GROUPED view must page like the flat one: on a TTY the section headings +
    # tickets flow THROUGH the pager (sentinel file), not to stdout. Covers the grouped path.
    from tasklib import cli as _cli

    store = {
        "acme/frontend": [Ticket(title="Fix header", state=State.IN_PROGRESS, id="#12")],
        "acme/backend": [Ticket(title="DB migration", state=State.TODO, id="#7")],
    }
    monkeypatch.setattr("tasklib.backends.get_backend", _ByCoordBackend.factory(store))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(tmp_path, "projects:\n  - {repo: acme/frontend}\n  - {repo: acme/backend}\n"),
    )
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(_cli.sys.stdout, "isatty", lambda: True, raising=False)

    rc = main(["list", "-C", str(tmp_path)])
    assert rc == 0
    paged = sink.read_text(encoding="utf-8")
    # the heading-per-project body reached the pager, including the session-vs-all notice.
    assert "showing all project tasks" in paged
    assert "acme/frontend" in paged and "#12" in paged
    assert "acme/backend" in paged and "#7" in paged


def test_read_outside_repo_routes_to_single_github_project(capsys, tmp_path, monkeypatch):
    # exactly one github project registered → a `#id` routes there unambiguously.
    store = {"acme/only": [Ticket(title="The one", state=State.TODO, id="#42")]}

    class _Get:
        def __call__(self, cfg, env=None):
            assert cfg.section("github").get("repo") == "acme/only"
            return _One()

    class _One:
        def get(self, tid):
            assert tid == "#42"
            return Ticket(title="The one", what="body", state=State.TODO, id="#42", url="u")

    monkeypatch.setattr("tasklib.backends.get_backend", _Get())
    monkeypatch.setenv(
        "XDG_CONFIG_HOME", _gh_config(tmp_path, "projects:\n  - {repo: acme/only}\n")
    )
    rc = main(["read", "#42", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#42" in out and "The one" in out


def test_read_outside_repo_ambiguous_github_id_errors(capsys, tmp_path, monkeypatch):
    # two github projects → a bare `#id` is ambiguous → honest 3-part error, no guess.
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: object())
    monkeypatch.setenv(
        "XDG_CONFIG_HOME", _gh_config(tmp_path, "projects:\n  - {repo: a/one}\n  - {repo: a/two}\n")
    )
    rc = main(["read", "#9", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "cannot resolve which project" in out


def test_status_outside_repo_routes_linear_id_by_team(capsys, tmp_path, monkeypatch):
    # a Linear-shaped id (HYP-3) routes to the linear project whose team is HYP.
    class _Get:
        def __call__(self, cfg, env=None):
            assert cfg.backend == "linear"
            assert cfg.section("linear").get("team") == "HYP"
            return _Lin()

    class _Lin:
        def get(self, tid):
            return Ticket(title="Linear one", state=State.IN_PROGRESS, id="HYP-3", url="u")

    monkeypatch.setattr("tasklib.backends.get_backend", _Get())
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(
            tmp_path,
            "projects:\n  - {repo: a/gh}\n  - {name: HYP, backend: linear, team: HYP}\n",
        ),
    )
    rc = main(["status", "HYP-3", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HYP-3" in out and "in-progress" in out


def test_read_outside_repo_two_same_team_linear_projects_routes_deterministically(capsys, tmp_path, monkeypatch):
    # two HYP projects (different Linear projects, same team). A Linear id is team-scoped and
    # get/update ignore project, so the id resolves via either — route to the first, NOT "ambiguous".
    seen = {}

    class _Get:
        def __call__(self, cfg, env=None):
            seen["team"] = cfg.section("linear").get("team")
            seen["project"] = cfg.section("linear").get("project")
            return _Lin()

    class _Lin:
        def get(self, tid):
            return Ticket(title="Linear one", state=State.TODO, id="HYP-7", url="u")

    monkeypatch.setattr("tasklib.backends.get_backend", _Get())
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(
            tmp_path,
            "projects:\n"
            "  - {name: HYP-alpha, backend: linear, linear: {team: HYP, project: alpha}}\n"
            "  - {name: HYP-beta, backend: linear, linear: {team: HYP, project: beta}}\n",
        ),
    )
    rc = main(["read", "HYP-7", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HYP-7" in out
    assert seen["team"] == "HYP" and seen["project"] == "alpha"  # the FIRST same-team match


# ── in-repo session fallback (session-vs-all messaging) ─────────────────────────────


def _pin_current_repo(monkeypatch, coord="acme/here"):
    """Make the cwd look like an in-repo github project without a real git origin."""
    monkeypatch.setattr(
        cli,
        "_current_project_overlay",
        lambda cfg: (coord, {"backend": "github-issues", "github": {"repo": coord}}),
    )


class _SessionFake:
    """FakeBackend with a tunable session-vs-all split."""

    def __init__(self, all_tickets, session_tickets):
        self._all = all_tickets
        self._session = session_tickets

    def list(self, *, labels=None, state=None, limit=30):
        out = list(self._all)
        if state is not None:
            out = [t for t in out if t.state == state]
        return out[:limit]

    def session_tickets(self, label, *, limit=30):
        return list(self._session)


def test_list_in_repo_no_session_falls_back_to_all(capsys, tmp_path, monkeypatch):
    _pin_current_repo(monkeypatch)
    backend = _SessionFake(
        all_tickets=[Ticket(title="A", state=State.TODO, id="#1"), Ticket(title="B", state=State.TODO, id="#2")],
        session_tickets=[],
    )
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: backend)
    # no TASK_SESSION, cwd is not a git repo → session.source == 'none' → fallback
    rc = main(["list", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "showing all project tasks" in out
    assert "#1" in out and "#2" in out


def test_list_in_repo_session_with_no_tickets_falls_back(capsys, tmp_path, monkeypatch):
    _pin_current_repo(monkeypatch)
    monkeypatch.setenv("TASK_SESSION", "sess-empty")
    backend = _SessionFake(
        all_tickets=[Ticket(title="A", state=State.TODO, id="#1")],
        session_tickets=[],  # session resolves but has nothing → fallback + say so
    )
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: backend)
    rc = main(["list", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "showing all project tasks" in out
    assert "#1" in out


def test_list_in_repo_session_with_tickets_scopes_no_fallback_line(capsys, tmp_path, monkeypatch):
    _pin_current_repo(monkeypatch)
    monkeypatch.setenv("TASK_SESSION", "sess-mine")
    backend = _SessionFake(
        all_tickets=[Ticket(title="A", state=State.TODO, id="#1"), Ticket(title="Mine", state=State.TODO, id="#9")],
        session_tickets=[Ticket(title="Mine", state=State.TODO, id="#9")],
    )
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: backend)
    rc = main(["list", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "showing all project tasks" not in out
    assert "session sess-mine" in out
    assert "#9" in out and "#1" not in out


def test_list_all_in_repo_uses_grouped_view(capsys, tmp_path, monkeypatch):
    # --all inside a repo → the cross-project grouped view (current repo is one group).
    _pin_current_repo(monkeypatch, coord="acme/here")
    backend = _SessionFake(
        all_tickets=[Ticket(title="A", state=State.TODO, id="#1")],
        session_tickets=[],
    )
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: backend)
    rc = main(["list", "--all", "-C", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    # explicit --all does NOT print the apologetic session-vs-all line
    assert "showing all project tasks" not in out
    assert "acme/here" in out and "#1" in out
    # the repo you're inside is flagged as the current group
    assert "(current)" in out
    # heading is informative: name · backend · count
    assert "github-issues" in out


def test_list_all_marks_current_even_when_repo_is_in_registry(capsys, tmp_path, monkeypatch):
    # the cwd repo is ALSO a registry entry (under its own label). It must still be flagged
    # (current) and report current:true in JSON — currentness tracks by coordinate, not by
    # registry-explicitness. Regression for the codex P2 (synthetic suppression dropped current).
    _pin_current_repo(monkeypatch, coord="acme/web")
    store = {
        "acme/web": [Ticket(title="W", state=State.TODO, id="#1")],
        "acme/other": [Ticket(title="O", state=State.TODO, id="#2")],
    }
    monkeypatch.setattr("tasklib.backends.get_backend", _ByCoordBackend.factory(store))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        _gh_config(
            tmp_path,
            "projects:\n  - {name: The Web App, repo: acme/web}\n  - {repo: acme/other}\n",
        ),
    )
    rc = main(["list", "--all", "-C", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    payload = json.loads(out)
    by_name = {g["project"]: g for g in payload}
    # registered under its label, but flagged current by coordinate match
    assert by_name["The Web App"]["current"] is True
    assert by_name["acme/other"]["current"] is False
    # not double-listed (the synthetic current project was suppressed by coordinate)
    assert sum(1 for g in payload if g["current"]) == 1
