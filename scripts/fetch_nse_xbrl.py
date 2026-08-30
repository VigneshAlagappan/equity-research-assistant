"""Download a company's NSE quarterly-results XBRL filings into
data/raw/<company>/nse/, ready for ingestion (main.py ingest / the Admin
Ingest queue). sources/nse_fetch.py owns the actual HTTP/session/caching
work; this script just resolves a company_id -> nse_symbol, filters the
filing index to the requested window, and downloads what's missing.

Downloading files here never touches the database — it only stages input
under data/raw/, same as a user manually saving a Screener export there
(README: Ingestion Approach by Source). Ingesting them into
canonical_financials is a separate, explicit step.

Usage:
  python -m scripts.fetch_nse_xbrl IDFCFIRSTB --years 2
  python -m scripts.fetch_nse_xbrl IDFCFIRSTB --from-date 2023-01-01 --to-date 2025-12-31
  python -m scripts.fetch_nse_xbrl IDFCFIRSTB --period Annual
(a plain `python scripts/fetch_nse_xbrl.py` fails on the `storage`/`sources`
imports below -- run as a module so the repo root, not scripts/, lands on
sys.path, same as every other script here.)
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from companies.registry import get_company
from config import settings
from sources.nse_fetch import (
    NSEFetchError,
    download_filing,
    fetch_filing_index,
    fetch_integrated_filing_index,
    filter_last_n_years,
)
from storage.database import init_db

_CACHE_DIR = settings.DATA_DIR / ".cache" / "nse_xbrl"


def _resolve_symbol(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise SystemExit(f"No company registered as {company_id!r}")
    if not company["nse_symbol"]:
        raise SystemExit(f"{company_id} has no nse_symbol on file")
    return company["nse_symbol"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company_id")
    parser.add_argument("--period", default="Quarterly", choices=["Quarterly", "Annual"])
    parser.add_argument("--years", type=int, default=None, help="Only filings reporting within the trailing N years")
    parser.add_argument("--from-date", type=str, default=None, help="YYYY-MM-DD — only filings with to_date on/after this")
    parser.add_argument("--to-date", type=str, default=None, help="YYYY-MM-DD — only filings with to_date on/before this")
    args = parser.parse_args()

    conn = init_db()
    company_id = args.company_id.upper()
    symbol = _resolve_symbol(conn, company_id)
    conn.close()

    print(f"{company_id} -> NSE symbol {symbol!r}", flush=True)

    try:
        filings = fetch_filing_index(symbol, nse_period=args.period, cache_dir=_CACHE_DIR)
    except NSEFetchError as exc:
        raise SystemExit(f"Failed to list NSE filings for {symbol}: {exc}") from exc

    # NSE migrated financial-results filing to SEBI's newer "Integrated
    # Filing" framework partway through (verified: fetch_filing_index()'s
    # own listing for IDFCFIRSTB stops dead at Q3 FY25, this one picks up
    # cleanly from Q4 FY25 onward with no gap or overlap) — only relevant
    # for Quarterly, the only cadence this framework reports at.
    if args.period == "Quarterly":
        try:
            integrated_filings = fetch_integrated_filing_index(symbol, cache_dir=_CACHE_DIR)
        except NSEFetchError as exc:
            print(f"WARNING: failed to list newer Integrated Filing results for {symbol}: {exc}", flush=True)
            integrated_filings = []
        if integrated_filings:
            print(f"{len(integrated_filings)} additional filing(s) from NSE's newer Integrated Filing listing", flush=True)
        filings = filings + integrated_filings

    if not filings:
        print("NSE returned no filings for this symbol/period.")
        return

    if args.years is not None:
        filings = filter_last_n_years(filings, args.years)
    if args.from_date:
        cutoff = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        filings = [f for f in filings if f.to_date >= cutoff]
    if args.to_date:
        cutoff = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        filings = [f for f in filings if f.to_date <= cutoff]

    filings.sort(key=lambda f: (f.to_date, f.statement_type))
    most_recent = max(f.to_date for f in filings) if filings else None
    print(f"{len(filings)} filing(s) in window. Most recent reporting period end on NSE: {most_recent}", flush=True)

    dest_dir = settings.RAW_DIR / company_id / "nse"
    downloaded = skipped = errors = 0
    for filing in filings:
        dest_path = dest_dir / f"{filing.to_date.isoformat()}_{filing.statement_type}_{filing.seq_number}.xml"
        try:
            fetched = download_filing(filing, dest_path)
        except NSEFetchError as exc:
            errors += 1
            print(f"  ERROR {filing.to_date} {filing.statement_type} seq={filing.seq_number}: {exc}", flush=True)
            continue
        if fetched:
            downloaded += 1
            print(f"  downloaded {dest_path.relative_to(settings.BASE_DIR)}", flush=True)
        else:
            skipped += 1
            print(f"  already on disk: {dest_path.relative_to(settings.BASE_DIR)}", flush=True)

    print(f"\nDone. downloaded={downloaded} already_on_disk={skipped} errors={errors}", flush=True)


if __name__ == "__main__":
    main()
