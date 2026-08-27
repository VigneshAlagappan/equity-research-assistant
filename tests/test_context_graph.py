"""context/graph.py — cross-company relationship traversal: sector peers,
direct metric mentions, and domain-knowledge seed-edge bridging."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from context.graph import find_related_investigations
from ingestion.pipeline import ingest_file
from storage.repositories import save_generated_report, save_report_evidence
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def two_bank_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    """HDFCBANK and ICICIBANK — seed_companies() already registers both as
    "Private Sector Bank" (companies/registry.py), i.e. real sector peers."""
    seed_companies(db_conn)
    for company_id in ("HDFCBANK", "ICICIBANK"):
        file_path = tmp_path / f"{company_id}.xlsx"
        _make_screener_workbook(file_path)
        ingest_file(db_conn, file_path, company_id=company_id, source_id="screener")
    return db_conn


def _save_report(conn: sqlite3.Connection, thread_id: str, question: str, company_id: str, evidence_label: str) -> None:
    save_generated_report(conn, thread_id, question, [company_id], "consolidated", f"# Report\n{question}")
    save_report_evidence(conn, thread_id, [
        {"kind": "FACT", "company_id": company_id, "label": evidence_label, "value": "1", "citation": "c"},
    ])


def test_finds_a_sector_peer_investigation_on_a_directly_mentioned_metric(two_bank_conn: sqlite3.Connection) -> None:
    _save_report(two_bank_conn, "t1", "How is NIM trending?", "HDFCBANK", "net_interest_margin (FY2024)")

    candidates = find_related_investigations(two_bank_conn, "What is the net interest margin outlook?", ["ICICIBANK"])

    assert len(candidates) == 1
    assert candidates[0].thread_id == "t1"
    assert candidates[0].company_ids == ["HDFCBANK"]
    assert "SAME_SECTOR_AS" in candidates[0].path


def test_excludes_investigations_about_the_target_company_itself(two_bank_conn: sqlite3.Connection) -> None:
    _save_report(two_bank_conn, "t1", "How is NIM trending?", "ICICIBANK", "net_interest_margin (FY2024)")

    candidates = find_related_investigations(two_bank_conn, "What is the net interest margin outlook?", ["ICICIBANK"])

    assert candidates == []  # this is context/reuse.py's job, not the graph's


def test_no_candidates_when_no_metric_is_mentioned(two_bank_conn: sqlite3.Connection) -> None:
    _save_report(two_bank_conn, "t1", "How is NIM trending?", "HDFCBANK", "net_interest_margin (FY2024)")

    candidates = find_related_investigations(two_bank_conn, "Tell me something interesting", ["ICICIBANK"])

    assert candidates == []


def test_multi_company_question_returns_nothing(two_bank_conn: sqlite3.Connection) -> None:
    _save_report(two_bank_conn, "t1", "How is NIM trending?", "HDFCBANK", "net_interest_margin (FY2024)")

    candidates = find_related_investigations(two_bank_conn, "Compare NIM", ["ICICIBANK", "HDFCBANK"])

    assert candidates == []


def test_seed_edge_bridges_a_related_but_unmentioned_metric(two_bank_conn: sqlite3.Connection) -> None:
    """The question names repo rate, not NIM — config/knowledge_graph_seed.py's
    rbi_repo_rate -> AFFECTS -> net_interest_margin edge should still surface
    a peer's NIM investigation."""
    _save_report(two_bank_conn, "t1", "How is NIM trending?", "HDFCBANK", "net_interest_margin (FY2024)")

    candidates = find_related_investigations(two_bank_conn, "How would an RBI repo rate cut play out?", ["ICICIBANK"])

    assert len(candidates) == 1
    assert "bridged via net_interest_margin" in candidates[0].path


def test_no_sector_data_means_no_candidates(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    from companies.registry import register_company

    register_company(db_conn, company_id="NOSECTOR", legal_name="No Sector Co Ltd", display_name="No Sector Co")
    file_path = tmp_path / "NOSECTOR.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="NOSECTOR", source_id="screener")

    candidates = find_related_investigations(db_conn, "What is the net interest margin?", ["NOSECTOR"])

    assert candidates == []


def test_unrelated_sector_peer_investigation_is_not_surfaced(two_bank_conn: sqlite3.Connection) -> None:
    _save_report(two_bank_conn, "t1", "What's the dividend history?", "HDFCBANK", "Net Profit FY2024")

    candidates = find_related_investigations(two_bank_conn, "What is the net interest margin outlook?", ["ICICIBANK"])

    assert candidates == []  # peer exists, but nothing in that investigation matches the metric


def test_injected_fact_store_is_used_for_the_sqlite_path(two_bank_conn: sqlite3.Connection) -> None:
    """Proves the FactStore seam is wired into find_related_investigations'
    SQLite path — real sector-peer data (get_company/list_companies_by_sector_field,
    left as the real default) plus a FAKE list_generated_reports/
    list_report_evidence surfaces a candidate even though no real
    generated_reports row was ever inserted into two_bank_conn."""
    from dataclasses import replace

    from storage.fact_store import default_fact_store

    fake_report = {
        "thread_id": "fake-t1", "question": "How is NIM trending?", "company_ids": ["HDFCBANK"],
        "report_markdown": "# fake",
    }
    fs = replace(
        default_fact_store(),
        list_generated_reports=lambda conn: [fake_report],
        list_report_evidence=lambda conn, thread_id: [
            {"label": "net_interest_margin (FY2024)"},
        ],
    )

    candidates = find_related_investigations(
        two_bank_conn, "What is the net interest margin outlook?", ["ICICIBANK"], fact_store=fs
    )

    assert len(candidates) == 1
    assert candidates[0].thread_id == "fake-t1"
