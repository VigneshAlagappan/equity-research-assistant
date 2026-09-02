"""Structured-data retrieval for the research assistant.

Retrieval never calls the LLM (README: Retrieval Architecture) — this module
only reads canonical_financials via financials/calculations.py and
financials/ratios.py, the same functions financials/report.py's text report
and charts/financial_charts.py's charts are built from, and returns typed
Evidence. The assistant's prompt is grounded in exactly the numbers a human
reading `analyze` would see — never the full company archive.
"""

from __future__ import annotations

from storage.db_types import DBConnection

from financials.calculations import CalculationError, CalculationResult, cagr_for_metric, yoy_growth_for_metric
from financials.ratios import MissingDataError, SectorMismatchError, roa_for_company, roe_for_company, vendor_reported
from financials.report import TREND_METRICS, VENDOR_RATIO_METRICS
from research.evidence import Evidence
from research.temporal import fiscal_year_visible
from storage.fact_store import FactStore, default_fact_store


def _format_value(value: float, unit: str) -> str:
    return f"{value:.2f}%" if unit == "PERCENT" else f"{value:,.2f} {unit}"


def _result_to_evidence(company_id: str, result: CalculationResult) -> Evidence:
    return Evidence(
        kind=result.kind,  # already "FACT" or "CALCULATION" (financials/ratios.py, calculations.py)
        company_id=company_id,
        label=result.label,
        value=_format_value(result.value, result.unit),
        citation=result.explanation,
    )


def _company_fiscal_year_end(conn: DBConnection, company_id: str, fact_store: FactStore) -> str | None:
    """`companies.fiscal_year_end` (MM-DD) if this company has one on record —
    research/temporal.py falls back to the Indian 31-March default otherwise.
    Only looked up when a cutoff is actually in force."""
    company = fact_store.get_company(conn, company_id)
    if company is None:
        return None
    try:
        return company["fiscal_year_end"]
    except (KeyError, IndexError, TypeError):
        return None


def get_company_evidence(
    conn: DBConnection, company_id: str, statement_type: str | None = "consolidated",
    *, fact_store: FactStore | None = None, as_of: str | None = None,
) -> list[Evidence]:
    """Gather deterministic Evidence for one company: reported metric trends
    (as FACT lines) plus their YoY/CAGR growth, ROA/ROE, and vendor-reported
    ratios (as CALCULATION/FACT lines) — the same data financials/report.py's
    text report is built from. Returns [] if nothing has been ingested yet.

    `as_of` (ISO date) restricts every series to fiscal periods that had
    already ENDED by that date — research/temporal.py — so a historical
    "could this have been detected at the time?" investigation never sees a
    later year's numbers. The derived YoY/CAGR/ROA/ROE lines are computed
    from the truncated series, not truncated afterwards, so the "latest year"
    they describe is the latest year visible at the cutoff.
    """
    fs = fact_store or default_fact_store()
    evidence: list[Evidence] = []
    fiscal_year_end = _company_fiscal_year_end(conn, company_id, fs) if as_of else None

    def _visible(rows: list) -> list:
        if not as_of:
            return rows
        return [
            r for r in rows
            if fiscal_year_visible(r["fiscal_year"], as_of, fiscal_year_end=fiscal_year_end)
        ]

    net_profit_fiscal_years = [
        row["fiscal_year"]
        for row in _visible(fs.get_canonical_series(conn, company_id, "net_profit", "annual", statement_type))
    ]

    for metric_key, title in TREND_METRICS:
        series = _visible(fs.get_canonical_series(conn, company_id, metric_key, "annual", statement_type))
        for row in series:
            evidence.append(
                Evidence(
                    kind="FACT",
                    company_id=company_id,
                    label=f"{title} {row['fiscal_year']}",
                    value=_format_value(row["canonical_value"], row["unit"]),
                    citation=f"reported for {row['fiscal_year']} ({row['reconciliation_reason']})",
                )
            )
        if not series:
            continue
        try:
            yoy = yoy_growth_for_metric(
                conn, company_id, metric_key, series[-1]["fiscal_year"], statement_type=statement_type
            )
            evidence.append(_result_to_evidence(company_id, yoy))
        except CalculationError:
            pass
        if len(series) >= 2:
            try:
                growth = cagr_for_metric(
                    conn, company_id, metric_key, series[0]["fiscal_year"], series[-1]["fiscal_year"],
                    statement_type=statement_type,
                )
                evidence.append(_result_to_evidence(company_id, growth))
            except CalculationError:
                pass

    for fiscal_year in net_profit_fiscal_years:
        for compute in (roa_for_company, roe_for_company):
            try:
                result = compute(conn, company_id, fiscal_year, statement_type=statement_type)
            # ValueError alongside MissingDataError: roa()/roe() (financials/ratios.py)
            # raise a bare ValueError for a degenerate (<=0) denominator, which happens
            # for real ingested data — e.g. ICICIBANK's total_assets is reported as a
            # genuine 0.0 for several early years rather than left unreported. Same
            # pattern web/valuation_feed.py's ratio_series() already applies.
            except (MissingDataError, ValueError):
                continue
            evidence.append(_result_to_evidence(company_id, result))

    if net_profit_fiscal_years:
        latest_fiscal_year = net_profit_fiscal_years[-1]
        for metric_key, _label in VENDOR_RATIO_METRICS:
            try:
                result = vendor_reported(conn, company_id, metric_key, latest_fiscal_year, statement_type=statement_type)
            except (MissingDataError, SectorMismatchError):
                continue
            evidence.append(_result_to_evidence(company_id, result))

    return evidence


def get_comparison_evidence(
    conn: DBConnection, company_ids: list[str], statement_type: str | None = "consolidated"
) -> list[Evidence]:
    """Evidence for multiple companies, concatenated — for peer-comparison
    questions (README: POC Success Criteria, Question 2). A company with
    nothing ingested simply contributes no evidence, rather than erroring."""
    evidence: list[Evidence] = []
    for company_id in company_ids:
        evidence.extend(get_company_evidence(conn, company_id, statement_type))
    return evidence
