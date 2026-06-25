"""classify.py — verdict parsing, the fallback chain, and provider availability."""

from __future__ import annotations

from tasklib.classify import (
    DEFAULT_FALLBACKS,
    Verdict,
    build_prompt,
    capability_head,
    parse_verdict,
    provider_available,
    resolve_chain,
    to_model_arg,
)


def test_parse_clear_change():
    assert parse_verdict("...panel output...\n\nchange") is Verdict.CHANGE


def test_parse_clear_just_ask():
    assert parse_verdict("the model says: justAsk") is Verdict.JUST_ASK


def test_parse_ambiguous_biases_to_change_by_default():
    # no decisive token in the answer → bias
    assert parse_verdict("hmm not sure, could be either") is Verdict.CHANGE


def test_parse_respects_explicit_bias_on_ambiguity():
    assert parse_verdict("unclear", bias=Verdict.JUST_ASK) is Verdict.JUST_ASK


def test_parse_takes_last_decisive_token():
    # the prompt echo mentions change then justAsk; the model's real answer is last
    blob = "Classify ... change ... justAsk ...\n---\nAnswer: change"
    assert parse_verdict(blob) is Verdict.CHANGE


def test_parse_prompt_echo_only_biases_not_misreads():
    # a model that echoes ONLY the prompt (ending '...change or justAsk.') must not be read
    # as justAsk — the prompt text is stripped, so it falls back to the bias (change).
    assert parse_verdict(build_prompt("add a button")) is Verdict.CHANGE
    assert parse_verdict(build_prompt("add a button"), bias=Verdict.JUST_ASK) is Verdict.JUST_ASK


def test_parse_prompt_echo_plus_answer():
    assert parse_verdict(build_prompt("add a button") + "\n\njustAsk") is Verdict.JUST_ASK
    assert parse_verdict(build_prompt("add a button") + "\n\nchange") is Verdict.CHANGE


def test_build_prompt_includes_message_and_bias_instruction():
    p = build_prompt("please add a button")
    assert "please add a button" in p
    assert "Bias to `change`" in p
    assert "VERDICT" in p


def test_to_model_arg_per_provider():
    assert to_model_arg("anthropic", "claude-haiku-4-5") == "claude:claude-haiku-4-5"
    assert to_model_arg("commandcode", "deepseek/deepseek-v4-flash") == "commandcode:deepseek/deepseek-v4-flash"
    assert to_model_arg("zai", "glm-4.6-flash") == "zai:glm-4.6-flash"
    assert to_model_arg("google", "gemini-2.5-flash") == "gemini"
    assert to_model_arg("ollama", "qwen2.5:3b") == "ollama:qwen2.5:3b"


def test_provider_available_reads_env_key():
    assert provider_available("anthropic", {"ANTHROPIC_API_KEY": "x"})
    assert not provider_available("anthropic", {})
    assert provider_available("google", {"GOOGLE_API_KEY": "x"})
    assert provider_available("zai", {"ZHIPU_API_KEY": "x"})


def test_resolve_chain_picks_first_available():
    # only zai has a key → it wins even though anthropic/openai/commandcode are higher.
    env = {"ZAI_API_KEY": "x"}
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env)
    assert resolved is not None
    assert resolved.provider == "zai"
    assert resolved.model_arg.startswith("zai:")


def test_resolve_chain_commandcode_key_resolves_openai_first():
    # COMMANDCODE_API_KEY signals both openai (gateway-routed) and commandcode; openai is
    # higher in the chain, so it wins. (Documents the shared-credential ordering.)
    env = {"COMMANDCODE_API_KEY": "x"}
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env)
    assert resolved is not None
    assert resolved.provider == "openai"


def test_openai_requires_commandcode_key_not_openai_key():
    # the openai entry emits a `commandcode:` model arg (gateway-routed), so a bare
    # OPENAI_API_KEY must NOT mark it reachable — otherwise we'd call the gateway with a key it
    # can't authenticate, the shell-out fails, and classification silently biases.
    assert not provider_available("openai", {"OPENAI_API_KEY": "x"})
    assert provider_available("openai", {"COMMANDCODE_API_KEY": "x"})


def test_resolve_chain_prefers_head_when_present():
    env = {"ANTHROPIC_API_KEY": "x", "COMMANDCODE_API_KEY": "y"}
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env)
    assert resolved is not None
    assert resolved.provider == "anthropic"


def test_resolve_chain_none_when_nothing_available(monkeypatch):
    # no keys, and ensure ollama is treated as absent
    monkeypatch.setattr("tasklib.classify.shutil.which", lambda _name: None)
    assert resolve_chain([("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")], {}) is None


# ── rig#8 consumer half: manifest capability → preferred head ────────────────────────


def test_capability_head_none_for_empty_capability():
    # no capability configured → no manifest lookup at all (the pre-rig#8 behaviour)
    assert capability_head("", {}) is None
    assert capability_head(None, {}) is None


def test_capability_head_falls_through_when_resolver_returns_none(monkeypatch):
    # the manifest/resolver could not resolve (lib absent / no manifest / unknown tag) → None
    monkeypatch.setattr("tasklib.manifest.resolve_capability", lambda *a, **k: None)
    assert capability_head("reasoning", {}) is None


def test_capability_head_normalizes_manifest_gemini_to_google(monkeypatch):
    # the manifest names Google's provider `gemini`; this CLI's provider key is `google`.
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("gemini", "gemini-2.5-flash")
    )
    assert capability_head("fast", {}) == ("google", "gemini-2.5-flash")


def test_capability_head_passthrough_for_aligned_provider(monkeypatch):
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("anthropic", "claude-opus-4-8")
    )
    assert capability_head("reasoning", {}) == ("anthropic", "claude-opus-4-8")


def test_resolve_chain_prepends_reachable_manifest_head(monkeypatch):
    # a configured capability resolves to a manifest model whose provider is reachable → it
    # wins over the hardcoded chain head, even though the chain's anthropic entry is also up.
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("anthropic", "claude-opus-4-8")
    )
    env = {"ANTHROPIC_API_KEY": "x"}
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env, capability="reasoning")
    assert resolved is not None
    # the manifest model (opus), not the chain's hardcoded haiku, is preferred
    assert resolved.model_arg == "claude:claude-opus-4-8"


def test_resolve_chain_promotes_manifest_model_over_higher_chain_provider(monkeypatch):
    # the manifest picks openai/gpt-5-mini (position 1 in DEFAULT_FALLBACKS); anthropic (position
    # 0) is ALSO reachable. Without dedup-then-prepend the higher anthropic entry would win — the
    # manifest's preference must promote the openai model to the head and win instead.
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("openai", "gpt-5-mini")
    )
    env = {"ANTHROPIC_API_KEY": "x", "COMMANDCODE_API_KEY": "y"}  # both providers reachable
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env, capability="fast")
    assert resolved is not None
    assert resolved.provider == "openai"
    assert resolved.model_arg == "commandcode:gpt-5-mini"


def test_resolve_chain_head_unreachable_still_falls_through(monkeypatch):
    # the manifest head's provider has no key → it is skipped and the chain still resolves.
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("zai", "glm-5.2")
    )
    monkeypatch.setattr("tasklib.classify.shutil.which", lambda _name: None)
    env = {"ANTHROPIC_API_KEY": "x"}  # zai (head) unreachable, anthropic reachable
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env, capability="code")
    assert resolved is not None
    assert resolved.provider == "anthropic"


def test_resolve_chain_unknown_manifest_provider_skips_gracefully(monkeypatch):
    # the manifest is a foreign, independently-updated artifact: the cron checker could pin a
    # provider this CLI doesn't know (`mistral`, `xai`, …). Such a head must degrade to "not
    # reachable" and fall through to the hardcoded chain — never crash (the never-fatal invariant).
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("mistral", "mistral-large")
    )
    env = {"ANTHROPIC_API_KEY": "x"}  # unknown 'mistral' head has no key → skipped
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env, capability="reasoning")
    assert resolved is not None
    assert resolved.provider == "anthropic"  # fell through cleanly, no KeyError


def test_resolve_chain_gemini_head_normalizes_to_google(monkeypatch):
    # gemini→google normalization end-to-end: the manifest's `gemini` provider resolves through
    # resolve_chain as this CLI's `google` key, with review's bare `gemini` arg.
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("gemini", "gemini-2.5-flash")
    )
    env = {"GOOGLE_API_KEY": "x"}  # only google reachable
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env, capability="fast")
    assert resolved is not None
    assert resolved.provider == "google"
    assert resolved.model_arg == "gemini"  # review's gemini backend takes the bare token


def test_resolve_chain_dedups_exact_duplicate_head(monkeypatch):
    # the manifest head is an EXACT duplicate of a chain entry (same provider AND model). Dedup
    # must drop the lower duplicate and keep ONE promoted copy — a custom chain with a tail entry
    # equal to the head, where the head provider is the only reachable one, proves no double-count
    # by resolving to exactly that model (and the test exercises the `entry != head` removal).
    head = ("commandcode", "deepseek/deepseek-v4-flash")
    monkeypatch.setattr("tasklib.manifest.resolve_capability", lambda *a, **k: head)
    custom_chain = [("anthropic", "claude-haiku-4-5"), head]  # head also present at the tail
    env = {"COMMANDCODE_API_KEY": "x"}  # only the head provider reachable
    resolved = resolve_chain(custom_chain, env, capability="code")
    assert resolved is not None
    assert resolved.provider == "commandcode"
    assert resolved.model_arg == "commandcode:deepseek/deepseek-v4-flash"


def test_resolve_chain_does_not_mutate_caller_fallbacks(monkeypatch):
    # prepending the manifest head must NOT mutate the caller's list (resolve_chain copies it).
    monkeypatch.setattr(
        "tasklib.manifest.resolve_capability", lambda *a, **k: ("anthropic", "claude-opus-4-8")
    )
    caller_chain = [("zai", "glm-4.6-flash")]
    before = list(caller_chain)
    resolve_chain(caller_chain, {"ANTHROPIC_API_KEY": "x"}, capability="reasoning")
    assert caller_chain == before  # untouched — no leaked insert


def test_resolve_chain_no_capability_is_unchanged(monkeypatch):
    # with no capability the resolver is never even consulted — behaviour is the old chain.
    def _boom(*_a, **_k):
        raise AssertionError("resolve_capability must not be called without a capability")

    monkeypatch.setattr("tasklib.manifest.resolve_capability", _boom)
    env = {"ANTHROPIC_API_KEY": "x"}
    resolved = resolve_chain(list(DEFAULT_FALLBACKS), env)
    assert resolved is not None
    assert resolved.provider == "anthropic"
