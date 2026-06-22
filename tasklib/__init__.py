"""task-cli — the enforced interface to the ticket system.

Every request becomes a durable, well-formed ticket the moment it arrives; ticket
*quality* (acceptance criteria, motivation, user-impact, cost-of-inaction, screenshots,
formatting) is enforced by the tool itself, not by convention. Backends: GitHub Issues
(default) and Linear (per-repo). Stdlib-only at import time; heavy work is lazy.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

_DISTRIBUTION = "task-cli"

# Scoped regex for the `[project] version = "..."` line. We deliberately do NOT use tomllib
# here: the live-checkout fallback must work on any interpreter (and a one-field scoped regex
# is cheaper than parsing the whole TOML), and it keeps the source of truth as pyproject.toml.
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _version_from_pyproject() -> str:
    """Parse the declared version out of the repo's pyproject.toml (live-checkout fallback).

    Reached only when the package is not pip-installed (``importlib.metadata`` can't find the
    ``task-cli`` distribution), e.g. running straight from a git checkout via the ``bin/task``
    shim. We scan only the ``[project]`` table so a ``version`` key elsewhere can't shadow it;
    the scope ends at the next table header of any kind (``[project.scripts]`` etc.), which is
    TOML-correct — keys after a subtable header no longer belong to ``[project]``.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    project_table = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    scope = project_table.group(1) if project_table else text
    match = _PYPROJECT_VERSION_RE.search(scope)
    return match.group(1) if match else "0+unknown"


def _resolve_version() -> str:
    """Resolve the version dynamically: installed-distribution metadata, else pyproject.toml.

    pyproject.toml is the single source of truth; there is no hardcoded literal to drift.
    """
    try:
        return _pkg_version(_DISTRIBUTION)
    except PackageNotFoundError:
        try:
            return _version_from_pyproject()
        except OSError:
            return "0+unknown"


__version__ = _resolve_version()
