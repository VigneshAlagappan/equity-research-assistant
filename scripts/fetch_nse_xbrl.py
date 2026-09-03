"""Download a company's NSE quarterly-results XBRL filings into
data/raw/<company>/nse/, ready for ingestion (main.py ingest / the Admin
Ingest queue). sources/nse_fetch.py's refresh_company_filings() owns the
actual fetch+filter+download logic (and the HTTP/session/caching work
under that) — the same function the company-page "Refresh" button
(web/app.py's admin_refresh_company) calls; this script is a thin CLI
wrapper: resolve a company_id -> nse_symbol, parse args into that
function's parameters, print the result.

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
from datetime import datetime

from companies.registry import get_company
from config import settings
from sources.nse_fetch import refresh_company_filings
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

    from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date() if args.from_date else None
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else None

    dest_dir = settings.RAW_DIR / company_id / "nse"
    result = refresh_company_filings(
        symbol, dest_dir,
        periods=(args.period,),
        years=args.years, from_date=from_date, to_date=to_date,
        cache_dir=_CACHE_DIR,
    )

    if result.most_recent_date is None and not result.downloaded_files and result.skipped_count == 0:
        print("NSE returned no filings for this symbol/period.")
        return

    print(f"Most recent reporting period end on NSE: {result.most_recent_date}", flush=True)
    for path in result.downloaded_files:
        print(f"  downloaded {path.relative_to(settings.BASE_DIR)}", flush=True)

    print(
        f"\nDone. downloaded={len(result.downloaded_files)} already_on_disk={result.skipped_count} "
        f"errors={result.error_count}", flush=True,
    )


if __name__ == "__main__":
    main()
