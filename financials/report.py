"""Assembles calculated data into the single-company text report.

Shared by the CLI (main.py analyze) and the local web viewer (web/app.py) —
one implementation, two presentations. Proves raw -> normalized -> derived
end-to-end (README: Implementation Sequence, step 3). TREND_METRICS and
VENDOR_RATIO_METRICS are also the metric catalog retrieval/structured_search.py
uses to gather evidence for the LLM research assistant (step 5) — the
assistant's evidence should cover the same numbers a human reading this
report would see, not a separately-curated list.
"""

from __future__ import annotations

import sqlite3

from companies.registry import get_company
from financials.calculations import CalculationError, cagr_for_metric, yoy_growth_for_metric
from financials.ratios import MissingDataError, SectorMismatchError, roa_for_company, roe_for_company, vendor_reported
from storage.repositories import get_canonical_series

TREND_METRICS = [
    ("net_profit", "Net Profit"),
    ("total_assets", "Total Assets"),
    ("advances", "Advances (Loans)"),
    ("deposits", "Deposits"),
]

VENDOR_RATIO_METRICS = [
    ("gross_npa_percent", "Gross NPA %"),
    ("net_npa_percent", "Net NPA %"),
    ("casa_percent", "CASA %"),
    ("net_interest_margin", "Net Interest Margin"),
    ("return_on_equity_percent", "ROE % (vendor-reported)"),
    ("book_value", "Book Value"),
]


def _format_trend_section(
    conn: sqlite3.Connection, company_id: str, metric_key: str, title: str, statement_type: str
) -> list[str]:
    """One metric's full annual series, with YoY per year and CAGR end-to-end."""
    series = get_canonical_series(conn, company_id, metric_key, "annual", statement_type)
    if not series:
        return []

    lines = [f"-- {title} ({series[0]['unit']}) --"]
    for row in series:
        line = f"{row['fiscal_year']}: {row['canonical_value']:,.2f}"
        try:
            yoy = yoy_growth_for_metric(conn, company_id, metric_key, row["fiscal_year"], statement_type=statement_type)
            sign = "+" if yoy.value >= 0 else ""
            line += f"  (YoY: {sign}{yoy.value:.1f}%)"
        except CalculationError:
            pass  # no prior-year value to compare against — first year in the series
        lines.append(line)

    if len(series) >= 2:
        try:
            growth = cagr_for_metric(
                conn, company_id, metric_key, series[0]["fiscal_year"], series[-1]["fiscal_year"],
                statement_type=statement_type,
            )
            lines.append(
                f"CAGR {series[0]['fiscal_year']}-{series[-1]['fiscal_year']}: "
                f"{growth.value:+.1f}%  [{growth.kind}]"
            )
        except CalculationError:
            pass

    return lines


def _format_profitability_ratio_section(
    conn: sqlite3.Connection, company_id: str, fiscal_years: list[str], statement_type: str
) -> list[str]:
    """ROA/ROE per fiscal year, wherever a prior-year balance sheet figure makes the average computable."""
    lines: list[str] = []
    for fiscal_year in fiscal_years:
        for compute, label in ((roa_for_company, "ROA"), (roe_for_company, "ROE")):
            try:
                result = compute(conn, company_id, fiscal_year, statement_type=statement_type)
            # ValueError too: roa()/roe() raise it for a degenerate (<=0) denominator,
            # which real ingested data can produce (e.g. a genuine 0.0 total_assets for
            # early years some companies didn't actually report it for).
            except (MissingDataError, ValueError):
                continue
            lines.append(f"{label} {fiscal_year}: {result.value:.2f}%  [{result.kind}]")
    if lines:
        lines.insert(0, "-- Profitability Ratios (calculated) --")
    return lines


def _format_vendor_ratio_section(
    conn: sqlite3.Connection, company_id: str, latest_fiscal_year: str, statement_type: str
) -> list[str]:
    """Vendor-reported ratios (Gross NPA %, NIM, ...) for the latest year — labeled FACT, not computed."""
    lines: list[str] = []
    for metric_key, label in VENDOR_RATIO_METRICS:
        try:
            result = vendor_reported(conn, company_id, metric_key, latest_fiscal_year, statement_type=statement_type)
        except (MissingDataError, SectorMismatchError):
            continue
        lines.append(f"{label}: {result.explanation}  [{result.kind}]")
    if lines:
        lines.insert(0, f"-- Other Ratios, vendor-reported ({latest_fiscal_year}) --")
    return lines


def build_analysis_report(conn: sqlite3.Connection, company_id: str, statement_type: str = "consolidated") -> str:
    """Text report: annual trends, YoY/CAGR growth, ROA/ROE, and vendor-reported ratios."""
    company = get_company(conn, company_id)
    if company is None:
        raise MissingDataError(f"No company registered with company_id={company_id!r}")

    lines = [
        f"=== {company['display_name']} ({company['company_id']}) — Annual Overview ===",
        f"Sector: {company['sector'] or 'n/a'} | Industry: {company['industry'] or 'n/a'}",
        f"Statement type: {statement_type}",
        "",
    ]

    net_profit_fiscal_years = [
        row["fiscal_year"] for row in get_canonical_series(conn, company_id, "net_profit", "annual", statement_type)
    ]

    for metric_key, title in TREND_METRICS:
        section = _format_trend_section(conn, company_id, metric_key, title, statement_type)
        if section:
            lines.extend(section)
            lines.append("")

    ratio_section = _format_profitability_ratio_section(conn, company_id, net_profit_fiscal_years, statement_type)
    if ratio_section:
        lines.extend(ratio_section)
        lines.append("")

    if net_profit_fiscal_years:
        vendor_section = _format_vendor_ratio_section(conn, company_id, net_profit_fiscal_years[-1], statement_type)
        if vendor_section:
            lines.extend(vendor_section)
            lines.append("")

    if not net_profit_fiscal_years:
        lines.append(
            "No data ingested yet for this company. Run: "
            f"python main.py ingest data/raw/{company_id}/screener/<file>.xlsx"
        )

    return "\n".join(lines).rstrip() + "\n"
