"""The recurring-batch-job registry -- one service, three call paths.

Moved out of web/app.py's create_app() closure (where it used to live as a
Flask-only construct) so it's a plain, framework-independent module: every
job here is reachable identically from

  * the CLI (scripts/run_job.py) -- for manual ops work without the web UI
  * the Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) -- synchronous, same UX as before this module existed
  * a cron-triggered HTTP endpoint (web/app.py's run-async route) -- for
    EventBridge Scheduler (or any other external scheduler) to fire these
    unattended, without blocking on however long the job takes

None of the runner functions below ever touched Flask's `g`/`request` --
they only take a `conn` and call the same run_*() functions the CLI
scripts already expose -- so extracting them here changes nothing about
what each job does, only where the registry lives.

open_db() is the one thing genuinely new here: every script's own CLI
main() used to hardcode init_db() (always local SQLite, ignoring
DATABASE_BACKEND) -- fine for a laptop, wrong for a manual CLI run against
a Postgres-backed production deployment. open_db() mirrors web/app.py's
get_db() so scripts/run_job.py resolves the same backend the web app
would."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import storage.backend_bootstrap
from config.settings import DATABASE_BACKEND
from scripts.batch_fetch_fred import run_fred_batch, TRACKED_SERIES
from scripts.batch_fetch_nse import run_nse_batch
from scripts.batch_fetch_sec_edgar import run_sec_edgar_batch
from scripts.batch_generate_insights import run_key_insights_batch
from scripts.db_shard import run_db_shard_job
from scripts.fetch_daily_prices import run_price_history_update
from scripts.fetch_daily_prices_usa import run_price_history_update_usa
from scripts.fetch_investor_relations import run_investor_relations_batch, SUPPORTED_COMPANY_IDS
from scripts.process_pending_documents_batch import run_document_processing_batch
from storage.company_repository import select_active_companies_by_country, select_company_ids_by_index
from storage.database import init_db
from storage.db_types import DBConnection


def open_db() -> DBConnection:
    """Same backend resolution as web/app.py's get_db(): install the
    Postgres sys.modules swap (a no-op unless DATABASE_BACKEND=postgres),
    then open a connection on whichever backend is actually configured.
    Every entry point in this module (CLI, web route, cron route) should
    open its connection through this, not init_db() directly."""
    storage.backend_bootstrap.install()
    if DATABASE_BACKEND == "postgres":
        from storage.database import init_postgres_db

        return init_postgres_db()
    return init_db()


@dataclass
class ScheduledJob:
    """One row of the Settings > Data Operations > Schedule panel's
    registry -- see SCHEDULED_JOBS.md for the underlying gap analysis this
    table is a UI over. `runner` is None for jobs that aren't wired to
    anything real yet (a design gap, a missing fetch source, or a
    batch-loop script nobody's written) -- those render as a disabled row
    with `reason` as subtext, pulled verbatim from SCHEDULED_JOBS.md's own
    verdict so the UI can't drift from the actual gap analysis by
    inventing softer wording. `job_name` is the batch_job_runs.job_name to
    look up "last run" by (None for the disabled rows, which have never
    run anything)."""

    job_id: str
    label: str
    cadence: str
    job_name: str | None
    reason: str | None
    runner: Callable[[DBConnection], int] | None


def _run_financials_india(conn) -> int:
    """Nifty 50 constituents only (not the full Nifty 500 the doc-level
    gap analysis mentions for "planned") -- matches the registry table in
    this task's own spec, and keeps a manual "Run now" click from
    accidentally kicking off a several-hundred-company NSE crawl."""
    companies = [r["company_id"] for r in select_company_ids_by_index(conn, "Nifty 50")]
    return run_nse_batch(conn, "financials", companies, scope_label=f"Nifty 50 ({len(companies)})")


def _run_shareholding_india(conn) -> int:
    companies = [r["company_id"] for r in select_company_ids_by_index(conn, "Nifty 50")]
    return run_nse_batch(conn, "shareholding", companies, scope_label=f"Nifty 50 ({len(companies)})")


def _run_corporate_actions_india(conn) -> int:
    """Nifty 50 only, hardcoded rather than via _make_nse_tier_runner like
    its sibling tiers below -- same scoping reasoning as
    _run_financials_india above -- raw fetch only (see
    scripts/batch_fetch_nse.py's _run_corporate_actions)."""
    companies = [r["company_id"] for r in select_company_ids_by_index(conn, "Nifty 50")]
    return run_nse_batch(conn, "corporate_actions", companies, scope_label=f"Nifty 50 ({len(companies)})")


def _run_corporate_actions_ingest_india(conn) -> int:
    """Classifies whatever _run_corporate_actions_india above has fetched
    into corporate_actions -- a separate scheduled job (not a step of the
    fetch job itself) so re-classifying later never requires re-fetching,
    same as canonical_financials' own reconciliation being a distinct
    action from ingestion."""
    companies = [r["company_id"] for r in select_company_ids_by_index(conn, "Nifty 50")]
    return run_nse_batch(conn, "corporate_actions_ingest", companies, scope_label=f"Nifty 50 ({len(companies)})")


# The other ~449 Nifty 500 constituents (everything not already covered by
# the Nifty 50 job above) used to be one "Nifty 500 remaining" job --
# replaced with three smaller ones along NSE's own standard tiering
# instead, so a single "Run now" click is a few dozen-to-150 companies, not
# 449 in one blocking synchronous request. Nifty Next 50 (50) + Nifty
# Midcap 150 (150) + Nifty Smallcap 250 (249) is a clean, verified,
# non-overlapping partition of exactly that same 449-company pool (Nifty
# 100 = Nifty 50 + Nifty Next 50, and Nifty 500 = Nifty 100 + Midcap 150 +
# Smallcap 250 -- NSE's own tier composition, not something picked
# arbitrarily) -- deliberately Next 50, not the full Nifty 100 tier, so
# this doesn't re-fetch the 50 companies the standalone Nifty 50 job above
# already covers.
#
# One factory instead of six near-identical closures (financials x
# shareholding, each x 3 tiers) that would otherwise drift out of sync
# with each other if one got edited and the other five didn't.
def _make_nse_tier_runner(kind: str, index_name: str, job_name: str):
    def _runner(conn) -> int:
        companies = [r["company_id"] for r in select_company_ids_by_index(conn, index_name)]
        return run_nse_batch(
            conn, kind, companies,
            scope_label=f"{index_name} ({len(companies)})", job_name=job_name,
        )
    return _runner


_run_financials_nifty_next50 = _make_nse_tier_runner(
    "financials", "Nifty Next 50", "nse_xbrl_fetch_nifty_next50")
_run_financials_nifty_midcap150 = _make_nse_tier_runner(
    "financials", "Nifty Midcap 150", "nse_xbrl_fetch_nifty_midcap150")
_run_financials_nifty_smallcap250 = _make_nse_tier_runner(
    "financials", "Nifty Smallcap 250", "nse_xbrl_fetch_nifty_smallcap250")
_run_shareholding_nifty_next50 = _make_nse_tier_runner(
    "shareholding", "Nifty Next 50", "nse_shareholding_fetch_nifty_next50")
_run_shareholding_nifty_midcap150 = _make_nse_tier_runner(
    "shareholding", "Nifty Midcap 150", "nse_shareholding_fetch_nifty_midcap150")
_run_shareholding_nifty_smallcap250 = _make_nse_tier_runner(
    "shareholding", "Nifty Smallcap 250", "nse_shareholding_fetch_nifty_smallcap250")

_run_corporate_actions_nifty_next50 = _make_nse_tier_runner(
    "corporate_actions", "Nifty Next 50", "nse_corporate_actions_fetch_nifty_next50")
_run_corporate_actions_nifty_midcap150 = _make_nse_tier_runner(
    "corporate_actions", "Nifty Midcap 150", "nse_corporate_actions_fetch_nifty_midcap150")
_run_corporate_actions_nifty_smallcap250 = _make_nse_tier_runner(
    "corporate_actions", "Nifty Smallcap 250", "nse_corporate_actions_fetch_nifty_smallcap250")
_run_corporate_actions_ingest_nifty_next50 = _make_nse_tier_runner(
    "corporate_actions_ingest", "Nifty Next 50", "nse_corporate_actions_ingest_nifty_next50")
_run_corporate_actions_ingest_nifty_midcap150 = _make_nse_tier_runner(
    "corporate_actions_ingest", "Nifty Midcap 150", "nse_corporate_actions_ingest_nifty_midcap150")
_run_corporate_actions_ingest_nifty_smallcap250 = _make_nse_tier_runner(
    "corporate_actions_ingest", "Nifty Smallcap 250", "nse_corporate_actions_ingest_nifty_smallcap250")

_run_financials_nifty_microcap = _make_nse_tier_runner(
    "financials", "Nifty Micro-Cap", "nse_xbrl_fetch_nifty_microcap")
_run_shareholding_nifty_microcap = _make_nse_tier_runner(
    "shareholding", "Nifty Micro-Cap", "nse_shareholding_fetch_nifty_microcap")
_run_corporate_actions_nifty_microcap = _make_nse_tier_runner(
    "corporate_actions", "Nifty Micro-Cap", "nse_corporate_actions_fetch_nifty_microcap")
_run_corporate_actions_ingest_nifty_microcap = _make_nse_tier_runner(
    "corporate_actions_ingest", "Nifty Micro-Cap", "nse_corporate_actions_ingest_nifty_microcap")


def _run_price_history_india(conn) -> int:
    # run_price_history_update() opens its own main-db/price-db connections
    # internally (see scripts/fetch_daily_prices.py's own refactor notes on
    # why its BatchRun must live on the main db, not the price db) -- the
    # `conn` this route hands every runner is ignored here, not reused,
    # which is fine: it's the same main db underneath, just a second
    # connection to it.
    return run_price_history_update()


def _run_price_history_nifty_microcap(conn) -> int:
    # Same "conn ignored, own connections opened internally" shape as
    # _run_price_history_india above.
    return run_price_history_update(index_name="Nifty Micro-Cap", job_name="price_history_india_nifty_microcap")


def _run_db_shard(conn) -> int:
    return run_db_shard_job(conn)


def _run_fred_macro(conn) -> int:
    return run_fred_batch(conn, TRACKED_SERIES, scope_label=f"FRED ({len(TRACKED_SERIES)} series)")


def _run_doc_analysis(conn) -> int:
    return run_document_processing_batch(conn)


def _run_insights_companies(conn) -> int:
    return run_key_insights_batch(conn)


def _run_financials_usa(conn) -> int:
    """Every active US company on file (a dozen today, same "no
    index-membership filter" reasoning select_active_companies_by_country's
    own docstring gives -- a couple of them aren't tagged into any of the
    US indices in company_index_membership, so filtering by one of those
    would silently drop them)."""
    companies = [r["company_id"] for r in select_active_companies_by_country(conn, "US")]
    return run_sec_edgar_batch(conn, companies, scope_label=f"US companies ({len(companies)})")


def _run_price_history_usa(conn) -> int:
    # Same shape as _run_price_history_india above: run_price_history_
    # update_usa() opens its own main-db/price-db connections internally,
    # so the `conn` this route hands every runner is ignored here rather
    # than reused -- it's the same main db underneath either way.
    return run_price_history_update_usa()


def _run_investor_relations(conn) -> int:
    """Every Q4/Berkshire-covered company (SUPPORTED_COMPANY_IDS) -- see
    scripts/fetch_investor_relations.py's run_investor_relations_batch()
    for the per-company fetch+ingest logic this wraps into the same
    BatchRun-audited shape every other job here already uses."""
    return run_investor_relations_batch(conn, SUPPORTED_COMPANY_IDS)


# Order here is the display order in the Schedule panel table.
SCHEDULED_JOBS: list[ScheduledJob] = [
    ScheduledJob("price_history_india", "Price history — India", "Daily",
                 "price_history_india", None, _run_price_history_india),
    ScheduledJob("price_history_india_nifty_microcap", "Price history — India (Nifty Micro-Cap)", "Monthly",
                 "price_history_india_nifty_microcap", None, _run_price_history_nifty_microcap),
    ScheduledJob("price_history_usa", "Price history — USA", "Weekly",
                 "price_history_usa", None, _run_price_history_usa),
    ScheduledJob("db_shard", "DB sharding", "Daily",
                 "db_shard", None, _run_db_shard),
    ScheduledJob("financials_india", "Financials — India (Nifty 50)", "Quarterly",
                 "nse_xbrl_fetch", None, _run_financials_india),
    ScheduledJob("financials_india_next50", "Financials — India (Nifty Next 50)", "Quarterly",
                 "nse_xbrl_fetch_nifty_next50", None, _run_financials_nifty_next50),
    ScheduledJob("financials_india_midcap150", "Financials — India (Nifty Midcap 150)", "Quarterly",
                 "nse_xbrl_fetch_nifty_midcap150", None, _run_financials_nifty_midcap150),
    ScheduledJob("financials_india_smallcap250", "Financials — India (Nifty Smallcap 250)", "Quarterly",
                 "nse_xbrl_fetch_nifty_smallcap250", None, _run_financials_nifty_smallcap250),
    ScheduledJob("financials_india_microcap", "Financials — India (Nifty Micro-Cap)", "Monthly",
                 "nse_xbrl_fetch_nifty_microcap", None, _run_financials_nifty_microcap),
    ScheduledJob("shareholding_india", "Shareholding pattern — India (Nifty 50)", "Quarterly",
                 "nse_shareholding_fetch", None, _run_shareholding_india),
    ScheduledJob("shareholding_india_next50", "Shareholding pattern — India (Nifty Next 50)", "Quarterly",
                 "nse_shareholding_fetch_nifty_next50", None, _run_shareholding_nifty_next50),
    ScheduledJob("shareholding_india_midcap150", "Shareholding pattern — India (Nifty Midcap 150)", "Quarterly",
                 "nse_shareholding_fetch_nifty_midcap150", None, _run_shareholding_nifty_midcap150),
    ScheduledJob("shareholding_india_smallcap250", "Shareholding pattern — India (Nifty Smallcap 250)", "Quarterly",
                 "nse_shareholding_fetch_nifty_smallcap250", None, _run_shareholding_nifty_smallcap250),
    ScheduledJob("shareholding_india_microcap", "Shareholding pattern — India (Nifty Micro-Cap)", "Monthly",
                 "nse_shareholding_fetch_nifty_microcap", None, _run_shareholding_nifty_microcap),
    ScheduledJob("corporate_actions_india", "Corporate actions — India (Nifty 50)", "Quarterly",
                 "nse_corporate_actions_fetch", None, _run_corporate_actions_india),
    ScheduledJob("corporate_actions_ingest_india", "Corporate actions ingest — India (Nifty 50)", "Quarterly",
                 "nse_corporate_actions_ingest", None, _run_corporate_actions_ingest_india),
    ScheduledJob("corporate_actions_india_next50", "Corporate actions — India (Nifty Next 50)", "Quarterly",
                 "nse_corporate_actions_fetch_nifty_next50", None, _run_corporate_actions_nifty_next50),
    ScheduledJob("corporate_actions_ingest_india_next50", "Corporate actions ingest — India (Nifty Next 50)", "Quarterly",
                 "nse_corporate_actions_ingest_nifty_next50", None, _run_corporate_actions_ingest_nifty_next50),
    ScheduledJob("corporate_actions_india_midcap150", "Corporate actions — India (Nifty Midcap 150)", "Quarterly",
                 "nse_corporate_actions_fetch_nifty_midcap150", None, _run_corporate_actions_nifty_midcap150),
    ScheduledJob("corporate_actions_ingest_india_midcap150", "Corporate actions ingest — India (Nifty Midcap 150)", "Quarterly",
                 "nse_corporate_actions_ingest_nifty_midcap150", None, _run_corporate_actions_ingest_nifty_midcap150),
    ScheduledJob("corporate_actions_india_smallcap250", "Corporate actions — India (Nifty Smallcap 250)", "Quarterly",
                 "nse_corporate_actions_fetch_nifty_smallcap250", None, _run_corporate_actions_nifty_smallcap250),
    ScheduledJob("corporate_actions_ingest_india_smallcap250", "Corporate actions ingest — India (Nifty Smallcap 250)", "Quarterly",
                 "nse_corporate_actions_ingest_nifty_smallcap250", None, _run_corporate_actions_ingest_nifty_smallcap250),
    ScheduledJob("corporate_actions_india_microcap", "Corporate actions — India (Nifty Micro-Cap)", "Monthly",
                 "nse_corporate_actions_fetch_nifty_microcap", None, _run_corporate_actions_nifty_microcap),
    ScheduledJob("corporate_actions_ingest_india_microcap", "Corporate actions ingest — India (Nifty Micro-Cap)", "Monthly",
                 "nse_corporate_actions_ingest_nifty_microcap", None, _run_corporate_actions_ingest_nifty_microcap),
    ScheduledJob("financials_usa", "Financials — USA", "Quarterly",
                 "sec_edgar_financials_fetch", None, _run_financials_usa),
    ScheduledJob("doc_analysis", "Document analysis (transcripts/concalls)", "Quarterly",
                 "document_processing", None, _run_doc_analysis),
    ScheduledJob("insights_companies", "Company insights", "Monthly",
                 "key_insights_batch", None, _run_insights_companies),
    ScheduledJob("insights_macro", "Macro insights", "Monthly", None,
                 "The generation function itself doesn't exist yet — needs a design "
                 "decision on what a macro insight is first", None),
    ScheduledJob("fred_macro", "FRED macro data", "Quarterly",
                 "fred_macro_fetch", None, _run_fred_macro),
    ScheduledJob("rbi_macro", "RBI / IITM macro data", "Weekly", None,
                 "These sources are file-based parsers over manually-downloaded files, "
                 "not live fetchers — \"weekly\" here still means a human stages the file "
                 "first", None),
    ScheduledJob("investor_relations", "Investor relations documents (Q4/Berkshire)", "Quarterly",
                 "investor_relations_fetch", None, _run_investor_relations),
]


def get_job(job_id: str) -> ScheduledJob | None:
    return next((j for j in SCHEDULED_JOBS if j.job_id == job_id), None)
