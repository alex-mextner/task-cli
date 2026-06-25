"""Optional bridge to the ecosystem's shared model manifest (``models.yaml``).

WHAT this is: the *additive*, fail-soft consumer side of rig-cli#8 — resolving the
classifier's preferred model from the shared, capability-tagged manifest
(``agent-tools/lib/contracts/models.yaml``) instead of a literal pin that drifts. The
manifest is kept current by the daily cron checker, so a classifier that asks for "a
fast/reasoning/code model" gets whatever the manifest currently pins — no hand-edit here.

HOW it is reached at runtime: ``classify.resolve_chain`` calls
:func:`resolve_capability` when the ``classify.capability`` config key is set, and slots
the returned ``(provider, model)`` in as the PREFERRED head of the existing availability
fallback chain.

INVARIANT — fully optional, never fatal: every dependency this module touches is soft.
- The shared resolver (``agenttools_providers``) is an INDEPENDENT distribution that may
  not be installed alongside a standalone ``task`` — so the import is lazy and a missing
  package yields ``None``, never an ``ImportError`` that crashes ``task classify``.
- The manifest FILE may not be on this machine (task-cli installs standalone, without an
  agent-tools checkout) — a missing/garbage file yields ``None``.
- A misconfigured / unknown capability yields ``None`` (the caller falls through to the
  hardcoded chain), so a typo degrades to the prior behaviour, it does not error out.
Result: with no ``classify.capability``, or no manifest, or no resolver lib, behaviour is
byte-for-byte the pre-rig#8 chain — this only ADDS a manifest-resolved preferred head.
"""

from __future__ import annotations

import os
from pathlib import Path

# The env override a caller (or rig) can point at an explicit manifest. Checked first so a
# deployment that knows where its manifest lives never depends on the location heuristics.
MANIFEST_ENV_VAR = "TASK_MODELS_MANIFEST"

# The manifest's path WITHIN an agent-tools checkout — the stable handle rig keys on.
_MANIFEST_RELPATH = Path("lib") / "contracts" / "models.yaml"


def find_manifest(env: dict[str, str] | None = None) -> Path | None:
    """Locate ``models.yaml`` on this machine, or ``None`` if it isn't reachable.

    Resolution order (first hit wins), all OPTIONAL — a standalone ``task`` install with no
    agent-tools checkout simply gets ``None`` and the caller falls through:

    1. ``$TASK_MODELS_MANIFEST`` — an explicit path (a file rig/an operator points at).
    2. A few conventional agent-tools checkout locations under ``$AGENT_TOOLS_HOME`` / the
       common dev roots, each probed for ``lib/contracts/models.yaml``.

    No network, no import — pure filesystem probing, so it is cheap and side-effect-free.
    """
    env = os.environ if env is None else env

    explicit = env.get(MANIFEST_ENV_VAR)
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None

    for root in _candidate_checkout_roots(env):
        candidate = root / _MANIFEST_RELPATH
        if candidate.is_file():
            return candidate
    return None


def _candidate_checkout_roots(env: dict[str, str]) -> list[Path]:
    """Conventional agent-tools checkout roots to probe for the manifest, de-duplicated.

    ``$AGENT_TOOLS_HOME`` is the explicit pointer; the rest are the common dev-machine
    layouts an agent-tools checkout tends to sit at. Kept ordered + de-duped so the first
    real checkout wins and a repeated path is probed once.
    """
    # A PRESENT-but-empty HOME ("" — real in some containers/sandboxes) must NOT become Path(".")
    # and make the conventional roots cwd-relative (it could then pick up a stray models.yaml next
    # to the launch dir). Fall back to the expanded ~ on any falsy value, not just a missing key.
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    roots: list[Path] = []
    explicit_home = env.get("AGENT_TOOLS_HOME")
    if explicit_home:
        roots.append(Path(explicit_home).expanduser())
    roots += [
        home / "xp" / "agent-tools",
        home / "work" / "agent-tools",
        home / "agent-tools",
        home / "src" / "agent-tools",
    ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def resolve_capability(
    capability: str,
    *,
    env: dict[str, str] | None = None,
    manifest: Path | None = None,
) -> tuple[str, str] | None:
    """Resolve a ``capability``/role to a concrete ``(provider, model)`` via the manifest.

    Returns ``None`` — and the caller falls through to the hardcoded chain — on ANY soft
    failure: the resolver lib not installed, no manifest found, an unparseable manifest, or
    an unknown/misconfigured capability. Never raises for these; the whole point is that the
    manifest path is purely additive.

    ``capability`` is matched against the manifest's ``roles:`` / ``aliases:`` map (so the
    config may say ``fast`` / ``reasoning`` / ``code`` — the symbolic lenses the manifest
    already defines) and, failing that, against a capability TAG (the first entry carrying
    that capability, in the manifest's priority order). The returned ``provider`` is the
    manifest's provider for the resolved entry; the caller maps it onto its own ``review``
    ``-m`` arg + availability check.
    """
    try:
        from agenttools_providers import ProviderError, load_registry, resolve_role
    except ImportError:
        # The shared resolver is an independent distribution; a standalone `task` install may
        # not carry it. That is fine — fall through to the hardcoded chain.
        return None

    path = manifest if manifest is not None else find_manifest(env)
    if path is None:
        return None

    try:
        registry = load_registry(path)
    except Exception:  # noqa: BLE001 - a missing PyYAML / malformed manifest must not crash classify
        return None

    try:
        entry = _resolve_entry(registry, capability, resolve_role, ProviderError)
        if entry is None:
            _log_degrade(capability, "capability resolved to no model in the manifest")
            return None
        return (entry.provider, entry.id)
    except Exception as exc:  # noqa: BLE001 - the shared resolver is a foreign dist; ANY error must not crash classify
        # agenttools_providers is an INDEPENDENT distribution whose exception contract we don't
        # control: a future/older version could raise a KeyError/AttributeError (not ProviderError)
        # on a bad capability, OR rename ModelEntry's `.id`/`.provider` fields (an AttributeError on
        # the access below). Catch broadly so the module's "never fatal" invariant holds regardless
        # of the resolver's internals — and log it at DEBUG so a BROKEN integration is distinguishable
        # from a deliberate fall-through (a silent no-op would otherwise hide a contract mismatch).
        _log_degrade(capability, f"resolver raised {type(exc).__name__}: {exc}")
        return None


def _log_degrade(capability: str, reason: str) -> None:
    """DEBUG-log a manifest fall-through so a misconfig/broken-integration isn't fully silent.

    The classifier degrading to its hardcoded chain is by design, but a SILENT degrade makes a
    typo'd ``classify.capability`` or a drifted resolver contract undiagnosable. A DEBUG event
    (call-time import so tests can patch it) gives the operator a thread to pull without making the
    normal no-manifest path noisy.

    Itself NEVER raises: it is called from inside :func:`resolve_capability`'s broad ``except`` (and
    its happy ``entry is None`` branch), so a failing logger here must not become the exception that
    escapes and breaks the "never fatal" invariant. Any logging failure is swallowed.
    """
    try:
        from .logging import log_event

        log_event("classify.manifest-fallthrough", level="DEBUG", capability=capability, reason=reason)
    except Exception:  # noqa: BLE001 - observability is best-effort; never let logging crash classify
        pass


def _resolve_entry(registry, capability: str, resolve_role, provider_error):  # type: ignore[no-untyped-def]
    """Resolve ``capability`` to a single ModelEntry: a role/alias first, then a tag.

    Split out of :func:`resolve_capability` so the lazy-import shell stays small. A role or
    alias name (``fast``/``reasoning``/``code``) resolves via ``resolve_role``; if that name
    isn't a role, it is tried as a capability TAG and the first carrying entry (manifest
    priority order) is taken. A ``ProviderError`` (unknown role/tag) → ``None``; any OTHER
    resolver error propagates to the broad guard in :func:`resolve_capability`.

    Robust to a resolver whose ``resolve_role`` signals an unknown role by RETURNING a falsy
    value instead of raising ``ProviderError`` — in that case we still fall through to the
    capability-tag lookup rather than treating the falsy as a final answer.
    """
    try:
        resolved = resolve_role(registry, capability)
    except provider_error:
        resolved = None
    if resolved:
        return resolved
    try:
        carrying = registry.with_capability(capability)
    except provider_error:
        return None
    return carrying[0] if carrying else None
