"""End-to-end: build_analysis_report() over real ingested (synthetic) data.

Proves raw -> normalized -> derived end-to-end via a text report (README:
Implementation Sequence, step 3) — no charts, no LLM narrative yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from financials.report import build_analysis_report
from ingestion.pipeline import ingest_file
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_report_header_identifies_company(ingested_conn: sqlite3.Connection) -> None:
    report = build_analysis_report(ingested_conn, "HDFCBANK")
    assert "HDFC Bank (HDFCBANK)" in report
    assert "Sector: Financial Services" in report


def test_report_includes_net_profit_trend_with_yoy_and_cagr(ingested_conn: sqlite3.Connection) -> None:
    report = build_analysis_report(ingested_conn, "HDFCBANK")
    assert "-- Net Profit (INR_CRORE) --" in report
    assert "FY2023: 17,000.00" in report
    assert "FY2024: 20,500.00" in report
    assert "YoY:" in report
    assert "CAGR FY2023-FY2024:" in report
    assert "[CALCULATION]" in report


def test_report_includes_roa_and_roe(ingested_conn: sqlite3.Connection) -> None:
    report = build_analysis_report(ingested_conn, "HDFCBANK")
    assert "-- Profitability Ratios (calculated) --" in report
    assert "ROA FY2024:" in report
    assert "ROE FY2024:" in report
    # FY2023 has no prior-year balance sheet data in the fixture, so no ROA/ROE line for it.
    assert "ROA FY2023:" not in report


def test_report_includes_vendor_reported_ratios_labeled_fact(ingested_conn: sqlite3.Connection) -> None:
    report = build_analysis_report(ingested_conn, "HDFCBANK")
    assert "Other Ratios, vendor-reported" in report
    assert "Gross NPA %" in report
    assert "[FACT]" in report


def test_report_for_company_with_no_data_says_so(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    report = build_analysis_report(db_conn, "ICICIBANK")
    assert "No data ingested yet" in report


def test_report_unknown_company_raises(db_conn: sqlite3.Connection) -> None:
    from financials.calculations import MissingDataError

    with pytest.raises(MissingDataError):
        build_analysis_report(db_conn, "NOPE")
