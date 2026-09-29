"""llm/complexity.py — Jev, the Signals complexity classifier. No real
network access: the provider is monkeypatched at its generate() entry
point, same pattern as tests/test_llm_router.py. The real test env has no
OPENROUTER_API_KEY, so JEV_CLASSIFIER_MODEL_CHAIN's OpenRouter entry is
disabled and every call here actually reaches anthropic_provider.generate.
"""

from __future__ import annotations

import json

from llm.complexity import ComplexityLevel, classify_complexity
from llm.providers.base import ProviderResponse, ProviderUnavailable


def _jev_response(level: int, confidence: float = 0.9, reason: str = "test reason") -> ProviderResponse:
    payload = json.dumps({"complexity_level": level, "confidence": confidence, "reason": reason})
    return ProviderResponse(
        text=payload, stop_reason="end_turn", input_tokens=50, output_tokens=20,
        model="claude-haiku-4-5", provider="anthropic",
    )


def test_classifies_simple_lookup_as_level_1(monkeypatch) -> None:
    monkeypatch.setattr("llm.router.anthropic_provider.generate", lambda **kw: _jev_response(1))

    classification, route_result = classify_complexity("What was HDFC Bank ROE in FY2025?", ["HDFCBANK"])

    assert classification.level is ComplexityLevel.RETRIEVE
    assert classification.source == "jev"
    assert classification.confidence == 0.9
    assert route_result is not None


def test_classifies_causal_question_as_level_5(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: _jev_response(5, reason="causal divergence"),
    )

    classification, _ = classify_complexity(
        "Why has HDFC Bank's credit growth diverged from the banking system?", ["HDFCBANK"],
    )

    assert classification.level is ComplexityLevel.HYPOTHESIZE
    assert classification.reason == "causal divergence"


def test_unparseable_response_falls_back_to_level_3(monkeypatch) -> None:
    bad = ProviderResponse(
        text="not json at all", stop_reason="end_turn", input_tokens=5, output_tokens=5,
        model="claude-haiku-4-5", provider="anthropic",
    )
    monkeypatch.setattr("llm.router.anthropic_provider.generate", lambda **kw: bad)

    classification, route_result = classify_complexity("some question", [])

    assert classification.level is ComplexityLevel.INTERPRET
    assert classification.source == "deterministic_fallback"
    assert route_result is not None  # a model DID respond, just unparseably


def test_out_of_range_level_falls_back_to_level_3(monkeypatch) -> None:
    monkeypatch.setattr("llm.router.anthropic_provider.generate", lambda **kw: _jev_response(9))

    classification, _ = classify_complexity("some question", [])

    assert classification.level is ComplexityLevel.INTERPRET
    assert classification.source == "deterministic_fallback"


def test_all_providers_unavailable_falls_back_to_level_3(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.router.anthropic_provider.generate",
        lambda **kw: (_ for _ in ()).throw(ProviderUnavailable("outage")),
    )

    classification, route_result = classify_complexity("some question", [])

    assert classification.level is ComplexityLevel.INTERPRET
    assert classification.source == "deterministic_fallback"
    assert route_result is None
