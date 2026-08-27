"""Analytics (Tools tab) — deterministic, out-of-the-box pattern detection.
No LLM call: every pattern here is a plain calculation over
canonical_financials, same functions financials/report.py's own text report
already calls (financials/calculations.py::yoy_growth_for_metric). System-
computed on every page view, not user-triggered and not persisted — same
"retrieval is cheap and deterministic, redo it every time" philosophy the
rest of this app already follows (context/reuse.py, retrieval/*.py).

V1 ships exactly one pattern type: a significant year-over-year move in one
of financials/report.py's TREND_METRICS, for the latest fiscal year each
company with ingested financials has. More pattern types (sector-peer
outliers, macro/company correlation) are natural follow-ups, not built here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from financials.calculations import CalculationError, yoy_growth_for_metric
from financials.report import TREND_METRICS
from storage.fact_store import FactStore, default_fact_store

DEFAULT_YOY_THRESHOLD_PERCENT = 25.0


@dataclass(frozen=True)
class Pattern:
    company_id: str
    metric_key: str
    metric_label: str
    fiscal_year: str
    yoy_percent: float


def detect_yoy_spikes(
    conn: sqlite3.Connection, *, threshold_percent: float = DEFAULT_YOY_THRESHOLD_PERCENT,
    statement_type: str | None = "consolidated", fact_store: FactStore | None = None,
) -> list[Pattern]:
    """Every (company, TREND_METRICS metric) whose latest-fiscal-year YoY
    move is at least `threshold_percent` in magnitude, sorted by magnitude
    descending. Skips a company/metric pair silently (not an error) if
    there's no prior-year value to compare against — same "absence isn't an
    error" rule the rest of this app's retrieval already follows."""
    fs = fact_store or default_fact_store()
    patterns: list[Pattern] = []
    for company_id in fs.list_company_ids_with_financial_data(conn):
        for metric_key, metric_label in TREND_METRICS:
            series = fs.get_canonical_series(conn, company_id, metric_key, "annual", statement_type)
            if not series:
                continue
            latest_fiscal_year = series[-1]["fiscal_year"]
            try:
                result = yoy_growth_for_metric(
                    conn, company_id, metric_key, latest_fiscal_year, statement_type=statement_type,
                )
            except CalculationError:
                continue  # no prior-year value, or a zero-denominator edge case
            if abs(result.value) >= threshold_percent:
                patterns.append(
                    Pattern(
                        company_id=company_id, metric_key=metric_key, metric_label=metric_label,
                        fiscal_year=latest_fiscal_year, yoy_percent=result.value,
                    )
                )
    patterns.sort(key=lambda p: abs(p.yoy_percent), reverse=True)
    return patterns
