from __future__ import annotations

from dataclasses import replace

import pytest

from ingestion.validation import validate_macro_observation, validate_observation
from sources.base import NormalizedObservation
from sources.macro import MacroNormalizedObservation

VALID = NormalizedObservation(
    company_id="HDFCBANK",
    metric_key="net_profit",
    period_type="annual",
    fiscal_year="FY2024",
    value=20500.0,
    unit="INR_CRORE",
    source="screener",
    source_file="HDFCBANK.xlsx",
    parser_version="screener-v1",
    statement_type="consolidated",
)


def test_valid_observation_has_no_problems() -> None:
    assert validate_observation(VALID) == []


def test_quarterly_without_quarter_is_flagged() -> None:
    obs = replace(VALID, period_type="quarterly", quarter=None)
    problems = validate_observation(obs)
    assert any("quarter" in p for p in problems)


def test_annual_with_quarter_is_flagged() -> None:
    obs = replace(VALID, period_type="annual", quarter="Q1")
    problems = validate_observation(obs)
    assert any("quarter" in p for p in problems)


@pytest.mark.parametrize("bad_fy", ["2024", "FY24", "FY202A", "FY20244", ""])
def test_malformed_fiscal_year_is_flagged(bad_fy: str) -> None:
    obs = replace(VALID, fiscal_year=bad_fy)
    problems = validate_observation(obs)
    assert any("fiscal_year" in p for p in problems)


def test_invalid_unit_is_flagged() -> None:
    obs = replace(VALID, unit="DOLLARS")
    problems = validate_observation(obs)
    assert any("unit" in p for p in problems)


def test_usd_billion_is_a_valid_unit() -> None:
    obs = replace(VALID, unit="USD_BILLION", currency="USD", source="yfinance")
    assert validate_observation(obs) == []


def test_invalid_statement_type_is_flagged() -> None:
    obs = replace(VALID, statement_type="restated")
    problems = validate_observation(obs)
    assert any("statement_type" in p for p in problems)


def test_non_finite_value_is_flagged() -> None:
    obs = replace(VALID, value=float("nan"))
    problems = validate_observation(obs)
    assert any("value" in p for p in problems)


def test_empty_company_id_is_flagged() -> None:
    obs = replace(VALID, company_id="")
    problems = validate_observation(obs)
    assert any("company_id" in p for p in problems)


VALID_MACRO = MacroNormalizedObservation(
    series_key="rainfall_index",
    period_type="annual",
    period="2015",
    value=1108.9,
    unit="MILLIMETRES",
    source="macro",
    source_file="rainfall_index.csv",
    parser_version="macro-v1-csv",
)


def test_valid_macro_observation_has_no_problems() -> None:
    assert validate_macro_observation(VALID_MACRO) == []


def test_valid_macro_observation_with_region_has_no_problems() -> None:
    obs = replace(VALID_MACRO, region="Maharashtra")
    assert validate_macro_observation(obs) == []


def test_macro_monthly_period_matches_monthly_type() -> None:
    obs = replace(VALID_MACRO, period_type="monthly", period="2015-06")
    assert validate_macro_observation(obs) == []


def test_macro_empty_series_key_is_flagged() -> None:
    obs = replace(VALID_MACRO, series_key="")
    problems = validate_macro_observation(obs)
    assert any("series_key" in p for p in problems)


def test_macro_invalid_period_type_is_flagged() -> None:
    obs = replace(VALID_MACRO, period_type="quarterly")
    problems = validate_macro_observation(obs)
    assert any("period_type" in p for p in problems)


def test_macro_period_mismatched_with_period_type_is_flagged() -> None:
    """period_type says annual but period looks monthly — a validation bug
    upstream (adapter and validator disagreeing) should never pass silently."""
    obs = replace(VALID_MACRO, period_type="annual", period="2015-06")
    problems = validate_macro_observation(obs)
    assert any("period" in p for p in problems)


def test_macro_malformed_period_is_flagged() -> None:
    obs = replace(VALID_MACRO, period="FY2015")  # fiscal-year style, not calendar
    problems = validate_macro_observation(obs)
    assert any("period" in p for p in problems)


def test_macro_empty_unit_is_flagged() -> None:
    obs = replace(VALID_MACRO, unit="")
    problems = validate_macro_observation(obs)
    assert any("unit" in p for p in problems)


def test_macro_non_finite_value_is_flagged() -> None:
    obs = replace(VALID_MACRO, value=float("inf"))
    problems = validate_macro_observation(obs)
    assert any("value" in p for p in problems)
