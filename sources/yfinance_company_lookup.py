"""Look up a company's basic profile (name, currency, sector, industry,
website) from Yahoo Finance by ticker -- the piece that lets Settings >
Companies > Add Company ask for just a ticker + country and have the
backend fill in everything else, instead of an admin typing legal name,
display name, currency, sector, and industry by hand.

Uses the same yf.Ticker(...) + resolve_yfinance_ticker() (.NS suffixing for
India) as sources/yfinance_prices.py and sources/yfinance_financials.py --
no new ticker-resolution logic here.
"""

from __future__ import annotations

from dataclasses import dataclass

import yfinance as yf
from yfinance import Search

from sources.yfinance_prices import resolve_yfinance_ticker


class CompanyLookupError(Exception):
    """Yahoo Finance has no usable profile for this ticker/country."""


@dataclass(frozen=True)
class CompanyProfile:
    legal_name: str
    display_name: str
    currency: str
    website: str | None
    sector: str | None
    industry: str | None


def search_companies(query: str, country: str = "IN", *, max_results: int = 8) -> list[dict]:
    """Plausible-name-as-you-type suggestions for Add Company's ticker
    field, backed by Yahoo Finance's own search (yf.Search -- the same
    typeahead Yahoo's own site uses, not a new scraping surface). Filtered
    to plain equity listings on the requested country's home exchange:
    NSI (NSE) for India, and dot-free symbols for the US -- Yahoo's search
    also returns foreign cross-listings of the same company (e.g.
    searching "Nike" also returns NKE.SG on the Stuttgart exchange, NKE.MU
    on Munich), and this app has no use for those, only the primary
    listing whose ticker matches company_id conventions elsewhere in this
    codebase (see sources/yfinance_prices.py's resolve_yfinance_ticker()).
    Returns [] (not an error) for a short/empty query or if Yahoo Finance's
    search has nothing -- same "absence isn't an error" rule as
    lookup_company_profile()'s siblings elsewhere in sources/."""
    query = query.strip()
    if len(query) < 2:
        return []

    try:
        quotes = Search(query, max_results=max_results * 3).quotes
    except Exception:  # noqa: BLE001 -- typeahead best-effort, never worth a 500
        return []

    results = []
    seen_symbols = set()
    for q in quotes:
        if q.get("quoteType") != "EQUITY":
            continue
        symbol = q.get("symbol", "")
        if country == "IN":
            if not symbol.endswith(".NS"):
                continue
            symbol = symbol[: -len(".NS")]
        else:
            if "." in symbol:
                continue
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        name = q.get("longname") or q.get("shortname") or symbol
        results.append({"ticker": symbol, "name": name})
        if len(results) >= max_results:
            break
    return results


def lookup_company_profile(ticker: str, country: str = "IN") -> CompanyProfile:
    """Raises CompanyLookupError if Yahoo Finance has no name on file for
    this ticker -- the one signal available up front that a ticker/country
    pairing is wrong (a typo, or the wrong country picked for the exchange
    suffix), before onboarding spends time on financials/price ingestion."""
    yf_ticker = resolve_yfinance_ticker(ticker, country)
    try:
        info = yf.Ticker(yf_ticker).get_info()
    except Exception as exc:  # noqa: BLE001 -- yfinance raises a mix of HTTP/JSON errors for a bad symbol
        raise CompanyLookupError(f"Yahoo Finance lookup failed for {ticker!r} ({yf_ticker}): {exc}") from exc

    long_name = info.get("longName") or info.get("shortName")
    if not long_name:
        raise CompanyLookupError(f"Yahoo Finance has no company profile for {ticker!r} ({yf_ticker})")

    return CompanyProfile(
        legal_name=long_name,
        display_name=info.get("shortName") or long_name,
        currency=(info.get("currency") or ("INR" if country == "IN" else "USD")).upper(),
        website=info.get("website"),
        sector=info.get("sector"),
        industry=info.get("industry"),
    )
