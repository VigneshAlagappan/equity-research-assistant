"""research/system_insights.py (Tools tab's Insights panel) — cross-company,
Knowledge-Graph-grounded, batch-generated. Anthropic mocked throughout, same
dispatch-by-system-prompt pattern tests/test_investigation.py already uses."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from research.system_insights import (
    SYSTEM_INSIGHTS_SYSTEM_PROMPT,
    SystemInsightGenerationError,
    generate_system_insights,
)
from storage.repositories import insert_knowledge_claim, list_system_insights, save_company_document


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _insert_claim(conn: sqlite3.Connection, company_id: str, *, claim_type: str, claim_text: str) -> int:
    doc = save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/doc.pdf",
    )
    claim = insert_knowledge_claim(
        conn, document_id=doc["document_id"], company_id=company_id, claim_type=claim_type, category="risk",
        claim_text=claim_text, speaker=None, fiscal_year="FY2024", quarter=None, extraction_confidence=0.9,
    )
    return claim["claim_id"]


class _FakeMessages:
    def __init__(self, text: str, captured: list) -> None:
        self._text = text
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn")


class _FakeClient:
    def __init__(self, text: str, captured: list) -> None:
        self.messages = _FakeMessages(text, captured)


def _install_fake_client(monkeypatch, text: str) -> list:
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: _FakeClient(text, captured)
    )
    return captured


def test_no_candidates_returns_empty_without_an_llm_call(company_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, text="should never be called")

    insights = generate_system_insights(company_conn)

    assert insights == []
    assert captured == []


def test_generates_and_persists_a_grounded_insight(company_conn: sqlite3.Connection, monkeypatch) -> None:
    claim_id = _insert_claim(
        company_conn, "HDFCBANK", claim_type="CAUSATION",
        claim_text="Rising input costs directly compressed margins.",
    )
    response_text = (
        '[{"insight_text": "HDFCBANK margins compressed due to rising input costs.", '
        f'"company_ids": ["HDFCBANK"], "source_claim_ids": [{claim_id}]}}]'
    )
    captured = _install_fake_client(monkeypatch, response_text)

    insights = generate_system_insights(company_conn)

    assert len(insights) == 1
    assert insights[0].company_ids == ["HDFCBANK"]
    assert insights[0].source_claim_ids == [claim_id]
    assert captured[0]["system"] == SYSTEM_INSIGHTS_SYSTEM_PROMPT.format(max_insights=5)[:len(captured[0]["system"])]

    rows = list_system_insights(company_conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "new"
    assert rows[0]["insight_text"] == "HDFCBANK margins compressed due to rising input costs."


def test_hallucinated_claim_id_is_dropped(company_conn: sqlite3.Connection, monkeypatch) -> None:
    """An insight grounded in a claim_id that isn't actually one of the
    candidates gets dropped, not stored as if it were real provenance."""
    _insert_claim(company_conn, "HDFCBANK", claim_type="CAUSATION", claim_text="Real claim.")
    response_text = '[{"insight_text": "Fabricated.", "company_ids": ["HDFCBANK"], "source_claim_ids": [999999]}]'
    _install_fake_client(monkeypatch, response_text)

    insights = generate_system_insights(company_conn)

    assert insights == []
    assert list_system_insights(company_conn) == []


def test_unparseable_response_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert_claim(company_conn, "HDFCBANK", claim_type="CAUSATION", claim_text="Real claim.")
    _install_fake_client(monkeypatch, "not json")

    with pytest.raises(SystemInsightGenerationError):
        generate_system_insights(company_conn)
