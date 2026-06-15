"""classify.py — verdict parsing, the fallback chain, and provider availability."""

from __future__ import annotations

from tasklib.classify import (
    DEFAULT_FALLBACKS,
    Verdict,
    build_prompt,
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
