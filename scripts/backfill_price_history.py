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

--index-name/--all-tiers close a second gap: the original version always
pulled the blanket "Nifty 500" membership regardless of what a caller
actually wanted, so there was no way to backfill just one market-cap tier
(e.g. re-pulling only Nifty Micro-Cap, which sits outside Nifty 500
entirely -- see scheduling/jobs.py's price_history_india vs
price_history_india_nifty_microcap split). --all-tiers runs the five
standard NSE tiers (Nifty 50, Nifty Next 50, Nifty Midcap 150, Nifty
Smallcap 250, Nifty Micro-Cap) back to back in one invocation, each as its
own pass with its own summary line -- these five are mutually exclusive
company sets (unlike "Nifty 500", which is itself the union of the first
four), so nothing is fetched twice.

--years closes a third gap: yfinance's own period vocabulary
(fetch_daily_bars's period=) only has 1y/2y/5y/10y/max/ytd -- no "3y" --
so an exact N-year window has to go through fetch_daily_bars's other mode
instead, an explicit start= date (today minus N years), mutually exclusive
with --period.

run_price_history_backfill() (one tier per call, BatchRun-audited) is what
scheduling/jobs.py's five price_history_backfill_* (India) + one
price_history_backfill_usa job call so this is also triggerable from
Settings > Data Operations > Schedule's "Run now" button, one tier/country
at a time -- same "one function, several call sites" shape scripts/
fetch_daily_prices.py's run_price_history_update() already established for
the daily job. The CLI below (including --all-tiers) is just that same
function called once per tier in a loop, so a CLI run gets an identical
audit trail (Audit Log -> Job Runs) to a Schedule-panel-triggered one.

Resume-if-interrupted: this is a slow, several-hundred/thousand-company
job (Nifty Micro-Cap alone is ~2,000 companies), run via a "Run now" click
that's synchronous and bound by gunicorn's worker timeout (120s, see the
Dockerfile) -- meaning a bigger tier WILL routinely get cut off mid-run in
practice, not just on a rare crash. Recovering from that without redoing
already-fetched companies matters here more than for the smaller NSE/
financials jobs.

Unlike scripts/batch_fetch_sec_edgar.py's "succeeded recently" TTL skip
(a coarse audit-log timestamp that says nothing about which dates that
success actually covered), this checks the real stored data:
storage.price_repository.list_earliest_trade_dates() reads daily_prices'
own MIN(trade_date) per company, and a company whose earliest bar already
reaches back to (or past) the requested --years start date is skipped
outright -- it has nothing left to backfill for this window, full stop,
regardless of how long ago that data was fetched. This is the "check what
data exists for the period, only fetch what's missing" principle applied
directly against the data itself rather than an audit-log proxy for it.
So an interrupted run, whether auto-replayed by web/app.py's _resume_
interrupted_batch_jobs() on the next server restart or a human just
clicking "Run now" again, picks up only the companies still missing
coverage -- --force bypasses this for a deliberate full re-pull. Only
applies to --years (an exact, comparable start date); a bare --period
call (no explicit start date to compare against) always fetches, same as
before.

Usage (run as a module -- a plain `python scripts/backfill_price_history.py`
fails on the `storage`/`sources` imports below, since sys.path[0] then
resolves to scripts/, not the repo root):
    python -m scripts.backfill_price_history --period 1y
    python -m scripts.backfill_price_history --period 10y --company-id RELIANCE
    python -m scripts.backfill_price_history --country US --period 10y
    python -m scripts.backfill_price_history --country US --period max --company-id AAPL
    python -m scripts.backfill_price_history --years 3 --index-name "Nifty 50"
    python -m scripts.backfill_price_history --years 3 --all-tiers
    python -m scripts.backfill_price_history --years 3 --country US
    python -m scripts.backfill_price_history --years 3 --all-tiers --force  # bypass the skip-if-recent check
    python -m scripts.backfill_price_history --years 20 --index-name "Nifty Micro-Cap" --time-budget-minutes 25
"""

from __future__ import annotations

import argparse
import time
from datetime import date

# Must run before any other import in this file touches storage.company_
# repository/price_repository/etc. -- see storage/backend_bootstrap.py's
# own docstring for why, and scripts/run_job.py's identical top-of-file
# comment: `from storage.company_repository import X` below binds X to
# whatever module object sys.modules["storage.company_repository"] holds
# AT IMPORT TIME. Calling install() later (e.g. only inside open_db(),
# the first time this script actually opens a connection) is too late --
# X is already bound to the pre-swap SQLite functions in this module's own
# namespace by then, so a Postgres-backed run crashes with
# `AttributeError: 'psycopg2.extensions.connection' object has no
# attribute 'execute'` the moment it calls select_index_members_with_nse_
# symbol()/select_active_companies_by_country() -- found this the hard
# way running `python -m scripts.backfill_price_history --years 3
# --all-tiers` directly against DATABASE_BACKEND=postgres (this script's
# own run_price_history_backfill() was already safe when called via
# scripts/run_job.py or the Schedule panel, since both of those install()
# before scheduling.jobs -- and therefore this module -- ever gets
# imported; only a direct `python -m scripts.backfill_price_history`
# invocation hit this).
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from ingestion.batch_log import BatchRun
from sources.yfinance_prices import fetch_daily_bars
from storage.backend_bootstrap import open_db, open_price_db
from storage.company_repository import select_active_companies_by_country, select_index_members_with_nse_symbol
from storage.price_repository import list_earliest_trade_dates, upsert_daily_bars

# Tuned to spread a full --all-tiers (+ USA) run (~2,589 companies as of
# this writing, Nifty 50/Next 50/Midcap 150/Smallcap 250/Micro-Cap + USA)
# over roughly 2 hours -- gentler than the original 0.6s/25-company-batch
# pacing (~50 min total), deliberately loosened once this script started
# running from a shared cloud IP (AWS Lightsail) rather than a home
# connection, where the same request volume is more likely to trip
# Yahoo's anti-bot/rate-limit heuristics. `--company-id`-scoped or small
# single-tier runs finish proportionally faster; this budget is sized for
# the full combined run, not any one invocation.
REQUEST_DELAY_SECONDS = 1.6
BATCH_SIZE = 25
BATCH_PAUSE_SECONDS = 30

#: The five standard NSE market-cap tiers, mutually exclusive, together
#: covering the same universe as "Nifty 500" (the first four) plus Nifty
#: Micro-Cap (which sits outside Nifty 500) -- exact company_index_
#: membership.index_name strings, same ones scheduling/jobs.py's
#: _make_nse_tier_runner()-based runners already use.
NSE_TIERS = ("Nifty 50", "Nifty Next 50", "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty Micro-Cap")

#: batch_job_runs.job_name for each tier's backfill -- shared between this
#: script's own CLI and scheduling/jobs.py's price_history_backfill_* jobs
#: (Settings > Data Operations > Schedule's "Run now" button) so both call
#: paths land in the same Audit Log -> Job Runs bucket per tier, and
#: "last run"/skip-if-recent lookups (job_name-keyed) see either trigger's
#: runs.
TIER_JOB_NAMES: dict[str, str] = {
    "Nifty 50": "price_history_backfill_nifty50",
    "Nifty Next 50": "price_history_backfill_nifty_next50",
    "Nifty Midcap 150": "price_history_backfill_nifty_midcap150",
    "Nifty Smallcap 250": "price_history_backfill_nifty_smallcap250",
    "Nifty Micro-Cap": "price_history_backfill_nifty_microcap",
}

#: Same idea as TIER_JOB_NAMES, for the country="US" backfill (no tiering
#: there -- see _resolve_ticker_pairs).
US_JOB_NAME = "price_history_backfill_usa"


def _resolve_ticker_pairs(main_conn, country: str, company_id: str | None, index_name: str) -> list[tuple[str, str]]:
    """(company_id, yfinance_ticker) pairs for the requested universe --
    India's own nse_symbol column for country="IN" (unchanged from before
    this function existed), or company_id itself for country="US" (that's
    already the yfinance ticker convention -- see
    scripts/fetch_daily_prices_usa.py's own docstring; fetch_daily_bars's
    own country="US" branch/US_TICKER_OVERRIDES handles the handful that
    need translating, e.g. BRKA -> BRK-A). `index_name` only applies to
    country="IN" -- US has no equivalent tiering here."""
    if country == "IN":
        # Dict-style access, not positional tuple-unpacking -- sqlite3.Row
        # iterates by VALUE (so `company_id_, ticker = row` used to work by
        # accident), but psycopg2's RealDictRow iterates by KEY once
        # DATABASE_BACKEND=postgres, silently unpacking the literal strings
        # "company_id"/"nse_symbol" instead of the row's actual data --
        # same trap scripts/fetch_daily_prices.py's own comment already
        # flags for its identical loop. Build explicit tuples here so the
        # caller's `for company_id_, ticker in rows:` never has to unpack a
        # raw Row at all.
        rows = select_index_members_with_nse_symbol(main_conn, index_name, company_id=company_id)
        return [(r["company_id"], r["nse_symbol"]) for r in rows]
    rows = select_active_companies_by_country(main_conn, country)
    return [(r["company_id"], r["company_id"]) for r in rows if company_id is None or r["company_id"] == company_id]


def run_price_history_backfill(
    main_conn=None, price_conn=None, *, index_name: str = "Nifty 500", years: int | None = None,
    period: str | None = None, country: str = "IN", company_id: str | None = None,
    job_name: str = "price_history_backfill", scope_label: str | None = None, force: bool = False,
    time_budget_seconds: float | None = None,
) -> int:
    """One backfill pass over one ticker universe, BatchRun-audited --
    same connection-ownership and BatchRun shape as scripts/fetch_daily_
    prices.py's run_price_history_update() (see that function's own
    docstring for why main_conn/price_conn are separate, each opened here
    when omitted, and why BatchRun's own SQLite-only audit connection is
    unaffected by which backend main_conn is on).

    Pass exactly one of `years` (an exact N-year window ending today) or
    `period` (yfinance's own rolling-window vocabulary); defaults to
    period="1y" if neither is given, same as fetch_daily_bars itself.

    Skips any company whose earliest stored trade_date is already <= the
    requested --years start date (force=True bypasses this) -- see this
    module's own docstring for why checking real coverage, not an
    audit-log "succeeded recently" proxy, matters here: an interrupted run
    (gunicorn's 120s worker timeout on a "Run now" click, or a real crash)
    resumes cheaply, whether replayed automatically at next server start
    or by a human re-clicking "Run now" -- either way it's the exact same
    function, just called again, with no separate "which companies are
    left" bookkeeping to maintain. A skipped company still gets its own
    run.item(...) entry (status 'ok', detail explaining why) so Audit Log
    -> Job Runs shows every company considered, not just the ones
    actually re-fetched this pass. Only applies when years is given (a
    concrete date to compare stored coverage against) -- a bare `period`
    call always fetches, same as before this function had a skip check.

    A company with SOME data already on file (earlier `--years N` run, or
    the daily job's own trailing window) but not yet covering back to
    `start` fetches only the missing OLDER gap -- `[start, earliest_on_
    file)`, via fetch_daily_bars's new `end` param -- never re-pulling
    days already on file between `earliest_on_file` and today. A company
    with nothing on file yet still gets the full `start`..today pull (no
    `end`), same as before.

    `time_budget_seconds`, when given, makes this resumable across
    scheduled runs rather than one long blocking pull: once elapsed time
    since the loop started exceeds the budget, the loop stops immediately
    -- companies not yet reached this run are simply left as-is (their
    on-file coverage hasn't changed, so they still read as "needs more"
    next time this same job fires) and pick up automatically on the next
    scheduled invocation, no separate checkpoint/offset to persist. Built
    for exactly this: EventBridge-scheduled weekly runs, each tier given a
    fixed slot (e.g. 15-30 minutes) before the next tier's rule fires, so
    a 20-year Nifty Micro-Cap backfill across ~2,000 companies completes
    gradually over several weeks instead of blowing through its slot (or
    gunicorn's 120s worker timeout on a manual "Run now") in one run.

    Returns the BatchRun's run_id."""
    if years is not None and period is not None:
        raise ValueError("run_price_history_backfill: pass only one of years or period, not both")
    start = None
    if years is not None:
        start = date(date.today().year - years, date.today().month, date.today().day).isoformat()
    elif period is None:
        period = "1y"

    owns_main_conn = main_conn is None
    if main_conn is None:
        main_conn = open_db()
    owns_price_conn = price_conn is None
    if price_conn is None:
        price_conn = open_price_db()

    label = scope_label or (index_name if country == "IN" else f"{country} companies")
    try:
        rows = _resolve_ticker_pairs(main_conn, country, company_id, index_name)
        total = len(rows)
        print(
            f"\n=== {label}: {total} companies (period={period!r} start={start!r} "
            f"time_budget_seconds={time_budget_seconds!r}) ===",
            flush=True,
        )

        earliest_on_file = {}
        if start is not None and not force:
            earliest_on_file = list_earliest_trade_dates(price_conn, [c for c, _ in rows])

        loop_started = time.monotonic()
        updated = no_data = errors = skipped = deferred = 0
        with BatchRun(main_conn, job_name, scope_label=f"{label} ({total})") as run:
            for i, (company_id_, ticker) in enumerate(rows, 1):
                if time_budget_seconds is not None and time.monotonic() - loop_started >= time_budget_seconds:
                    deferred = total - i + 1
                    print(
                        f"[{i}/{total}] time budget ({time_budget_seconds:.0f}s) reached -- "
                        f"stopping early, {deferred} compan{'y' if deferred == 1 else 'ies'} "
                        f"deferred to the next scheduled run",
                        flush=True,
                    )
                    break

                earliest = earliest_on_file.get(company_id_)
                if earliest is not None and earliest <= start:
                    with run.item(company_id_) as item:
                        item.detail = f"skipped (already have data back to {earliest}, covers requested start {start})"
                    skipped += 1
                    print(f"[{i}/{total}] {company_id_:24s} SKIPPED (covered back to {earliest})", flush=True)
                    continue

                # A company with SOME coverage already gets only the missing
                # older gap fetched (start..earliest), not a full re-pull of
                # days it already has -- earliest itself is exclusive of the
                # gap (already on file), so `end` is earliest, not earliest-1;
                # yfinance's own `end` is exclusive of that date too.
                fetch_start = start
                fetch_end = earliest if (earliest is not None and start is not None) else None

                with run.item(company_id_) as item:
                    try:
                        bars = fetch_daily_bars(ticker, period=period, start=fetch_start, end=fetch_end, country=country)
                    except Exception as exc:
                        errors += 1
                        print(f"[{i}/{total}] {company_id_:24s} ERROR {exc}", flush=True)
                        time.sleep(REQUEST_DELAY_SECONDS)
                        raise

                    if not bars:
                        no_data += 1
                        print(f"[{i}/{total}] {company_id_:24s} no price data", flush=True)
                        item.detail = "no data"
                    else:
                        upsert_daily_bars(
                            price_conn,
                            (
                                {
                                    "company_id": company_id_,
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
                            f"[{i}/{total}] {company_id_:24s} rows={len(bars)} "
                            f"{bars[0].trade_date}..{bars[-1].trade_date}",
                            flush=True,
                        )
                        item.detail = f"updated rows={len(bars)} latest={bars[-1].trade_date}"

                    time.sleep(REQUEST_DELAY_SECONDS)
                    if i % BATCH_SIZE == 0 and i < total:
                        time.sleep(BATCH_PAUSE_SECONDS)

        print(
            f"--- {label} done. updated={updated} no_data={no_data} errors={errors} "
            f"skipped={skipped} deferred={deferred} total={total} ---",
            flush=True,
        )
        return run.run_id
    finally:
        if owns_price_conn:
            price_conn.close()
        if owns_main_conn:
            main_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument("--period", choices=["1y", "5y", "10y", "max"], default=None)
    period_group.add_argument("--years", type=int, default=None,
                               help="Exact N-year window ending today (e.g. --years 3), for windows yfinance's "
                                    "own period vocabulary doesn't have a name for.")
    parser.add_argument("--company-id", default=None, help="Limit the run to a single company_id")
    parser.add_argument("--country", choices=["IN", "US"], default="IN")
    tier_group = parser.add_mutually_exclusive_group()
    tier_group.add_argument("--index-name", default="Nifty 500",
                             help='NSE tier to backfill, e.g. "Nifty 50" / "Nifty Next 50" / "Nifty Midcap 150" / '
                                  '"Nifty Smallcap 250" / "Nifty Micro-Cap" / "Nifty 500" (default). country=IN only.')
    tier_group.add_argument("--all-tiers", action="store_true",
                             help=f"Run all five standard tiers in one invocation: {', '.join(NSE_TIERS)}.")
    parser.add_argument("--force", action="store_true",
                         help="Bypass the skip-if-already-covers-the-requested-start-date check "
                              "and re-fetch every company regardless of existing coverage.")
    parser.add_argument("--time-budget-minutes", type=float, default=None,
                         help="Stop after this many minutes, leaving whatever's left for the next scheduled run "
                              "-- see run_price_history_backfill()'s own docstring. Applies per tier, not to the "
                              "whole --all-tiers invocation.")
    args = parser.parse_args()

    main_conn = open_db()
    price_conn = open_price_db()

    time_budget_seconds = args.time_budget_minutes * 60 if args.time_budget_minutes is not None else None
    tiers = list(NSE_TIERS) if args.all_tiers else [args.index_name]
    run_ids = []
    for index_name in tiers:
        job_name = TIER_JOB_NAMES.get(index_name, "price_history_backfill") if args.country == "IN" \
            else US_JOB_NAME
        run_id = run_price_history_backfill(
            main_conn, price_conn, country=args.country, company_id=args.company_id, index_name=index_name,
            years=args.years, period=args.period, job_name=job_name, force=args.force,
            time_budget_seconds=time_budget_seconds,
        )
        run_ids.append(run_id)
        if args.country != "IN":
            break  # index_name is meaningless for US -- one pass only, regardless of tiers/--all-tiers

    main_conn.close()
    price_conn.close()
    print(f"\nAll done. BatchRun ids: {run_ids} -- see Audit Log -> Job Runs for per-tier detail.", flush=True)


if __name__ == "__main__":
    main()
