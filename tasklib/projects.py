"""The known-projects registry — what ``task list`` aggregates across repos/projects.

``task`` resolves ONE backend per invocation from the cwd's repo (``github.repo: auto`` =
the git ``origin``). That answers "what am I doing in THIS repo". But the user also wants the
cross-repo view: ``task list`` run **outside** any git repo, or ``task list --all`` inside one,
should show ALL tasks GROUPED by repo/project. That needs a list of which projects exist — the
single-backend resolution can't discover them on its own.

A **project** is one named ticket source: a backend (``github-issues``/``linear``) plus the
coordinates that scope it (a GitHub ``owner/repo``, or a Linear ``team``/``project``). The
registry lives under a ``projects:`` key in the config cascade — usually the GLOBAL config
(``~/.config/task-cli/config.yaml``), since it spans repos, but a per-repo ``task.yaml`` may
add to it too. Each entry is turned into a config *overlay* that the existing backend selector
(:func:`tasklib.backends.get_backend`) resolves unchanged — so a project reuses the whole
backend seam with zero special-casing.

This module is PURE: it parses the registry and shapes the config overlays. The effectful part
(querying each project's backend, catching a degraded one) lives in ``cli.py``. Nothing here
imports a provider, the network, or yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Project:
    """One ticket source in the registry: a name + the config overlay that resolves it.

    ``overlay`` is deep-merged over the base config so :func:`tasklib.backends.get_backend`
    constructs the right backend (e.g. ``{"backend": "github-issues", "github": {"repo":
    "owner/name"}}``). ``backend`` is carried for display/grouping. ``explicit`` is ``True``
    for a registry-declared project and ``False`` for the synthetic current-repo fallback.
    """

    name: str
    backend: str
    overlay: dict[str, Any] = field(default_factory=dict)
    explicit: bool = True

    @property
    def coordinate(self) -> str:
        """The backend-scoped identity (``owner/repo`` or ``TEAM/project``), NOT the display
        name. Two registry entries with the same coordinate are the same project even if they
        carry different display names; two different repos that happen to share a display name
        are NOT. Dedup and current-repo matching key off this, never ``name``.
        """
        if self.backend == "linear":
            lin = self.overlay.get("linear", {})
            team = str(lin.get("team", "")).strip().upper()
            project = str(lin.get("project", "")).strip()
            return f"linear:{team}/{project}" if project else f"linear:{team}"
        repo = str(self.overlay.get("github", {}).get("repo", "")).strip().lower()
        return f"github-issues:{repo}"


def projects_from_config(data: dict[str, Any]) -> list[Project]:
    """Parse the ``projects:`` registry from a config mapping. Empty/absent → ``[]``.

    Each entry is a mapping. Recognized keys::

        - name:    display label for the group (defaults to the repo/team coordinate)
        - backend: github-issues | linear (defaults to the top-level ``backend``)
        - repo:    owner/name              (github-issues shorthand)
        - github:  {repo: owner/name}      (explicit github block)
        - team:    HYP                      (linear shorthand)
        - project: <id>                     (linear project, optional)
        - linear:  {team: HYP, project: <id>}

    A malformed entry (not a mapping, or missing the backend coordinate) is skipped rather
    than aborting the whole registry — the aggregation is best-effort by design.
    """
    raw = data.get("projects")
    if not isinstance(raw, list):
        return []
    default_backend = str(data.get("backend", "github-issues"))
    out: list[Project] = []
    seen: set[str] = set()
    for entry in raw:
        proj = _project_from_entry(entry, default_backend)
        if proj is None:
            continue
        if proj.coordinate in seen:  # dedup by coordinate (a repo + global both listing it)
            continue
        seen.add(proj.coordinate)
        out.append(proj)
    return out


def _project_from_entry(entry: Any, default_backend: str) -> Project | None:
    if not isinstance(entry, dict):
        return None
    backend = str(entry.get("backend", default_backend))
    if backend == "github-issues":
        return _github_project(entry, backend)
    if backend == "linear":
        return _linear_project(entry, backend)
    return None


def _github_project(entry: dict[str, Any], backend: str) -> Project | None:
    gh = entry.get("github") if isinstance(entry.get("github"), dict) else {}
    repo = str(entry.get("repo") or gh.get("repo") or "").strip()
    # ``auto`` is meaningless in the registry (there's no single cwd to resolve it against),
    # so a github project must name an explicit ``owner/name``.
    if not repo or repo == "auto" or "/" not in repo:
        return None
    name = str(entry.get("name") or repo).strip()
    overlay: dict[str, Any] = {"backend": "github-issues", "github": {"repo": repo}}
    return Project(name=name, backend=backend, overlay=overlay)


def _linear_project(entry: dict[str, Any], backend: str) -> Project | None:
    lin = entry.get("linear") if isinstance(entry.get("linear"), dict) else {}
    team = str(entry.get("team") or lin.get("team") or "").strip()
    if not team:
        return None
    project_id = str(entry.get("project") or lin.get("project") or "").strip()
    name = str(entry.get("name") or (f"{team}/{project_id}" if project_id else team)).strip()
    overlay: dict[str, Any] = {"backend": "linear", "linear": {"team": team, "project": project_id}}
    return Project(name=name, backend=backend, overlay=overlay)


def current_repo_project(name: str, backend: str, overlay: dict[str, Any]) -> Project:
    """Build the synthetic project for the repo the user is currently inside.

    Used when aggregating from inside a repo: the current repo is always one of the groups
    even if it isn't (yet) in the registry. Marked ``explicit=False`` so callers can tell it
    apart from a registry entry (e.g. to suggest adding it).
    """
    return Project(name=name, backend=backend, overlay=overlay, explicit=False)
