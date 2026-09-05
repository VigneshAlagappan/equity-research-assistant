"""research/hypothesis_evaluator.py (Step 2G) — Anthropic client mocked,
same pattern as tests/test_hypothesis_generator.py. Uses the real db_conn
fixture (not a bare in-memory connection) since evaluate_hypothesis()
writes to llm_call_log via llm/observability.py::record()."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from context.knowledge_graph import KnowledgeClaimView
from research.evidence import Evidence
from research.hypothesis_evaluator import HypothesisEvaluationError, _render_plan, evaluate_hypothesis
from research.hypothesis_generator import Hypothesis
from research.investigation_planner import InvestigationPlan


class _FakeMessages:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self._text = text
        self._stop_reason = stop_reason
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        content = [SimpleNamespace(type="text", text=self._text)] if self._text else []
        return SimpleNamespace(content=content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self.messages = _FakeMessages(text, stop_reason, captured)


def _install_fake_client(monkeypatch, text: str | None, stop_reason: str = "end_turn") -> list:
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(text, stop_reason, captured),
    )
    return captured


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="inv1-h1", investigation_id="inv1", statement="Margins declined due to input costs.",
        mechanism="Higher COGS ate into margins.", category="financial", companies=["HDFCBANK"],
        rationale="plausible", known_relationships=[], unknowns=[], generation_order=0,
    )


def _plan_with_evidence() -> InvestigationPlan:
    plan = InvestigationPlan(hypothesis_id="inv1-h1")
    plan.evidence = [
        Evidence(kind="FACT", company_id="HDFCBANK", label="Net Profit FY2024", value="20,500.00 INR_CRORE", citation="reported"),
    ]
    return plan


_VALID_RESPONSE = """{
  "verdict": "SUPPORTED",
  "confidence_basis": "Net profit declined alongside rising costs, consistent with the hypothesis.",
  "supporting_evidence": [
    {"kind": "FACT", "label": "Net Profit FY2024", "value": "20,500.00 INR_CRORE", "citation": "reported"}
  ],
  "contradicting_evidence": [],
  "missing_evidence": ["Direct input cost / raw material price data"]
}"""


def test_render_plan_omits_the_multi_hop_block_when_empty() -> None:
    rendered = _render_plan(_plan_with_evidence())

    assert "Multi-hop knowledge graph connections" not in rendered


def test_render_plan_includes_the_multi_hop_block_and_always_says_inference() -> None:
    plan = _plan_with_evidence()
    plan.inferred_connections = [
        KnowledgeClaimView(
            claim_id=202, company_id="ICICIBANK", claim_text="Gross margin expanded.", claim_type="FACT",
            category="fact", speaker=None, fiscal_year="FY2024", quarter=None, confidence=0.8, document_id=2,
            hop_distance=2, path="Risk:Input cost inflation --MAY_AFFECT--> Metric:Gross Margin",
        )
    ]

    rendered = _render_plan(plan)

    assert "Multi-hop knowledge graph connections" in rendered
    assert "[INFERENCE] only, never [FACT] or [CALCULATION]" in rendered
    assert "ICICIBANK" in rendered
    assert "Gross margin expanded." in rendered
    assert "Risk:Input cost inflation --MAY_AFFECT--> Metric:Gross Margin" in rendered


def test_evaluates_and_parses_verdict_and_evidence(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    evaluation = evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())

    assert evaluation.hypothesis_id == "inv1-h1"
    assert evaluation.verdict == "SUPPORTED"
    assert len(evaluation.supporting_evidence) == 1
    assert evaluation.supporting_evidence[0].kind == "FACT"
    assert evaluation.contradicting_evidence == []
    assert evaluation.missing_evidence == ["Direct input cost / raw material price data"]


def test_invalid_verdict_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    response = '{"verdict": "MAYBE", "confidence_basis": "x", "supporting_evidence": [], "contradicting_evidence": [], "missing_evidence": []}'
    _install_fake_client(monkeypatch, response)

    with pytest.raises(HypothesisEvaluationError):
        evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())


def test_hallucinated_evidence_kind_is_dropped(db_conn: sqlite3.Connection, monkeypatch) -> None:
    response = (
        '{"verdict": "SUPPORTED", "confidence_basis": "x", '
        '"supporting_evidence": [{"kind": "NOT_A_REAL_KIND", "label": "x", "value": "x", "citation": "x"}], '
        '"contradicting_evidence": [], "missing_evidence": []}'
    )
    _install_fake_client(monkeypatch, response)

    evaluation = evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())
    assert evaluation.supporting_evidence == []


def test_unparseable_response_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json")

    with pytest.raises(HypothesisEvaluationError):
        evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())


def test_refusal_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "won't answer", stop_reason="refusal")

    with pytest.raises(HypothesisEvaluationError):
        evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())


def test_truncated_response_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"verdict": "SUPPORTED"', stop_reason="max_tokens")

    with pytest.raises(HypothesisEvaluationError, match="truncated"):
        evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())


def test_all_providers_unavailable_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    from llm.router import AllProvidersUnavailableError, Attempt

    monkeypatch.setattr(
        "research.hypothesis_evaluator.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )

    with pytest.raises(HypothesisEvaluationError):
        evaluate_hypothesis(db_conn, _hypothesis(), _plan_with_evidence())


def test_no_evidence_renders_a_clear_message(db_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)

    evaluate_hypothesis(db_conn, _hypothesis(), InvestigationPlan(hypothesis_id="inv1-h1"))

    user_message = captured[0]["messages"][0]["content"]
    assert "No evidence was retrieved for this hypothesis." in user_message
