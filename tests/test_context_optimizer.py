"""context/optimizer.py — dedup, value scoring, and token-budget compression."""

from __future__ import annotations

from llm.hardness import Tier
from context.optimizer import TIER_EVIDENCE_TOKEN_BUDGET, optimize
from research.evidence import Evidence


def _fact(company: str, label: str, value: str = "100.00 INR_CRORE") -> Evidence:
    return Evidence(kind="FACT", company_id=company, label=label, value=value, citation="reported")


# ------------------------------------------------------------------
# Deduplication
# ------------------------------------------------------------------


def test_exact_duplicate_evidence_is_removed() -> None:
    evidence = [_fact("HDFCBANK", "Net Profit FY2024"), _fact("HDFCBANK", "Net Profit FY2024")]

    result = optimize("net profit", evidence, Tier.STANDARD)

    assert len(result.evidence) == 1


def test_dedup_preserves_first_occurrence_order() -> None:
    evidence = [
        _fact("HDFCBANK", "Net Profit FY2023"),
        _fact("HDFCBANK", "Net Profit FY2024"),
        _fact("HDFCBANK", "Net Profit FY2023"),  # duplicate of the first
    ]

    result = optimize("net profit", evidence, Tier.STANDARD)

    assert [e.label for e in result.evidence] == ["Net Profit FY2023", "Net Profit FY2024"]


def test_distinct_evidence_with_same_label_different_value_is_kept() -> None:
    evidence = [
        Evidence(kind="FACT", company_id="HDFCBANK", label="Net Profit FY2024", value="100", citation="a"),
        Evidence(kind="FACT", company_id="HDFCBANK", label="Net Profit FY2024", value="200", citation="a"),
    ]

    result = optimize("net profit", evidence, Tier.STANDARD)

    assert len(result.evidence) == 2


# ------------------------------------------------------------------
# Compression — only kicks in once the deduped set exceeds the tier budget;
# small evidence sets pass through untouched (no answer-quality risk).
# ------------------------------------------------------------------


def test_small_evidence_set_is_untouched() -> None:
    evidence = [_fact("HDFCBANK", f"Net Profit FY202{i}") for i in range(3)]

    result = optimize("net profit", evidence, Tier.STANDARD)

    assert result.evidence == evidence
    assert result.dropped == []
    assert result.tokens_saved == 0


def test_oversized_evidence_set_is_trimmed_to_budget() -> None:
    # Each line is padded to guarantee it blows well past the QUICK budget.
    evidence = [
        Evidence(
            kind="FACT", company_id="HDFCBANK", label=f"Metric {i} FY2020",
            value="x" * 500, citation="reported",
        )
        for i in range(50)
    ]
    assert sum(len(e.as_prompt_line()) // 4 for e in evidence) > TIER_EVIDENCE_TOKEN_BUDGET[Tier.QUICK]

    result = optimize("metric", evidence, Tier.QUICK)

    assert result.total_tokens_after <= result.budget or len(result.evidence) == 1
    assert result.tokens_saved > 0
    assert len(result.dropped) > 0
    assert len(result.evidence) < len(evidence)


def test_always_keeps_at_least_one_line_even_if_it_exceeds_budget_alone() -> None:
    huge = Evidence(kind="FACT", company_id="HDFCBANK", label="Huge FY2024", value="x" * 50_000, citation="c")

    result = optimize("huge", [huge], Tier.QUICK)

    assert result.evidence == [huge]


def test_question_mentioning_a_metric_keeps_it_over_an_unmentioned_one_under_pressure() -> None:
    padding = "x" * 400
    mentioned = Evidence(kind="FACT", company_id="HDFCBANK", label="Net Profit FY2024", value=padding, citation="c")
    unmentioned = Evidence(kind="FACT", company_id="HDFCBANK", label="Deposits FY2018", value=padding, citation="c")
    evidence = [unmentioned] + [mentioned] + [
        Evidence(kind="FACT", company_id="HDFCBANK", label=f"Other {i} FY2010", value=padding, citation="c")
        for i in range(20)
    ]

    result = optimize("What was net profit in FY2024?", evidence, Tier.QUICK)

    assert mentioned in result.evidence


def test_deep_tier_has_a_larger_budget_than_quick() -> None:
    assert TIER_EVIDENCE_TOKEN_BUDGET[Tier.DEEP] > TIER_EVIDENCE_TOKEN_BUDGET[Tier.STANDARD] > TIER_EVIDENCE_TOKEN_BUDGET[Tier.QUICK]
