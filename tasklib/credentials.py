"""Harvest provider credentials from existing CLI configs — no re-prompting (§1).

A user who already ran ``gh auth login`` / ``linear auth`` needs zero extra setup: task-cli
reads the same credentials those CLIs persist and calls the provider API directly. The CLI is
only needed for the one-time auth flow, never for task-cli's calls.

Resolution order:
  GitHub: ``$GITHUB_TOKEN`` / ``$GH_TOKEN`` → ``gh auth token`` (the reliable path; gh stores
          the real token in the OS keychain, not in hosts.yml) → ``~/.config/gh/hosts.yml``
          ``oauth_token`` (older gh, plaintext-token installs).
  Linear: ``$LINEAR_API_KEY`` → ``~/.config/linear/credentials.toml`` (the ``default``
          workspace's key, or a named workspace) → ``$XDG_CONFIG_HOME/linear/...``.

Tokens are returned, never logged or persisted by this module. The only subprocess is the
one-shot ``gh auth token`` — there is no per-API-call subprocess (that is the whole point).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CredentialError(RuntimeError):
    """No usable credential could be harvested for a backend."""


@dataclass
class GitHubCreds:
    token: str
    source: str  # provenance, for diagnostics (never the token itself)


@dataclass
class LinearCreds:
    api_key: str
    workspace: str
    source: str


# ── GitHub ─────────────────────────────────────────────────────────────────────────


def github_token(env: dict[str, str] | None = None) -> GitHubCreds:
    """Resolve a GitHub token. Raises :class:`CredentialError` if none is available."""
    env = os.environ if env is None else env

    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = env.get(var)
        if val and val.strip():
            return GitHubCreds(token=val.strip(), source=f"env:{var}")

    tok = _gh_auth_token()
    if tok:
        return GitHubCreds(token=tok, source="gh auth token")

    tok = _gh_hosts_oauth_token(env)
    if tok:
        return GitHubCreds(token=tok, source="~/.config/gh/hosts.yml")

    raise CredentialError(
        "no GitHub token found. Run `gh auth login`, or set $GITHUB_TOKEN. "
        "(task-cli reads gh's stored credential; it never re-prompts.)"
    )


def _gh_auth_token() -> str | None:
    """Ask the ``gh`` CLI for the current token. Returns ``None`` if gh is absent/unauthed."""
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tok = out.stdout.strip()
    return tok or None


def _gh_hosts_oauth_token(env: dict[str, str]) -> str | None:
    """Read an ``oauth_token`` from ``hosts.yml`` (older gh installs that store it in plaintext).

    Modern gh keeps the token in the keychain and ``hosts.yml`` has no token, so this is a
    best-effort last-resort fallback (env and ``gh auth token`` are tried first). It is parsed
    with a tiny line scanner ON PURPOSE: the credential path must work even when pyyaml is
    absent (config parsing degrades to defaults without it, but credential harvest must not).
    """
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    path = Path(base) / "gh" / "hosts.yml"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("oauth_token:"):
                tok = stripped.split(":", 1)[1].strip().strip("'\"")
                if tok:
                    return tok
    except OSError:
        return None
    return None


# ── Linear ─────────────────────────────────────────────────────────────────────────


def linear_key(env: dict[str, str] | None = None, workspace: str | None = None) -> LinearCreds:
    """Resolve a Linear API key. Raises :class:`CredentialError` if none is available.

    ``workspace`` (if given) selects a specific named entry in ``credentials.toml``; otherwise
    the file's ``default`` pointer is followed.
    """
    env = os.environ if env is None else env

    val = env.get("LINEAR_API_KEY")
    if val and val.strip():
        return LinearCreds(api_key=val.strip(), workspace=workspace or "env", source="env:LINEAR_API_KEY")

    creds = _linear_credentials_toml(env, workspace)
    if creds:
        return creds

    raise CredentialError(
        "no Linear API key found. Run `linear auth` (or set $LINEAR_API_KEY). "
        "(task-cli reads linear's stored key; it never re-prompts.)"
    )


def _linear_credentials_toml(env: dict[str, str], workspace: str | None) -> LinearCreds | None:
    """Parse ``~/.config/linear/credentials.toml``.

    Format (observed): ``default = "<workspace>"`` plus ``<workspace> = "lin_api_..."`` lines.
    The ``default`` value names which workspace key to use unless ``workspace`` overrides it.
    """
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    path = Path(base) / "linear" / "credentials.toml"
    if not path.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    target = workspace or data.get("default")
    if isinstance(target, str) and target and target != "default":
        key = data.get(target)
        if isinstance(key, str) and key.strip():
            return LinearCreds(api_key=key.strip(), workspace=target, source="~/.config/linear/credentials.toml")
    # fall back: any value that looks like a Linear key
    for name, key in data.items():
        if name == "default":
            continue
        if isinstance(key, str) and key.startswith("lin_api_"):
            return LinearCreds(api_key=key.strip(), workspace=str(name), source="~/.config/linear/credentials.toml")
    return None
