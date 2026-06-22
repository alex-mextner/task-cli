"""Version drift guards — `task --version` must track pyproject.toml, never a stale literal.

Regression for #22: `__version__` was a hardcoded `"0.1.0"` that nobody ever bumped, so
`task --version` lied. The version is now resolved dynamically — installed-distribution
metadata (``importlib.metadata``) first, falling back to parsing the repo's pyproject.toml
when the package isn't pip-installed (a raw git checkout). pyproject.toml is the single
source of truth; there is no hardcoded literal to drift.

These tests pin that contract WITHOUT coupling to ambient install state. We deliberately do
NOT assert `tasklib.__version__ == <pyproject>` unconditionally: when the package is installed
editable at one version and pyproject is later bumped without reinstalling, the metadata path
correctly returns the *installed* (frozen) version — that equality would be a flaky,
install-state-dependent assertion. So the pyproject-equality contract is pinned only on the
live-checkout branch (forced via a mocked ``PackageNotFoundError``), which is the branch #22
actually fixed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import tasklib

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _declared_version() -> str:
    """The version string declared in the ``[project]`` table of pyproject.toml."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    project = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    scope = project.group(1) if project else text
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', scope, re.MULTILINE)
    assert match, "no [project] version in pyproject.toml"
    return match.group(1)


def test_pyproject_fallback_matches_declared_version(monkeypatch):
    """Live-checkout path (no installed metadata) → version equals pyproject's declared value.

    This is THE drift guard: a pyproject bump can't silently fail to reach ``--version``. We
    force the ``importlib.metadata`` lookup to miss so the deterministic pyproject parse runs.
    """

    def _raise(_dist):
        raise PackageNotFoundError("task-cli")

    monkeypatch.setattr(tasklib, "_pkg_version", _raise)
    assert tasklib._resolve_version() == _declared_version()


def test_resolved_version_is_not_the_stale_literal():
    """The exported version is never the old hardcoded `0.1.0` that #22 fixed.

    Holds regardless of install state: the source literal is gone, so neither the metadata
    branch nor the pyproject branch can resurrect it (pyproject is bumped to 0.2.0).
    """
    assert tasklib.__version__ != "0.1.0"


def test_pyproject_parse_ignores_version_outside_project_table(monkeypatch, tmp_path):
    """The fallback reads the [project] version, not a version= key in another table."""
    pkg_dir = tmp_path / "tasklib"
    pkg_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nversion = "9.9.9"\n\n[project]\nname = "task-cli"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    # `_version_from_pyproject` derives the file from this module's location
    # (``Path(__file__).resolve().parent.parent / "pyproject.toml"``). Point ``__file__`` at the
    # crafted layout so the resolved sibling pyproject.toml is the one we wrote.
    monkeypatch.setattr(tasklib, "__file__", str(pkg_dir / "__init__.py"))
    assert tasklib._version_from_pyproject() == "1.2.3"


def test_fallback_degrades_when_pyproject_missing(monkeypatch, tmp_path):
    """Both branches missing (not installed, no pyproject) → a safe sentinel, never a crash."""

    def _raise(_dist):
        raise PackageNotFoundError("task-cli")

    monkeypatch.setattr(tasklib, "_pkg_version", _raise)
    # Resolve pyproject to a path that does not exist → OSError on read → sentinel.
    monkeypatch.setattr(tasklib, "__file__", str(tmp_path / "nope" / "__init__.py"))
    assert tasklib._resolve_version() == "0+unknown"


def test_cli_version_flag_reflects_dynamic_resolver():
    """`python -m tasklib --version` prints exactly what the dynamic resolver yields.

    Compared against the resolver run in THIS interpreter (not against pyproject directly) so
    the test is correct whether the package is pip-installed or running from the checkout — it
    proves the CLI is wired to the dynamic version, with no stale `0.1.0` literal in the output.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "tasklib", "--version"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    assert out == f"task {tasklib._resolve_version()}"
    assert "0.1.0" not in out
