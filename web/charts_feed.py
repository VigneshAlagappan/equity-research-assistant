"""Builds the Charts-tab overlay feed (see web/static/js/charts_overlay.js).

Same underlying metric vocabulary and derivation logic as web/valuation_feed.py
(the Financials/Valuation Model feed), generalized from "one value per fiscal
year" to "one value per period", where a period is either a fiscal year
(period_type="annual") or a fiscal-year+quarter (period_type="quarterly") —
this is a separate feed/module rather than a period_type param bolted onto
build_valuation_feed() because two of its ratio rows (ROE, ROA) are genuinely
annual-only: financials/ratios.py's roe_for_company/roa_for_company average a
metric across two consecutive *annual* balance-sheet snapshots (this FY vs.
FY-1), and there's no established, non-arbitrary way to redefine that for a
single quarter (average of 4 quarters back? annualize the quarter's net
profit? both are real methodology choices, not something to silently guess
at in a finance tool). Every other row is period-agnostic arithmetic
(division/addition of two same-period raw figures), so it works unchanged for
quarterly — ROE/ROA/Net-Profit-to-Assets are the only three omitted when
period_type="quarterly" (left out of METRICS entirely rather than showing an
all-null row).
"""

from __future__ import annotations

import sqlite3

from companies.registry import get_company
from financials.ratios import MissingDataError, SectorMismatchError, roa_for_company, roe_for_company
from storage.repositories import get_canonical_series

_CRORE_PER_LAKH = 0.01
_QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}

_RAW_METRIC_KEYS = (
    "net_profit", "total_assets", "total_revenue", "other_income", "interest_expended",
    "tax", "profit_before_tax", "operating_expenses", "depreciation",
    "equity_share_capital", "reserves", "borrowings", "investments",
    "deposits", "advances", "eps", "book_value", "dividend_per_share", "sales_per_share",
    "shares_outstanding", "total_shareholders_funds",
)


def _period_key(fiscal_year: str, quarter: str | None) -> tuple[int, int]:
    return (int(fiscal_year.removeprefix("FY")), _QUARTER_ORDER.get(quarter, 0))


def _period_label(fiscal_year: str, quarter: str | None) -> str:
    return f"{quarter} {fiscal_year}" if quarter else fiscal_year


def _series_by_period(
    conn: sqlite3.Connection, company_id: str, metric_key: str, period_type: str, statement_type: str
) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    for row in get_canonical_series(conn, company_id, metric_key, period_type, statement_type):
        value = row["canonical_value"]
        if row["unit"] == "INR_LAKH":
            value *= _CRORE_PER_LAKH
        out[_period_key(row["fiscal_year"], row["quarter"])] = value
    return out


def _values_for(period_keys: list[tuple[int, int]], series: dict[tuple[int, int], float]) -> list[float | None]:
    return [series.get(pk) for pk in period_keys]


def _row(key: str, label: str, unit: str, period_keys: list[tuple[int, int]], series: dict[tuple[int, int], float]) -> dict:
    return {"key": key, "label": label, "unit": unit, "values": _values_for(period_keys, series)}


def build_charts_feed(
    conn: sqlite3.Connection, company_id: str, statement_type: str = "consolidated", period_type: str = "annual"
) -> dict:
    raw = {key: _series_by_period(conn, company_id, key, period_type, statement_type) for key in _RAW_METRIC_KEYS}
    period_keys = sorted({pk for series in raw.values() for pk in series})
    # fiscal_year/quarter text per period_key, for ROE/ROA lookups (annual
    # only) and for building the display label — reconstructed directly from
    # the sorted key rather than threading the original strings through
    # every series above.
    fy_by_key: dict[tuple[int, int], str] = {}
    quarter_by_key: dict[tuple[int, int], str | None] = {}
    _REVERSE_QUARTER = {v: k for k, v in _QUARTER_ORDER.items()}
    for pk in period_keys:
        year_num, q_num = pk
        fy_by_key[pk] = f"FY{year_num}"
        quarter_by_key[pk] = _REVERSE_QUARTER.get(q_num)

    def ratio_series(fn, unit_scale: float = 1.0) -> dict[tuple[int, int], float]:
        if period_type != "annual":
            return {}
        out = {}
        for pk in period_keys:
            try:
                result = fn(conn, company_id, fy_by_key[pk], statement_type=statement_type)
            except (MissingDataError, SectorMismatchError, ZeroDivisionError, ValueError):
                continue
            out[pk] = result.value * unit_scale
        return out

    def divide(numerator: str, denominator: str) -> dict[tuple[int, int], float]:
        num, den = raw[numerator], raw[denominator]
        return {pk: num[pk] / den[pk] for pk in period_keys if den.get(pk) and pk in num}

    def add(a: str, b: str) -> dict[tuple[int, int], float]:
        va, vb = raw[a], raw[b]
        return {pk: va[pk] + vb[pk] for pk in period_keys if pk in va and pk in vb}

    def fill_missing(primary: dict[tuple[int, int], float], fallback: dict[tuple[int, int], float]) -> dict[tuple[int, int], float]:
        return {**fallback, **primary}

    networth = raw["reserves"]
    she = fill_missing(raw["total_shareholders_funds"], add("equity_share_capital", "reserves"))
    eps_series = fill_missing(raw["eps"], divide("net_profit", "shares_outstanding"))
    book_value_series = fill_missing(
        raw["book_value"],
        {
            pk: she[pk] / raw["shares_outstanding"][pk]
            for pk in period_keys
            if raw["shares_outstanding"].get(pk) and pk in she
        },
    )
    sales_per_share_series = fill_missing(raw["sales_per_share"], divide("total_revenue", "shares_outstanding"))
    net_margin = divide("net_profit", "total_revenue")
    tax_rate = divide("tax", "profit_before_tax")
    other_inc_earn = divide("other_income", "total_revenue")
    int_coverage = divide("total_revenue", "interest_expended")
    int_over_profit = divide("interest_expended", "net_profit")
    roe_series = ratio_series(roe_for_company, unit_scale=0.01)
    roa_series = ratio_series(roa_for_company, unit_scale=0.01)

    deposits_plus_borrowings = add("deposits", "borrowings")
    cd_ratio = divide("advances", "deposits")
    adv_dep_borrow = {
        pk: raw["advances"][pk] / deposits_plus_borrowings[pk]
        for pk in period_keys
        if deposits_plus_borrowings.get(pk) and pk in raw["advances"]
    }

    total_dividend = {
        pk: raw["dividend_per_share"][pk] * raw["shares_outstanding"][pk]
        for pk in period_keys
        if pk in raw["dividend_per_share"] and pk in raw["shares_outstanding"]
    }
    payout = {
        pk: total_dividend[pk] / raw["net_profit"][pk]
        for pk in period_keys
        if raw["net_profit"].get(pk) and pk in total_dividend
    }
    retention = {pk: 1 - value for pk, value in payout.items()}

    metrics: dict[str, list[dict]] = {
        "balanceSheet": [
            _row("networth", "Networth (reserves only)", "big", period_keys, networth),
            _row("she", "Shareholders Equity (SHE)", "big", period_keys, she),
            _row("deposits", "Gross Deposits", "big", period_keys, raw["deposits"]),
            _row("borrowings", "Gross Borrowings", "big", period_keys, raw["borrowings"]),
            _row("advances", "Advances", "big", period_keys, raw["advances"]),
            _row("investments", "Investments", "big", period_keys, raw["investments"]),
            _row("totalAssets", "Total Assets / Liabilities", "big", period_keys, raw["total_assets"]),
        ],
        "incomeStatement": [
            _row("earnings", "Earnings (Total Income)", "big", period_keys, raw["total_revenue"]),
            _row("expenses", "Expenses", "big", period_keys, raw["operating_expenses"]),
            _row("interestOutgo", "Interest Out-go", "big", period_keys, raw["interest_expended"]),
            _row("otherIncome", "Other Income", "big", period_keys, raw["other_income"]),
            _row("depreciation", "Depreciation", "big", period_keys, raw["depreciation"]),
            _row("netProfit", "Net Profit (PAT)", "big", period_keys, raw["net_profit"]),
        ],
        "perShare": [
            _row("eps", "EPS (Net Profit / share)", "perShare", period_keys, eps_series),
            _row("bookValue", "Book Value (Networth based)", "perShare", period_keys, book_value_series),
            _row("dividend", "Dividend per share", "perShare", period_keys, raw["dividend_per_share"]),
            _row("salesPerShare", "Sales (Revenue per share)", "perShare", period_keys, sales_per_share_series),
            _row("shares", "Shares Outstanding", "sharesCount", period_keys, raw["shares_outstanding"]),
        ],
        "profitability": [
            _row("netMargin", "Net Profit Margin", "pct", period_keys, net_margin),
            _row("roe", "RONW / ROE", "pct", period_keys, roe_series),
            _row("taxRate", "Tax Paid % of Revenue", "pctAbs", period_keys, tax_rate),
            _row("payout", "Dividend Payout Ratio", "pct", period_keys, payout),
            _row("retention", "Retention Ratio", "pct", period_keys, retention),
        ],
        "bankRatios": [
            _row("cdRatio", "Credit-Deposit Ratio", "pct", period_keys, cd_ratio),
            _row("advDepBorrow", "Advances / (Deposits + Borrowings)", "pct", period_keys, adv_dep_borrow),
            _row("otherIncEarn", "Other Income / Earnings", "pct", period_keys, other_inc_earn),
            _row("npAssets", "Net Profit / Total Assets", "pct", period_keys, roa_series),
            _row("intCoverage", "Interest Coverage (Earnings / Interest Outgo)", "x", period_keys, int_coverage),
            _row("intOverProfit", "Interest Outgo / Net Profit", "x", period_keys, int_over_profit),
        ],
    }
    if period_type != "annual":
        # ROE/ROA have no defined quarterly meaning here (see module
        # docstring) — drop the rows outright rather than ship an all-null
        # "RONW / ROE" that looks like a real, just-currently-empty metric.
        metrics["profitability"] = [r for r in metrics["profitability"] if r["key"] != "roe"]
        metrics["bankRatios"] = [r for r in metrics["bankRatios"] if r["key"] != "npAssets"]

    company = get_company(conn, company_id)
    currency = company["currency"] if company else "INR"
    periods = [_period_label(fy_by_key[pk], quarter_by_key[pk]) for pk in period_keys]
    return {"PERIODS": periods, "CURRENCY": currency, "METRICS": metrics}
