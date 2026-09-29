"""research/routing_policy.py — end-to-end dispatch for the Signals
Complexity Classification and Execution Routing Policy (docs/ADR/023). No
real network access: only anthropic_provider.generate is monkeypatched
(OpenRouter is unconfigured in the test env, same as every other test in
this suite that touches llm/router.py). The first call every route_question()
makes is always Jev's own classification call -- tests that need a
deterministic (no-answer-LLM-call) level assert exactly one call total;
tests for Levels 3/4/5 supply a second, stateful response for the answer
call itself.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from llm.complexity import ComplexityLevel
from llm.providers.base import ProviderResponse
from research.routing_policy import route_question
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)  # HDFCBANK + ICICIBANK, same basic_industry
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)  # net_profit: FY2023=17,000, FY2024=20,500 (INR_CRORE)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _jev_json(level: int) -> str:
    return json.dumps({"complexity_level": level, "confidence": 0.9, "reason": "test"})


def _response(text: str, model: str = "claude-haiku-4-5") -> ProviderResponse:
    return ProviderResponse(
        text=text, stop_reason="end_turn", input_tokens=10, output_tokens=10, model=model, provider="anthropic",
    )


def _mock_chain(monkeypatch, *responses: str) -> list[dict]:
    """Each successive call to anthropic_provider.generate returns the next
    `responses` entry in order -- the first is always Jev's classification
    JSON, later ones are whatever an answer-generation call should return."""
    calls: list[dict] = []

    def fake_generate(**kw):
        calls.append(kw)
        return _response(responses[len(calls) - 1], model=kw["model"])

    monkeypatch.setattr("llm.router.anthropic_provider.generate", fake_generate)
    return calls


def test_level1_retrieve_makes_no_answer_llm_call(ingested_conn, monkeypatch) -> None:
    calls = _mock_chain(monkeypatch, _jev_json(1))

    result = route_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])

    assert len(calls) == 1  # only Jev's own classification call
    assert "20500" in result.answer
    assert result.classification.level is ComplexityLevel.RETRIEVE
    assert result.audit.execution_status == "answered"
    assert result.audit.model_selected is None  # no answer-generation model was used


def test_level2_calculate_makes_no_answer_llm_call(ingested_conn, monkeypatch) -> None:
    calls = _mock_chain(monkeypatch, _jev_json(2))

    result = route_question(ingested_conn, "What was net profit YoY growth in FY2024?", ["HDFCBANK"])

    assert len(calls) == 1
    assert "%" in result.answer
    assert result.classification.level is ComplexityLevel.CALCULATE
    assert result.audit.calculations_performed


def test_level3_interpret_makes_exactly_one_answer_llm_call(ingested_conn, monkeypatch) -> None:
    calls = _mock_chain(monkeypatch, _jev_json(3), "Profit grew steadily. [FACT] ... **Confidence:** High -- clear evidence.")

    result = route_question(ingested_conn, "Analyze HDFC Bank's profit growth over the last five years.", ["HDFCBANK"])

    assert len(calls) == 2
    assert "Profit grew steadily" in result.answer
    assert result.classification.level is ComplexityLevel.INTERPRET
    assert result.audit.final_confidence == "High"


def test_level4_compare_grounds_a_peer_and_makes_one_answer_llm_call(ingested_conn, monkeypatch) -> None:
    calls = _mock_chain(monkeypatch, _jev_json(4), "HDFC outpaces peers. [FACT] ... **Confidence:** Moderate -- partial peer data.")

    result = route_question(ingested_conn, "Compare HDFC Bank's profit growth with the industry", ["HDFCBANK"])

    assert len(calls) == 2
    assert result.classification.level is ComplexityLevel.COMPARE
    assert result.audit.planner_used is True
    assert "HDFCBANK" in calls[1]["user_message"]
    assert result.audit.missing_data_issues == []  # a peer (ICICIBANK) was actually found


def test_level1_escalates_through_to_level3_when_extraction_fails(ingested_conn, monkeypatch) -> None:
    """Jev picks Level 1, but the question has no extractable metric/fiscal
    year -- Level 1 and then Level 2 must escalate rather than guess, and
    Level 3 is the one that finally makes an answer-generation call."""
    calls = _mock_chain(monkeypatch, _jev_json(1), "General commentary. **Confidence:** Low -- vague question.")

    result = route_question(ingested_conn, "Tell me about HDFC Bank's profitability", ["HDFCBANK"])

    assert len(calls) == 2  # Jev's call, then Level 3's answer call
    assert result.classification.level is ComplexityLevel.RETRIEVE  # Jev's own decision is still recorded as-is
    assert any("escalated to Level 2" in note for note in result.audit.missing_data_issues)
    assert any("escalated to Level 3" in note for note in result.audit.missing_data_issues)


def test_audit_row_is_persisted(ingested_conn, monkeypatch) -> None:
    from storage.repositories import list_signals_routing_log

    _mock_chain(monkeypatch, _jev_json(1))
    result = route_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])

    rows = list_signals_routing_log(ingested_conn)
    assert any(row["run_id"] == result.audit.run_id for row in rows)
