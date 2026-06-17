"""Pure tests for the known-projects registry (``tasklib.projects``).

No I/O, no backend — the registry parsing + overlay shaping is pure string/dict work, so it
is tested directly without a FakeBackend or tmp dirs.
"""

from __future__ import annotations

from tasklib.projects import Project, current_repo_project, projects_from_config


def test_empty_or_absent_registry_is_empty():
    assert projects_from_config({}) == []
    assert projects_from_config({"projects": None}) == []
    assert projects_from_config({"projects": "nope"}) == []
    assert projects_from_config({"projects": []}) == []


def test_github_shorthand_repo():
    projs = projects_from_config({"projects": [{"repo": "acme/frontend"}]})
    assert len(projs) == 1
    p = projs[0]
    assert p.name == "acme/frontend"
    assert p.backend == "github-issues"
    assert p.overlay == {"backend": "github-issues", "github": {"repo": "acme/frontend"}}
    assert p.explicit is True


def test_github_explicit_block_and_name():
    projs = projects_from_config({"projects": [{"name": "Frontend", "github": {"repo": "acme/frontend"}}]})
    assert projs[0].name == "Frontend"
    assert projs[0].overlay["github"]["repo"] == "acme/frontend"


def test_github_auto_or_bare_name_is_rejected():
    # 'auto' is meaningless in the registry (no cwd to resolve against); a bare repo with no
    # owner is malformed — both are skipped rather than producing a broken project.
    assert projects_from_config({"projects": [{"repo": "auto"}]}) == []
    assert projects_from_config({"projects": [{"repo": "justname"}]}) == []
    assert projects_from_config({"projects": [{"backend": "github-issues"}]}) == []


def test_linear_shorthand_team():
    projs = projects_from_config({"projects": [{"backend": "linear", "team": "HYP"}]})
    assert len(projs) == 1
    p = projs[0]
    assert p.name == "HYP"
    assert p.backend == "linear"
    assert p.overlay == {"backend": "linear", "linear": {"team": "HYP", "project": ""}}


def test_linear_team_and_project_name():
    projs = projects_from_config(
        {"projects": [{"backend": "linear", "linear": {"team": "HYP", "project": "P1"}}]}
    )
    assert projs[0].name == "HYP/P1"
    assert projs[0].overlay["linear"] == {"team": "HYP", "project": "P1"}


def test_linear_without_team_is_rejected():
    assert projects_from_config({"projects": [{"backend": "linear", "project": "P1"}]}) == []


def test_default_backend_applies_when_entry_omits_it():
    projs = projects_from_config({"backend": "linear", "projects": [{"team": "HYP"}]})
    assert projs[0].backend == "linear"


def test_duplicate_coordinates_deduped():
    projs = projects_from_config(
        {"projects": [{"repo": "acme/x"}, {"repo": "acme/x"}, {"repo": "acme/y"}]}
    )
    assert [p.name for p in projs] == ["acme/x", "acme/y"]


def test_dedup_keys_off_coordinate_not_display_name():
    # same repo under two different display names → ONE project (the coordinate is identity).
    projs = projects_from_config(
        {"projects": [{"name": "Frontend", "repo": "acme/web"}, {"name": "The Web App", "repo": "acme/web"}]}
    )
    assert len(projs) == 1
    # two DIFFERENT repos that share a display name → both kept (not collapsed).
    projs2 = projects_from_config(
        {"projects": [{"name": "App", "repo": "acme/one"}, {"name": "App", "repo": "acme/two"}]}
    )
    assert {p.coordinate for p in projs2} == {"github-issues:acme/one", "github-issues:acme/two"}


def test_coordinate_is_case_insensitive_for_github():
    a = projects_from_config({"projects": [{"repo": "Acme/Web"}]})[0]
    b = projects_from_config({"projects": [{"repo": "acme/web"}]})[0]
    assert a.coordinate == b.coordinate


def test_linear_coordinate_includes_team_and_project():
    p = projects_from_config({"projects": [{"backend": "linear", "team": "hyp", "project": "P1"}]})[0]
    assert p.coordinate == "linear:HYP/P1"


def test_unknown_backend_skipped():
    assert projects_from_config({"projects": [{"backend": "jira", "repo": "a/b"}]}) == []


def test_registry_order_is_preserved():
    projs = projects_from_config({"projects": [{"repo": "z/z"}, {"repo": "a/a"}, {"repo": "m/m"}]})
    assert [p.name for p in projs] == ["z/z", "a/a", "m/m"]


def test_current_repo_project_is_non_explicit():
    p = current_repo_project("acme/here", "github-issues", {"github": {"repo": "acme/here"}})
    assert isinstance(p, Project)
    assert p.explicit is False
    assert p.name == "acme/here"
