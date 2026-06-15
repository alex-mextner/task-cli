"""config.py — the cascade loader, zero-config defaults, and validation."""

from __future__ import annotations

import pytest

from tasklib.config import ConfigError, load


def test_zero_config_defaults_to_github(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert cfg.layers == ["builtin-defaults"]
    assert cfg.enforce["acceptance_criteria"] == "required"


def test_repo_task_yaml_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: linear\nlinear: {team: HYP}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"


def test_global_layer_under_repo(tmp_path, monkeypatch):
    gdir = tmp_path / "cfg" / "task-cli"
    gdir.mkdir(parents=True)
    (gdir / "config.yaml").write_text("classify: {bias: justAsk}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path)
    # global sets bias, repo keeps backend; repo wins on conflicts but bias only in global
    assert cfg.classify_bias == "justAsk"
    assert cfg.backend == "github-issues"


def test_invalid_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: jira\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_classify_fallbacks_parsed_to_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    chain = cfg.classify_fallbacks
    assert ("anthropic", "claude-haiku-4-5") in chain
    assert chain[0][0] == "anthropic"  # default head


def test_session_detect_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.session_detect == ("env:TASK_SESSION", "tmux-pane", "git-branch")


def test_unsupported_version_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_load_does_not_mutate_module_defaults(tmp_path, monkeypatch):
    # mutating a loaded config's nested dict must not leak into the module-global DEFAULTS
    # (deepcopy guard) — otherwise --repo on one run would contaminate the next.
    from tasklib.config import DEFAULTS

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    cfg.data["github"]["repo"] = "owner/repo-override"
    assert DEFAULTS["github"]["repo"] == "auto"
    # a fresh load is unaffected
    assert load(tmp_path).data["github"]["repo"] == "auto"


def test_session_label_prefix_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    assert load(tmp_path).session_label_prefix == "session:"
