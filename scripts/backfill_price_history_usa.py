"""US-scoped sibling of scripts/backfill_price_history.py: backfill/
reconcile a full historical OHLCV window for every US company on file into
the price-history db (config/settings.py's PRICE_DB_PATH), instead of the
daily job's trailing 5-day window (scripts/fetch_daily_prices_usa.py).

Without this, "52W Range" and "All-Time Range" on the Companies list are
silently wrong for every US company, not just missing -- storage/
price_repository.py's list_52_week_range()/list_all_time_range() just take
MIN/MAX(close) over whatever's in daily_prices, with no floor on how much
history that actually is. Verified as a real, live gap: before this script
ever ran, every US company had exactly 5 rows on file (2026-08-31..
2026-09-04, from the daily job's own trailing window), so both columns
showed the same 5-day range under two different labels -- not an
approximation of 52 weeks or all-time, just wrong. India never had this
problem because scripts/backfill_price_history.py already existed
alongside its own daily job; USA only had the daily half until now.

Usage (run as a module -- a plain `python scripts/backfill_price_history_
usa.py` fails on the `storage`/`sources` imports below, since sys.path[0]
then resolves to scripts/, not the repo root):
    python -m scripts.backfill_price_history_usa --period 10y
    python -m scripts.backfill_price_history_usa --period max --company-id AAPL
"""

from __future__ import annotations

import argparse
import time

from sources.yfinance_prices import fetch_daily_bars
from storage.company_repository import select_active_companies_by_country
from storage.database import init_db
from storage.price_database import init_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", choices=["1y", "5y", "10y", "max"], default="10y")
    parser.add_argument("--company-id", default=None, help="Limit the run to a single company_id")
    args = parser.parse_args()

    main_conn = init_db()
    rows = select_active_companies_by_country(main_conn, "US")
    main_conn.close()
    company_ids = [r["company_id"] for r in rows]
    if args.company_id:
        company_ids = [c for c in company_ids if c == args.company_id.upper()]

    total = len(company_ids)
    print(f"{total} US companies to backfill at period={args.period!r}", flush=True)

    price_conn = init_price_db()
    updated = no_data = errors = 0

    for i, company_id in enumerate(company_ids, 1):
        try:
            # country="US" -- both the ".NS"-suffix skip and the Berkshire
            # BRKB->BRK-B override (sources/yfinance_prices.py's
            # US_TICKER_OVERRIDES) go through the same resolve_yfinance_
            # ticker() this hits internally, same as the daily job.
            bars = fetch_daily_bars(company_id, period=args.period, country="US")
        except Exception as exc:
            errors += 1
            print(f"[{i}/{total}] {company_id:24s} ERROR {exc}", flush=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not bars:
            no_data += 1
            print(f"[{i}/{total}] {company_id:24s} no price data", flush=True)
        else:
            upsert_daily_bars(
                price_conn,
                (
                    {
                        "company_id": company_id,
                        "trade_date": bar.trade_date,
                        "open_": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                    for bar in bars
                ),
            )
            updated += 1
            print(
                f"[{i}/{total}] {company_id:24s} rows={len(bars)} "
                f"{bars[0].trade_date}..{bars[-1].trade_date}",
                flush=True,
            )

        time.sleep(REQUEST_DELAY_SECONDS)

    price_conn.close()
    print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)


if __name__ == "__main__":
    main()
