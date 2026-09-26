"""One-time backfill: register every S&P 500 constituent not already in
`companies` as a US company, enriched with sector/industry/website/fiscal
year end from Yahoo Finance (yfinance's Ticker.info -- company-profile
metadata, not a price or financial-statement fact, same approved boundary
scripts/backfill_company_websites.py and scripts/backfill_sector_industry.py
already use).

Constituent list source: the community-maintained "datasets/s-and-p-500-
companies" CSV (Wikipedia's own "List of S&P 500 companies" table,
republished as a static, versioned file) -- ticker, security name, GICS
sector/sub-industry, CIK. Tickers with a dot (BRK.B, BF.B) are normalized
the same way this app's existing US rows already are (BRKA, BRKB --
verified against the real registered rows), since normalization.companies
technically allows a literal dot but the established convention here strips
it instead.

Never overwrites an existing row's fields other than filling genuine gaps
via register_company()'s own merge semantics would -- a company_id already
registered (e.g. AAPL, one of the 36 S&P 500 names already on file from
earlier SEC EDGAR work) is left untouched, not re-registered.

Usage: python3 -m scripts.register_sp500_companies [--csv PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import time

import yfinance as yf

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from companies.registry import register_company
from companies.us_universe import (
    fiscal_year_end_month_from_yfinance_info,
    normalize_us_ticker,
    resolve_us_company_id,
)
from storage.backend_bootstrap import open_db

REQUEST_DELAY_SECONDS = 0.5
DEFAULT_CSV = "/tmp/sp500.csv"


def _is_stale_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return name in ("OperationalError", "InterfaceError") and (
        "server closed the connection" in text
        or "connection already closed" in text
        or "terminat" in text.lower()
    )


def _existing_companies(conn) -> dict[str, str]:
    """company_id -> country for every company already on file."""
    with conn.cursor() as cur:
        cur.execute("SELECT company_id, country FROM companies")
        return {row["company_id"]: row["country"] for row in cur.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N missing companies (testing)")
    args = parser.parse_args()

    with open(args.csv) as f:
        rows = list(csv.DictReader(f))

    conn = open_db()
    existing = _existing_companies(conn)

    to_register = []
    for row in rows:
        ticker = normalize_us_ticker(row["Symbol"])
        resolved = resolve_us_company_id(ticker, existing)
        if resolved is None:
            continue
        company_id, fetch_symbol = resolved
        to_register.append((company_id, fetch_symbol, row))
    if args.limit:
        to_register = to_register[: args.limit]

    total = len(to_register)
    print(f"{len(rows)} S&P 500 constituents, {len(existing)} companies already on file, "
          f"{total} to register", flush=True)

    registered = errors = 0
    for i, (company_id, fetch_symbol, row) in enumerate(to_register, 1):
        legal_name = row["Security"]
        ticker = fetch_symbol or company_id
        try:
            info = yf.Ticker(ticker).info
        except Exception as exc:  # noqa: BLE001 -- one ticker's Yahoo lookup failing must not abort the batch
            info = {}
            print(f"[{i}/{total}] {company_id}: yfinance lookup failed ({exc}) -- registering with CSV data only", flush=True)
            errors += 1

        kwargs = dict(
            legal_name=info.get("longName") or legal_name,
            display_name=legal_name,
            country="US",
            currency="USD",
            fetch_symbol=fetch_symbol,
            fiscal_year_end_month=fiscal_year_end_month_from_yfinance_info(info),
            website=info.get("website"),
            sector=info.get("sector") or row["GICS Sector"],
            industry=info.get("industry") or row["GICS Sub-Industry"],
        )
        try:
            try:
                register_company(conn, company_id, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry-once, same as every bulk script this session
                if not _is_stale_connection_error(exc):
                    raise
                print(f"[{i}/{total}] {company_id}: DB connection went stale -- reopening and retrying once", flush=True)
                conn.close()
                conn = open_db()
                register_company(conn, company_id, **kwargs)
            registered += 1
            print(f"[{i}/{total}] {company_id}: registered ({info.get('sector') or row['GICS Sector']})", flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad row must not abort the rest of the batch
            print(f"[{i}/{total}] {company_id}: registration failed ({exc})", flush=True)
            errors += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(f"\nDone. {registered} registered, {errors} errors, out of {total} attempted.")


if __name__ == "__main__":
    main()
