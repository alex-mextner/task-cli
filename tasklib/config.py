"""Config cascade loader for the per-repo ``rig.yaml`` / ``task.yaml`` + global config.

Layers, cascaded by **location** (no scope flag), exactly like rig-cli — later layers win:

1. **Built-in defaults** — GitHub Issues, every gate on (the zero-config path).
2. **Global** — ``~/.config/task-cli/config.yaml`` (or ``$XDG_CONFIG_HOME/...``). Machine-wide
   defaults a developer carries across repos.
3. **Per-repo ``rig.yaml`` ``task:`` block** — the repo's committed ``rig.yaml`` is the single
   source of truth for the whole agent toolchain (rig provisions it). Its ``task:`` block
   selects the tracker backend per repo (CTO decision #4136.4: hyperide → Linear/HYP, every
   other repo → GitHub Issues by default). See :func:`rig_task_overlay` for the shape.
4. **Per-repo ``task.yaml``** — the native task-cli config, if a repo keeps one. It still wins
   over ``rig.yaml`` so an existing repo with a hand-tuned ``task.yaml`` is untouched.
5. **``--config P``** — an explicit file replaces the ``task.yaml`` layer.

The merge is a deep dict merge: later keys win, dicts merge recursively, scalars and lists
replace wholesale. ``yaml`` is imported lazily so ``task --help`` works without PyYAML; the
loader degrades to built-in defaults if no config file is present (so the tool works with
**zero config** on any GitHub repo — and a repo with only a ``rig.yaml`` task: block needs no
``task.yaml`` at all).

Validation is fail-closed on the few enum-ish keys (backend, version), lenient on the rest —
this is a personal-tooling config, not a hostile-input parser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "task.yaml"
RIG_CONFIG_FILENAME = "rig.yaml"

VALID_BACKENDS = {"github-issues", "linear"}


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any backend call)."""


def global_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    return Path(base) / "task-cli" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


def rig_config_path(repo_root: Path) -> Path:
    return repo_root / RIG_CONFIG_FILENAME


# Aliases in the rig.yaml ``task:`` block → the dotted path they map onto in task-cli's own
# config shape. The block is intentionally flat (a CTO drops `task: {backend: linear, team:
# HYP}` into rig.yaml without learning task-cli's nesting), so we translate the shorthands.
# ``team``/``project`` are Linear coordinates; ``repo`` is the GitHub coordinate.
_RIG_TASK_ALIASES: dict[str, tuple[str, ...]] = {
    "team": ("linear", "team"),
    "project": ("linear", "project"),
    "repo": ("github", "repo"),
}
# Keys passed through verbatim (same name in both shapes): the backend selector + the nested
# sections a power user might want to override straight from rig.yaml. ``version`` is
# intentionally NOT here — that is task-cli's own config-schema version (rig.yaml carries its
# OWN top-level version), and passing it through would make it fail-closed, so a future rig
# bumping a stray task.version would crash an older task-cli — the opposite of forward-compat.
_RIG_TASK_PASSTHROUGH = frozenset(
    {"backend", "github", "linear", "projects", "enforce", "classify", "session"}
)


def rig_task_overlay(rig_data: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``rig.yaml`` ``task:`` block into a task-cli config overlay.

    The block is the per-repo tracker selection the CTO drops into a repo's ``rig.yaml``::

        task:
          backend: linear      # or github-issues (the default)
          team: HYP            # → linear.team
          project: ""          # → linear.project
          # repo: owner/name   # → github.repo (for the github-issues backend)

    Returns ``{}`` when there is no ``task:`` block, so a ``rig.yaml`` that says nothing about
    the tracker leaves the cascade untouched (clean fall-through to the github default). The
    same for a ``rig.yaml`` whose document root is not a mapping (defensive — ``_load_yaml``
    already guards this, but this keeps the function safe on any caller). A non-mapping
    ``task:`` is a config error (fail-closed before any backend call).

    Unknown sub-keys inside ``task:`` are **warned-and-ignored**, not fatal: ``rig.yaml`` is
    owned and evolved by rig-cli, so a sub-key a newer rig writes must not crash every ``task``
    command in a repo just because this task-cli predates it (forward-compat across two tools
    that ship independently). This mirrors the lenient handling of the native ``task.yaml``.
    """
    if not isinstance(rig_data, dict):
        return {}
    block = rig_data.get("task")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"'task' block in {RIG_CONFIG_FILENAME} must be a mapping, got {type(block).__name__}")

    # Each key contributes a fragment; we deep-merge them so a flat shorthand (team: HYP →
    # {linear: {team}}) and a same-named nested section (linear: {project}) in the SAME block
    # combine instead of clobbering. Order-independent: deep-merge is commutative on disjoint
    # leaves, and a true leaf collision (same dotted path set twice) is caught explicitly.
    import copy

    overlay: dict[str, Any] = {}
    for key, value in block.items():
        if key in _RIG_TASK_ALIASES:
            *parents, leaf = _RIG_TASK_ALIASES[key]
            fragment: dict[str, Any] = {}
            cursor = fragment
            for part in parents:
                cursor = cursor.setdefault(part, {})
            cursor[leaf] = value
        elif key in _RIG_TASK_PASSTHROUGH:
            fragment = {key: copy.deepcopy(value)} if isinstance(value, dict) else {key: value}
        else:
            # Forward-compat: a sub-key a newer rig-cli writes that this task-cli doesn't know.
            # Skip it (don't crash); log so a genuine typo is still discoverable (TASK_LOG=json).
            from .logging import log_event

            log_event("rig.task: ignoring unknown key", level="WARN", key=str(key))
            continue
        _assert_no_leaf_conflict(overlay, fragment, prefix=f"{RIG_CONFIG_FILENAME} task")
        overlay = _deep_merge(overlay, fragment)
    return overlay


def _assert_no_leaf_conflict(base: dict[str, Any], over: dict[str, Any], *, prefix: str) -> None:
    """Raise ConfigError if ``over`` would overwrite a leaf that ``base`` already set.

    Deep-merge silently lets a later scalar replace an earlier one; for the ``task:`` block we
    want a true collision (e.g. ``team: HYP`` AND ``linear: {team: X}`` both setting
    ``linear.team``) to be a hard error, not a last-writer-wins surprise.
    """
    for k, v in over.items():
        if k not in base:
            continue
        bv = base[k]
        if isinstance(bv, dict) and isinstance(v, dict):
            _assert_no_leaf_conflict(bv, v, prefix=f"{prefix}.{k}")
        elif bv != v:
            raise ConfigError(f"conflicting value for '{prefix}.{k}' in {RIG_CONFIG_FILENAME}")


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


def _ignore_rig(exc: Exception) -> dict[str, Any]:
    """Log a foreign-rig.yaml problem at WARN and yield an empty overlay (clean fall-through).

    Shared by the rig.yaml load's tolerated failures — unreadable/invalid content
    (``ConfigError``) and PyYAML being absent (``ModuleNotFoundError`` on the yaml import) — so
    both warn-and-ignore identically. The message says "not loaded" rather than "unreadable": a
    missing-PyYAML file is perfectly readable, only unparsed.

    The ``log_event`` import is deliberately call-time (not hoisted to module top): tests patch
    ``tasklib.logging.log_event`` to assert the WARN, which only works while the lookup is
    deferred to call time. Keep it here.
    """
    from .logging import log_event

    log_event("rig.yaml not loaded; ignoring", level="WARN", error=str(exc))
    return {}


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

    def with_overlay(self, overlay: dict[str, Any]) -> "LoadedConfig":
        """A fresh config with ``overlay`` deep-merged over this one's data (self untouched).

        Used to resolve a registered project's backend coordinates against the same base
        config (so per-repo ``enforce``/``classify`` defaults carry, but the project's
        ``backend``/``github``/``linear`` win). The clone is deep so aggregating across
        projects can't leak one project's coordinates into the next.
        """
        import copy

        merged = _deep_merge(copy.deepcopy(self.data), overlay)
        return LoadedConfig(data=merged, repo_root=self.repo_root, layers=list(self.layers))

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
    def classify_capability(self) -> str:
        """The capability/role to resolve the classifier's preferred model from ``models.yaml``.

        Empty (the default) means "no manifest resolution" — the classifier uses the
        hardcoded/config fallback chain alone, exactly as before rig#8. A value (``fast`` /
        ``reasoning`` / ``code`` — a role or capability tag the shared manifest defines)
        makes the classifier prefer the model the manifest currently pins for it, kept fresh
        by the cron checker. (One caveat: for the ``gemini`` provider the manifest steers only
        the provider/role, not the exact version — ``review``'s gemini backend uses its own
        env-configured model; every other provider carries the manifest's exact id.) The
        resolution is fail-soft: an unresolvable value, a missing manifest, or the resolver
        lib being absent all fall through to the fallback chain.
        """
        return str(self.section("classify").get("capability", "")).strip()

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

    Layer order (later wins): defaults → global → ``rig.yaml`` ``task:`` block → ``task.yaml``
    → ``--config P`` (the last replaces the ``task.yaml`` layer). The ``rig.yaml`` block is the
    canonical per-repo tracker selector (rig provisions it); a native ``task.yaml`` still wins
    so a repo that keeps one is unaffected. The result is validated (fail-closed on
    backend/version) before return. With no config file at all the built-in defaults are
    returned (the zero-config GitHub-Issues path).
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

    # rig.yaml task: block — the per-repo tracker selector, below the native task.yaml so a
    # repo keeping a hand-tuned task.yaml is untouched. Read whether or not --config is given:
    # --config replaces the task.yaml layer, not the toolchain's rig.yaml selection.
    # rig.yaml is a FOREIGN file (owned/evolved by rig-cli): a parse error or unexpected root
    # shape must NOT crash every `task` command in the repo — it falls through to the github
    # default (forward-compat). This tolerance is rig.yaml-only; task-cli's own task.yaml/global
    # stay fail-closed below.
    rig_path = rig_config_path(repo_root)
    if rig_path.is_file():
        try:
            rig_data = _load_yaml(rig_path)
        except ConfigError as exc:
            rig_data = _ignore_rig(exc)
        except ModuleNotFoundError as exc:
            # PyYAML absent — the installer may continue without it, promising built-in defaults.
            # A repo merely CONTAINING a rig.yaml must NOT traceback then; it falls through to the
            # github default, same as any other foreign-file problem above. ModuleNotFoundError
            # (not the broader ImportError: a circular/broken-init ImportError is a real bug that
            # must surface) scoped to the yaml package by exc.name keeps this to "PyYAML not
            # installed" — any other missing module is a broken environment and is re-raised.
            # Matched on exc.name only (CPython always sets it), never the message, so a missing
            # module that merely has "yaml" in its name (e.g. yaml_processor) still surfaces.
            name = exc.name or ""
            if not (name == "yaml" or name == "_yaml" or name.startswith("yaml.")):
                raise
            rig_data = _ignore_rig(exc)
        overlay = rig_task_overlay(rig_data)
        if overlay:
            merged = _deep_merge(merged, overlay)
            layers.append(f"rig:{rig_path}")

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
    # isinstance guard BEFORE the membership test: VALID_BACKENDS is a set, so a non-hashable
    # value (a dict/list from a malformed `backend:` in the foreign rig.yaml) would raise an
    # unhandled TypeError on `in`. Convert that into a clean fail-closed ConfigError.
    if not isinstance(backend, str) or backend not in VALID_BACKENDS:
        raise ConfigError(f"backend must be one of {sorted(VALID_BACKENDS)}, got {backend!r}")

    for key in ("github", "linear", "enforce", "classify", "session"):
        if key in data and not isinstance(data[key], dict):
            raise ConfigError(f"'{key}' must be a mapping")
