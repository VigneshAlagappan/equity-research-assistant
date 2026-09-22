"""Tests for reports/schema/investigation_report.py's from_investigation_data()
adapter — the pure read-side reshape of investigate_view's existing
investigation/hypotheses dict shapes into the normalized InvestigationReport
the Signal Report Design System renders. No database, no Flask, no research
pipeline involved; these are pure data-in/data-out checks.
"""

from __future__ import annotations

from reports.schema import from_investigation_data, score_band


def _complete_investigation() -> dict:
    return {
        "investigation_id": "inv-1",
        "question": "Why did HDFC Bank's net interest margin compress in FY24?",
        "company_ids": ["HDFCBANK"],
        "statement_type": "consolidated",
        "strongest_explanation": "Funding-cost pressure from the HDFC Ltd merger outweighed asset repricing.",
        "unanswered_questions": ["How much of the compression reverses in FY25?"],
        "additional_evidence_needed": ["Segment-wise deposit cost breakdown"],
        "generated_at": "2026-01-01T00:00:00Z",
        "as_of": "2026-01-01",
    }


def _complete_hypothesis(**overrides) -> dict:
    base = {
        "hypothesis_id": "hyp-1",
        "statement": "Merger-driven funding cost pressure compressed NIM.",
        "mechanism": "Higher-cost HDFC Ltd borrowings replaced low-cost CASA funding mix.",
        "chain_steps": ["HDFC Ltd merger closes", "Wholesale borrowings absorbed", "Funding cost rises", "NIM compresses"],
        "category": "Financial",
        "rationale": "Initial rationale before evaluation.",
        "unknowns": ["Exact merged funding mix"],
        "generation_order": 1,
        "verdict": "SUPPORTED",
        "confidence_basis": "Cost of funds rose 45bps while yield on assets rose only 20bps.",
        "confidence_score": 78,
        "synthesis_rank": 1,
        "supporting_evidence": [
            {"stance": "supporting", "kind": "FACT", "label": "Cost of funds", "value": "+45bps YoY", "citation": "FY24 AR p.12"},
        ],
        "contradicting_evidence": [
            {"stance": "contradicting", "kind": "MANAGEMENT_OPINION", "label": "Management guided margin stability", "value": None, "citation": "Q3FY24 concall"},
        ],
        "missing_evidence": [
            {"stance": "missing", "kind": "INFERENCE", "label": "Segment-level funding cost", "value": None, "citation": None},
        ],
    }
    base.update(overrides)
    return base


def test_score_band_thresholds():
    assert score_band(60) == "leading"
    assert score_band(100) == "leading"
    assert score_band(59) == "contested"
    assert score_band(30) == "contested"
    assert score_band(29) == "weak"
    assert score_band(0) == "weak"
    assert score_band(None) == "unscored"


def test_adapter_complete_investigation_preserves_all_content():
    investigation = _complete_investigation()
    hypotheses = [_complete_hypothesis()]
    cost = {"calls": 3, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.0123}

    report = from_investigation_data(investigation, hypotheses, cost)

    # Metadata
    assert report.metadata.report_id == "inv-1"
    assert report.metadata.company_ids == ["HDFCBANK"]
    assert report.metadata.statement_type == "consolidated"
    assert report.metadata.as_of == "2026-01-01"
    assert report.metadata.hypothesis_count == 1
    assert report.metadata.cost_usd == 0.0123
    assert report.metadata.cost_calls == 3

    # Executive summary / strongest explanation preserved verbatim
    assert report.strongest_explanation == investigation["strongest_explanation"]
    assert report.executive_summary == investigation["strongest_explanation"]
    assert report.overall_verdict == "SUPPORTED"  # rank-1 hypothesis's verdict

    # Follow-ups / gaps preserved verbatim
    assert report.follow_up_questions == investigation["unanswered_questions"]
    assert report.additional_evidence_needed == investigation["additional_evidence_needed"]

    assert len(report.hypotheses) == 1
    h = report.hypotheses[0]
    assert h.hypothesis_id == "hyp-1"
    assert h.claim == "Merger-driven funding cost pressure compressed NIM."
    assert h.verdict == "SUPPORTED"
    assert h.rank == 1
    assert h.category == "Financial"
    assert h.score == 78
    assert h.band == "leading"
    assert h.band_label == "Leading"
    # confidence_basis wins over rationale when both are present
    assert h.assessment == "Cost of funds rose 45bps while yield on assets rose only 20bps."
    # chain_steps wins over mechanism prose when both are present
    assert h.causal_chain == [
        "HDFC Ltd merger closes", "Wholesale borrowings absorbed", "Funding cost rises", "NIM compresses",
    ]
    assert h.mechanism is None

    assert len(h.supporting_evidence) == 1
    assert h.supporting_evidence[0].status == "have"
    assert h.supporting_evidence[0].label == "Cost of funds"
    assert h.supporting_evidence[0].value == "+45bps YoY"
    assert h.supporting_evidence[0].citation == "FY24 AR p.12"

    assert len(h.contradicting_evidence) == 1
    assert h.contradicting_evidence[0].status == "contradicts"

    assert len(h.missing_evidence) == 1
    assert h.missing_evidence[0].status == "missing"
    assert h.missing_evidence[0].label == "Segment-level funding cost"

    assert h.unknowns == ["Exact merged funding mix"]


def test_adapter_older_investigation_missing_newer_fields_renders_gracefully():
    """A hypothesis generated before chain_steps/confidence_score/
    synthesis_rank existed (storage/database.py's incremental migrations)
    simply lacks those keys entirely -- the adapter must not raise, and
    should fall back sensibly (mechanism prose instead of a chain, no rank,
    no score)."""
    investigation = {
        "investigation_id": "inv-old",
        "question": "Old-style investigation with no strongest_explanation.",
        # company_ids/unanswered_questions/additional_evidence_needed omitted entirely
    }
    old_hypothesis = {
        "hypothesis_id": "hyp-old",
        "statement": "A pre-scoring-era hypothesis.",
        "mechanism": "Cause leads to effect via some plain-prose mechanism.",
        "category": "Operational",
        "verdict": "PARTIALLY_SUPPORTED",
        # no chain_steps, no confidence_score, no confidence_basis,
        # no synthesis_rank, no unknowns, no evidence lists at all.
    }

    report = from_investigation_data(investigation, [old_hypothesis], cost=None)

    assert report.metadata.company_ids == []
    assert report.metadata.as_of is None
    assert report.metadata.cost_usd is None
    assert report.metadata.cost_calls is None
    assert report.strongest_explanation is None
    assert report.overall_verdict is None  # no hypothesis has rank == 1
    assert report.follow_up_questions == []
    assert report.additional_evidence_needed == []

    assert len(report.hypotheses) == 1
    h = report.hypotheses[0]
    assert h.rank is None
    assert h.score is None
    assert h.band == "unscored"
    assert h.band_label == "Unscored"
    assert h.causal_chain == []
    assert h.mechanism == "Cause leads to effect via some plain-prose mechanism."
    assert h.supporting_evidence == []
    assert h.contradicting_evidence == []
    assert h.missing_evidence == []
    assert h.unknowns == []


def test_adapter_none_confidence_score_does_not_crash_band_comparison():
    """confidence_score=None (explicitly present but null -- the evaluator
    didn't return a usable one) must not be compared numerically against the
    60/30 thresholds; it must resolve to the "unscored" band, not raise."""
    hypothesis = _complete_hypothesis(confidence_score=None, synthesis_rank=None)
    report = from_investigation_data(_complete_investigation(), [hypothesis])

    h = report.hypotheses[0]
    assert h.score is None
    assert h.band == "unscored"
    assert h.band_label == "Unscored"
    # overall_verdict must not blow up either when nothing is rank 1
    assert report.overall_verdict is None


def test_adapter_handles_no_hypotheses_at_all():
    report = from_investigation_data(_complete_investigation(), [], cost=None)
    assert report.hypotheses == []
    assert report.metadata.hypothesis_count == 0
    assert report.overall_verdict is None


def test_adapter_missing_evidence_items_as_plain_strings_are_tolerated():
    """Some synthetic/legacy callers pass missing_evidence as a plain list
    of strings rather than {label: ...} dicts -- the adapter should still
    produce a usable EvidenceRow rather than crashing on .get()."""
    hypothesis = _complete_hypothesis(missing_evidence=["Just a plain string gap"])
    report = from_investigation_data(_complete_investigation(), [hypothesis])
    h = report.hypotheses[0]
    assert len(h.missing_evidence) == 1
    assert h.missing_evidence[0].label == "Just a plain string gap"
    assert h.missing_evidence[0].status == "missing"
