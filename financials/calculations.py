"""Deterministic growth calculations: CAGR, YoY, QoQ, rolling average.

The LLM never performs a calculation Python can do deterministically (README:
Deterministic Calculation Layer). Every public function here is pure math,
unit-tested against fixture values; the *_for_metric wrappers below them fetch
from canonical_financials via storage/repositories.py and return a typed
CalculationResult that carries its own citation, e.g. "CAGR = 4.2%, calculated
from FY2023-FY2026 reported revenue" (README's worked example).
"""

from __future__ import annotations

from storage.db_types import DBConnection
from dataclasses import dataclass

from normalization.periods import fiscal_year_number, previous_quarter
from storage.repositories import get_canonical_value


class CalculationError(ValueError):
    """Base class for calculation failures — bad math inputs or missing data."""


class MissingDataError(CalculationError):
    """Raised when canonical_financials has no value for a required (metric, period)."""


@dataclass(frozen=True)
class CalculationResult:
    """A computed or vendor-reported figure, with its own citation text.

    kind is "CALCULATION" for anything derived here, or "FACT" for a
    vendor-reported pass-through (financials/ratios.py uses the latter) —
    the same two labels README's Evidence & Citations section uses.
    """

    label: str
    value: float
    unit: str
    explanation: str
    kind: str = "CALCULATION"


# ------------------------------------------------------------------
# Pure functions — no DB, no citations, just the math.
# ------------------------------------------------------------------


def cagr(begin_value: float, end_value: float, num_periods: float) -> float:
    """Compound annual growth rate, as a percent (12.4 means 12.4%)."""
    if num_periods <= 0:
        raise CalculationError(f"num_periods must be positive, got {num_periods!r}")
    if begin_value <= 0:
        raise CalculationError(f"CAGR requires a positive beginning value, got {begin_value!r}")
    return ((end_value / begin_value) ** (1 / num_periods) - 1) * 100


def yoy_growth(current: float, previous: float) -> float:
    """Year-over-year growth, as a percent."""
    if previous == 0:
        raise CalculationError("yoy_growth: previous value is zero — growth rate undefined")
    return (current - previous) / previous * 100


def qoq_growth(current: float, previous: float) -> float:
    """Quarter-over-quarter growth, as a percent. Same formula as yoy_growth,
    kept as a distinct function because the caller's period semantics differ."""
    if previous == 0:
        raise CalculationError("qoq_growth: previous value is zero — growth rate undefined")
    return (current - previous) / previous * 100


def rolling_avg(values: list[float], window: int) -> list[float]:
    """Simple moving average with the given window size, oldest to newest."""
    if window <= 0:
        raise CalculationError(f"window must be positive, got {window!r}")
    if len(values) < window:
        raise CalculationError(f"need at least {window} values, got {len(values)}")
    return [sum(values[i : i + window]) / window for i in range(len(values) - window + 1)]


# ------------------------------------------------------------------
# DB-aware wrappers — fetch from canonical_financials, cite the inputs.
# ------------------------------------------------------------------


def cagr_for_metric(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    start_fiscal_year: str,
    end_fiscal_year: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> CalculationResult:
    start_row = get_canonical_value(conn, company_id, metric_key, period_type, start_fiscal_year, None, statement_type)
    end_row = get_canonical_value(conn, company_id, metric_key, period_type, end_fiscal_year, None, statement_type)
    if start_row is None or end_row is None:
        missing = start_fiscal_year if start_row is None else end_fiscal_year
        raise MissingDataError(f"No canonical value for {company_id} {metric_key} {missing}")

    num_periods = fiscal_year_number(end_fiscal_year) - fiscal_year_number(start_fiscal_year)
    value = cagr(start_row["canonical_value"], end_row["canonical_value"], num_periods)
    return CalculationResult(
        label=f"{metric_key} CAGR ({start_fiscal_year}-{end_fiscal_year})",
        value=value,
        unit="PERCENT",
        explanation=(
            f"CAGR = {value:.1f}%, calculated from {start_fiscal_year}-{end_fiscal_year} "
            f"reported {metric_key} ({start_row['canonical_value']:g} -> {end_row['canonical_value']:g} "
            f"{start_row['unit']})"
        ),
    )


def yoy_growth_for_metric(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> CalculationResult:
    previous_fiscal_year = f"FY{fiscal_year_number(fiscal_year) - 1}"
    current_row = get_canonical_value(conn, company_id, metric_key, period_type, fiscal_year, None, statement_type)
    previous_row = get_canonical_value(conn, company_id, metric_key, period_type, previous_fiscal_year, None, statement_type)
    if current_row is None or previous_row is None:
        missing = fiscal_year if current_row is None else previous_fiscal_year
        raise MissingDataError(f"No canonical value for {company_id} {metric_key} {missing}")

    value = yoy_growth(current_row["canonical_value"], previous_row["canonical_value"])
    return CalculationResult(
        label=f"{metric_key} YoY growth ({fiscal_year})",
        value=value,
        unit="PERCENT",
        explanation=(
            f"YoY growth = {value:.1f}%, calculated from {previous_fiscal_year}-{fiscal_year} "
            f"reported {metric_key} ({previous_row['canonical_value']:g} -> {current_row['canonical_value']:g} "
            f"{current_row['unit']})"
        ),
    )


def qoq_growth_for_metric(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    quarter: str,
    statement_type: str | None = "consolidated",
) -> CalculationResult:
    previous_fy, previous_q = previous_quarter(fiscal_year, quarter)
    current_row = get_canonical_value(conn, company_id, metric_key, "quarterly", fiscal_year, quarter, statement_type)
    previous_row = get_canonical_value(conn, company_id, metric_key, "quarterly", previous_fy, previous_q, statement_type)
    if current_row is None or previous_row is None:
        missing = f"{fiscal_year}{quarter}" if current_row is None else f"{previous_fy}{previous_q}"
        raise MissingDataError(f"No canonical value for {company_id} {metric_key} {missing}")

    value = qoq_growth(current_row["canonical_value"], previous_row["canonical_value"])
    return CalculationResult(
        label=f"{metric_key} QoQ growth ({fiscal_year}{quarter})",
        value=value,
        unit="PERCENT",
        explanation=(
            f"QoQ growth = {value:.1f}%, calculated from {previous_fy}{previous_q}-{fiscal_year}{quarter} "
            f"reported {metric_key} ({previous_row['canonical_value']:g} -> {current_row['canonical_value']:g} "
            f"{current_row['unit']})"
        ),
    )
