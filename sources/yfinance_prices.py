"""YFinancePriceAdapter -- pulls daily OHLCV bars for an NSE-listed ticker
from Yahoo Finance (via yfinance, already a dependency for
web/live_quote.py's live quotes and sources/yfinance_financials.py's
statement data).

Not a sources.base.SourceAdapter subclass, for the same reason
sources/yfinance_financials.py isn't one: SourceAdapter.parse(file_path,
...) assumes a raw file to read, and there isn't one here -- the API itself
is the source. A daily OHLCV bar also isn't a NormalizedObservation (that
shape is for financial-statement line items: metric_key/period_type/
fiscal_year), so this module defines its own plain PriceBar shape instead of
forcing a fit.

See scripts/fetch_daily_prices.py (daily previous-close job) and
scripts/backfill_price_history.py (historical backfill) for the two callers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import yfinance as yf

logger = logging.getLogger(__name__)


# A handful of US companies' company_id doesn't match Yahoo Finance's own
# ticker symbol -- company_id is a plain alphanumeric identifier used as a
# primary key/foreign key throughout this app (financials, price history,
# ...), so it can't carry the punctuation some real tickers do. Berkshire
# Hathaway's Class B shares trade on Yahoo as "BRK-B" (verified: "BRKB" and
# "BRK.B" both return zero rows, "BRK-B" returns real data), but this app's
# company_id for it is "BRKB" -- confirmed root cause of a real production
# gap (Settings > Data Operations > Schedule's "Price history — USA" job,
# run_id 17, silently recorded "no data" for BRKB every day since it never
# fails, it just legitimately finds nothing for the wrong symbol).
# resolve_yfinance_ticker() is the one place this override applies --
# web/live_quote.py's own price-badge lookup imports it too, so the two
# don't drift into resolving the same company_id two different ways.
# Extend this table if another US company_id/ticker mismatch turns up;
# a dedicated db column would only be worth it if this list grows past a
# handful.
US_TICKER_OVERRIDES = {"BRKB": "BRK-B"}


def resolve_yfinance_ticker(ticker: str, country: str = "IN") -> str:
    """The literal string yfinance needs for this company_id/nse_symbol --
    NSE-listed tickers get ".NS" appended (country="IN", the default);
    anything else is looked up in US_TICKER_OVERRIDES first, falling back
    to the ticker unchanged when there's no override (true for all but
    Berkshire today)."""
    if country == "IN":
        return f"{ticker}.NS"
    return US_TICKER_OVERRIDES.get(ticker, ticker)


@dataclass(frozen=True)
class PriceBar:
    trade_date: str  # ISO date, e.g. "2026-08-27"
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


def fetch_daily_bars(
    nse_symbol: str,
    *,
    period: str | None = None,
    start: str | None = None,
    country: str = "IN",
) -> list[PriceBar]:
    """Daily bars for one ticker. Pass exactly one of period (a rolling
    window like "5d"/"1y"/"10y"/"max", yfinance's own vocabulary) or start
    (an explicit ISO date, for a reconciliation run from a known point) --
    mirrors yfinance's own history() signature, which treats period and
    start as mutually exclusive. Defaults to period="1y" if neither is given.

    country decides the yfinance exchange suffix, same convention as
    web/live_quote.py: NSE-listed tickers need ".NS" appended, a US ticker
    needs none. Returns [] (not an error) if yfinance has nothing for this
    ticker, or the call fails outright -- same "absence isn't an error" rule
    sources/yfinance_financials.py's fetch() follows, so one bad ticker
    doesn't abort a 500-ticker loop.

    auto_adjust=True (yfinance's own default) so returned OHLC is split/
    dividend-adjusted -- without it, a stock split would show up as a fake
    ~50% price crash on a chart spanning the split date."""
    if period and start:
        raise ValueError("fetch_daily_bars: pass only one of period or start, not both")
    if not period and not start:
        period = "1y"

    yf_ticker = resolve_yfinance_ticker(nse_symbol, country)
    try:
        frame = yf.Ticker(yf_ticker).history(period=period, start=start, interval="1d", auto_adjust=True)
    except Exception:
        logger.warning("yfinance history() failed for ticker=%s", yf_ticker, exc_info=True)
        return []
    if frame is None or frame.empty:
        return []

    bars: list[PriceBar] = []
    for trade_date, row in frame.iterrows():
        close = row.get("Close")
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        bars.append(
            PriceBar(
                trade_date=trade_date.strftime("%Y-%m-%d"),
                open=_clean(row.get("Open")),
                high=_clean(row.get("High")),
                low=_clean(row.get("Low")),
                close=float(close),
                volume=_clean_int(row.get("Volume")),
            )
        )
    if not bars:
        logger.warning("yfinance returned no usable price bars for ticker=%s", yf_ticker)
    return bars


def _clean(value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return float(value)


def _clean_int(value: float | None) -> int | None:
    cleaned = _clean(value)
    return None if cleaned is None else int(cleaned)
