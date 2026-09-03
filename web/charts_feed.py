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

Price/Volume (a "priceVolume" METRICS section, sourced from storage/
price_repository.py's daily_prices via an optional price_conn) are the one
exception to "one value per period" being purely arithmetic on already-
period-keyed rows — daily bars have no fiscal_year/quarter label at all, so
_period_date_range() derives each period's actual calendar date range from
the company's fiscal_year_end_month and aggregates onto it (period-end
close, average daily volume). Omitted entirely (all-null, filtered out
client-side same as any other all-null attribute) when price_conn isn't
passed or the company has no rows in the price db yet — e.g. a non-NSE
company, or an NSE one whose fiscal periods predate however much history
has been backfilled. P/E and P/B ratios live in the same section (real
division against eps_series/book_value_series, not placeholders) but are
narrower still — populated only where a period has both a close price AND
the underlying per-share fundamental, so they fill in as more price history
is backfilled even for periods that already have every other metric.
"""

from __future__ import annotations

import calendar
from storage.db_types import DBConnection
from datetime import date

from companies.registry import get_company
from financials.ratios import MissingDataError, SectorMismatchError, roa_for_company, roe_for_company
from storage.price_repository import get_avg_volume, get_close_as_of_range
from storage.repositories import get_canonical_series

# See web/valuation_feed.py's identical table for the rationale — rescales
# any unit that isn't already each currency's "big" display unit (crore for
# INR, million for USD) into that unit.
_UNIT_RESCALE_TO_DISPLAY: dict[str, float] = {
    "INR_LAKH": 0.01,
    "USD_THOUSAND": 0.001,
    "USD_BILLION": 1000,
}
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


def _period_date_range(fiscal_year_end_month: int, year_num: int, quarter_num: int) -> tuple[date, date]:
    """Calendar date range [start, end] for one fiscal period, so
    Price/Volume (daily data, storage/price_repository.py) can be aggregated
    onto the same period_keys the financial-statement metrics above already
    use -- those only carry a fiscal_year/quarter label, never a date range,
    so this derives one from the company's own fiscal_year_end_month
    (schemas/sqlite_schema.sql) the same way every Indian/US filing
    calendar actually works: FY `year_num` ends on the last day of
    `fiscal_year_end_month` in calendar year `year_num`, and quarters are
    consecutive 3-month blocks counting from the fiscal year's start month
    (Q1FY24 = Apr-Jun 2023 for a March-ending fiscal year, not a calendar
    quarter) -- quarter_num=0 means the full fiscal year."""
    if fiscal_year_end_month == 12:
        fy_start_year, fy_start_month = year_num, 1
    else:
        fy_start_year, fy_start_month = year_num - 1, fiscal_year_end_month + 1

    def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
        total = month - 1 + offset
        return year + total // 12, total % 12 + 1

    if quarter_num == 0:
        start = date(fy_start_year, fy_start_month, 1)
        end = date(year_num, fiscal_year_end_month, calendar.monthrange(year_num, fiscal_year_end_month)[1])
        return start, end

    start_year, start_month = _add_months(fy_start_year, fy_start_month, (quarter_num - 1) * 3)
    end_year, end_month = _add_months(fy_start_year, fy_start_month, (quarter_num - 1) * 3 + 2)
    start = date(start_year, start_month, 1)
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return start, end


def _period_label(fiscal_year: str, quarter: str | None) -> str:
    return f"{quarter} {fiscal_year}" if quarter else fiscal_year


def _series_by_period(
    conn: DBConnection, company_id: str, metric_key: str, period_type: str, statement_type: str
) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    for row in get_canonical_series(conn, company_id, metric_key, period_type, statement_type):
        value = row["canonical_value"] * _UNIT_RESCALE_TO_DISPLAY.get(row["unit"], 1.0)
        out[_period_key(row["fiscal_year"], row["quarter"])] = value
    return out


def _annual_book_value_and_shares(
    conn: DBConnection, company_id: str, statement_type: str
) -> tuple[dict[tuple[int, int], float], dict[tuple[int, int], float]]:
    """Book value per share and shares outstanding, forced to
    period_type="annual" regardless of the page's own period_type — the
    fallback denominators for quarterly P/E (TTM EPS) / P/B (latest annual
    book value) below, since this app's ingested quarterly data is
    income-statement-only for most companies (no quarterly EPS/shares/book
    value at all, confirmed against HDFCBANK: 0 quarterly rows for any of
    those three). Same derivation chain as the main annual book_value_series
    (SHE = total_shareholders_funds, falling back to equity_share_capital +
    reserves; book value = raw book_value, falling back to SHE / shares),
    just independently re-fetched at annual granularity."""
    shares = _series_by_period(conn, company_id, "shares_outstanding", "annual", statement_type)
    book_value_direct = _series_by_period(conn, company_id, "book_value", "annual", statement_type)
    she_direct = _series_by_period(conn, company_id, "total_shareholders_funds", "annual", statement_type)
    equity = _series_by_period(conn, company_id, "equity_share_capital", "annual", statement_type)
    reserves = _series_by_period(conn, company_id, "reserves", "annual", statement_type)
    she = {**{pk: equity[pk] + reserves[pk] for pk in equity if pk in reserves}, **she_direct}
    book_value = {
        **{pk: she[pk] / shares[pk] for pk in she if shares.get(pk)},
        **book_value_direct,
    }
    return book_value, shares


def _values_for(period_keys: list[tuple[int, int]], series: dict[tuple[int, int], float]) -> list[float | None]:
    return [series.get(pk) for pk in period_keys]


def _row(
    key: str, label: str, unit: str, period_keys: list[tuple[int, int]], series: dict[tuple[int, int], float],
    row_type: str = "fact",
) -> dict:
    """row_type is "fact" (a canonical_financials value passed through
    unchanged — even if just relabeled, like "networth" = raw reserves) or
    "calc" (built from arithmetic on top of one or more raw values —
    divide/add/fill_missing-with-a-formula/ratio_series below all count,
    even where a fallback is only used for SOME periods: the row can show a
    computed figure at all, so it's calc for its whole column, not per
    cell). Surfaced client-side as the Financials tab's FACT/CALC badge
    (web/static/js/valuation_dashboard.js) — same tag-fact/tag-calculation
    styling already used for [FACT]/[CALCULATION] in AI-generated insights
    (base.html), reused here rather than inventing a second badge design."""
    return {"key": key, "label": label, "unit": unit, "values": _values_for(period_keys, series), "type": row_type}


def build_charts_feed(
    conn: DBConnection,
    company_id: str,
    statement_type: str = "consolidated",
    period_type: str = "annual",
    price_conn: DBConnection | None = None,
) -> dict:
    company = get_company(conn, company_id)
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

    close_price_series: dict[tuple[int, int], float] = {}
    volume_series: dict[tuple[int, int], float] = {}
    if price_conn is not None and company is not None:
        fy_end_month = company["fiscal_year_end_month"]
        for pk in period_keys:
            year_num, q_num = pk
            start, end = _period_date_range(fy_end_month, year_num, q_num)
            close = get_close_as_of_range(price_conn, company_id, start.isoformat(), end.isoformat())
            if close is not None:
                close_price_series[pk] = close
            volume = get_avg_volume(price_conn, company_id, start.isoformat(), end.isoformat())
            if volume is not None:
                volume_series[pk] = volume

    # Valuation ratios derived from the same period-end close price above.
    # Annual: straight division against per-share fundamentals already
    # computed for the perShare section (eps_series/book_value_series) —
    # real numbers, only null where a period lacks a close price or the
    # per-share figure itself.
    #
    # Quarterly: this app's ingested quarterly data is income-statement-only
    # for most companies — no quarterly EPS/shares outstanding/book value at
    # all (confirmed against HDFCBANK: 0 quarterly rows for any of the
    # three), so eps_series/book_value_series are empty here and a straight
    # quarterly division has nothing to divide by. Falls back to the
    # standard real-world approximation instead: P/E uses trailing-twelve-
    # month EPS (this quarter's + the prior 3 quarters' net profit, divided
    # by the latest known annual shares outstanding); P/B uses the latest
    # completed fiscal year's book value per share (book value moves slowly
    # enough within a year that this is a reasonable stand-in, same
    # reasoning used elsewhere in this app for other slow-moving figures).
    # Labeled "(TTM)" / "(Latest Annual BV)" below so it reads as derived,
    # not a precise point-in-time quarterly figure.
    if period_type == "quarterly":
        annual_book_value, annual_shares = _annual_book_value_and_shares(conn, company_id, statement_type)

        def _latest_annual(series: dict[tuple[int, int], float], as_of_year: int) -> float | None:
            eligible = sorted((y for y, q in series if q == 0 and y <= as_of_year), reverse=True)
            return series[(eligible[0], 0)] if eligible else None

        def _ttm_eps(year_num: int, q_num: int) -> float | None:
            quarters: list[tuple[int, int]] = []
            y, q = year_num, q_num
            for _ in range(4):
                quarters.append((y, q))
                q -= 1
                if q == 0:
                    q, y = 4, y - 1
            profits = [raw["net_profit"].get(pk) for pk in quarters]
            if any(p is None for p in profits):
                return None
            shares = _latest_annual(annual_shares, year_num)
            return sum(profits) / shares if shares else None  # type: ignore[arg-type]

        pe_ratio_series = {}
        pb_ratio_series = {}
        for pk in period_keys:
            year_num, q_num = pk
            close = close_price_series.get(pk)
            if close is None:
                continue
            ttm_eps = _ttm_eps(year_num, q_num)
            if ttm_eps:
                pe_ratio_series[pk] = close / ttm_eps
            latest_bv = _latest_annual(annual_book_value, year_num)
            if latest_bv:
                pb_ratio_series[pk] = close / latest_bv
        pe_label, pb_label = "P/E Ratio (TTM)", "P/B Ratio (Latest Annual BV)"
    else:
        pe_ratio_series = {
            pk: close_price_series[pk] / eps_series[pk]
            for pk in period_keys
            if eps_series.get(pk) and pk in close_price_series
        }
        pb_ratio_series = {
            pk: close_price_series[pk] / book_value_series[pk]
            for pk in period_keys
            if book_value_series.get(pk) and pk in close_price_series
        }
        pe_label, pb_label = "P/E Ratio", "P/B Ratio (Price to Book)"

    metrics: dict[str, list[dict]] = {
        "balanceSheet": [
            _row("networth", "Networth (reserves only)", "big", period_keys, networth),
            _row("she", "Shareholders Equity (SHE)", "big", period_keys, she, row_type="calc"),
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
            _row("eps", "EPS (Net Profit / share)", "perShare", period_keys, eps_series, row_type="calc"),
            _row("bookValue", "Book Value (Networth based)", "perShare", period_keys, book_value_series, row_type="calc"),
            _row("dividend", "Dividend per share", "perShare", period_keys, raw["dividend_per_share"]),
            _row("salesPerShare", "Sales (Revenue per share)", "perShare", period_keys, sales_per_share_series, row_type="calc"),
            _row("shares", "Shares Outstanding", "sharesCount", period_keys, raw["shares_outstanding"]),
        ],
        "profitability": [
            _row("netMargin", "Net Profit Margin", "pct", period_keys, net_margin, row_type="calc"),
            _row("roe", "RONW / ROE", "pct", period_keys, roe_series, row_type="calc"),
            _row("taxRate", "Tax Paid % of Revenue", "pctAbs", period_keys, tax_rate, row_type="calc"),
            _row("payout", "Dividend Payout Ratio", "pct", period_keys, payout, row_type="calc"),
            _row("retention", "Retention Ratio", "pct", period_keys, retention, row_type="calc"),
        ],
        "bankRatios": [
            _row("cdRatio", "Credit-Deposit Ratio", "pct", period_keys, cd_ratio, row_type="calc"),
            _row("advDepBorrow", "Advances / (Deposits + Borrowings)", "pct", period_keys, adv_dep_borrow, row_type="calc"),
            _row("otherIncEarn", "Other Income / Earnings", "pct", period_keys, other_inc_earn, row_type="calc"),
            _row("npAssets", "Net Profit / Total Assets", "pct", period_keys, roa_series, row_type="calc"),
            _row("intCoverage", "Interest Coverage (Earnings / Interest Outgo)", "x", period_keys, int_coverage, row_type="calc"),
            _row("intOverProfit", "Interest Outgo / Net Profit", "x", period_keys, int_over_profit, row_type="calc"),
        ],
        "priceVolume": [
            _row("closePrice", "Close Price (period end)", "rupee", period_keys, close_price_series),
            _row("volume", "Avg Daily Volume", "volume", period_keys, volume_series),
            _row("peRatio", pe_label, "x", period_keys, pe_ratio_series, row_type="calc"),
            _row("pbRatio", pb_label, "x", period_keys, pb_ratio_series, row_type="calc"),
        ],
    }
    if period_type != "annual":
        # ROE/ROA have no defined quarterly meaning here (see module
        # docstring) — drop the rows outright rather than ship an all-null
        # "RONW / ROE" that looks like a real, just-currently-empty metric.
        metrics["profitability"] = [r for r in metrics["profitability"] if r["key"] != "roe"]
        metrics["bankRatios"] = [r for r in metrics["bankRatios"] if r["key"] != "npAssets"]

    currency = company["currency"] if company else "INR"
    periods = [_period_label(fy_by_key[pk], quarter_by_key[pk]) for pk in period_keys]
    # PERIOD_KEYS (parallel to PERIODS, [year, quarter_num] per entry) lets a
    # client correctly merge/sort multiple companies' period axes together
    # (web/static/js/charts_overlay.js's Compare With) without re-parsing a
    # formatted label like "Q1 FY2024" back into a sortable key.
    period_key_pairs = [[year, quarter] for year, quarter in period_keys]
    return {"PERIODS": periods, "PERIOD_KEYS": period_key_pairs, "CURRENCY": currency, "METRICS": metrics}
