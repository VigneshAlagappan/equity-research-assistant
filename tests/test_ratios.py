from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from companies.registry import register_company, seed_companies
from financials.calculations import MissingDataError
from financials.ratios import (
    SectorMismatchError,
    get_required_metric,
    gnpa_ratio,
    net_profit_margin,
    nim,
    roa,
    roa_for_company,
    roe,
    roe_for_company,
    vendor_reported,
)
from ingestion.pipeline import ingest_file
from tests.test_screener_adapter import _make_screener_workbook


# ------------------------------------------------------------------
# Pure functions.
# ------------------------------------------------------------------


def test_roa_matches_hand_calculation() -> None:
    assert roa(20500, 2375000) == pytest.approx(20500 / 2375000 * 100)


def test_roa_rejects_non_positive_denominator() -> None:
    with pytest.raises(ValueError):
        roa(20500, 0)


def test_roe_matches_hand_calculation() -> None:
    assert roe(20500, 235552.5) == pytest.approx(20500 / 235552.5 * 100)


def test_roe_rejects_non_positive_denominator() -> None:
    with pytest.raises(ValueError):
        roe(20500, -1)


def test_nim_matches_hand_calculation() -> None:
    assert nim(36000, 1625000) == pytest.approx(36000 / 1625000 * 100)


def test_gnpa_ratio_matches_hand_calculation() -> None:
    assert gnpa_ratio(19250, 1750000) == pytest.approx(19250 / 1750000 * 100)


def test_net_profit_margin_matches_hand_calculation() -> None:
    assert net_profit_margin(20500, 66000) == pytest.approx(20500 / 66000 * 100)


# ------------------------------------------------------------------
# Sector-aware fetch + DB-backed wrappers, against ingested (synthetic) data.
# ------------------------------------------------------------------


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_get_required_metric_returns_canonical_row(ingested_conn: sqlite3.Connection) -> None:
    row = get_required_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2024")
    assert row["canonical_value"] == 20500.0


def test_get_required_metric_rejects_unknown_company(ingested_conn: sqlite3.Connection) -> None:
    with pytest.raises(MissingDataError):
        get_required_metric(ingested_conn, "NOPE", "net_profit", "FY2024")


def test_get_required_metric_rejects_missing_period(ingested_conn: sqlite3.Connection) -> None:
    with pytest.raises(MissingDataError):
        get_required_metric(ingested_conn, "HDFCBANK", "net_profit", "FY1999")


def test_get_required_metric_refuses_sector_inapplicable_metric(ingested_conn: sqlite3.Connection) -> None:
    register_company(
        ingested_conn, "TESTCO", "Test Software Co Ltd", "Test Software Co",
        sector="Technology", industry="IT Services",
    )
    # interest_earned is tagged ["bank"] in metrics_dictionary — TESTCO isn't a bank.
    with pytest.raises(SectorMismatchError):
        get_required_metric(ingested_conn, "TESTCO", "interest_earned", "FY2024")


def test_roa_for_company(ingested_conn: sqlite3.Connection) -> None:
    result = roa_for_company(ingested_conn, "HDFCBANK", "FY2024")
    avg_assets = (2550000.0 + 2200000.0) / 2
    assert result.value == pytest.approx(20500.0 / avg_assets * 100)
    assert result.kind == "CALCULATION"
    assert "FY2024" in result.explanation


def test_roe_for_company(ingested_conn: sqlite3.Connection) -> None:
    result = roe_for_company(ingested_conn, "HDFCBANK", "FY2024")
    avg_equity = (250555.0 + 220550.0) / 2
    assert result.value == pytest.approx(20500.0 / avg_equity * 100)


def test_roa_for_company_missing_prior_year_raises(ingested_conn: sqlite3.Connection) -> None:
    with pytest.raises(MissingDataError):
        roa_for_company(ingested_conn, "HDFCBANK", "FY2023")  # no FY2022 total_assets


def test_roe_for_company_derives_shareholders_funds_when_not_directly_reported(
    tmp_path: Path, db_conn: sqlite3.Connection
) -> None:
    """Regression test: some real exports (verified: Jio Financial Services,
    an NBFC) report Equity Share Capital and Reserves separately with no
    combined Total Shareholders Funds row at all. ROE must still compute,
    deriving shareholders' funds via the exact identity ESC + Reserves.
    """
    from companies.registry import register_company

    register_company(
        db_conn, "NOTOTALSFCO", "No Total SF Co Ltd", "No Total SF Co",
        sector="Financial Services", industry="NBFC",
    )

    file_path = tmp_path / "NOTOTALSFCO.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data Sheet"
    sheet.append(["PROFIT & LOSS"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Net profit", "1000", "1500"])
    sheet.append([])
    sheet.append(["BALANCE SHEET"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Equity Share Capital", "100", "100"])
    sheet.append(["Reserves", "9000", "10000"])
    sheet.append(["Total Assets", "50000", "60000"])
    sheet.append([])
    workbook.save(file_path)

    ingest_file(db_conn, file_path, company_id="NOTOTALSFCO", source_id="screener")

    result = roe_for_company(db_conn, "NOTOTALSFCO", "FY2024")
    avg_equity = ((100 + 10000) + (100 + 9000)) / 2
    assert result.value == pytest.approx(1500.0 / avg_equity * 100)
    assert "derived as equity_share_capital + reserves" in result.explanation


def test_vendor_reported_is_labeled_fact_not_calculation(ingested_conn: sqlite3.Connection) -> None:
    result = vendor_reported(ingested_conn, "HDFCBANK", "gross_npa_percent", "FY2024")
    assert result.kind == "FACT"
    assert result.value == pytest.approx(1.1)
    assert result.unit == "PERCENT"
    assert "as reported" in result.explanation
