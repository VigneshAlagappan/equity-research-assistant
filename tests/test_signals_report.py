"""research/signals_report.py tests — the Anthropic client is mocked
throughout, same pattern as tests/test_assistant.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from research.signals_report import generate_signals_report
from tests.test_screener_adapter import _make_screener_workbook


class _FakeMessages:
    def __init__(self, content: list, stop_reason: str, captured: list) -> None:
        self._content = content
        self._stop_reason = stop_reason
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(content=self._content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, content: list, stop_reason: str, captured: list) -> None:
        self.messages = _FakeMessages(content, stop_reason, captured)


def _install_fake_client(monkeypatch, text: str = "report", stop_reason: str = "end_turn"):
    captured: list = []
    content = [SimpleNamespace(type="text", text=text)] if text else []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, stop_reason, captured),
    )
    return captured


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_generate_signals_report_returns_llm_text(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, text="## The Short Answer\nNet profit grew. [FACT] ...")

    result = generate_signals_report(ingested_conn, "How did net profit change?", ["HDFCBANK"])

    assert result.report_markdown == "## The Short Answer\nNet profit grew. [FACT] ..."
    assert result.evidence  # grounded in the ingested company's real evidence
    assert result.followups == []


def test_generate_signals_report_reuses_a_fresh_prior_report_without_calling_the_llm(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    from storage.repositories import save_generated_report, save_report_evidence

    captured = _install_fake_client(monkeypatch, text="## The Short Answer\nNet profit grew. [FACT] ...")
    first = generate_signals_report(ingested_conn, "How did net profit change?", ["HDFCBANK"])
    save_generated_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"], "consolidated", first.report_markdown)
    save_report_evidence(ingested_conn, "t1", [
        {"kind": e.kind, "company_id": e.company_id, "label": e.label, "value": e.value, "citation": e.citation}
        for e in first.evidence
    ])

    second = generate_signals_report(ingested_conn, "How did net profit change", ["HDFCBANK"])

    assert len(captured) == 1  # the LLM was called exactly once, not twice
    assert second.report_markdown == first.report_markdown


def test_generate_signals_report_parses_followup_marker(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(
        monkeypatch,
        text=(
            "## The Short Answer\nNet profit grew. [FACT] ...\n"
            "===FOLLOWUP_QUESTIONS===\n"
            "How does this compare to peers?\n"
            "- What drove the FY2024 jump?\n"
        ),
    )

    result = generate_signals_report(ingested_conn, "How did net profit change?", ["HDFCBANK"])

    assert "===FOLLOWUP_QUESTIONS===" not in result.report_markdown
    assert result.followups == ["How does this compare to peers?", "What drove the FY2024 jump?"]


def test_generate_signals_report_sends_evidence_and_signals_prompt(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    captured = _install_fake_client(monkeypatch)

    generate_signals_report(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])

    assert len(captured) == 1
    sent = captured[0]["messages"][0]["content"]
    assert "Net Profit FY2024" in sent
    assert "20,500.00" in sent
    assert "Question: What was net profit in FY2024?" in sent
    assert "Signals" in captured[0]["system"]


def test_generate_signals_report_without_any_data_skips_the_api_call(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    seed_companies(db_conn)
    captured = _install_fake_client(monkeypatch, text="should never be returned")

    result = generate_signals_report(db_conn, "How is HDFC doing?", ["HDFCBANK"])

    assert "No data ingested yet" in result.report_markdown
    assert result.evidence == []
    assert result.followups == []
    assert captured == []


def test_generate_signals_report_handles_refusal(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, text="", stop_reason="refusal")

    result = generate_signals_report(ingested_conn, "test question", ["HDFCBANK"])

    assert "declined" in result.report_markdown.lower()
    assert result.evidence == []


def test_generate_signals_report_handles_empty_non_refusal_response(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    _install_fake_client(monkeypatch, text="", stop_reason="max_tokens")

    result = generate_signals_report(ingested_conn, "test question", ["HDFCBANK"])

    assert "no report" in result.report_markdown.lower()
    assert result.evidence == []


def test_injected_investigation_memory_capability_is_used(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    """Proves the Investigation Memory capability seam
    (research/capabilities.py's InvestigationMemoryCapabilities) is wired
    into generate_signals_report's related_investigations call — an injected
    fake GraphCandidate's path shows up in the prompt sent to the LLM, even
    though no real sector-peer investigation exists in ingested_conn."""
    from context.graph import GraphCandidate
    from research.capabilities import InvestigationMemoryCapabilities

    captured = _install_fake_client(monkeypatch, text="## The Short Answer\nfine")
    fake_candidate = GraphCandidate(
        thread_id="fake-t1", company_ids=["ICICIBANK"], question="q", report_markdown="r",
        score=0.9, path="fake injected path",
    )
    mem = InvestigationMemoryCapabilities(
        reusable_report=lambda conn, question, company_ids, statement_type: None,
        related_investigations=lambda conn, question, company_ids: [fake_candidate],
    )

    generate_signals_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], investigation_memory=mem)

    sent = captured[0]["messages"][0]["content"]
    assert "fake injected path" in sent
