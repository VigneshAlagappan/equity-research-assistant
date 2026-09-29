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

import datetime as dt
import json
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from llm.complexity import ComplexityClassification, ComplexityLevel
from llm.providers.base import ProviderResponse
from research.routing_policy import attempt_deterministic_level, route_question
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)  # HDFCBANK + ICICIBANK, same basic_industry
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)  # net_profit: FY2023=17,000, FY2024=20,500 (INR_CRORE)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _make_screener_workbook_without_vendor_roe(path: Path) -> None:
    """Same shape as _make_screener_workbook, minus the RATIOS sheet's
    vendor-reported "Return on Equity %" row -- so return_on_equity_percent
    has no canonical_financials row at all, and answering an ROE question
    can only come from _level2_calculate deriving it via
    financials/ratios.py's roe_for_company() (net_profit / average
    total_shareholders_funds), not a stored value."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data Sheet"

    sheet.append(["COMPANY NAME", "HDFC BANK LTD"])
    sheet.append([])
    sheet.append(["PROFIT & LOSS"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Net Profit", "17,000", "20,500"])
    sheet.append([])
    sheet.append(["BALANCE SHEET"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Total Shareholders Funds", "220550", "250555"])
    sheet.append([])
    workbook.save(path)


@pytest.fixture
def ingested_conn_without_vendor_roe(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook_without_vendor_roe(file_path)
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
    assert "₹20,500.00 Cr" in result.answer  # financials/calculations.py's format_currency_value (net_profit is INR_CRORE)
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


def _classification(level: int, source: str = "jev") -> ComplexityClassification:
    """Builds a ComplexityClassification directly -- attempt_deterministic_level()
    never calls classify_complexity() itself (the whole point is reusing a
    classification the caller, e.g. web/app.py's classify_and_log(), already
    computed), so these tests never mock the Jev LLM call at all."""
    return ComplexityClassification(level=ComplexityLevel(level), confidence=0.9, reason="test", source=source)


class _RaisingProvider:
    """Fails the test if the answer-generation LLM is ever reached --
    attempt_deterministic_level() must never make one for a real Level 1/2
    outcome."""

    def generate(self, **kw):
        raise AssertionError(f"unexpected LLM call: {kw}")


def test_attempt_deterministic_level_answers_level1_with_no_llm_call(ingested_conn, monkeypatch) -> None:
    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())

    outcome = attempt_deterministic_level(
        ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"], _classification(1),
    )

    assert outcome is not None
    assert "₹20,500.00 Cr" in outcome.answer
    assert outcome.execution_status == "answered"


def test_attempt_deterministic_level_answers_level2_with_no_llm_call(ingested_conn, monkeypatch) -> None:
    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())

    outcome = attempt_deterministic_level(
        ingested_conn, "What was net profit YoY growth in FY2024?", ["HDFCBANK"], _classification(2),
    )

    assert outcome is not None
    assert "%" in outcome.answer
    assert outcome.calculations_performed


def test_attempt_deterministic_level_persists_a_signals_routing_log_row(ingested_conn, monkeypatch) -> None:
    from storage.repositories import list_signals_routing_log

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"], _classification(1),
    )

    rows = list_signals_routing_log(ingested_conn)
    matching = [row for row in rows if row["question"] == "What was net profit in FY2024?"]
    assert len(matching) == 1
    assert matching[0]["jev_level"] == 1
    assert matching[0]["execution_status"] == outcome.execution_status


def test_attempt_deterministic_level_returns_none_for_multi_company(ingested_conn, monkeypatch) -> None:
    from storage.repositories import list_signals_routing_log

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "What was net profit in FY2024?", ["HDFCBANK", "ICICIBANK"], _classification(1),
    )

    assert outcome is None
    assert list_signals_routing_log(ingested_conn) == []


def test_attempt_deterministic_level_returns_none_when_level1_and_2_both_escalate(ingested_conn, monkeypatch) -> None:
    """No extractable metric/fiscal year -- Level 1 then Level 2 both
    escalate, so this must return None (let the caller's own Level 3+
    pipeline run) and must NOT persist a signals_routing_log row, since a
    partial row here would misrepresent what actually answers the question."""
    from storage.repositories import list_signals_routing_log

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "Tell me about HDFC Bank's profitability", ["HDFCBANK"], _classification(1),
    )

    assert outcome is None
    assert list_signals_routing_log(ingested_conn) == []


def test_level1_escalates_to_level2_when_roe_is_not_vendor_reported(
    ingested_conn_without_vendor_roe, monkeypatch,
) -> None:
    """return_on_equity_percent has no canonical_financials row at all in
    this fixture -- Level 1 must escalate (not answer "no data on file"),
    and Level 2 must derive it via financials/ratios.py's roe_for_company()
    (net_profit / average total_shareholders_funds), with zero LLM calls."""
    calls = _mock_chain(monkeypatch, _jev_json(1))

    result = route_question(ingested_conn_without_vendor_roe, "What was HDFC Bank ROE in FY2024?", ["HDFCBANK"])

    assert len(calls) == 1  # only Jev's own classification call -- no answer-generation LLM call
    assert "ROE" in result.answer and "[CALCULATION]" in result.answer
    assert result.classification.level is ComplexityLevel.RETRIEVE  # Jev's own decision recorded as-is
    assert result.audit.execution_status == "answered"
    assert any("escalated to Level 2" in note for note in result.audit.missing_data_issues)


def test_attempt_deterministic_level_derives_roe_with_no_llm_call(
    ingested_conn_without_vendor_roe, monkeypatch,
) -> None:
    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())

    outcome = attempt_deterministic_level(
        ingested_conn_without_vendor_roe, "What was HDFC Bank ROE in FY2024?", ["HDFCBANK"], _classification(1),
    )

    assert outcome is not None
    assert outcome.calculations_performed
    assert outcome.execution_status == "answered"


def test_level1_still_answers_directly_when_roe_is_vendor_reported(ingested_conn, monkeypatch) -> None:
    """ingested_conn's fixture DOES carry a vendor-reported Return on Equity
    % row -- Level 1 must still answer it as a plain FACT, not detour
    through Level 2's derivation path just because ROE is in
    _DERIVED_RATIO_CALCULATORS."""
    calls = _mock_chain(monkeypatch, _jev_json(1))

    result = route_question(ingested_conn, "What was HDFC Bank ROE in FY2024?", ["HDFCBANK"])

    assert len(calls) == 1
    assert "[FACT]" in result.answer
    assert "[CALCULATION]" not in result.answer
    assert result.audit.missing_data_issues == []  # no escalation needed


def test_attempt_deterministic_level_records_an_execution_metrics_row(ingested_conn, monkeypatch) -> None:
    """Execution Analytics (llm/execution_metrics.py) previously had no
    visibility into the fast path at all -- a real Level 1/2 answer never
    showed up on the dashboard, which otherwise only ever saw the much
    slower answer_question()/run_investigation() traffic."""
    from storage.repositories import list_execution_metrics

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"], _classification(1),
    )

    assert outcome is not None
    rows = list_execution_metrics(ingested_conn)
    matching = [row for row in rows if row["task_name"] == "signals_fast_path"]
    assert len(matching) == 1
    assert matching[0]["complexity_level"] == 1  # Jev's real level, not hardness.py's unrelated tier number
    assert matching[0]["status"] == "answered"


def test_attempt_deterministic_level_records_escalated_status_with_no_llm_call(
    ingested_conn, monkeypatch,
) -> None:
    from storage.repositories import list_execution_metrics

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "Tell me about HDFC Bank's profitability", ["HDFCBANK"], _classification(1),
    )

    assert outcome is None
    rows = list_execution_metrics(ingested_conn)
    matching = [row for row in rows if row["task_name"] == "signals_fast_path"]
    assert len(matching) == 1
    assert matching[0]["status"] == "escalated"


def test_attempt_deterministic_level_returns_none_immediately_for_level3_plus(ingested_conn, monkeypatch) -> None:
    """A Level 3/4/5 classification must never even attempt Level 1/2 code --
    returns None with zero LLM calls and zero signals_routing_log rows,
    leaving the caller's existing answer_question()/investigation pipeline
    as the only thing that runs."""
    from storage.repositories import list_signals_routing_log

    monkeypatch.setattr("llm.router.anthropic_provider", _RaisingProvider())
    outcome = attempt_deterministic_level(
        ingested_conn, "Why did HDFC Bank's margins compress?", ["HDFCBANK"], _classification(3),
    )

    assert outcome is None
    assert list_signals_routing_log(ingested_conn) == []
