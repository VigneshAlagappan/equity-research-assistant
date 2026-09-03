"""llm/router.py — model routing and fallback. No real network access: both
providers are monkeypatched at their generate() entry point (llm/providers).
"""

from __future__ import annotations

import pytest

from llm import capability_registry
from llm.hardness import Tier, fixed
from llm.providers.base import ProviderResponse, ProviderUnavailable
from llm.router import AllProvidersUnavailableError, route


def _response(model: str, provider: str, text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        text=text, stop_reason="end_turn", input_tokens=10, output_tokens=5, model=model, provider=provider
    )


# ------------------------------------------------------------------
# Hardness routing — simple tasks use the cheap model, hard tasks the strong one.
# ------------------------------------------------------------------


def test_quick_tier_prefers_haiku(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )
    result = route(system="s", user_message="u", hardness=fixed(Tier.QUICK, "test"), max_tokens=100)
    assert result.response.model == "claude-haiku-4-5"
    assert result.fallback_used is False


def test_deep_tier_prefers_sonnet_not_opus(monkeypatch) -> None:
    """Opus is disabled by operator policy (llm/capability_registry.py) —
    DEEP's preferred model is Sonnet, and Opus never appears in the chain
    at all (not even as a fallback candidate)."""
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _response(kw["model"], "anthropic"),
    )
    result = route(system="s", user_message="u", hardness=fixed(Tier.DEEP, "test"), max_tokens=100)
    assert result.response.model == "claude-sonnet-5"
    assert all(a.model != "claude-opus-5" for a in result.attempts)


# ------------------------------------------------------------------
# Cloud failure -> automatic fallback to the next cloud model.
# ------------------------------------------------------------------


def test_preferred_model_unavailable_falls_back_to_next_cloud_model(monkeypatch) -> None:
    """STANDARD, not DEEP: with Opus disabled, DEEP's only eligible cloud
    candidate is Sonnet itself (Haiku's reasoning_strength is below what
    DEEP requires) — there's no other cloud model left to fall back to.
    STANDARD now prefers Haiku (config.settings.TIER_PREFERRED_MODEL), so
    Haiku -> Sonnet is the real same-tier cloud fallback to exercise here."""
    def fake_generate(**kw):
        if kw["model"] == "claude-haiku-4-5":
            raise ProviderUnavailable("rate limited")
        return _response(kw["model"], "anthropic")

    monkeypatch.setattr("llm.router.anthropic_provider.generate", fake_generate)

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
