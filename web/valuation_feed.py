"""Builds the annual-only valuation-model JSON feed for the Valuation Model
tab's Growth Projection / Intrinsic Value calculator (see
web/static/js/valuation_dashboard_interactive.js) — the path every company
without a ported, richer dataset (companies.valuation_model_file — see the
"HDFC Bank Equity Dashboard" Claude Design import) falls back to. Same shape
as a ported file: {"YEARS": [...], "METRICS": {section: [{key, label, unit,
values}]}}.

The Financials tab (and the Overview tab's snapshot) moved off this feed to
web/charts_feed.py's build_charts_feed() instead, which is annual/quarterly-
period-aware — this feed stays annual-only on purpose, since Growth
Projection's assumptions (CAGR window, projected/terminal growth) are
inherently annual concepts with no established quarterly equivalent, same
reasoning build_charts_feed() itself gives for dropping ROE/ROA in quarterly
mode.

Advances/deposits/EPS/book value/dividend/shares-outstanding used to have no
ingested source anywhere and came back as an all-null row — that changed
once sources/proprietary.py started aliasing them (verified against a real
ICICI Bank workbook). Several ratio rows (SHE, Credit-Deposit, Dividend
Payout/Retention) were still wired to an empty series even after that,
never caught up to what the raw data could already answer — fixed by
computing them from what's on file, same reasoning financials/ratios.py
already applies to ROE/ROA, rather than leaving a division away as "—".

Two categories of gap are left deliberately unfilled, not overlooked:
1. A handful of income-statement rows (Earnings, Interest Out-go,
   Depreciation, and the ratios built from them) only run 10 years deep —
   that's the real depth of this Screener export, not a parsing bug. The
   Proprietary file's same-looking rows ("Earnings", "Interest Out-go") were
   deliberately never aliased to fill the gap: verified against a real
   non-bank export (Amara Raja) that "Earnings" doesn't behave like net
   profit or total revenue at any consistent scale — aliasing it on a guess
   risks quietly wrong numbers, which is worse than "—" in a finance tool.
2. The whole price/valuation-multiples section has no source at all (no
   live market-data pipeline, see README) — this is the one gap an LLM
   should never paper over: a stock's historical price is a specific,
   externally-verifiable fact, not something to infer from context, and a
   plausible-looking guess here is worse than an honest blank.
"""

from __future__ import annotations

from storage.db_types import DBConnection

from companies.registry import get_company
from financials.ratios import MissingDataError, SectorMismatchError, roa_for_company, roe_for_company
from normalization.periods import fiscal_year_number
from storage.repositories import get_canonical_series

# Rescales any unit that isn't already each currency's "big" display unit
# (crore for INR, million for USD) into that unit — e.g. an INR_LAKH series
# alongside INR_CRORE ones, or a USD_THOUSAND/USD_BILLION series alongside
# USD_MILLION ones. A unit not in this table (already the big display unit,
# or a non-scaled unit like PERCENT/RATIO/NUMBER) passes through unchanged.
_UNIT_RESCALE_TO_DISPLAY: dict[str, float] = {
    "INR_LAKH": 0.01,
    "USD_THOUSAND": 0.001,
    "USD_BILLION": 1000,
}

# The subset of the ingested metric vocabulary this feed reads.
_RAW_METRIC_KEYS = (
    "net_profit", "total_assets", "total_revenue", "other_income", "interest_expended",
    "tax", "profit_before_tax", "operating_expenses", "depreciation",
    "equity_share_capital", "reserves", "borrowings", "investments",
    "deposits", "advances", "eps", "book_value", "dividend_per_share", "sales_per_share",
    "shares_outstanding", "total_shareholders_funds",
)


def _series_by_year(conn: DBConnection, company_id: str, metric_key: str, statement_type: str) -> dict[int, float]:
    """fiscal_year (int) -> value in the company's currency's "big" display
    unit (crore for INR, million for USD), for one metric/company/statement_type."""
    out: dict[int, float] = {}
    for row in get_canonical_series(conn, company_id, metric_key, "annual", statement_type):
        year = fiscal_year_number(row["fiscal_year"])
        value = row["canonical_value"] * _UNIT_RESCALE_TO_DISPLAY.get(row["unit"], 1.0)
        out[year] = value
    return out


def _values_for(years: list[int], series: dict[int, float]) -> list[float | None]:
    return [series.get(year) for year in years]


def _row(
    key: str, label: str, unit: str, years: list[int], series: dict[int, float], row_type: str = "fact",
) -> dict:
    """row_type ("fact" | "calc") — see web/charts_feed.py's identical _row()
    docstring; same FACT/CALC badge, same classification rule, just kept as
    a separate function here since this feed's whole shape (year-keyed, not
    period-key-tuple-keyed) is already independently duplicated from
    charts_feed.py."""
    return {"key": key, "label": label, "unit": unit, "values": _values_for(years, series), "type": row_type}


def build_valuation_feed(conn: DBConnection, company_id: str, statement_type: str = "consolidated") -> dict:
    raw = {key: _series_by_year(conn, company_id, key, statement_type) for key in _RAW_METRIC_KEYS}
    years = sorted({year for series in raw.values() for year in series})

    def ratio_series(fn, unit_scale: float = 1.0) -> dict[int, float]:
        """fn is roe_for_company/roa_for_company — needs the prior fiscal year
        too, so a given year silently drops out (not "—", genuinely absent
        from `years`) only if it never appears in any raw series at all."""
        out = {}
        for year in years:
            try:
                result = fn(conn, company_id, f"FY{year}", statement_type=statement_type)
            except (MissingDataError, SectorMismatchError, ZeroDivisionError, ValueError):
                continue
            out[year] = result.value * unit_scale
        return out

    def divide(numerator: str, denominator: str) -> dict[int, float]:
        num, den = raw[numerator], raw[denominator]
        return {year: num[year] / den[year] for year in years if den.get(year) and year in num}

    def add(a: str, b: str) -> dict[int, float]:
        va, vb = raw[a], raw[b]
        return {year: va[year] + vb[year] for year in years if year in va and year in vb}

    def fill_missing(primary: dict[int, float], fallback: dict[int, float]) -> dict[int, float]:
        """Prefer a directly-reported figure; fall back to computing it only
        for years the source didn't report that figure itself but did
        report its ingredients — same reasoning financials/ratios.py
        already applies to ROE/ROA. Concretely: Screener's per-share rows
        (EPS/Book Value/Sales-per-share) only run through the year its
        Balance Sheet section last updated, while its P&L/shares data can
        run a year or two further — this fills that gap from what's already
        on file instead of leaving it "—" when the answer is one division
        away."""
        return {**fallback, **primary}

    networth = raw["reserves"]
    # Prefer the directly-reported SHE (Screener's own "Total Shareholders
    # Funds" row, Proprietary's "SHE" row — both alias straight to
    # total_shareholders_funds) over summing the two components, which only
    # works for years both happen to be present; on ICICI Bank, that
    # sum covered 10 years, the direct figure covers 21.
    she = fill_missing(raw["total_shareholders_funds"], add("equity_share_capital", "reserves"))
    eps_series = fill_missing(raw["eps"], divide("net_profit", "shares_outstanding"))
    book_value_series = fill_missing(
        raw["book_value"],
        {
            year: she[year] / raw["shares_outstanding"][year]
            for year in years
            if raw["shares_outstanding"].get(year) and year in she
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

    # Bank ratios: both credit-deposit and advances/(deposits+borrowings)
    # were wired to `empty` from this feed's very first version, back when
    # neither deposits nor advances had any source at all — sources/
    # proprietary.py's bank-row aliases (see normalization/financials.py)
    # changed that, this just catches the ratio rows up to it.
    deposits_plus_borrowings = add("deposits", "borrowings")
    cd_ratio = divide("advances", "deposits")
    adv_dep_borrow = {
        year: raw["advances"][year] / deposits_plus_borrowings[year]
        for year in years
        if deposits_plus_borrowings.get(year) and year in raw["advances"]
    }

    # Dividend payout: (dividend/share x shares) / net profit — same
    # ingredients-not-yet-multiplied-together situation as the per-share
    # fallbacks above, just combined the other direction.
    total_dividend = {
        year: raw["dividend_per_share"][year] * raw["shares_outstanding"][year]
        for year in years
        if year in raw["dividend_per_share"] and year in raw["shares_outstanding"]
    }
    payout = {
        year: total_dividend[year] / raw["net_profit"][year]
        for year in years
        if raw["net_profit"].get(year) and year in total_dividend
    }
    retention = {year: 1 - value for year, value in payout.items()}

    empty: dict[int, float] = {}

    # Unit categories are currency-agnostic labels ("big"/"perShare"/
    # "sharesCount", not "crore"/"rupee"/"crShares") — the actual symbol and
    # scale word are chosen client-side (valuation_dashboard.js's fmt())
    # from CURRENCY below, same value regardless of which currency a company
    # reports in.
    metrics = {
        "balanceSheet": [
            _row("networth", "Networth (reserves only)", "big", years, networth),
            _row("she", "Shareholders Equity (SHE)", "big", years, she, row_type="calc"),
            _row("deposits", "Gross Deposits", "big", years, raw["deposits"]),
            _row("borrowings", "Gross Borrowings", "big", years, raw["borrowings"]),
            _row("advances", "Advances", "big", years, raw["advances"]),
            _row("investments", "Investments", "big", years, raw["investments"]),
            _row("totalAssets", "Total Assets / Liabilities", "big", years, raw["total_assets"]),
        ],
        "incomeStatement": [
            _row("earnings", "Earnings (Total Income)", "big", years, raw["total_revenue"]),
            _row("expenses", "Expenses", "big", years, raw["operating_expenses"]),
            _row("interestOutgo", "Interest Out-go", "big", years, raw["interest_expended"]),
            _row("otherIncome", "Other Income", "big", years, raw["other_income"]),
            _row("depreciation", "Depreciation", "big", years, raw["depreciation"]),
            _row("netProfit", "Net Profit (PAT)", "big", years, raw["net_profit"]),
        ],
        "perShare": [
            _row("eps", "EPS (Net Profit / share)", "perShare", years, eps_series, row_type="calc"),
            _row("bookValue", "Book Value (Networth based)", "perShare", years, book_value_series, row_type="calc"),
            _row("dividend", "Dividend per share", "perShare", years, raw["dividend_per_share"]),
            _row("salesPerShare", "Sales (Revenue per share)", "perShare", years, sales_per_share_series, row_type="calc"),
            _row("shares", "Shares Outstanding", "sharesCount", years, raw["shares_outstanding"]),
        ],
        "profitability": [
            _row("netMargin", "Net Profit Margin", "pct", years, net_margin, row_type="calc"),
            _row("roe", "RONW / ROE", "pct", years, roe_series, row_type="calc"),
            _row("taxRate", "Tax Paid % of Revenue", "pctAbs", years, tax_rate, row_type="calc"),
            _row("payout", "Dividend Payout Ratio", "pct", years, payout, row_type="calc"),
            _row("retention", "Retention Ratio", "pct", years, retention, row_type="calc"),
        ],
        "bankRatios": [
            _row("cdRatio", "Credit-Deposit Ratio", "pct", years, cd_ratio, row_type="calc"),
            _row("advDepBorrow", "Advances / (Deposits + Borrowings)", "pct", years, adv_dep_borrow, row_type="calc"),
            _row("otherIncEarn", "Other Income / Earnings", "pct", years, other_inc_earn, row_type="calc"),
            _row("npAssets", "Net Profit / Total Assets", "pct", years, roa_series, row_type="calc"),
            _row("intCoverage", "Interest Coverage (Earnings / Interest Outgo)", "x", years, int_coverage, row_type="calc"),
            _row("intOverProfit", "Interest Outgo / Net Profit", "x", years, int_over_profit, row_type="calc"),
        ],
        "valuation": [
            _row("price", "Price", "perShare", years, empty),
            _row("pe", "P/E", "x", years, empty, row_type="calc"),
            _row("pbv", "P/BV (Shareholder Equity)", "x", years, empty, row_type="calc"),
            _row("divYield", "Dividend Yield", "pct", years, empty, row_type="calc"),
        ],
    }

    company = get_company(conn, company_id)
    currency = company["currency"] if company else "INR"
    return {"YEARS": years, "CURRENCY": currency, "METRICS": metrics}
