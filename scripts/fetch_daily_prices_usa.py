"""US-scoped sibling of scripts/fetch_daily_prices.py: upsert the latest
closing price (and a few trailing days, for self-healing) for every US
company on file into the price-history db (config/settings.py's
PRICE_DB_PATH).

fetch_daily_prices.py's own docstring/SCHEDULED_JOBS.md both flag the gap
this closes: that script's universe query (company_index_membership's
"Nifty 500" tag) is NSE-specific, and sources/yfinance_prices.py's
fetch_daily_bars() itself is ticker-agnostic (it already takes a
`country` param that just decides whether to append ".NS" -- yfinance
covers US tickers fine without it). Only the universe query and the
missing `country="US"` argument were ever missing -- this script is that,
not a new fetch capability.

For a US company, `company_id` *is* the yfinance ticker (no separate
"us_ticker" column exists -- see companies.country/currency and
web/live_quote.py's own `company['nse_symbol'] or company_id` convention),
so there's no NSE-symbol-style join needed here, just companies WHERE
country = 'US'.

Usage: python -m scripts.fetch_daily_prices_usa
(a plain `python scripts/fetch_daily_prices_usa.py` fails on the
`storage`/`sources` imports below -- run as a module so the repo root, not
scripts/, lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

import time

from ingestion.batch_log import BatchRun
from sources.yfinance_prices import fetch_daily_bars
from storage.company_repository import select_active_companies_by_country
from storage.database import init_db
from storage.price_database import init_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.4
FETCH_PERIOD = "5d"


def run_price_history_update_usa(main_conn=None, price_conn=None) -> int:
    """The actual per-company fetch+upsert loop, factored out of main() so
    the Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) can trigger the identical job on demand -- same
    one-capability-two-triggers shape scripts/fetch_daily_prices.py's own
    run_price_history_update() already uses (that function is this one's
    template; keep the two in sync if either changes shape).

    No batching pause between companies here (unlike the India job) -- a
    dozen companies is nowhere near yfinance's soft rate limits, and adding
    BATCH_SIZE/BATCH_PAUSE_SECONDS machinery for a universe this small
    would be complexity with nothing to earn it. Revisit if the US company
    list grows into the hundreds.

    Takes both connections as optional params (each opened here if
    omitted), same reasoning as run_price_history_update(): the BatchRun
    audit trail below is opened on `main_conn` (the *main* db) even though
    the actual price upserts go through the separate price db --
    batch_job_runs/batch_job_items live in the main db.

    Returns the BatchRun's run_id."""
    owns_main_conn = main_conn is None
    if main_conn is None:
        main_conn = init_db()
    owns_price_conn = price_conn is None
    if price_conn is None:
        price_conn = init_price_db()

    try:
        rows = select_active_companies_by_country(main_conn, "US")
        total = len(rows)
        print(f"{total} US companies on file", flush=True)

        updated = no_data = errors = 0
        with BatchRun(main_conn, "price_history_usa", scope_label=f"US companies ({total})") as run:
            for i, row in enumerate(rows, 1):
                company_id = row["company_id"]
                with run.item(company_id) as item:
                    try:
                        bars = fetch_daily_bars(company_id, period=FETCH_PERIOD, country="US")
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

        print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)
        return run.run_id
    finally:
        if owns_price_conn:
            price_conn.close()
        if owns_main_conn:
            main_conn.close()


def main() -> None:
    run_price_history_update_usa()


if __name__ == "__main__":
    main()
