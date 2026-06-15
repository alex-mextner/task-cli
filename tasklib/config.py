"""Config cascade loader for ``task.yaml`` (per-repo) + ``~/.config/task-cli/config.yaml``.

Two layers, cascaded by **location** (no scope flag), exactly like rig-cli:

1. **Global** — ``~/.config/task-cli/config.yaml`` (or ``$XDG_CONFIG_HOME/...``). Machine-wide
   defaults a developer carries across repos.
2. **Per-repo** — ``task.yaml`` at the repo root. Committed by default; the reproducible
   source of truth; **overrides** the global layer.

The merge is a deep dict merge: per-repo keys win, dicts merge recursively, scalars and
lists replace wholesale. ``yaml`` is imported lazily so ``task --help`` works without PyYAML;
the loader degrades to built-in defaults if no config file is present (so the tool works with
**zero config** on any GitHub repo, as promised in §2).

Validation is fail-closed on the few enum-ish keys (backend, version), lenient on the rest —
this is a personal-tooling config, not a hostile-input parser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "task.yaml"

VALID_BACKENDS = {"github-issues", "linear"}


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any backend call)."""


def global_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    return Path(base) / "task-cli" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


# Built-in defaults — what the tool uses with zero config (GitHub Issues, everything enforced).
DEFAULTS: dict[str, Any] = {
    "version": 1,
    "backend": "github-issues",
    "github": {"repo": "auto", "default_labels": ["agent"]},
    "linear": {"team": "", "project": ""},
    "enforce": {
        "acceptance_criteria": "required",
        "motivation": "required",
        "user_impact": "required",
        "cost_of_inaction": "required",
        "formatting": "strict",
        "screenshots": {
            "on_create": {"required_if_label": ["ui", "visual"]},
            "on_done": {"required_if_label": ["ui", "visual"]},
        },
        "escape_hatch": "explain",
    },
    "classify": {
        "fallbacks": [
            {"anthropic": "claude-haiku-4-5"},
            {"openai": "gpt-5-mini"},
            {"commandcode": "deepseek/deepseek-v4-flash"},
            {"zai": "glm-4.6-flash"},
            {"google": "gemini-2.5-flash"},
            {"ollama": "qwen2.5:3b"},
        ],
        "bias": "change",
    },
    "session": {
        "detect": ["env:TASK_SESSION", "tmux-pane", "git-branch"],
        "label_prefix": "session:",
    },
}


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # lazy: keeps `task --help` dependency-free

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class LoadedConfig:
    """A cascaded, validated config plus provenance of where each layer came from."""

    data: dict[str, Any]
    repo_root: Path
    layers: list[str] = field(default_factory=list)

    @property
    def backend(self) -> str:
        return str(self.data.get("backend", "github-issues"))

    def section(self, name: str) -> dict[str, Any]:
        sec = self.data.get(name)
        return sec if isinstance(sec, dict) else {}

    @property
    def enforce(self) -> dict[str, Any]:
        return self.section("enforce")

    @property
    def classify_fallbacks(self) -> list[tuple[str, str]]:
        """The fallback chain as ``[(provider, model), ...]``, in declared order."""
        raw = self.section("classify").get("fallbacks") or []
        chain: list[tuple[str, str]] = []
        for entry in raw:
            if isinstance(entry, dict) and len(entry) == 1:
                ((provider, model),) = entry.items()
                chain.append((str(provider), str(model)))
        return chain

    @property
    def classify_bias(self) -> str:
        return str(self.section("classify").get("bias", "change"))

    @property
    def session_detect(self) -> tuple[str, ...]:
        raw = self.section("session").get("detect")
        if isinstance(raw, list) and raw:
            return tuple(str(x) for x in raw)
        return ("env:TASK_SESSION", "tmux-pane", "git-branch")

    @property
    def session_label_prefix(self) -> str:
        return str(self.section("session").get("label_prefix", "session:"))


def load(
    repo_root: Path,
    *,
    explicit_config: Path | None = None,
    include_global: bool = True,
    env: dict[str, str] | None = None,
) -> LoadedConfig:
    """Cascade-load config for ``repo_root``, layered over the built-in :data:`DEFAULTS`.

    ``explicit_config`` (from ``--config P``) replaces the per-repo layer. The result is
    validated (fail-closed on backend/version) before return. With no config file at all,
    the built-in defaults are returned (the zero-config path).
    """
    import copy

    repo_root = repo_root.resolve()
    # deepcopy: DEFAULTS has nested dicts; a shallow dict(DEFAULTS) would share them, so a
    # later mutation (e.g. cli `--repo` writing cfg.data["github"]["repo"]) would leak into
    # the module-global DEFAULTS for the whole process (and contaminate across main() calls).
    merged: dict[str, Any] = copy.deepcopy(DEFAULTS)
    layers: list[str] = ["builtin-defaults"]

    if include_global:
        gpath = global_config_path(env)
        if gpath.is_file():
            merged = _deep_merge(merged, _load_yaml(gpath))
            layers.append(f"global:{gpath}")

    if explicit_config is not None:
        rpath = explicit_config.resolve()
        if not rpath.is_file():
            raise ConfigError(f"--config file not found: {rpath}")
        merged = _deep_merge(merged, _load_yaml(rpath))
        layers.append(f"config:{rpath}")
    else:
        rpath = repo_config_path(repo_root)
        if rpath.is_file():
            merged = _deep_merge(merged, _load_yaml(rpath))
            layers.append(f"repo:{rpath}")

    validate(merged)
    return LoadedConfig(data=merged, repo_root=repo_root, layers=layers)


def validate(data: dict[str, Any]) -> None:
    """Fail-closed validation of the enum-ish keys."""
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    version = data.get("version", 1)
    if not isinstance(version, int):
        raise ConfigError(f"version must be an int, got {version!r}")
    if version != 1:
        raise ConfigError(f"unsupported config version {version} (this task supports v1)")

    backend = data.get("backend", "github-issues")
    if backend not in VALID_BACKENDS:
        raise ConfigError(f"backend must be one of {sorted(VALID_BACKENDS)}, got {backend!r}")

    for key in ("github", "linear", "enforce", "classify", "session"):
        if key in data and not isinstance(data[key], dict):
            raise ConfigError(f"'{key}' must be a mapping")
