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
    python -m scripts.backfill_price_history_usa --start 2015-01-01 --company-id AAPL
    python -m scripts.backfill_price_history_usa --start 2015-01-01 --workers 3

--workers > 1 fans the per-company yfinance fetch + upsert out across a
thread pool (I/O-bound: the GIL is released during both the yfinance HTTP
call and psycopg2's network round-trip, so real concurrency here, not just
interleaving). Each worker thread gets its own price-db connection
(thread-local, opened lazily on that thread's first company) rather than
sharing one -- psycopg2 connections aren't safe to use from multiple
threads at once.
"""

from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from sources.yfinance_prices import fetch_daily_bars
from storage.company_repository import select_active_companies_by_country
from storage.backend_bootstrap import open_db, open_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.6

_thread_local = threading.local()
_print_lock = threading.Lock()


def _thread_price_conn(*, fresh: bool = False):
    """Lazily opens (and caches) this thread's own connection. `fresh=True`
    discards a stale one and opens a replacement -- Neon's pooler closes
    idle-too-long connections out from under a long-running thread pool,
    same reconnect-once pattern scripts/register_sp500_companies.py already
    uses for its single connection."""
    conn = getattr(_thread_local, "conn", None)
    if conn is None or fresh:
        conn = open_price_db()
        _thread_local.conn = conn
    return conn


def _is_stale_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return name in ("OperationalError", "InterfaceError") and (
        "server closed the connection" in text
        or "connection already closed" in text
        or "terminat" in text.lower()
    )


def _process_company(company_id: str, ticker: str, *, period: str | None, start: str | None) -> str:
    """Fetch + upsert one company's history; returns an outcome string
    ("updated" | "no_data" | "error") for the caller to tally."""
    try:
        # country="US" -- both the ".NS"-suffix skip and the Berkshire
        # BRKB->BRK-B / Brown-Forman BFB->BF-B overrides
        # (sources/yfinance_prices.py's US_TICKER_OVERRIDES) go through the
        # same resolve_yfinance_ticker() this hits internally, same as the
        # daily job. `ticker` is already resolved to fetch_symbol for the
        # handful of company_ids disambiguated from a pre-existing Indian
        # one (e.g. "PNC-US" vs the real ticker "PNC").
        bars = fetch_daily_bars(ticker, period=period, start=start, country="US")
    except Exception as exc:  # noqa: BLE001 -- one bad ticker must not abort the rest of the pool
        with _print_lock:
            print(f"{company_id:24s} ERROR {exc}", flush=True)
        time.sleep(REQUEST_DELAY_SECONDS)
        return "error"

    if not bars:
        with _print_lock:
            print(f"{company_id:24s} no price data", flush=True)
        time.sleep(REQUEST_DELAY_SECONDS)
        return "no_data"

    rows = [
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
    ]
    try:
        upsert_daily_bars(_thread_price_conn(), rows)
    except Exception as exc:  # noqa: BLE001 -- one bad/stale connection must not abort the rest of the pool
        if not _is_stale_connection_error(exc):
            with _print_lock:
                print(f"{company_id:24s} ERROR (upsert) {exc}", flush=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            return "error"
        with _print_lock:
            print(f"{company_id:24s} DB connection went stale -- reopening and retrying once", flush=True)
        try:
            upsert_daily_bars(_thread_price_conn(fresh=True), rows)
        except Exception as exc2:  # noqa: BLE001
            with _print_lock:
                print(f"{company_id:24s} ERROR (upsert retry) {exc2}", flush=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            return "error"

    with _print_lock:
        print(f"{company_id:24s} rows={len(bars)} {bars[0].trade_date}..{bars[-1].trade_date}", flush=True)
    time.sleep(REQUEST_DELAY_SECONDS)
    return "updated"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", choices=["1y", "5y", "10y", "max"], default=None)
    parser.add_argument("--start", default=None, help="Explicit ISO start date, e.g. 2015-01-01 (mutually exclusive with --period)")
    parser.add_argument("--company-id", default=None, help="Limit the run to a single company_id")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent worker threads (default 1 = sequential)")
    args = parser.parse_args()
    if not args.period and not args.start:
        args.period = "10y"

    main_conn = open_db()
    rows = select_active_companies_by_country(main_conn, "US")
    main_conn.close()
    tickers_by_company_id = {r["company_id"]: (r["fetch_symbol"] or r["company_id"]) for r in rows}
    company_ids = list(tickers_by_company_id)
    if args.company_id:
        company_ids = [c for c in company_ids if c == args.company_id.upper()]

    total = len(company_ids)
    window = f"start={args.start!r}" if args.start else f"period={args.period!r}"
    print(f"{total} US companies to backfill at {window}, workers={args.workers}", flush=True)

    updated = no_data = errors = 0
    done = 0

    if args.workers <= 1:
        for company_id in company_ids:
            outcome = _process_company(
                company_id, tickers_by_company_id[company_id], period=args.period, start=args.start
            )
            done += 1
            if outcome == "updated":
                updated += 1
            elif outcome == "no_data":
                no_data += 1
            else:
                errors += 1
            with _print_lock:
                print(f"  [{done}/{total}]", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    _process_company,
                    company_id,
                    tickers_by_company_id[company_id],
                    period=args.period,
                    start=args.start,
                ): company_id
                for company_id in company_ids
            }
            for future in as_completed(futures):
                company_id = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 -- belt-and-suspenders: _process_company already catches
                    # its own errors, but no exception escaping here may ever kill the whole pool
                    with _print_lock:
                        print(f"{company_id:24s} UNEXPECTED ERROR {exc}", flush=True)
                    outcome = "error"
                done += 1
                if outcome == "updated":
                    updated += 1
                elif outcome == "no_data":
                    no_data += 1
                else:
                    errors += 1
                with _print_lock:
                    print(f"  [{done}/{total}]", flush=True)

    print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)


if __name__ == "__main__":
    main()
