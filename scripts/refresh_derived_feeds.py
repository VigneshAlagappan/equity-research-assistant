"""Background refresh of the stored, calculated Financials/Charts feeds
(derived_financial_feeds, see web/derived_feed_store.py).

Walks companies in priority order -- Nifty 50, then Next 50 + Midcap 150
(together with the first 50 = the Nifty 250 tier), then Smallcap 250 (the
rest of Nifty 500) -- and rebuilds every stored feed whose fingerprint is
stale or missing. Companies already current are skipped and don't count
toward the per-run limit, so each run does real work on up to
DEFAULT_LIMIT companies and, once everything is fresh, finishes almost
instantly. Companies outside Nifty 500 are filled on first view instead.

Runs only through the background scheduler (scheduling/jobs.py), never in
the user request path.

Usage: python -m scripts.refresh_derived_feeds [--limit 100]
"""

from __future__ import annotations

import argparse

from ingestion.batch_log import BatchRun
from storage.backend_bootstrap import open_db, open_price_db
from storage.company_repository import select_company_ids_by_index
from web.derived_feed_store import refresh_company

JOB_NAME = "derived_feeds_refresh"
DEFAULT_LIMIT = 100
PRIORITY_TIERS = ("Nifty 50", "Nifty Next 50", "Nifty Midcap 150", "Nifty Smallcap 250")


def priority_company_ids(conn) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for tier in PRIORITY_TIERS:
        for row in select_company_ids_by_index(conn, tier):
            if row["company_id"] not in seen:
                seen.add(row["company_id"])
                ordered.append(row["company_id"])
    return ordered


def run_derived_feeds_refresh(conn, limit: int = DEFAULT_LIMIT) -> int:
    """Returns the batch run_id, like the NSE batch runners."""
    companies = priority_company_ids(conn)
    price_conn = open_price_db()
    rebuilt_companies = 0
    skipped = 0
    try:
        with BatchRun(conn, JOB_NAME, f"Nifty 500 priority order, limit {limit}") as run:
            for company_id in companies:
                if rebuilt_companies >= limit:
                    break
                with run.item(company_id) as item:
                    n = refresh_company(conn, company_id, price_conn)
                    item.detail = f"rebuilt {n} feeds" if n else "already current"
                    if n:
                        rebuilt_companies += 1
                    else:
                        skipped += 1
            print(f"{JOB_NAME}: rebuilt={rebuilt_companies} already_current={skipped} limit={limit}", flush=True)
    finally:
        try:
            price_conn.close()
        except Exception:  # noqa: BLE001
            pass
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    conn = open_db()
    run_derived_feeds_refresh(conn, args.limit)


if __name__ == "__main__":
    main()
