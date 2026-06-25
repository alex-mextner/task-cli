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


def test_classify_capability_default_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.classify_capability == ""  # unset → no manifest resolution


def test_classify_capability_read_and_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        'version: 1\nbackend: github-issues\nclassify: {capability: "  fast  "}\n', encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.classify_capability == "fast"  # whitespace trimmed


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


# ── rig.yaml task: block — the CTO-decision tracker selector (#4136.4) ────────────────


def test_rig_yaml_missing_defaults_to_github(tmp_path, monkeypatch):
    # No rig.yaml and no task.yaml → the github default (the every-other-repo path).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_without_task_block_is_inert(tmp_path, monkeypatch):
    # A rig.yaml that says nothing about the tracker leaves the cascade on the github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("version: 1\nskills: {enabled: true}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_task_block_selects_linear(tmp_path, monkeypatch):
    # The hyperide case: a flat `task: {backend: linear, team: HYP}` block selects Linear/HYP.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "version: 1\ntask:\n  backend: linear\n  team: HYP\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert any(layer.endswith("rig.yaml") and layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_task_project_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task: {backend: linear, team: HYP, project: proj-123}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.section("linear")["project"] == "proj-123"


def test_rig_yaml_task_repo_alias_for_github(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: github-issues, repo: acme/web}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert cfg.section("github")["repo"] == "acme/web"


def test_task_yaml_wins_over_rig_yaml(tmp_path, monkeypatch):
    # A repo that keeps a native task.yaml is untouched: it overrides the rig.yaml block.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"


def test_explicit_config_overrides_rig_yaml(tmp_path, monkeypatch):
    # --config replaces the task.yaml layer but still sits above the rig.yaml task: block.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path, explicit_config=explicit)
    assert cfg.backend == "github-issues"


def test_rig_yaml_invalid_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: jira}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_not_a_mapping_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: linear\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_unknown_task_key_ignored_not_fatal(tmp_path, monkeypatch):
    # Forward-compat: an unknown sub-key (e.g. one a NEWER rig-cli writes) is warned-and-skipped,
    # not fatal — it must not crash every `task` command in a repo. The known keys still apply.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP, squad: blue}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert "squad" not in cfg.data  # the unknown key did not leak into the config


def test_rig_yaml_non_mapping_document_is_inert(tmp_path, monkeypatch):
    # A rig.yaml whose root is not a mapping must not crash task; _load_yaml guards it, and
    # rig_task_overlay is defensively inert on a non-dict too.
    from tasklib.config import rig_task_overlay

    assert rig_task_overlay([]) == {}  # type: ignore[arg-type]
    assert rig_task_overlay("nonsense") == {}  # type: ignore[arg-type]


def test_rig_yaml_non_mapping_root_via_load_falls_through(tmp_path, monkeypatch):
    # End-to-end: a rig.yaml with a list/scalar root (a foreign file rig-cli owns) must NOT
    # crash every `task` command — it falls through cleanly to the github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_broken_yaml_via_load_falls_through(tmp_path, monkeypatch):
    # Likewise, unparseable YAML in rig.yaml is tolerated (logged, ignored), not fatal.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, : broken\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"


def test_rig_yaml_present_but_pyyaml_missing_falls_through(tmp_path, monkeypatch):
    # The installer can continue without PyYAML (built-in defaults promised). A repo that merely
    # CONTAINS a rig.yaml must NOT traceback with ModuleNotFoundError when `import yaml` fails in
    # _load_yaml — it falls through to the github default, same as an unreadable rig.yaml, and
    # logs a WARN (the contract: ignore-and-warn, not silently drop).
    import sys

    import tasklib.logging as task_logging

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")

    warns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        task_logging,
        "log_event",
        lambda event, level="INFO", **f: warns.append((level, event)),
    )
    # Stub yaml out of the import system. On CPython a None entry makes `import yaml` raise a
    # real ModuleNotFoundError with name="yaml" (verified: "import of yaml halted; None in
    # sys.modules") — a true integration check that drives the actual `import yaml` in _load_yaml,
    # unlike the sibling tests that mock _load_yaml. Do NOT "fix" this to a message-match shim.
    monkeypatch.setitem(sys.modules, "yaml", None)

    cfg = load(tmp_path)  # must not raise
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)
    assert ("WARN", "rig.yaml not loaded; ignoring") in warns


@pytest.mark.parametrize("missing", ["something_else", "yaml_processor", "my_yaml_utils"])
def test_rig_yaml_unrelated_modulenotfound_propagates(tmp_path, monkeypatch, missing):
    # The PyYAML-missing tolerance is narrow and keyed on exc.name only: a ModuleNotFoundError for
    # anything OTHER than the yaml package is a genuinely broken environment and must surface, not
    # masquerade as "no rig.yaml". Includes substring-collision names (yaml_processor) that a
    # message-based match would have wrongly swallowed.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear}\n", encoding="utf-8")

    import tasklib.config as config_mod

    def _boom(_path):
        raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)

    monkeypatch.setattr(config_mod, "_load_yaml", _boom)

    with pytest.raises(ModuleNotFoundError):
        load(tmp_path)


@pytest.mark.parametrize("yaml_name", ["yaml", "_yaml", "yaml.cyaml"])
def test_rig_yaml_pyyaml_submodule_missing_falls_through(tmp_path, monkeypatch, yaml_name):
    # A partially-broken PyYAML whose C-extension or a submodule is missing (exc.name in the yaml
    # package) is treated the same as PyYAML wholly absent: ignore-and-warn, github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear}\n", encoding="utf-8")

    import tasklib.config as config_mod

    def _boom(_path):
        raise ModuleNotFoundError(f"No module named '{yaml_name}'", name=yaml_name)

    monkeypatch.setattr(config_mod, "_load_yaml", _boom)

    cfg = load(tmp_path)  # must not raise
    assert cfg.backend == "github-issues"


def test_rig_yaml_task_version_subkey_ignored_not_fatal(tmp_path, monkeypatch):
    # `version` under task: is NOT task-cli's schema version (rig.yaml has its own top-level
    # version); a stray one is warn-and-ignored, not fail-closed — forward-compat with rig-cli.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP, version: 99}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.data["version"] == 1  # task-cli's own version is untouched by the stray key


def test_rig_yaml_task_flat_alias_merges_with_nested_section(tmp_path, monkeypatch):
    # README invites mixing a flat shorthand (team:) with a nested section (linear:) in the
    # same task: block — they must deep-merge, not clobber, regardless of key order.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  team: HYP\n  linear: {project: proj-123}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    lin = cfg.section("linear")
    assert lin["team"] == "HYP"  # from the flat alias
    assert lin["project"] == "proj-123"  # from the nested section


def test_rig_yaml_task_flat_alias_merges_nested_section_reverse_order(tmp_path, monkeypatch):
    # Same as above but the nested section is declared BEFORE the alias — order-independent.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  linear: {project: proj-123}\n  team: HYP\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    lin = cfg.section("linear")
    assert lin["team"] == "HYP"
    assert lin["project"] == "proj-123"


def test_rig_yaml_task_alias_vs_nested_leaf_conflict_rejected(tmp_path, monkeypatch):
    # team: HYP and linear.team: OTHER both set linear.team to different values → hard error.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  team: HYP\n  linear: {team: OTHER}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_repo_alias_merges_with_github_section(tmp_path, monkeypatch):
    # Symmetric to the linear case: the repo: shorthand and a nested github: section deep-merge.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: github-issues\n  repo: acme/web\n  github: {default_labels: [bug]}\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    gh = cfg.section("github")
    assert gh["repo"] == "acme/web"  # from the flat alias
    assert gh["default_labels"] == ["bug"]  # from the nested section


def test_rig_yaml_task_repo_alias_vs_github_leaf_conflict_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  repo: acme/web\n  github: {repo: other/repo}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_malformed_backend_value_clean_error(tmp_path, monkeypatch):
    # A non-string backend (dict/list) from the foreign rig.yaml must raise a clean ConfigError,
    # not an unhandled TypeError on the `in` membership test against the VALID_BACKENDS set.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task:\n  backend: {nested: bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_passthrough_section_applies(tmp_path, monkeypatch):
    # A nested enforce: section in the rig.yaml task: block actually lands in the config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: github-issues\n  enforce: {motivation: optional}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.enforce["motivation"] == "optional"
    # other enforce defaults survive the deep merge
    assert cfg.enforce["acceptance_criteria"] == "required"


def test_rig_yaml_task_global_layer_carries(tmp_path, monkeypatch):
    # rig.yaml task: sits above global, so a global classify.bias still carries through.
    gdir = tmp_path / "cfg" / "task-cli"
    gdir.mkdir(parents=True)
    (gdir / "config.yaml").write_text("classify: {bias: justAsk}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.classify_bias == "justAsk"
