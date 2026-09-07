"""Backfill/reconcile daily OHLCV history into the price-history db
(config/settings.py's PRICE_DB_PATH), for either NSE 500 (default) or every
registered US company (--country US) -- same idempotent-upsert approach as
scripts/fetch_daily_prices.py / scripts/fetch_daily_prices_usa.py, but pulls
a full historical window per company instead of a trailing few days -- for
the initial backfill, or to re-pull a wider range later (e.g. moving from
--period 1y to --period 10y).

--country US closes a real gap this script left open until now: US
companies only ever got scripts/fetch_daily_prices_usa.py's 5-day daily
top-up (FETCH_PERIOD="5d"), with nothing to pull deep history for a newly
registered one -- verified live: 13 of 25 registered US companies (every
one added after the original AAPL/AMZN/etc. batch, which got its history
from an earlier one-off pull, not a repeatable mechanism) had only 5 days
on file, making their "All-Time Range" display meaningless.

A full history pull is a heavier per-call cost than the daily job's 5-day
window, so batches use a longer pause between groups.

Usage (run as a module -- a plain `python scripts/backfill_price_history.py`
fails on the `storage`/`sources` imports below, since sys.path[0] then
resolves to scripts/, not the repo root):
    python -m scripts.backfill_price_history --period 1y
    python -m scripts.backfill_price_history --period 10y --company-id RELIANCE
    python -m scripts.backfill_price_history --country US --period 10y
    python -m scripts.backfill_price_history --country US --period max --company-id AAPL
"""

from __future__ import annotations

import argparse
import time

from sources.yfinance_prices import fetch_daily_bars
from storage.company_repository import select_active_companies_by_country, select_index_members_with_nse_symbol
from storage.database import init_db
from storage.price_database import init_price_db
from storage.price_repository import upsert_daily_bars

REQUEST_DELAY_SECONDS = 0.6
BATCH_SIZE = 25
BATCH_PAUSE_SECONDS = 15


def _resolve_ticker_pairs(main_conn, country: str, company_id: str | None) -> list[tuple[str, str]]:
    """(company_id, yfinance_ticker) pairs for the requested universe --
    India's own nse_symbol column for country="IN" (unchanged from before
    this function existed), or company_id itself for country="US" (that's
    already the yfinance ticker convention -- see
    scripts/fetch_daily_prices_usa.py's own docstring; fetch_daily_bars's
    own country="US" branch/US_TICKER_OVERRIDES handles the handful that
    need translating, e.g. BRKA -> BRK-A)."""
    if country == "IN":
        return list(select_index_members_with_nse_symbol(main_conn, "Nifty 500", company_id=company_id))
    rows = select_active_companies_by_country(main_conn, country)
    return [(r["company_id"], r["company_id"]) for r in rows if company_id is None or r["company_id"] == company_id]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", choices=["1y", "5y", "10y", "max"], default="1y")
    parser.add_argument("--company-id", default=None, help="Limit the run to a single company_id")
    parser.add_argument("--country", choices=["IN", "US"], default="IN")
    args = parser.parse_args()

    main_conn = init_db()
    rows = _resolve_ticker_pairs(main_conn, args.country, args.company_id)
    main_conn.close()

    total = len(rows)
    print(f"{total} companies to backfill at period={args.period!r} country={args.country!r}", flush=True)

    price_conn = init_price_db()
    updated = no_data = errors = 0

    for i, (company_id, ticker) in enumerate(rows, 1):
        try:
            bars = fetch_daily_bars(ticker, period=args.period, country=args.country)
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
        if i % BATCH_SIZE == 0 and i < total:
            time.sleep(BATCH_PAUSE_SECONDS)

    price_conn.close()
    print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)


if __name__ == "__main__":
    main()
