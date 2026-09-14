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

import json
import time

from ingestion.batch_log import BatchRun
from sources.yfinance_prices import fetch_daily_bars
from storage import raw_object_repository as ror
from storage.backend_bootstrap import open_db, open_price_db
from storage.company_repository import select_index_members_with_nse_symbol
from storage.price_repository import upsert_daily_bars
from storage.raw_object_store import store_raw_object

REQUEST_DELAY_SECONDS = 0.4
BATCH_SIZE = 25
BATCH_PAUSE_SECONDS = 5
FETCH_PERIOD = "5d"


def run_price_history_update(
    main_conn=None, price_conn=None, *, index_name: str = "Nifty 500", job_name: str = "price_history_india"
) -> int:
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

    `index_name`/`job_name` default to this job's original Nifty 500/
    price_history_india scope so every existing zero-arg caller keeps its
    current behavior -- web/app.py's Nifty Micro-Cap tier closure is the
    first caller to override either.

    main_conn is opened via storage.backend_bootstrap.open_db() (not
    storage.database.init_db() directly) -- select_index_members_with_
    nse_symbol below resolves to company_repository_pg's Postgres-
    flavored version once DATABASE_BACKEND=postgres (a process-wide
    sys.modules swap, not something this function controls), so main_conn
    must be on that same backend or every call against it breaks (found
    this the hard way in fetch_daily_prices_usa.py's sibling function:
    `'sqlite3.Cursor' object does not support the context manager
    protocol` the moment "Run now" was clicked against a Postgres-backed
    deployment). BatchRun below still gets its own dedicated SQLite
    connection either way (see ingestion/batch_log.py's docstring) --
    unaffected by this.

    Returns the BatchRun's run_id."""
    owns_main_conn = main_conn is None
    if main_conn is None:
        main_conn = open_db()
    owns_price_conn = price_conn is None
    if price_conn is None:
        price_conn = open_price_db()

    try:
        rows = select_index_members_with_nse_symbol(main_conn, index_name)
        total = len(rows)
        print(f"{total} {index_name} companies with an nse_symbol on file", flush=True)

        updated = no_data = errors = 0
        with BatchRun(main_conn, job_name, scope_label=f"{index_name} ({total} companies)") as run:
            for i, row in enumerate(rows, 1):
                # Dict-style access, not positional tuple-unpacking --
                # sqlite3.Row iterates by VALUE (so `company_id, nse_symbol
                # = row` used to work by accident), but psycopg2's
                # RealDictRow iterates by KEY once DATABASE_BACKEND=
                # postgres, silently unpacking the literal strings
                # "company_id"/"nse_symbol" instead of the row's actual
                # data (found this the hard way: every company came back
                # as ticker "NSE_SYMBOL.NS", not a real symbol). row["..."]
                # works identically on both backends' Row types.
                company_id = row["company_id"]
                nse_symbol = row["nse_symbol"]
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
                        # ADR-022: land the fetched bars in raw/market-data/
                        # BEFORE the upsert below touches daily_prices --
                        # dedup-by-hash means a re-fetch that returns the
                        # exact same window (e.g. the job re-run same-day)
                        # never creates a duplicate raw object, only a new
                        # one when the actual bars changed (a fresh trading
                        # day, a corrected close). raw_object's own conn is
                        # main_conn (the catalog lives wherever companies/
                        # batch_job_runs do), not price_conn.
                        period = f"{bars[0].trade_date}..{bars[-1].trade_date}"
                        raw_payload = json.dumps(
                            [
                                {
                                    "trade_date": bar.trade_date, "open": bar.open, "high": bar.high,
                                    "low": bar.low, "close": bar.close, "volume": bar.volume,
                                }
                                for bar in bars
                            ],
                            sort_keys=True,
                        ).encode("utf-8")
                        raw_result = store_raw_object(
                            main_conn, source="yfinance_prices", entity=company_id, object_type="ohlcv_batch",
                            period=period, source_url=None, raw_prefix="market-data",
                            content=raw_payload, extension="json",
                        )

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
                        ror.update_raw_object_state(main_conn, raw_result.object_id, state="ingested", mark_processed=True)
                        ror.insert_lineage(
                            main_conn, object_id=raw_result.object_id, derived_store="price_db",
                            derived_table="daily_prices", derived_record_id=f"{company_id}:{period}",
                        )
                        updated += 1
                        latest = bars[-1]
                        print(f"[{i}/{total}] {company_id:24s} rows={len(bars)} latest={latest.trade_date}", flush=True)
                        item.detail = f"updated rows={len(bars)} latest={latest.trade_date} raw_object_id={raw_result.object_id}"

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
