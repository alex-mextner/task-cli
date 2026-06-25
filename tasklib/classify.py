"""Classification of an inbound message → ``change | justAsk``.

Pure parts only: building the prompt, parsing ``review``'s answer, and resolving the first
*available* model from a per-provider fallback chain. The effectful shell-out to
``review just-ask`` lives in the entrypoint (``bin/task``) — this module decides WHAT to
run and HOW to read the result, so it is fully unit-testable without spawning anything.

Bias is to ``change`` when ambiguous — most questions to a dev agent are latent change
requests (spec §7).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from enum import Enum

CLASSIFY_PROMPT = (
    "Classify this message to a dev agent as `change` (should result in a code/doc/config "
    "edit -> needs a ticket) or `justAsk` (pure question, no edit). Bias to `change` "
    "when ambiguous -- most questions to a dev agent are latent edit requests. "
    "Respond with EXACTLY one line and nothing else: the literal word VERDICT, a colon, "
    "then your one-word answer (the change/justAsk word).\n\nMESSAGE:\n"
)

# The sentinel the model is asked to emit. Parsing keys off this first; it is unambiguous and
# cannot collide with the prompt body (which deliberately avoids the bare verdict words now).
_VERDICT_RE = re.compile(r"verdict\s*:\s*(change|justask|just-ask|just ask)", re.IGNORECASE)


class Verdict(str, Enum):
    CHANGE = "change"
    JUST_ASK = "justAsk"


# Default per-provider fallback chain (§2). One cheap/fast model PER PROVIDER; the runner
# uses the first AVAILABLE one. Default head is haiku. Each entry is (provider, model-id).
DEFAULT_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("anthropic", "claude-haiku-4-5"),
    ("openai", "gpt-5-mini"),
    ("commandcode", "deepseek/deepseek-v4-flash"),
    ("zai", "glm-4.6-flash"),
    ("google", "gemini-2.5-flash"),
    ("ollama", "qwen2.5:3b"),
)

# How review-cli names each provider in a ``-m`` model string, and which env var(s) signal
# the provider is reachable. ollama is reachable when the local daemon binary exists.
_PROVIDER_MODEL_PREFIX = {
    "anthropic": "claude",
    "openai": "commandcode",  # OpenAI models are routed through the commandcode gateway
    "commandcode": "commandcode",
    "zai": "zai",
    "google": "gemini",
    "ollama": "ollama",
}

_PROVIDER_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    # OpenAI models are routed through the commandcode gateway (the model arg is
    # `commandcode:<model>`), so reachability is the COMMANDCODE key — a bare OPENAI_API_KEY
    # would make us emit a commandcode arg that the gateway can't authenticate. Keep these in
    # lockstep with _PROVIDER_MODEL_PREFIX["openai"] == "commandcode".
    "openai": ("COMMANDCODE_API_KEY",),
    "commandcode": ("COMMANDCODE_API_KEY",),
    "zai": ("ZAI_API_KEY", "ZHIPU_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "ollama": (),  # availability is the daemon, not a key
}

# The shared manifest (models.yaml) names Google's provider ``gemini`` and has no ``ollama``;
# this CLI's provider tables use ``google`` (and route ``review`` to a bare ``gemini`` arg).
# Map a manifest provider onto this CLI's provider key so a manifest-resolved entry slots
# into the SAME availability check + ``-m`` building as the hardcoded chain. Providers not
# listed pass through unchanged (anthropic/openai/commandcode/zai already line up).
#
# KNOWN LIMITATION (google only): ``to_model_arg("google", …)`` collapses to the bare ``gemini``
# token because review's gemini backend ignores a model id and uses its OWN env-configured model.
# So for a manifest entry whose provider is ``gemini`` the manifest's exact VERSION is not threaded
# into ``-m`` — the manifest only steers WHICH provider/role is preferred, and review picks the
# concrete gemini version. Every other provider DOES carry the manifest's exact model id.
_MANIFEST_PROVIDER_ALIASES = {"gemini": "google"}


@dataclass
class ResolvedModel:
    """A resolved fallback entry — the provider plus the ``-m`` string for ``review``."""

    provider: str
    model_arg: str


def to_model_arg(provider: str, model: str) -> str:
    """Build the ``review -m`` string for a (provider, model) pair.

    ``claude:claude-haiku-4-5``, ``commandcode:deepseek/...``, ``zai:glm-4.6-flash``,
    ``gemini`` (bare; review's gemini backend takes the env model), ``ollama:qwen2.5:3b``.
    Two providers are special-cased explicitly (rather than only via the prefix table) so the
    routing is obvious to a reader:
      - ``google`` → the bare ``gemini`` token (review's gemini backend ignores a model id);
      - ``openai`` → the ``commandcode:`` gateway prefix (review reaches OpenAI models through
        the commandcode gateway, so reachability is the COMMANDCODE key — see _PROVIDER_KEY_ENV).
    """
    if provider == "google":
        return "gemini"
    prefix = _PROVIDER_MODEL_PREFIX.get(provider, provider)  # openai -> "commandcode"
    return f"{prefix}:{model}"


def provider_available(provider: str, env: dict[str, str] | None = None) -> bool:
    """Is a provider reachable? Key present in env, or (ollama) the daemon binary exists.

    Pure-ish: reads ``env`` (defaults to ``os.environ``) and, for ollama only, checks for the
    binary on PATH. No network probe — availability here means "we have a credible way to
    reach it", matching review-cli's startup failover posture.
    """
    env = os.environ if env is None else env
    if provider == "ollama":
        return shutil.which("ollama") is not None
    keys = _PROVIDER_KEY_ENV.get(provider, ())
    return any(env.get(k) for k in keys)


def capability_head(
    capability: str | None,
    env: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve ``capability`` to a manifest ``(provider, model)`` head, or ``None`` (rig#8).

    The consumer side of rig-cli#8: when a ``classify.capability`` is configured, ask the
    shared manifest (``models.yaml``) for the model it currently pins for that capability/role
    and return it as a ``(provider, model)`` pair to PREFER ahead of the hardcoded chain — so
    the classifier tracks the manifest the cron checker keeps fresh, instead of a literal pin.

    Fail-soft and purely additive: a falsy ``capability``, a missing manifest, or the shared
    resolver lib not being installed all yield ``None``, and the caller proceeds with the
    hardcoded chain exactly as before. The manifest's provider name is normalized onto this
    CLI's provider keys (``gemini`` -> ``google``) so the head slots into the same availability
    check + ``-m`` building as every other chain entry.
    """
    if not capability:
        return None
    from .manifest import resolve_capability

    resolved = resolve_capability(capability, env=env)
    if resolved is None:
        return None
    provider, model = resolved
    return _MANIFEST_PROVIDER_ALIASES.get(provider, provider), model


def resolve_chain(
    fallbacks: list[tuple[str, str]] | None = None,
    env: dict[str, str] | None = None,
    *,
    capability: str | None = None,
) -> ResolvedModel | None:
    """Return the first AVAILABLE (provider, model) in the chain, or ``None`` if none are.

    Same availability-failover as the review board, pool=1: walk the chain top-to-bottom and
    pick the first provider with a credential/daemon. Provider-agnostic — degrades to
    whatever's reachable so classification keeps working offline (ollama) or on any one key.

    When ``capability`` is given (the ``classify.capability`` config — rig#8), the model the
    shared manifest pins for it is PREPENDED as the preferred head, so a reachable manifest
    model wins over the hardcoded chain. The prepend is fail-soft (an unresolvable capability
    or a missing manifest adds nothing), and an UNREACHABLE manifest head still falls through
    to the rest of the chain — the head only changes the PREFERENCE, never removes a fallback.
    """
    chain = list(fallbacks) if fallbacks is not None else list(DEFAULT_FALLBACKS)
    head = capability_head(capability, env)
    if head is not None:
        # Promote the manifest model to the PREFERRED head. If it already appears in the chain
        # (anywhere — not just at position 0), drop that occurrence first so the promotion isn't
        # silently lost when the manifest picked an entry that sits LOWER than another reachable
        # provider; the manifest's choice must win the preference, never duplicate.
        chain = [entry for entry in chain if entry != head]
        chain.insert(0, head)
    for provider, model in chain:
        if provider_available(provider, env):
            return ResolvedModel(provider=provider, model_arg=to_model_arg(provider, model))
    return None


def build_prompt(message: str) -> str:
    """The fixed classification prompt with the message appended."""
    return CLASSIFY_PROMPT + message.strip()


def parse_verdict(output: str, *, bias: Verdict = Verdict.CHANGE) -> Verdict:
    """Parse ``review just-ask`` output into a verdict, biasing on ambiguity.

    Primary signal: the model is asked to emit a ``VERDICT: <word>`` line — we key off the
    LAST such sentinel (robust, can't collide with the prompt body, which no longer contains
    the bare verdict words). Fallback for a model that ignores the format: scan the output for
    a standalone ``change``/``justAsk`` token and take the last one. Failing both, ``bias``.
    """
    # strip the known fixed prompt text first so neither the sentinel match nor the fallback
    # token scan can pick up the prompt's own instruction words when a model echoes it.
    cleaned = output.replace(CLASSIFY_PROMPT, " ")

    matches = _VERDICT_RE.findall(cleaned)
    if matches:
        word = matches[-1].lower().replace(" ", "").replace("-", "")
        return Verdict.CHANGE if word == "change" else Verdict.JUST_ASK

    # fallback: no sentinel. Take the last decisive standalone token.
    tokens = re.findall(r"\b(change|justask|just-ask|just ask)\b", cleaned.lower())
    if tokens:
        last = tokens[-1].replace(" ", "").replace("-", "")
        return Verdict.CHANGE if last == "change" else Verdict.JUST_ASK
    return bias
