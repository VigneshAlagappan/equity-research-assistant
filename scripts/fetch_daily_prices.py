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

from ingestion.batch_log import BatchRun
from sources.yfinance_prices import fetch_daily_bars
from storage.company_repository import select_index_members_with_nse_symbol
from storage.database import init_db
from storage.price_database import init_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.4
BATCH_SIZE = 25
BATCH_PAUSE_SECONDS = 5
FETCH_PERIOD = "5d"


def run_price_history_update(main_conn=None, price_conn=None) -> int:
    """The actual per-company fetch+upsert loop, factored out of main() so
    the Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) can trigger the identical daily job on demand -- one
    capability, two triggers, same reuse shape as admin_refresh_company()'s
    own docstring describes.

    Takes both connections as optional params (each opened here if omitted)
    rather than always opening its own, so a caller that already holds one
    (the CLI's main() below, or a future test) doesn't pay for a second
    connection. Important: the BatchRun audit trail below is opened on
    `main_conn` (the *main* db), not `price_conn` -- batch_job_runs/
    batch_job_items live in the main db per ingestion/batch_log.py's other
    callers (main.py), even though this job's actual price upserts go
    through the separate price db. Get this backwards and the run would
    silently write its audit rows into a db nothing else queries them from.

    Returns the BatchRun's run_id."""
    owns_main_conn = main_conn is None
    if main_conn is None:
        main_conn = init_db()
    owns_price_conn = price_conn is None
    if price_conn is None:
        price_conn = init_price_db()

    try:
        rows = select_index_members_with_nse_symbol(main_conn, "Nifty 500")
        total = len(rows)
        print(f"{total} Nifty 500 companies with an nse_symbol on file", flush=True)

        updated = no_data = errors = 0
        with BatchRun(main_conn, "price_history_india", scope_label=f"Nifty 500 ({total} companies)") as run:
            for i, (company_id, nse_symbol) in enumerate(rows, 1):
                with run.item(company_id) as item:
                    try:
                        bars = fetch_daily_bars(nse_symbol, period=FETCH_PERIOD)
                    except Exception as exc:
                        errors += 1
                        print(f"[{i}/{total}] {company_id:24s} ERROR {exc}", flush=True)
                        time.sleep(REQUEST_DELAY_SECONDS)
                        raise

                    if not bars:
                        no_data += 1
                        print(f"[{i}/{total}] {company_id:24s} no price data", flush=True)
                        item.detail = "no data"
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
                        item.detail = f"updated rows={len(bars)} latest={latest.trade_date}"

                    time.sleep(REQUEST_DELAY_SECONDS)
                    if i % BATCH_SIZE == 0 and i < total:
                        time.sleep(BATCH_PAUSE_SECONDS)

        print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)
        return run.run_id
    finally:
        if owns_price_conn:
            price_conn.close()
        if owns_main_conn:
            main_conn.close()


def main() -> None:
    run_price_history_update()


if __name__ == "__main__":
    main()
