"""credentials.py — harvest GitHub/Linear creds from env + existing CLI configs.

The ``gh auth token`` subprocess is monkeypatched; no real CLI is invoked.
"""

from __future__ import annotations

import pytest

from tasklib import credentials
from tasklib.credentials import CredentialError, github_token, linear_key


def test_github_token_from_env():
    creds = github_token({"GITHUB_TOKEN": "ghp_envtoken"})
    assert creds.token == "ghp_envtoken"
    assert creds.source == "env:GITHUB_TOKEN"


def test_github_token_from_gh_cli(monkeypatch):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: "gho_fromcli")
    creds = github_token({})
    assert creds.token == "gho_fromcli"
    assert creds.source == "gh auth token"


def test_github_token_from_hosts_yml(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: None)
    gh = tmp_path / "gh"
    gh.mkdir()
    (gh / "hosts.yml").write_text(
        "github.com:\n    oauth_token: ghp_fromhosts\n    user: x\n", encoding="utf-8"
    )
    creds = github_token({"XDG_CONFIG_HOME": str(tmp_path)})
    assert creds.token == "ghp_fromhosts"


def test_github_token_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: None)
    with pytest.raises(CredentialError):
        github_token({"XDG_CONFIG_HOME": str(tmp_path)})


def test_linear_key_from_env():
    creds = linear_key({"LINEAR_API_KEY": "lin_api_env"})
    assert creds.api_key == "lin_api_env"


def test_linear_key_follows_default_pointer(tmp_path):
    lin = tmp_path / "linear"
    lin.mkdir()
    (lin / "credentials.toml").write_text(
        'default = "glide-vc"\nglide-vc = "lin_api_realkey123456789"\n', encoding="utf-8"
    )
    creds = linear_key({"XDG_CONFIG_HOME": str(tmp_path)})
    assert creds.api_key == "lin_api_realkey123456789"
    assert creds.workspace == "glide-vc"


def test_linear_key_named_workspace_override(tmp_path):
    lin = tmp_path / "linear"
    lin.mkdir()
    (lin / "credentials.toml").write_text(
        'default = "a"\na = "lin_api_aaa1111111111111"\nb = "lin_api_bbb2222222222222"\n', encoding="utf-8"
    )
    creds = linear_key({"XDG_CONFIG_HOME": str(tmp_path)}, workspace="b")
    assert creds.api_key == "lin_api_bbb2222222222222"


def test_linear_key_missing_raises(tmp_path):
    with pytest.raises(CredentialError):
        linear_key({"XDG_CONFIG_HOME": str(tmp_path)})
