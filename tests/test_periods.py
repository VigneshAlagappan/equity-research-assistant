from __future__ import annotations

import datetime as dt

import pytest

from normalization.periods import (
    PeriodParseError,
    fiscal_year_and_quarter_from_date,
    fiscal_year_number,
    parse_period_header,
    previous_quarter,
)


@pytest.mark.parametrize(
    "header,expected_fy",
    [
        ("Mar-24", "FY2024"),
        ("Mar-14", "FY2014"),
        ("Mar 24", "FY2024"),
        ("Mar-2024", "FY2024"),
    ],
)
def test_annual_header_parses_fiscal_year(header: str, expected_fy: str) -> None:
    fiscal_year, quarter = parse_period_header(header, "annual")
    assert fiscal_year == expected_fy
    assert quarter is None


@pytest.mark.parametrize(
    "header,expected_fy,expected_quarter",
    [
        ("Apr-23", "FY2024", "Q1"),
        ("Jun-23", "FY2024", "Q1"),
        ("Jul-23", "FY2024", "Q2"),
        ("Sep-23", "FY2024", "Q2"),
        ("Oct-23", "FY2024", "Q3"),
        ("Dec-23", "FY2024", "Q3"),
        ("Jan-24", "FY2024", "Q4"),
        ("Mar-24", "FY2024", "Q4"),
    ],
)
def test_quarterly_header_maps_indian_fiscal_quarters(
    header: str, expected_fy: str, expected_quarter: str
) -> None:
    fiscal_year, quarter = parse_period_header(header, "quarterly")
    assert fiscal_year == expected_fy
    assert quarter == expected_quarter


def test_non_period_header_raises() -> None:
    with pytest.raises(PeriodParseError):
        parse_period_header("TTM", "annual")


def test_blank_header_raises() -> None:
    with pytest.raises(PeriodParseError):
        parse_period_header("", "annual")


def test_invalid_period_type_raises() -> None:
    with pytest.raises(ValueError):
        parse_period_header("Mar-24", "bogus")


def test_fiscal_year_number() -> None:
    assert fiscal_year_number("FY2024") == 2024


@pytest.mark.parametrize("bad_fy", ["2024", "FY24", "FY202A", ""])
def test_fiscal_year_number_rejects_malformed_input(bad_fy: str) -> None:
    with pytest.raises(PeriodParseError):
        fiscal_year_number(bad_fy)


@pytest.mark.parametrize(
    "fiscal_year,quarter,expected",
    [
        ("FY2024", "Q4", ("FY2024", "Q3")),
        ("FY2024", "Q3", ("FY2024", "Q2")),
        ("FY2024", "Q2", ("FY2024", "Q1")),
        ("FY2024", "Q1", ("FY2023", "Q4")),  # crosses the fiscal-year boundary
    ],
)
def test_previous_quarter(fiscal_year: str, quarter: str, expected: tuple[str, str]) -> None:
    assert previous_quarter(fiscal_year, quarter) == expected


def test_previous_quarter_rejects_invalid_quarter() -> None:
    with pytest.raises(ValueError):
        previous_quarter("FY2024", "Q5")


# ------------------------------------------------------------------
# Per-company fiscal_year_end_month (US calendar-year default = 12, and a
# couple of other fiscal-year-end conventions) — the default (3, India's
# Apr-Mar) is exercised by every test above without passing the param at all.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected_fy,expected_quarter",
    [
        ("Jan-24", "FY2024", "Q1"),
        ("Mar-24", "FY2024", "Q1"),
        ("Apr-24", "FY2024", "Q2"),
        ("Jun-24", "FY2024", "Q2"),
        ("Jul-24", "FY2024", "Q3"),
        ("Sep-24", "FY2024", "Q3"),
        ("Oct-24", "FY2024", "Q4"),
        ("Dec-24", "FY2024", "Q4"),
    ],
)
def test_calendar_year_fiscal_year_end_month_maps_quarters(
    header: str, expected_fy: str, expected_quarter: str
) -> None:
    fiscal_year, quarter = parse_period_header(header, "quarterly", fiscal_year_end_month=12)
    assert fiscal_year == expected_fy
    assert quarter == expected_quarter


def test_calendar_year_annual_header_parses_fiscal_year() -> None:
    fiscal_year, quarter = parse_period_header("Dec-24", "annual", fiscal_year_end_month=12)
    assert fiscal_year == "FY2024"
    assert quarter is None


def test_default_fiscal_year_end_month_is_march_unchanged() -> None:
    """fiscal_year_end_month defaults to 3 — every existing India caller's
    behavior is unaffected by adding the parameter."""
    assert parse_period_header("Mar-24", "annual") == parse_period_header(
        "Mar-24", "annual", fiscal_year_end_month=3
    )


def test_fiscal_year_and_quarter_from_date_respects_fiscal_year_end_month() -> None:
    fiscal_year, quarter = fiscal_year_and_quarter_from_date(
        dt.date(2024, 9, 30), "quarterly", fiscal_year_end_month=12
    )
    assert fiscal_year == "FY2024"
    assert quarter == "Q3"


@pytest.mark.parametrize("bad_month", [0, 13, -1])
def test_invalid_fiscal_year_end_month_raises(bad_month: int) -> None:
    with pytest.raises(ValueError):
        parse_period_header("Mar-24", "annual", fiscal_year_end_month=bad_month)
