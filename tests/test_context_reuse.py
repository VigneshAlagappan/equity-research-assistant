"""context/reuse.py — reuse-before-recompute against generated_reports, and
the freshness check that stops a stale report from being reused."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from companies.registry import seed_companies
from context.reuse import find_reusable_report
from ingestion.pipeline import ingest_file
from storage.repositories import save_generated_report, save_report_evidence
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _save_report(conn: sqlite3.Connection, thread_id: str, question: str, company_ids: list[str]) -> None:
    save_generated_report(conn, thread_id, question, company_ids, "consolidated", f"# Report\n{question}")
    save_report_evidence(conn, thread_id, [
        {"kind": "FACT", "company_id": company_ids[0], "label": "Net Profit FY2024", "value": "100", "citation": "c"},
    ])


def test_fresh_report_on_same_question_is_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "consolidated")

    assert candidate is not None
    assert candidate.thread_id == "t1"
    assert candidate.evidence


def test_near_duplicate_wording_is_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change over time?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change over time", ["HDFCBANK"], "consolidated")

    assert candidate is not None


def test_unrelated_question_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "What is the CASA ratio outlook?", ["HDFCBANK"], "consolidated")

    assert candidate is None


def test_different_company_scope_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK", "ICICIBANK"], "consolidated")

    assert candidate is None


def test_different_statement_type_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "standalone")

    assert candidate is None


def test_stale_report_is_not_reused_after_new_data_is_ingested(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A report generated before fresh data was ingested must not be handed
    back as if it reflects that new data (README §17)."""
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    time.sleep(0.01)  # ensure a strictly later created_at timestamp
    file_path = tmp_path / "ICICIBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(ingested_conn, file_path, company_id="ICICIBANK", source_id="screener")
    # Re-ingesting HDFCBANK itself is what actually invalidates an HDFCBANK report;
    # simulate that by ingesting fresh HDFCBANK data again.
    hdfc_file = tmp_path / "HDFCBANK_v2.xlsx"
    _make_screener_workbook(hdfc_file)
    ingest_file(ingested_conn, hdfc_file, company_id="HDFCBANK", source_id="screener")

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "consolidated")

    assert candidate is None


def test_injected_fact_store_is_used_instead_of_the_real_tables(db_conn: sqlite3.Connection) -> None:
    """Proves the FactStore seam (storage/fact_store.py, architecture
    guardrail #3's 'access structured facts through a repository interface')
    is actually wired into find_reusable_report — an injected fake FactStore
    surfaces a candidate even though db_conn here has no real
    generated_reports row at all."""
    from dataclasses import replace

    from storage.fact_store import default_fact_store

    fake_report = {
        "thread_id": "fake-t1", "question": "How did net profit change?", "company_ids": ["HDFCBANK"],
        "statement_type": "consolidated", "report_markdown": "# fake", "generated_at": "2099-01-01T00:00:00Z",
    }
    fs = replace(
        default_fact_store(),
        list_generated_reports=lambda conn: [fake_report],
        list_report_evidence=lambda conn, thread_id: [],
        list_report_followups=lambda conn, thread_id: [],
        get_latest_data_timestamp=lambda conn, company_ids: None,
    )

    candidate = find_reusable_report(
        db_conn, "How did net profit change?", ["HDFCBANK"], "consolidated", fact_store=fs
    )

    assert candidate is not None
    assert candidate.thread_id == "fake-t1"
