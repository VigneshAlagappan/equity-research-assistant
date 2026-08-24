"""Live quotes via yfinance, for the company page header's price badge.

Everywhere else in this app, "price" means a point-in-time figure recorded
into ingested/ported financial data (see app.py's _latest_price) — this
module is the one place that calls out to the market for a real-time quote.
Not authoritative for any valuation math, just the header display.
"""

from __future__ import annotations

import time

import yfinance as yf

_CACHE_TTL_SECONDS = 60
_cache: dict[str, tuple[float, dict | None]] = {}


def get_live_quote(ticker: str | None, country: str = "IN") -> dict | None:
    """Latest price, previous close, and the change between them for a
    ticker. Returns None if the ticker is missing or the quote can't be
    fetched (no network, rate-limited, delisted, ...) — callers fall back to
    whatever ingested price they already show today.

    country decides the yfinance exchange suffix: NSE-listed tickers need
    ".NS" appended, a US ticker (e.g. "AAPL") needs none. Only IN/US are
    meaningful today — see web/app.py's company route for how the caller
    resolves which raw symbol to pass for a given company."""
    if not ticker:
        return None

    cache_key = f"{country}:{ticker}"
    cached = _cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    quote = _fetch(ticker, country)
    _cache[cache_key] = (time.monotonic(), quote)
    return quote


def peek_cached_quote(ticker: str | None, country: str = "IN") -> dict | None:
    """Whatever quote is already sitting in the cache for this ticker, no
    matter how stale — never fetches. For the Companies list (web/app.py's
    companies() route): with ~2,500 rows, a live yfinance call per row on
    every page load isn't viable, but showing a price that's already been
    fetched (because someone visited that company's own page, which does
    call get_live_quote()) costs nothing. Returns None for a ticker nobody's
    looked up yet — that's an honest "no price cached", not an error."""
    if not ticker:
        return None
    cached = _cache.get(f"{country}:{ticker}")
    return cached[1] if cached is not None else None


def _fetch(ticker: str, country: str) -> dict | None:
    yf_ticker = f"{ticker}.NS" if country == "IN" else ticker
    try:
        fast_info = yf.Ticker(yf_ticker).fast_info
        price = fast_info["lastPrice"]
        prev_close = fast_info["previousClose"]
    except Exception:
        return None
    if price is None or prev_close is None:
        return None
    change = price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else None
    return {"price": price, "prev_close": prev_close, "change": change, "change_pct": change_pct}
