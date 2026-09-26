"""Shared helpers for registering U.S. companies from an external constituent
list (S&P 500, Russell 3000/1000/2000, ...) — extracted out of
scripts/register_sp500_companies.py so a second/third caller
(scripts/register_russell3000_companies.py) doesn't grow its own copy of the
same collision-resolution logic.
"""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_us_ticker(raw_symbol: str) -> str:
    """"BRK.B" -> "BRKB", matching this app's existing convention for the
    two Berkshire share classes already on file (verified: company_id
    'BRKA'/'BRKB', not 'BRK.A'/'BRK.B'). Also strips a bare space
    (some sources format the same dual-share-class tickers as "BRK B"
    rather than "BRK.B" -- e.g. the iShares consolidated-membership xlsx
    -- found via 6 real registration failures in a production run:
    normalize_company_id()'s regex rejects the literal space)."""
    return raw_symbol.strip().upper().replace(".", "").replace(" ", "")


def resolve_us_company_id(ticker: str, existing: dict[str, str]) -> tuple[str, str | None] | None:
    """(company_id, fetch_symbol) for one US ticker, or None if this ticker
    is already correctly registered as a US company under its own name
    (nothing to do). company_id is a purely internal, guaranteed-unique key;
    fetch_symbol carries the real ticker whenever it differs from company_id.

    Three cases:
    - Not registered at all: (ticker, None) -- company_id IS the ticker,
      same as every existing non-colliding US company.
    - Already registered as a US company under this exact id (the common
      re-run case: this ticker was registered in an earlier pass of this
      script): None -- already done, nothing to register.
    - Registered under this id but as a DIFFERENT country (a genuine
      collision -- e.g. "PNC" is both PNC Financial's ticker and an
      existing Indian company_id, "Pritish Nandy Communications"):
      disambiguates with a "-US" suffix (hyphen, not underscore --
      normalization.companies' company_id regex allows [A-Z0-9&.-], not
      "_") and carries the real ticker in fetch_symbol instead -- every
      US-data call site (price fetch, website backfill, SEC EDGAR)
      resolves `fetch_symbol or company_id`, never company_id alone."""
    country = existing.get(ticker)
    if country is None:
        return ticker, None
    if country == "US":
        return None
    disambiguated = f"{ticker}-US"
    if disambiguated in existing:
        return None
    return disambiguated, ticker


def existing_us_company_id(ticker: str, existing: dict[str, str]) -> str:
    """The company_id a US ticker IS ACTUALLY STORED UNDER today, given the
    same `existing` (company_id -> country) map resolve_us_company_id()
    takes. Unlike that function -- which returns None for BOTH "already
    correctly registered" and "already registered as the disambiguated
    -US id" (it only answers "do I need to register something?") -- this
    always returns a usable company_id, which callers that need to
    reference an existing US company by ticker (e.g. tagging index
    membership after registration has already run) require. Falls back to
    the plain ticker if neither form is on file yet (e.g. a --limit
    truncated an earlier registration pass)."""
    if existing.get(ticker) == "US":
        return ticker
    disambiguated = f"{ticker}-US"
    if disambiguated in existing:
        return disambiguated
    return ticker


def fiscal_year_end_month_from_yfinance_info(info: dict) -> int:
    """yfinance's lastFiscalYearEnd is a Unix timestamp; most US companies
    are December-close, so that's the safe default when Yahoo doesn't have
    it rather than guessing March (this app's India-only default,
    meaningless for a US company)."""
    ts = info.get("lastFiscalYearEnd")
    if not ts:
        return 12
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).month
    except (ValueError, OSError, OverflowError):
        return 12
