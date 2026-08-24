"""llm/hardness.py — the shared complexity classifier used by all three LLM
call sites. Same cases research/assistant.py's old _select_model tests
covered, now against the shared module directly."""

from __future__ import annotations

from llm.hardness import Tier, classify


def test_short_factual_lookup_is_quick() -> None:
    result = classify("What was net profit in FY2024?", ["HDFCBANK"], evidence_count=8)
    assert result.tier is Tier.QUICK


def test_analysis_question_is_deep() -> None:
    result = classify("Why did net profit decline in FY2020?", ["HDFCBANK"], evidence_count=20)
    assert result.tier is Tier.DEEP


def test_peer_comparison_is_always_deep_regardless_of_wording() -> None:
    result = classify("net profit", ["HDFCBANK", "ICICIBANK"], evidence_count=5)
    assert result.tier is Tier.DEEP
    assert "comparison" in result.reason


def test_generic_question_is_standard() -> None:
    result = classify("How has net profit grown over the years?", ["HDFCBANK"], evidence_count=25)
    assert result.tier is Tier.STANDARD


def test_large_evidence_volume_forces_deep_even_for_plain_wording() -> None:
    result = classify("net profit", ["HDFCBANK"], evidence_count=41)
    assert result.tier is Tier.DEEP


def test_min_reasoning_strength_increases_with_tier() -> None:
    quick = classify("What was net profit in FY2024?", ["HDFCBANK"], evidence_count=8)
    deep = classify("Why did net profit decline?", ["HDFCBANK"], evidence_count=20)
    assert quick.min_reasoning_strength < deep.min_reasoning_strength
