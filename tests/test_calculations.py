from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from financials.calculations import (
    CalculationError,
    MissingDataError,
    cagr,
    cagr_for_metric,
    format_currency_value,
    qoq_growth,
    qoq_growth_for_metric,
    rolling_avg,
    yoy_growth,
    yoy_growth_for_metric,
)
from ingestion.pipeline import ingest_file
from tests.test_screener_adapter import _make_screener_workbook


# ------------------------------------------------------------------
# Pure functions — fixture values worked out by hand.
# ------------------------------------------------------------------


def test_cagr_two_year_doubling() -> None:
    # Doubling over 1 year is a 100% CAGR.
    assert cagr(100, 200, 1) == pytest.approx(100.0)


def test_cagr_matches_hand_calculation() -> None:
    # 17,000 -> 20,500 over 1 year: (20500/17000 - 1) * 100
    assert cagr(17000, 20500, 1) == pytest.approx((20500 / 17000 - 1) * 100)


def test_cagr_rejects_non_positive_begin_value() -> None:
    with pytest.raises(CalculationError):
        cagr(0, 100, 1)


def test_cagr_rejects_non_positive_periods() -> None:
    with pytest.raises(CalculationError):
        cagr(100, 200, 0)


def test_yoy_growth() -> None:
    assert yoy_growth(120, 100) == pytest.approx(20.0)
    assert yoy_growth(80, 100) == pytest.approx(-20.0)


def test_yoy_growth_rejects_zero_previous() -> None:
    with pytest.raises(CalculationError):
        yoy_growth(100, 0)


def test_qoq_growth() -> None:
    assert qoq_growth(5000, 4800) == pytest.approx((5000 - 4800) / 4800 * 100)


def test_rolling_avg() -> None:
    assert rolling_avg([1, 2, 3, 4, 5], 3) == pytest.approx([2.0, 3.0, 4.0])


def test_rolling_avg_rejects_window_larger_than_series() -> None:
    with pytest.raises(CalculationError):
        rolling_avg([1, 2], 3)


def test_rolling_avg_rejects_non_positive_window() -> None:
    with pytest.raises(CalculationError):
        rolling_avg([1, 2, 3], 0)


def test_format_currency_value_inr_crore_uses_rupee_symbol_and_cr_suffix() -> None:
    assert format_currency_value(20500.0, "INR_CRORE") == "₹20,500.00 Cr"


def test_format_currency_value_never_uses_scientific_notation_for_large_inr_crore() -> None:
    """A real, previously-visible bug (:g's default 6-significant-digit
    cutoff): a large-cap's total_revenue in INR_CRORE used to render as
    "1.07568e+06 INR_CRORE" instead of a readable figure."""
    formatted = format_currency_value(1_075_675.0, "INR_CRORE")
    assert formatted == "₹1,075,675.00 Cr"
    assert "e+" not in formatted


def test_format_currency_value_inr_lakh() -> None:
    assert format_currency_value(1234.5, "INR_LAKH") == "₹1,234.50 L"


def test_format_currency_value_raw_inr_picks_crore_or_lakh_by_magnitude() -> None:
    assert format_currency_value(2_00_00_000, "INR") == "₹2.00 Cr"
    assert format_currency_value(5_00_000, "INR") == "₹5.00 L"
    assert format_currency_value(50_000, "INR") == "₹50,000.00"


def test_format_currency_value_usd_units() -> None:
    assert format_currency_value(707.92, "USD_MILLION") == "$707.92 M"
    assert format_currency_value(1234.5, "USD_THOUSAND") == "$1,234.50 K"
    assert format_currency_value(99.5, "USD") == "$99.50"


def test_format_currency_value_percent_and_unknown_unit() -> None:
    assert format_currency_value(17.8, "PERCENT") == "17.80%"
    assert format_currency_value(42, "NUMBER") == "42 NUMBER"


# ------------------------------------------------------------------
# DB-aware wrappers — against a real ingested (synthetic) file.
# ------------------------------------------------------------------


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_cagr_for_metric_cites_its_inputs(ingested_conn: sqlite3.Connection) -> None:
    result = cagr_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2023", "FY2024")
    assert result.value == pytest.approx((20500 / 17000 - 1) * 100)
    assert result.unit == "PERCENT"
    assert result.kind == "CALCULATION"
    assert "FY2023-FY2024" in result.explanation
    assert "net_profit" in result.explanation


def test_yoy_growth_for_metric(ingested_conn: sqlite3.Connection) -> None:
    result = yoy_growth_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2024")
    assert result.value == pytest.approx((20500 - 17000) / 17000 * 100)


def test_yoy_growth_for_metric_missing_prior_year_raises(ingested_conn: sqlite3.Connection) -> None:
    with pytest.raises(MissingDataError):
        yoy_growth_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2023")  # no FY2022 data


def test_qoq_growth_for_metric_crosses_fiscal_year_boundary(ingested_conn: sqlite3.Connection) -> None:
    # Q1 FY2024's previous quarter is Q4 FY2023, which the fixture doesn't have.
    with pytest.raises(MissingDataError):
        qoq_growth_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2024", "Q1")


def test_qoq_growth_for_metric_within_same_fiscal_year(ingested_conn: sqlite3.Connection) -> None:
    result = qoq_growth_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2024", "Q4")
    assert result.value == pytest.approx((5000 - 4800) / 4800 * 100)


def test_cagr_for_metric_missing_data_raises(ingested_conn: sqlite3.Connection) -> None:
    with pytest.raises(MissingDataError):
        cagr_for_metric(ingested_conn, "HDFCBANK", "net_profit", "FY2019", "FY2024")
