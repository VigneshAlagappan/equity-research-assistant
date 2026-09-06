"""USD/INR spot rate, for the Compare page's cross-currency conversion
(comparing a US company against an Indian one needs one currency to
normalize monetary figures into — see web/templates/compare.html and
web/static/js/compare.js).

Same "not authoritative for any valuation math, just a display concern"
posture as web/live_quote.py's own quotes — nothing in canonical_financials
or any ratio calculation depends on this. Reuses sources/yfinance_prices.py's
existing fetch_daily_bars() against Yahoo's "INR=X" ticker (the standard
USD->INR spot-rate quote) rather than a new yfinance call path — no
per-company ticker resolution needed here (it's always the same one pair),
so this doesn't go through resolve_yfinance_ticker()'s override table.

Cached for 15 minutes, not live_quote.py's 60 seconds -- a spot rate used
only to roughly normalize a market-cap comparison doesn't need to track
every tick, and it avoids a yfinance call on every Compare page interaction
(adding/swapping a company doesn't need a fresh rate each time)."""

from __future__ import annotations

import time

from sources.yfinance_prices import fetch_daily_bars

_CACHE_TTL_SECONDS = 15 * 60
_cache: tuple[float, dict | None] | None = None


def get_usd_inr_rate() -> dict | None:
    """{"rate": <INR per 1 USD>, "as_of": "<ISO trade date>"} from the most
    recent close, or None if yfinance has nothing right now (network down,
    rate-limited, ...) -- callers should treat that the same as "can't
    convert currencies today" rather than guessing a rate."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]

    bars = fetch_daily_bars("INR=X", period="5d", country="US")
    result = {"rate": bars[-1].close, "as_of": bars[-1].trade_date} if bars else None
    _cache = (time.monotonic(), result)
    return result
