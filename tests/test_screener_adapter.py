"""ScreenerAdapter tests against a synthetic "Data Sheet"-shaped fixture.

No real Screener export is bundled with the repo (raw files are manually
obtained, never committed — README: Ingestion Approach by Source). This
fixture's *layout* is not a guess: it matches a real Screener.in export
verified by hand (Jio Financial Services) — a "Data Sheet" tab with labeled
sections ("PROFIT & LOSS", "Quarters", "BALANCE SHEET", "RATIOS"), each
followed by a "Report Date" row of real datetime values, then metric rows
aligned to those columns. The pretty per-topic sheets Screener also ships
(Profit & Loss, Balance Sheet, ...) are formula views with no cached values
and are not what this adapter reads.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from sources.screener import ScreenerAdapter


def _make_screener_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data Sheet"

    sheet.append(["COMPANY NAME", "HDFC BANK LTD"])
    sheet.append([])

    sheet.append(["PROFIT & LOSS"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Interest Earned", "50,000", "60,000"])
    sheet.append(["Other Income", "5,000", "6,000"])
    sheet.append(["Interest Expended", "20,000", "24,000"])
    sheet.append(["Operating Expenses", "10,000", "12,000"])
    sheet.append(["Provisions & Contingencies", "2,000", "2,500"])
    sheet.append(["Profit before tax", "23,000", "27,500"])
    sheet.append(["Tax", "6,000", "7,000"])
    sheet.append(["Net Profit", "17,000", "20,500"])
    sheet.append(["EPS in Rs", "22.5", "27.1"])
    sheet.append(["Some Unmapped Row", "999", "999"])  # no alias -> must be skipped
    sheet.append([])

    sheet.append(["Quarters"])
    sheet.append([
        "Report Date",
        dt.datetime(2023, 6, 30), dt.datetime(2023, 9, 30),
        dt.datetime(2023, 12, 31), dt.datetime(2024, 3, 31),
    ])
    sheet.append(["Interest Earned", "14,000", "14,500", "15,000", "16,500"])
    sheet.append(["Net Profit", "4,200", "4,500", "4,800", "5,000"])
    sheet.append([])

    sheet.append(["BALANCE SHEET"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Equity Share Capital", "550", "555"])
    sheet.append(["Reserves", "220000", "250000"])
    sheet.append(["Total Shareholders Funds", "220550", "250555"])
    sheet.append(["Deposits", "1800000", "2100000"])
    sheet.append(["Advances", "1500000", "1750000"])
    sheet.append(["Total Assets", "2200000", "2550000"])
    sheet.append([])

    sheet.append(["RATIOS"])
    sheet.append(["Report Date", dt.datetime(2023, 3, 31), dt.datetime(2024, 3, 31)])
    sheet.append(["Gross NPA %", "1.2%", "1.1%"])
    sheet.append(["Net NPA %", "0.3%", "0.2%"])
    sheet.append(["Return on Equity %", "17.1", "17.8"])
    sheet.append([])

    workbook.save(path)


def _find_one(observations, **filters):
    matches = [
        o for o in observations
        if all(getattr(o, key) == value for key, value in filters.items())
    ]
    assert len(matches) == 1, f"expected exactly one match for {filters}, got {len(matches)}"
    return matches[0]


def test_parses_annual_profit_and_loss(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")

    net_profit_fy24 = _find_one(
        observations, metric_key="net_profit", period_type="annual", fiscal_year="FY2024"
    )
    assert net_profit_fy24.value == 20500.0
    assert net_profit_fy24.unit == "INR_CRORE"
    assert net_profit_fy24.statement_type == "consolidated"
    assert net_profit_fy24.source == "screener"
    assert net_profit_fy24.quarter is None


def test_parses_balance_sheet(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")

    total_assets_fy24 = _find_one(
        observations, metric_key="total_assets", period_type="annual", fiscal_year="FY2024"
    )
    assert total_assets_fy24.value == 2550000.0

    deposits_fy23 = _find_one(
        observations, metric_key="deposits", period_type="annual", fiscal_year="FY2023"
    )
    assert deposits_fy23.value == 1800000.0


def test_parses_quarterly_section_into_correct_quarters(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")

    q1 = _find_one(
        observations, metric_key="net_profit", period_type="quarterly", fiscal_year="FY2024", quarter="Q1"
    )
    assert q1.value == 4200.0

    q4 = _find_one(
        observations, metric_key="net_profit", period_type="quarterly", fiscal_year="FY2024", quarter="Q4"
    )
    assert q4.value == 5000.0


def test_percent_sign_overrides_default_unit(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")

    gnpa_fy24 = _find_one(
        observations, metric_key="gross_npa_percent", period_type="annual", fiscal_year="FY2024"
    )
    assert gnpa_fy24.value == 1.1
    assert gnpa_fy24.unit == "PERCENT"


def test_unmapped_row_label_is_skipped_not_raised(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")

    assert not any(o.value == 999.0 for o in observations)


def test_standalone_statement_type_is_honored(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)

    observations = ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK", statement_type="standalone")

    assert all(o.statement_type == "standalone" for o in observations)


def test_raises_on_workbook_without_data_sheet(tmp_path: Path, db_conn: sqlite3.Connection) -> None:
    file_path = tmp_path / "not_a_screener_export.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Sheet1"
    workbook.save(file_path)

    with pytest.raises(ValueError, match="Data Sheet"):
        ScreenerAdapter(db_conn).parse(file_path, "HDFCBANK")
