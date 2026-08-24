"""retrieval/structured_search.py never calls the LLM — pure DB/calculation
tests, same fixture pattern as tests/test_calculations.py and test_ratios.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from retrieval.structured_search import get_comparison_evidence, get_company_evidence
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_get_company_evidence_empty_without_data(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert get_company_evidence(db_conn, "HDFCBANK") == []


def test_get_company_evidence_includes_fact_rows_for_reported_metrics(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    net_profit_fy24 = next(e for e in evidence if e.label == "Net Profit FY2024")
    assert net_profit_fy24.kind == "FACT"
    assert net_profit_fy24.value == "20,500.00 INR_CRORE"
    assert "only source available" in net_profit_fy24.citation


def test_get_company_evidence_includes_calculation_rows(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    calc_labels = {e.label for e in evidence if e.kind == "CALCULATION"}
    assert any("YoY growth" in label for label in calc_labels)
    assert any("CAGR" in label for label in calc_labels)
    assert any(label.startswith("ROA") for label in calc_labels)
    assert any(label.startswith("ROE") for label in calc_labels)


def test_get_company_evidence_includes_vendor_reported_facts(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    gnpa = next((e for e in evidence if "gross_npa_percent" in e.label), None)
    assert gnpa is not None
    assert gnpa.kind == "FACT"


def test_get_company_evidence_all_company_ids_match(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    assert evidence  # sanity: fixture actually produced evidence
    assert all(e.company_id == "HDFCBANK" for e in evidence)


def test_get_comparison_evidence_combines_multiple_companies(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_comparison_evidence(ingested_conn, ["HDFCBANK", "ICICIBANK"])
    company_ids = {e.company_id for e in evidence}
    # ICICIBANK is registered (seed_companies) but has no ingested data —
    # contributes nothing, doesn't error the whole comparison.
    assert company_ids == {"HDFCBANK"}


def test_get_comparison_evidence_unregistered_company_contributes_nothing(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_comparison_evidence(ingested_conn, ["HDFCBANK", "NOPE"])
    assert all(e.company_id == "HDFCBANK" for e in evidence)


def test_get_company_evidence_skips_ratio_for_zero_denominator_year(ingested_conn: sqlite3.Connection) -> None:
    """A genuinely-reported total_assets of 0.0 (not missing data — real ingested source
    data some companies have for early years, e.g. ICICIBANK FY2004-FY2013) makes ROA's
    average-assets denominator <=0. roa()/roe() (financials/ratios.py) raise ValueError
    for that, not MissingDataError — get_company_evidence must catch it too and just skip
    that year's ratio, the same as it already does for MissingDataError, rather than
    letting the whole evidence-gathering call blow up."""
    ingested_conn.execute(
        "UPDATE canonical_financials SET canonical_value = 0.0 "
        "WHERE company_id = 'HDFCBANK' AND metric_key = 'total_assets' AND fiscal_year IN ('FY2023', 'FY2024')"
    )
    ingested_conn.commit()

    evidence = get_company_evidence(ingested_conn, "HDFCBANK")  # must not raise

    roa_labels = [e.label for e in evidence if e.label.startswith("ROA (FY2024)")]
    assert roa_labels == []  # FY2024's prior-year (FY2023) assets are the degenerate 0.0
