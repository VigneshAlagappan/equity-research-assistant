"""llm/router.py — model routing and fallback. No real network access: both
providers are monkeypatched at their generate() entry point (llm/providers).
"""

from __future__ import annotations

import dataclasses

import pytest

from llm import capability_registry
from llm.hardness import Tier, fixed
from llm.providers.base import ProviderResponse, ProviderUnavailable
from llm.router import AllProvidersUnavailableError, route


def _response(model: str, provider: str, text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        text=text, stop_reason="end_turn", input_tokens=10, output_tokens=5, model=model, provider=provider
    )


def _enable_openrouter(monkeypatch) -> str:
    """Real test env has no OPENROUTER_API_KEY, so capability_registry.MODELS'
    openrouter ModelSpec is built disabled at import time (config.settings.
    OPENROUTER_API_KEY_SET is baked into ModelSpec.enabled once, not
    re-read) — tests exercising it as config.settings.TIER_PREFERRED_MODEL's
    preferred candidate (standard/deep tiers, as of 2026-09-27) must patch
    the spec itself, the same trick tests/test_web.py's
    _install_fake_signals_llm uses. Returns the model_id for convenience."""
    spec = capability_registry.get_model(capability_registry.OPENROUTER_MODEL_ID)
    enabled_spec = dataclasses.replace(spec, enabled=True)
    monkeypatch.setattr(
        capability_registry, "MODELS",
        [enabled_spec if m.model_id == spec.model_id else m for m in capability_registry.MODELS],
    )
    return spec.model_id


# ------------------------------------------------------------------
# Hardness routing — simple tasks use the cheap model, hard tasks the strong one.
# ------------------------------------------------------------------


def test_quick_tier_falls_back_to_haiku_when_openrouter_unconfigured(monkeypatch) -> None:
    """"quick"'s hand-specified chain (config.settings.
    TIER_FALLBACK_CHAIN_OVERRIDE, as of 2026-09-27) tries OPENROUTER_MODEL_ID
    first, but a real test env has no OPENROUTER_API_KEY, so it's silently
    skipped (disabled) and Haiku — the next entry — is reached directly."""
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )
    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)
    assert result.response.model == "claude-haiku-4-5"
    assert result.fallback_used is False


def test_quick_tier_tries_haiku_first_then_openrouter_when_configured(monkeypatch) -> None:
    """"quick"'s hand-specified chain (config.settings.TIER_FALLBACK_CHAIN_OVERRIDE,
    Haiku -> OpenRouter -> local) reaches Haiku first even with OpenRouter
    enabled -- OpenRouter is only the fallback if Haiku is unavailable."""
    model_id = _enable_openrouter(monkeypatch)
    calls: list[str] = []

    def anthropic_generate(**kw):
        calls.append("anthropic")
        return _response(kw["model"], "anthropic")

    def openrouter_generate(**kw):
        calls.append("openrouter")
        return _response(kw["model"], "openrouter")

    monkeypatch.setattr("llm.router.anthropic_provider.generate", anthropic_generate)
    monkeypatch.setattr("llm.router.openrouter_provider.generate", openrouter_generate)

    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)
    assert result.response.model == "claude-haiku-4-5"
    assert result.fallback_used is False
    assert calls == ["anthropic"]

    def anthropic_down(**kw):
        raise ProviderUnavailable("overloaded")

    monkeypatch.setattr("llm.router.anthropic_provider.generate", anthropic_down)
    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)
    assert result.response.model == model_id
    assert result.fallback_used is True


def test_deep_tier_falls_back_to_sonnet_when_openrouter_unconfigured(monkeypatch) -> None:
    """DEEP's configured preferred model is OPENROUTER_MODEL_ID
    (config.settings.TIER_PREFERRED_MODEL, as of 2026-09-27), but a real
    test env has no OPENROUTER_API_KEY, so capability_registry.MODELS'
    openrouter ModelSpec is disabled and simply never enters the chain —
    the next-strongest eligible cloud model is Sonnet (Opus is disabled by
    operator policy and never appears at all, not even as a fallback
    candidate)."""
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )
    result = route(system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"), max_tokens=100)
    assert result.response.model == "claude-sonnet-5"
    assert all(a.model != "claude-opus-5" for a in result.attempts)


def test_deep_tier_uses_sonnet_before_openrouter_when_configured(monkeypatch) -> None:
    """DEEP's eligible models are those meeting its minimum reasoning
    strength -- Haiku is excluded, and of the rest Sonnet is the configured
    preferred cloud model, so OpenRouter (also eligible) is only the
    fallback behind it."""
    model_id = _enable_openrouter(monkeypatch)
    monkeypatch.setattr(
        "llm.router.openrouter_provider.generate",
        lambda **kw: _response(kw["model"], "openrouter"),
    )
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )

    result = route(system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"), max_tokens=100)
    assert result.response.model == "claude-sonnet-5"
    assert result.fallback_used is False
    assert all(a.model != "claude-haiku-4-5" or a.outcome == "skipped_insufficient_reasoning" for a in result.attempts)

    def sonnet_down(**kw):
        raise ProviderUnavailable("overloaded")

    monkeypatch.setattr("llm.router.anthropic_provider.generate", sonnet_down)
    result = route(system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"), max_tokens=100)
    assert result.response.model == model_id
    assert result.fallback_used is True


# ------------------------------------------------------------------
# Cloud failure -> automatic fallback to the next cloud model.
# ------------------------------------------------------------------


def test_preferred_model_unavailable_falls_back_to_next_cloud_model(monkeypatch) -> None:
    """STANDARD's preferred model is Haiku (config.settings.TIER_PREFERRED_MODEL,
    as of 2026-09-28) -- when it's unavailable, the next-strongest eligible
    cloud model (Sonnet, reasoning_strength=4) is the real same-tier fallback,
    ahead of OpenRouter (same strength, but not Anthropic's own)."""
    _enable_openrouter(monkeypatch)

    def anthropic_generate(**kw):
        if kw["model"] == "claude-haiku-4-5":
            raise ProviderUnavailable("rate limited")
        return _response(kw["model"], "anthropic")

    monkeypatch.setattr("llm.router.anthropic_provider.generate", anthropic_generate)
    monkeypatch.setattr(
        "llm.router.openrouter_provider.generate",
        lambda **kw: _response(kw["model"], "openrouter"),
    )

    result = route(system="s", user_message="u", hardness=fixed(Tier.STANDARD, "test"), max_tokens=100)

    assert result.response.model == "claude-sonnet-5"
    assert result.fallback_used is True
    assert any(a.model == "claude-haiku-4-5" and a.outcome == "unavailable" for a in result.attempts)


def test_all_cloud_unavailable_falls_back_to_local(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )
    monkeypatch.setattr(
        "llm.router.local_provider.generate",
        lambda **kw: _response(kw["model"], "ollama"),
    )

    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)

    assert result.response.provider == "ollama"
    assert result.fallback_used is True


def test_every_provider_unavailable_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )
    monkeypatch.setattr(
        "llm.router.local_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("unreachable")),
    )

    with pytest.raises(AllProvidersUnavailableError):
        route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)


# ------------------------------------------------------------------
# Oversized local task — a DEEP task must never fall through to the weak
# local model, even once every cloud model has failed.
# ------------------------------------------------------------------


def test_deep_task_never_reaches_local_model(monkeypatch) -> None:
    local_called = []
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )
    monkeypatch.setattr(
        "llm.router.local_provider.generate",
        lambda **kw: local_called.append(kw["model"]) or _response(kw["model"], "ollama"),
    )

    with pytest.raises(AllProvidersUnavailableError) as excinfo:
        route(system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"), max_tokens=100)

    assert local_called == []  # local model was never even attempted
    assert any(a.outcome == "skipped_insufficient_reasoning" for a in excinfo.value.attempts)


def test_quick_task_can_reach_local_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )
    monkeypatch.setattr(
        "llm.router.local_provider.generate",
        lambda **kw: _response(kw["model"], "ollama"),
    )

    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)

    assert result.response.provider == "ollama"


# ------------------------------------------------------------------
# Local model disabled entirely (config.settings.LOCAL_MODEL_ENABLED=False)
# is simply excluded from every fallback chain.
# ------------------------------------------------------------------


def test_disabled_local_model_is_never_offered(monkeypatch) -> None:
    disabled_local = capability_registry.ModelSpec(
        "llama3.1:8b", provider="ollama", local=True, context_window=128_000,
        reasoning_strength=2, cost_class="free", speed_class="medium", enabled=False,
    )
    monkeypatch.setattr(
        "llm.capability_registry.MODELS",
        [m for m in capability_registry.MODELS if m.provider != "ollama"] + [disabled_local],
    )
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )
    local_called = []
    monkeypatch.setattr(
        "llm.router.local_provider.generate",
        lambda **kw: local_called.append(kw["model"]),
    )

    with pytest.raises(AllProvidersUnavailableError):
        route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)

    assert local_called == []


# ------------------------------------------------------------------
# Pinning (ANTHROPIC_MODEL env var / explicit model=) means "always this
# model" — no fallback chain, no tier preference.
# ------------------------------------------------------------------


def test_pinned_model_bypasses_tiering_and_does_not_fall_back(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )

    with pytest.raises(AllProvidersUnavailableError) as excinfo:
        route(
            system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"),
            max_tokens=100, pinned_model="claude-sonnet-5",
        )

    assert [a.model for a in excinfo.value.attempts] == ["claude-sonnet-5"]


def test_pinned_opus_is_blocked_even_though_it_would_otherwise_resolve(monkeypatch) -> None:
    """"claude-opus-5" is a real, known model_id (capability_registry.get_model
    resolves it) — but it's disabled by operator policy, so pinning to it
    must not reach the provider at all, the same as pinning to a typo'd
    unknown model_id would."""
    called = []
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: called.append(kw["model"]) or _response(kw["model"], "anthropic"),
    )

    with pytest.raises(AllProvidersUnavailableError) as excinfo:
        route(
            system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"),
            max_tokens=100, pinned_model="claude-opus-5",
        )

    assert called == []
    assert excinfo.value.attempts == []


# ------------------------------------------------------------------
# Opus is disabled by operator cost-control policy — never reachable at any
# tier, preferred or fallback, regardless of what fails.
# ------------------------------------------------------------------


def test_opus_is_never_offered_at_any_tier(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )
    for tier in Tier:
        result = route(system="s", user_message="u", hardness=fixed(tier, "test"), max_tokens=100)
        assert result.response.model != "claude-opus-5"
        assert all(a.model != "claude-opus-5" for a in result.attempts)
