"""Daily job: upsert the latest closing price (and a few trailing days, for
self-healing) for every NSE 500 company into the price-history db
(config/settings.py's PRICE_DB_PATH).

Reads the NSE 500 ticker universe from the *main* db (companies +
company_index_membership, already populated by companies/nse_import.py) --
this script never writes to that db, only reads from it. Fetches a 5-day
window per company rather than just "yesterday" so a missed run (weekend,
transient failure, the job not running for a few days) self-heals on the
next run instead of leaving a gap that needs separate reconciliation --
upserts make re-fetching overlapping days free (storage/price_repository.py's
upsert_daily_bar, keyed on (company_id, trade_date)).

Batched in groups (BATCH_SIZE) with a pause between batches, gentler on
yfinance's soft rate limits than one continuous loop over ~500 tickers.
Idempotent-safe to interrupt and re-run, same philosophy as
scripts/backfill_sector_industry.py.

Usage: python -m scripts.fetch_daily_prices
(a plain `python scripts/fetch_daily_prices.py` fails on the `storage`/
`sources` imports below -- run as a module so the repo root, not scripts/,
lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

import time

from sources.yfinance_prices import fetch_daily_bars
from storage.database import init_db
from storage.price_database import init_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.4
BATCH_SIZE = 25
BATCH_PAUSE_SECONDS = 5
FETCH_PERIOD = "5d"


def main() -> None:
    main_conn = init_db()
    rows = main_conn.execute(
        """
        SELECT c.company_id, c.nse_symbol
        FROM companies c
        JOIN company_index_membership m ON m.company_id = c.company_id
        WHERE m.index_name = 'Nifty 500' AND c.nse_symbol IS NOT NULL
        ORDER BY c.company_id
        """
    ).fetchall()
    main_conn.close()

    total = len(rows)
    print(f"{total} Nifty 500 companies with an nse_symbol on file", flush=True)

    price_conn = init_price_db()
    updated = no_data = errors = 0

    for i, (company_id, nse_symbol) in enumerate(rows, 1):
        try:
            bars = fetch_daily_bars(nse_symbol, period=FETCH_PERIOD)
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
            latest = bars[-1]
            print(f"[{i}/{total}] {company_id:24s} rows={len(bars)} latest={latest.trade_date}", flush=True)

        time.sleep(REQUEST_DELAY_SECONDS)
        if i % BATCH_SIZE == 0 and i < total:
            time.sleep(BATCH_PAUSE_SECONDS)

    price_conn.close()
    print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)


if __name__ == "__main__":
    main()
