"""manifest.py — models.yaml discovery + fail-soft capability resolution (rig#8 consumer)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tasklib import manifest

# The shared resolver (agenttools_providers) is an INDEPENDENT distribution that may not be
# installed alongside a standalone task-cli. The real-resolution tests are conditional on it.
_HAS_PROVIDERS = importlib.util.find_spec("agenttools_providers") is not None
_needs_providers = pytest.mark.skipif(
    not _HAS_PROVIDERS, reason="agenttools_providers (the shared resolver) is not installed"
)

# A minimal, valid models.yaml fixture — one entry per role we assert on.
_MANIFEST_YAML = """\
version: 1
models:
  - id: claude-opus-4-8
    provider: anthropic
    capabilities: [vision, reasoning, code]
  - id: gemini-2.5-flash
    provider: gemini
    capabilities: [vision, reasoning, code]
  - id: kimi-k2.7-code
    provider: commandcode
    capabilities: [code, reasoning]
roles:
  reasoning: claude-opus-4-8
  fast: gemini-2.5-flash
  code: kimi-k2.7-code
aliases:
  anthropic:latest: claude-opus-4-8
"""


def _write_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "lib" / "contracts" / "models.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(_MANIFEST_YAML, encoding="utf-8")
    return path


# ── discovery ────────────────────────────────────────────────────────────────────────


def test_find_manifest_env_override_wins(tmp_path):
    target = tmp_path / "custom-models.yaml"
    target.write_text(_MANIFEST_YAML, encoding="utf-8")
    found = manifest.find_manifest({manifest.MANIFEST_ENV_VAR: str(target)})
    assert found == target


def test_find_manifest_env_override_missing_file_is_none(tmp_path):
    found = manifest.find_manifest({manifest.MANIFEST_ENV_VAR: str(tmp_path / "nope.yaml")})
    assert found is None


def test_find_manifest_via_agent_tools_home(tmp_path):
    _write_manifest(tmp_path)
    found = manifest.find_manifest({"AGENT_TOOLS_HOME": str(tmp_path), "HOME": "/nonexistent"})
    assert found == tmp_path / "lib" / "contracts" / "models.yaml"


def test_find_manifest_env_override_beats_checkout(tmp_path):
    # both a discoverable agent-tools checkout AND $TASK_MODELS_MANIFEST exist → the env wins, so a
    # multi-checkout machine can pin the manifest deterministically (step 1 over step 2).
    checkout = tmp_path / "checkout"
    _write_manifest(checkout)
    override = tmp_path / "pinned-models.yaml"
    override.write_text(_MANIFEST_YAML, encoding="utf-8")
    found = manifest.find_manifest(
        {
            manifest.MANIFEST_ENV_VAR: str(override),
            "AGENT_TOOLS_HOME": str(checkout),
            "HOME": str(checkout),
        }
    )
    assert found == override  # the explicit env path, not the discovered checkout


def test_find_manifest_none_when_no_checkout(tmp_path):
    # an empty HOME with no agent-tools checkout anywhere → nothing found (standalone install)
    assert manifest.find_manifest({"HOME": str(tmp_path)}) is None


# ── resolution: fail-soft ──────────────────────────────────────────────────────────────


def test_resolve_capability_none_when_manifest_absent(tmp_path):
    # no manifest discoverable → None (caller falls through to the hardcoded chain)
    assert manifest.resolve_capability("reasoning", env={"HOME": str(tmp_path)}) is None


def test_resolve_capability_none_when_resolver_lib_absent(tmp_path, monkeypatch):
    # the shared resolver not being importable → None, regardless of a present manifest. Force the
    # ImportError branch so the standalone-install path is exercised even when the lib IS installed.
    path = _write_manifest(tmp_path)
    import builtins

    real_import = builtins.__import__

    def _no_providers(name, *args, **kwargs):
        if name == "agenttools_providers":
            raise ImportError("simulated standalone install: no agenttools_providers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_providers)
    assert manifest.resolve_capability("reasoning", manifest=path) is None


@_needs_providers
def test_resolve_capability_none_on_garbage_manifest(tmp_path):
    # a malformed manifest → None. Needs the resolver installed: without it the ImportError branch
    # short-circuits before load_registry, so the parse-failure path it targets never runs.
    bad = tmp_path / "bad.yaml"
    bad.write_text("{ not: valid: yaml ::", encoding="utf-8")
    out = manifest.resolve_capability("reasoning", manifest=bad)
    assert out is None


# ── resolution: real (needs the shared resolver) ──────────────────────────────────────


# ── resolution: real logic exercised with a FAKE resolver (runs in standalone CI) ─────
# These prove _resolve_entry's role→tag fallback and resolve_capability's broad guard WITHOUT the
# real agenttools_providers, so the logic is covered even on a standalone task-cli install where
# the shared lib isn't present (the @_needs_providers tests below skip there).


class _FakeProviderError(ValueError):
    pass


class _FakeEntry:
    def __init__(self, id_: str, provider: str):
        self.id = id_
        self.provider = provider


class _FakeRegistry:
    def __init__(self, roles, by_cap):
        self._roles = roles
        self._by_cap = by_cap

    def with_capability(self, capability: str):
        if capability not in self._by_cap:
            raise _FakeProviderError(f"unknown capability {capability!r}")
        return self._by_cap[capability]


def _install_fake_providers(monkeypatch, *, roles, by_cap, resolve_raises=None, load_raises=None):
    import sys
    import types

    registry = _FakeRegistry(roles, by_cap)

    def _resolve_role(_reg, role):
        if resolve_raises is not None:
            raise resolve_raises
        if role in roles:
            return roles[role]
        raise _FakeProviderError(f"unknown role {role!r}")

    def _load_registry(_path, **_kw):
        if load_raises is not None:
            raise load_raises
        return registry

    fake = types.ModuleType("agenttools_providers")
    fake.ProviderError = _FakeProviderError
    fake.resolve_role = _resolve_role
    fake.load_registry = _load_registry
    monkeypatch.setitem(sys.modules, "agenttools_providers", fake)


def test_resolve_capability_role_via_fake(tmp_path, monkeypatch):
    opus = _FakeEntry("claude-opus-4-8", "anthropic")
    _install_fake_providers(monkeypatch, roles={"reasoning": opus}, by_cap={})
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("reasoning", manifest=path) == ("anthropic", "claude-opus-4-8")


def test_resolve_capability_tag_fallback_via_fake(tmp_path, monkeypatch):
    # `vision` is not a role → _resolve_entry falls back to with_capability and takes the first.
    flash = _FakeEntry("gemini-2.5-flash", "gemini")
    _install_fake_providers(monkeypatch, roles={}, by_cap={"vision": [flash]})
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("vision", manifest=path) == ("gemini", "gemini-2.5-flash")


def test_resolve_capability_unknown_returns_none_via_fake(tmp_path, monkeypatch):
    _install_fake_providers(monkeypatch, roles={}, by_cap={})
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("nope", manifest=path) is None


def test_resolve_capability_broad_guard_swallows_foreign_error(tmp_path, monkeypatch):
    # a foreign resolver raising a NON-ProviderError (the never-fatal invariant's hard case) → None.
    _install_fake_providers(monkeypatch, roles={}, by_cap={}, resolve_raises=KeyError("boom"))
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("reasoning", manifest=path) is None


def test_resolve_capability_load_failure_via_fake(tmp_path, monkeypatch):
    _install_fake_providers(monkeypatch, roles={}, by_cap={}, load_raises=RuntimeError("bad yaml"))
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("reasoning", manifest=path) is None


def test_resolve_capability_falls_through_when_resolve_role_returns_falsy(tmp_path, monkeypatch):
    # a resolver whose resolve_role RETURNS None for an unknown role (instead of raising) must
    # still fall through to the capability-tag lookup — not treat the falsy as the final answer.
    import sys
    import types

    flash = _FakeEntry("gemini-2.5-flash", "gemini")

    def _resolve_role_returns_none(_reg, _role):
        return None  # unknown role signalled by a falsy return, not an exception

    class _Reg:
        def with_capability(self, _cap):
            return [flash]

    fake = types.ModuleType("agenttools_providers")
    fake.ProviderError = _FakeProviderError
    fake.resolve_role = _resolve_role_returns_none
    fake.load_registry = lambda _p, **_k: _Reg()
    monkeypatch.setitem(sys.modules, "agenttools_providers", fake)

    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("reasoning", manifest=path) == ("gemini", "gemini-2.5-flash")


def test_resolve_capability_logs_degrade_on_no_match(tmp_path, monkeypatch):
    # a configured-but-unresolvable capability degrades to the chain — but emits a DEBUG event so
    # a misconfig is diagnosable, not fully silent.
    _install_fake_providers(monkeypatch, roles={}, by_cap={})
    events = []
    monkeypatch.setattr("tasklib.logging.log_event", lambda *a, **k: events.append((a, k)))
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("nope", manifest=path) is None
    assert any(a and a[0] == "classify.manifest-fallthrough" for a, _ in events)


# ── _candidate_checkout_roots: $AGENT_TOOLS_HOME precedence + de-dup ───────────────────


def test_candidate_roots_agent_tools_home_first_and_deduped():
    roots = manifest._candidate_checkout_roots({"AGENT_TOOLS_HOME": "/explicit/at", "HOME": "/h"})
    assert roots[0] == Path("/explicit/at")
    assert len(roots) == len(set(roots))  # no duplicate paths probed twice


def test_candidate_roots_empty_home_not_cwd_relative():
    # a PRESENT-but-empty HOME must not make the conventional roots cwd-relative (Path(".")) — they
    # fall back to the expanded ~ instead, so probing can't pick up a stray cwd-adjacent manifest.
    roots = manifest._candidate_checkout_roots({"HOME": ""})
    assert all(r.is_absolute() for r in roots)
    assert Path("xp") / "agent-tools" not in roots  # not the cwd-relative form


@_needs_providers
def test_resolve_capability_role(tmp_path):
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("reasoning", manifest=path) == ("anthropic", "claude-opus-4-8")
    assert manifest.resolve_capability("fast", manifest=path) == ("gemini", "gemini-2.5-flash")
    assert manifest.resolve_capability("code", manifest=path) == ("commandcode", "kimi-k2.7-code")


@_needs_providers
def test_resolve_capability_by_tag_when_not_a_role(tmp_path):
    # `vision` is a capability tag (not a role in this fixture) → first carrying entry wins.
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("vision", manifest=path) == ("anthropic", "claude-opus-4-8")


@_needs_providers
def test_resolve_capability_unknown_is_none(tmp_path):
    path = _write_manifest(tmp_path)
    assert manifest.resolve_capability("no-such-capability", manifest=path) is None
