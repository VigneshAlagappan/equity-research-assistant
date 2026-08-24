"""Financial ratios: ROA, ROE, NIM, GNPA ratio, margins.

Sector-aware, raises a clear error if required inputs are missing rather than
guessing (README: Deterministic Calculation Layer). "Sector-aware" means: a
metric tagged to a sector in metrics_dictionary.applicable_sectors (e.g.
interest_earned -> ["bank"]) refuses to resolve for a company outside that
sector, instead of silently returning nothing or a wrong number.
"""

from __future__ import annotations

import json
import sqlite3

from companies.registry import get_company
from financials.calculations import CalculationResult, MissingDataError
from normalization.periods import fiscal_year_number
from storage.repositories import get_canonical_value


class SectorMismatchError(ValueError):
    """Raised when a sector-specific metric is requested for a company outside that sector."""


# ------------------------------------------------------------------
# Pure functions — no DB, ratio math only.
# ------------------------------------------------------------------


def roa(net_profit: float, avg_total_assets: float) -> float:
    """Return on Assets, as a percent. avg_total_assets is the average of the
    period's opening and closing total assets (the conventional ROA denominator)."""
    if avg_total_assets <= 0:
        raise ValueError(f"avg_total_assets must be positive, got {avg_total_assets!r}")
    return net_profit / avg_total_assets * 100


def roe(net_profit: float, avg_shareholders_funds: float) -> float:
    """Return on Equity, as a percent."""
    if avg_shareholders_funds <= 0:
        raise ValueError(f"avg_shareholders_funds must be positive, got {avg_shareholders_funds!r}")
    return net_profit / avg_shareholders_funds * 100


def nim(net_interest_income: float, avg_earning_assets: float) -> float:
    """Net Interest Margin, as a percent."""
    if avg_earning_assets <= 0:
        raise ValueError(f"avg_earning_assets must be positive, got {avg_earning_assets!r}")
    return net_interest_income / avg_earning_assets * 100


def gnpa_ratio(gross_npa_amount: float, gross_advances: float) -> float:
    """Gross NPA ratio, as a percent."""
    if gross_advances <= 0:
        raise ValueError(f"gross_advances must be positive, got {gross_advances!r}")
    return gross_npa_amount / gross_advances * 100


def net_profit_margin(net_profit: float, total_income: float) -> float:
    """Net profit margin, as a percent."""
    if total_income <= 0:
        raise ValueError(f"total_income must be positive, got {total_income!r}")
    return net_profit / total_income * 100


# ------------------------------------------------------------------
# Sector-aware fetch + DB-backed wrappers.
# ------------------------------------------------------------------


def _company_sector_tags(company_row: sqlite3.Row) -> set[str]:
    """Derive metrics_dictionary-style sector tags from a company's industry text.

    A POC-level heuristic (companies has free-text sector/industry, not a tag
    column): "bank" / "nbfc" if the industry text mentions them. Extend this
    as new sectors/tags are needed — it's the one place that mapping lives.
    """
    industry = (company_row["industry"] or "").lower()
    tags = set()
    if "bank" in industry:
        tags.add("bank")
    if "nbfc" in industry or "non-banking" in industry or "non banking" in industry:
        tags.add("nbfc")
    return tags


def get_required_metric(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    period_type: str = "annual",
    quarter: str | None = None,
    statement_type: str | None = "consolidated",
) -> sqlite3.Row:
    """Fetch one canonical value, refusing sector-inapplicable metrics and
    missing data with a clear error rather than guessing."""
    company = get_company(conn, company_id)
    if company is None:
        raise MissingDataError(f"No company registered with company_id={company_id!r}")

    metric_row = conn.execute(
        "SELECT applicable_sectors FROM metrics_dictionary WHERE metric_key = ?", (metric_key,)
    ).fetchone()
    if metric_row is None:
        raise MissingDataError(f"Unknown metric_key: {metric_key!r}")

    if metric_row["applicable_sectors"]:
        allowed = set(json.loads(metric_row["applicable_sectors"]))
        if not allowed & _company_sector_tags(company):
            raise SectorMismatchError(
                f"{metric_key!r} applies to {sorted(allowed)} companies; "
                f"{company_id} (industry={company['industry']!r}) is not one of them"
            )

    canonical = get_canonical_value(conn, company_id, metric_key, period_type, fiscal_year, quarter, statement_type)
    if canonical is None:
        period_label = f"{fiscal_year}{quarter or ''}"
        raise MissingDataError(f"No canonical value for {company_id} {metric_key} {period_label}")
    return canonical


def roa_for_company(
    conn: sqlite3.Connection, company_id: str, fiscal_year: str, statement_type: str | None = "consolidated"
) -> CalculationResult:
    prior_fy = f"FY{fiscal_year_number(fiscal_year) - 1}"
    net_profit = get_required_metric(conn, company_id, "net_profit", fiscal_year, statement_type=statement_type)
    assets_end = get_required_metric(conn, company_id, "total_assets", fiscal_year, statement_type=statement_type)
    assets_begin = get_required_metric(conn, company_id, "total_assets", prior_fy, statement_type=statement_type)

    avg_assets = (assets_end["canonical_value"] + assets_begin["canonical_value"]) / 2
    value = roa(net_profit["canonical_value"], avg_assets)
    return CalculationResult(
        label=f"ROA ({fiscal_year})",
        value=value,
        unit="PERCENT",
        explanation=(
            f"ROA = {value:.2f}%, calculated as net_profit {fiscal_year} "
            f"({net_profit['canonical_value']:g} {net_profit['unit']}) / average total_assets "
            f"({prior_fy}-{fiscal_year}: {avg_assets:g} {assets_end['unit']})"
        ),
    )


def _shareholders_funds(
    conn: sqlite3.Connection, company_id: str, fiscal_year: str, statement_type: str | None
) -> tuple[float, str, bool]:
    """total_shareholders_funds if reported directly, else the exact identity
    equity_share_capital + reserves. Not an estimate — every balance sheet
    ties out this way; some Screener exports simply don't carry the combined
    line (verified: Jio Financial Services' NBFC-template export reports
    Equity Share Capital and Reserves separately with no Total Shareholders
    Funds row at all). Returns (value, unit, was_derived).
    """
    try:
        row = get_required_metric(
            conn, company_id, "total_shareholders_funds", fiscal_year, statement_type=statement_type
        )
        return row["canonical_value"], row["unit"], False
    except MissingDataError:
        pass

    equity_share_capital = get_required_metric(
        conn, company_id, "equity_share_capital", fiscal_year, statement_type=statement_type
    )
    reserves = get_required_metric(conn, company_id, "reserves", fiscal_year, statement_type=statement_type)
    return equity_share_capital["canonical_value"] + reserves["canonical_value"], equity_share_capital["unit"], True


def roe_for_company(
    conn: sqlite3.Connection, company_id: str, fiscal_year: str, statement_type: str | None = "consolidated"
) -> CalculationResult:
    prior_fy = f"FY{fiscal_year_number(fiscal_year) - 1}"
    net_profit = get_required_metric(conn, company_id, "net_profit", fiscal_year, statement_type=statement_type)
    equity_end, unit, end_derived = _shareholders_funds(conn, company_id, fiscal_year, statement_type)
    equity_begin, _, begin_derived = _shareholders_funds(conn, company_id, prior_fy, statement_type)

    avg_equity = (equity_end + equity_begin) / 2
    value = roe(net_profit["canonical_value"], avg_equity)
    derived_note = " (derived as equity_share_capital + reserves)" if (end_derived or begin_derived) else ""
    return CalculationResult(
        label=f"ROE ({fiscal_year})",
        value=value,
        unit="PERCENT",
        explanation=(
            f"ROE = {value:.2f}%, calculated as net_profit {fiscal_year} "
            f"({net_profit['canonical_value']:g} {net_profit['unit']}) / average total_shareholders_funds "
            f"({prior_fy}-{fiscal_year}: {avg_equity:g} {unit}){derived_note}"
        ),
    )


def vendor_reported(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    period_type: str = "annual",
    quarter: str | None = None,
    statement_type: str | None = "consolidated",
) -> CalculationResult:
    """Wrap a vendor-reported canonical value (e.g. Gross NPA %) as a FACT,
    not a CALCULATION — nothing is derived here, this is a cited pass-through."""
    row = get_required_metric(conn, company_id, metric_key, fiscal_year, period_type, quarter, statement_type)
    period_label = f"{fiscal_year}{quarter or ''}"
    value_str = f"{row['canonical_value']:g}%" if row["unit"] == "PERCENT" else f"{row['canonical_value']:g} {row['unit']}"
    return CalculationResult(
        label=f"{metric_key} ({period_label})",
        value=row["canonical_value"],
        unit=row["unit"],
        explanation=f"{value_str}, as reported for {period_label} ({row['reconciliation_reason']})",
        kind="FACT",
    )
