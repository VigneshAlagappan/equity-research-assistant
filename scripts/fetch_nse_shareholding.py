"""Fetch + ingest a company's NSE Shareholding Pattern (SEBI LODR Reg 31)
history: per-quarter promoter/public/employee-trust percentages (no XBRL
parse needed) plus, for each submission, both the individually-named
holders AND the FII/DII/Government/Public(non-institutional) category
breakdown drilled out of that submission's own SHP XBRL (see
sources/nse_shareholding.py's module docstring for which sub-categories
are named vs. aggregate-only, why some are skipped, and the taxonomy-
version gap that leaves older quarters without a breakdown).

Unlike scripts/fetch_nse_xbrl.py, this never stages a file under
data/raw/ -- there's no separate parse step: the master listing's JSON and
each submission's parsed XBRL are inserted directly.

Usage:
  python -m scripts.fetch_nse_shareholding IDFCFIRSTB
  python -m scripts.fetch_nse_shareholding IDFCFIRSTB --years 3
(run as a module, same reason as fetch_nse_xbrl.py's own docstring.)
"""

from __future__ import annotations

import argparse
from datetime import date

from companies.registry import get_company
from sources.nse_fetch import NSEFetchError
from sources.nse_shareholding import fetch_shareholding_detail, fetch_shareholding_master
from storage.database import init_db
from storage.repositories import (
    insert_shareholding_holders,
    insert_shareholding_observations,
    update_shareholding_category_breakdown,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company_id")
    parser.add_argument("--years", type=int, default=None, help="Only submissions reporting within the trailing N years")
    args = parser.parse_args()

    conn = init_db()
    company_id = args.company_id.upper()
    company = get_company(conn, company_id)
    if company is None:
        raise SystemExit(f"No company registered as {company_id!r}")
    symbol = company["nse_symbol"]
    if not symbol:
        raise SystemExit(f"{company_id} has no nse_symbol on file")

    print(f"{company_id} -> NSE symbol {symbol!r}", flush=True)

    try:
        summaries = fetch_shareholding_master(symbol)
    except NSEFetchError as exc:
        raise SystemExit(f"Failed to list shareholding submissions for {symbol}: {exc}") from exc

    if not summaries:
        print("NSE returned no shareholding submissions for this symbol.")
        conn.close()
        return

    if args.years is not None:
        cutoff = date.today().replace(year=date.today().year - args.years)
        summaries = [s for s in summaries if s.period_end >= cutoff]

    summaries.sort(key=lambda s: s.period_end)
    print(f"{len(summaries)} submission(s) in window.", flush=True)

    obs_count = insert_shareholding_observations(conn, company_id, summaries)
    print(f"Upserted {obs_count} quarterly summary row(s).", flush=True)

    holder_total = 0
    errors = 0
    for s in summaries:
        if not s.source_url:
            print(f"  {s.fiscal_year} {s.quarter}: no XBRL link on file — summary percentages only", flush=True)
            continue
        try:
            holdings, breakdown = fetch_shareholding_detail(s.source_url)
        except NSEFetchError as exc:
            errors += 1
            print(f"  ERROR {s.fiscal_year} {s.quarter}: {exc}", flush=True)
            continue
        n = insert_shareholding_holders(
            conn, company_id, s.fiscal_year, s.quarter, holdings,
            source_url=s.source_url, submission_date=s.submission_date,
        )
        holder_total += n
        breakdown_note = ""
        if breakdown is not None:
            update_shareholding_category_breakdown(conn, company_id, s.fiscal_year, s.quarter, breakdown)
            breakdown_note = " + FII/DII/Government breakdown"
        print(f"  {s.fiscal_year} {s.quarter}: {len(holdings)} named holder(s){breakdown_note}", flush=True)

    conn.close()
    print(f"\nDone. summaries={obs_count} named_holders={holder_total} errors={errors}", flush=True)


if __name__ == "__main__":
    main()
