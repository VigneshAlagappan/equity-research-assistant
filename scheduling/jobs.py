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

from storage.backend_bootstrap import open_db
from scripts.backfill_price_history import NSE_TIERS, TIER_JOB_NAMES, US_JOB_NAME, run_price_history_backfill
from scripts.batch_fetch_fred import run_fred_batch, TRACKED_SERIES
from scripts.batch_fetch_nse import run_nse_batch
from scripts.batch_fetch_sec_edgar import run_sec_edgar_batch
from scripts.batch_generate_insights import run_key_insights_batch
from scripts.classify_macro_factors_batch import run_macro_factor_classification_batch
from scripts.db_shard import run_db_shard_job
from scripts.fetch_daily_prices import run_price_history_update
from scripts.fetch_daily_prices_usa import run_price_history_update_usa
from scripts.fetch_investor_relations import run_investor_relations_batch, SUPPORTED_COMPANY_IDS
from scripts.process_pending_documents_batch import run_document_processing_batch
from scripts.reconcile_generated_reports import run_generated_report_reconciliation
from scripts.reconcile_raw_objects import run_raw_object_reconciliation
from storage.company_repository import select_active_companies_by_country, select_company_ids_by_index


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
    run anything). `category` groups rows into the Schedule panel's
    collapsible sections (e.g. "Financials", "Shareholding") -- see
    CATEGORY_ORDER below for the fixed display order those sections
    render in."""

    job_id: str
    label: str
    cadence: str
    category: str
    job_name: str | None
    reason: str | None
    runner: Callable[[DBConnection], int] | None


#: Fixed display order for the Schedule panel's collapsible categories --
#: web/app.py's _schedule_panel_context() groups SCHEDULED_JOBS by
#: `category` and renders one <details> section per entry here, in this
#: order, skipping any category with zero jobs (there shouldn't be any,
#: this list and the categories actually used below must stay in sync).
CATEGORY_ORDER = (
    "Daily price",
    "History price",
    "Financials",
    "Shareholding",
    "Corporate actions",
    "Macro",
    "Insights",
    "Documents",
    "Maintenance",
)


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


HISTORY_BACKFILL_YEARS = 20

# Per-tier ceiling on how long one scheduled run is allowed to spend before
# it must stop and defer whatever's left to next week/month -- sized against
# each tier's actual EventBridge gap to the *next* scheduled job (see the
# "Automated schedule" table in docs/USER_GUIDE.md), minus a buffer so a
# slow run never bleeds into the next job's start time. Nifty 50/Next 50/
# Midcap 150 sit in a tight 15-30 min chain of Saturday-morning slots;
# Smallcap 250, Micro-Cap (monthly) and USA all have hours of clear runway
# before the next thing on the calendar (Financials, at noon+), so they get
# a more generous budget. A company already covered back to the 20-year
# target is skipped in O(1) regardless of budget (run_price_history_backfill's
# own coverage check), so once a tier catches up these runs go back to being
# fast no-ops.
HISTORY_TIER_TIME_BUDGET_MINUTES = {
    "Nifty 50": 12,  # 7:00am slot, Next 50 starts 7:15am
    "Nifty Next 50": 12,  # 7:15am slot, Midcap 150 starts 7:30am
    "Nifty Midcap 150": 25,  # 7:30am slot, Smallcap 250 starts 8:00am
    "Nifty Smallcap 250": 45,  # 8:00am slot, next job (Financials) is noon
    "Nifty Micro-Cap": 60,  # monthly 1st-Sat 8:30am slot, next job is noon
}
US_HISTORY_TIME_BUDGET_MINUTES = 30  # 7:00am slot, USA Financials at 1:40pm


def _make_price_history_backfill_runner(index_name: str):
    """One factory for the five NSE-tier backfills, same reasoning as
    _make_nse_tier_runner above -- run_price_history_backfill() opens its
    own main-db/price-db connections internally (same "conn ignored, own
    connections opened internally" shape _run_price_history_india uses),
    so `conn` is unused here too.

    years=20 (or as far back as the company was listed, whichever is
    shorter) is the target depth, but a single run never tries to pull the
    whole 20 years at once -- time_budget_seconds caps how long this run
    works before deferring the rest, and because "already covered back to
    the target" is a real, persisted fact (existing daily_prices rows, not
    a separate checkpoint), whatever's left just gets picked up and pushed
    further back by next week's run. See run_price_history_backfill's own
    docstring for the gap-fetch/time-budget mechanics."""
    job_name = TIER_JOB_NAMES[index_name]
    time_budget_seconds = HISTORY_TIER_TIME_BUDGET_MINUTES[index_name] * 60

    def _runner(conn) -> int:
        return run_price_history_backfill(
            index_name=index_name,
            years=HISTORY_BACKFILL_YEARS,
            job_name=job_name,
            time_budget_seconds=time_budget_seconds,
        )

    return _runner


_run_price_history_backfill_nifty50 = _make_price_history_backfill_runner("Nifty 50")
_run_price_history_backfill_next50 = _make_price_history_backfill_runner("Nifty Next 50")
_run_price_history_backfill_midcap150 = _make_price_history_backfill_runner("Nifty Midcap 150")
_run_price_history_backfill_smallcap250 = _make_price_history_backfill_runner("Nifty Smallcap 250")
_run_price_history_backfill_microcap = _make_price_history_backfill_runner("Nifty Micro-Cap")


def _run_price_history_backfill_usa(conn) -> int:
    """USA counterpart of the five NSE-tier backfills above -- same
    years=20-with-a-per-run-time-budget shape, same conn-ignored/own-
    connections-opened-internally shape, same skip-if-already-covers-the-
    requested-start-date resume behavior (run_price_history_backfill's own
    docstring)."""
    return run_price_history_backfill(
        country="US",
        years=HISTORY_BACKFILL_YEARS,
        job_name=US_JOB_NAME,
        time_budget_seconds=US_HISTORY_TIME_BUDGET_MINUTES * 60,
    )


def _run_db_shard(conn) -> int:
    return run_db_shard_job(conn)


def _run_raw_object_reconciliation(conn) -> int:
    """ADR-022's S3<->Postgres catalog reconciliation -- report-only,
    never deletes/recreates anything (see scripts/reconcile_raw_objects.py's
    own docstring). `conn` is used directly here (unlike the price-history
    jobs above) since this job only ever touches the main db's raw_objects
    table, no separate price/S3 connection to juggle."""
    return run_raw_object_reconciliation(conn)


def _run_generated_report_reconciliation(conn) -> int:
    """S3<->Postgres reconciliation for generated_reports.s3_key -- same
    ADR-022 report-only reasoning as _run_raw_object_reconciliation above,
    just for research threads instead of ingested documents. See scripts/
    reconcile_generated_reports.py's own docstring for the production gap
    that motivated this (research_thread() 500ing on a missing S3 object)."""
    return run_generated_report_reconciliation(conn)


def _run_fred_macro(conn) -> int:
    return run_fred_batch(conn, TRACKED_SERIES, scope_label=f"FRED ({len(TRACKED_SERIES)} series)")


def _run_doc_analysis(conn) -> int:
    return run_document_processing_batch(conn)


def _run_macro_factor_classification(conn) -> int:
    return run_macro_factor_classification_batch(conn)


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


# Grouped by `category` (see CATEGORY_ORDER above for display order);
# within a category, order here is the display order in the Schedule
# panel's per-category list.
SCHEDULED_JOBS: list[ScheduledJob] = [
    ScheduledJob("price_history_india", "India — close price & volume (Nifty 500)", "Daily", "Daily price",
                 "price_history_india", None, _run_price_history_india),
    ScheduledJob("price_history_india_nifty_microcap", "India — close price & volume (Nifty Micro-Cap)", "Monthly", "Daily price",
                 "price_history_india_nifty_microcap", None, _run_price_history_nifty_microcap),
    ScheduledJob("price_history_usa", "USA — close price & volume", "Weekly", "Daily price",
                 "price_history_usa", None, _run_price_history_usa),
    ScheduledJob("price_history_backfill_nifty50", "Nifty 50 — close price & volume, 20y incremental", "Manual", "History price",
                 TIER_JOB_NAMES["Nifty 50"], None, _run_price_history_backfill_nifty50),
    ScheduledJob("price_history_backfill_next50", "Nifty Next 50 — close price & volume, 20y incremental", "Manual", "History price",
                 TIER_JOB_NAMES["Nifty Next 50"], None, _run_price_history_backfill_next50),
    ScheduledJob("price_history_backfill_midcap150", "Nifty Midcap 150 — close price & volume, 20y incremental", "Manual", "History price",
                 TIER_JOB_NAMES["Nifty Midcap 150"], None, _run_price_history_backfill_midcap150),
    ScheduledJob("price_history_backfill_smallcap250", "Nifty Smallcap 250 — close price & volume, 20y incremental", "Manual", "History price",
                 TIER_JOB_NAMES["Nifty Smallcap 250"], None, _run_price_history_backfill_smallcap250),
    ScheduledJob("price_history_backfill_microcap", "Nifty Micro-Cap — close price & volume, 20y incremental", "Manual", "History price",
                 TIER_JOB_NAMES["Nifty Micro-Cap"], None, _run_price_history_backfill_microcap),
    ScheduledJob("price_history_backfill_usa", "USA — close price & volume, 20y incremental", "Manual", "History price",
                 US_JOB_NAME, None, _run_price_history_backfill_usa),
    ScheduledJob("financials_india", "Nifty 50", "Quarterly", "Financials",
                 "nse_xbrl_fetch", None, _run_financials_india),
    ScheduledJob("financials_india_next50", "Nifty Next 50", "Quarterly", "Financials",
                 "nse_xbrl_fetch_nifty_next50", None, _run_financials_nifty_next50),
    ScheduledJob("financials_india_midcap150", "Nifty Midcap 150", "Quarterly", "Financials",
                 "nse_xbrl_fetch_nifty_midcap150", None, _run_financials_nifty_midcap150),
    ScheduledJob("financials_india_smallcap250", "Nifty Smallcap 250", "Quarterly", "Financials",
                 "nse_xbrl_fetch_nifty_smallcap250", None, _run_financials_nifty_smallcap250),
    ScheduledJob("financials_india_microcap", "Nifty Micro-Cap", "Monthly", "Financials",
                 "nse_xbrl_fetch_nifty_microcap", None, _run_financials_nifty_microcap),
    ScheduledJob("financials_usa", "USA", "Quarterly", "Financials",
                 "sec_edgar_financials_fetch", None, _run_financials_usa),
    ScheduledJob("shareholding_india", "Nifty 50", "Quarterly", "Shareholding",
                 "nse_shareholding_fetch", None, _run_shareholding_india),
    ScheduledJob("shareholding_india_next50", "Nifty Next 50", "Quarterly", "Shareholding",
                 "nse_shareholding_fetch_nifty_next50", None, _run_shareholding_nifty_next50),
    ScheduledJob("shareholding_india_midcap150", "Nifty Midcap 150", "Quarterly", "Shareholding",
                 "nse_shareholding_fetch_nifty_midcap150", None, _run_shareholding_nifty_midcap150),
    ScheduledJob("shareholding_india_smallcap250", "Nifty Smallcap 250", "Quarterly", "Shareholding",
                 "nse_shareholding_fetch_nifty_smallcap250", None, _run_shareholding_nifty_smallcap250),
    ScheduledJob("shareholding_india_microcap", "Nifty Micro-Cap", "Monthly", "Shareholding",
                 "nse_shareholding_fetch_nifty_microcap", None, _run_shareholding_nifty_microcap),
    ScheduledJob("corporate_actions_india", "Fetch — Nifty 50", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_fetch", None, _run_corporate_actions_india),
    ScheduledJob("corporate_actions_ingest_india", "Ingest — Nifty 50", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_ingest", None, _run_corporate_actions_ingest_india),
    ScheduledJob("corporate_actions_india_next50", "Fetch — Nifty Next 50", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_fetch_nifty_next50", None, _run_corporate_actions_nifty_next50),
    ScheduledJob("corporate_actions_ingest_india_next50", "Ingest — Nifty Next 50", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_ingest_nifty_next50", None, _run_corporate_actions_ingest_nifty_next50),
    ScheduledJob("corporate_actions_india_midcap150", "Fetch — Nifty Midcap 150", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_fetch_nifty_midcap150", None, _run_corporate_actions_nifty_midcap150),
    ScheduledJob("corporate_actions_ingest_india_midcap150", "Ingest — Nifty Midcap 150", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_ingest_nifty_midcap150", None, _run_corporate_actions_ingest_nifty_midcap150),
    ScheduledJob("corporate_actions_india_smallcap250", "Fetch — Nifty Smallcap 250", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_fetch_nifty_smallcap250", None, _run_corporate_actions_nifty_smallcap250),
    ScheduledJob("corporate_actions_ingest_india_smallcap250", "Ingest — Nifty Smallcap 250", "Quarterly", "Corporate actions",
                 "nse_corporate_actions_ingest_nifty_smallcap250", None, _run_corporate_actions_ingest_nifty_smallcap250),
    ScheduledJob("corporate_actions_india_microcap", "Fetch — Nifty Micro-Cap", "Monthly", "Corporate actions",
                 "nse_corporate_actions_fetch_nifty_microcap", None, _run_corporate_actions_nifty_microcap),
    ScheduledJob("corporate_actions_ingest_india_microcap", "Ingest — Nifty Micro-Cap", "Monthly", "Corporate actions",
                 "nse_corporate_actions_ingest_nifty_microcap", None, _run_corporate_actions_ingest_nifty_microcap),
    ScheduledJob("fred_macro", "FRED macro data", "Quarterly", "Macro",
                 "fred_macro_fetch", None, _run_fred_macro),
    ScheduledJob("rbi_macro", "RBI / IITM macro data", "Weekly", "Macro", None,
                 "These sources are file-based parsers over manually-downloaded files, "
                 "not live fetchers — \"weekly\" here still means a human stages the file "
                 "first", None),
    ScheduledJob("insights_macro", "Macro insights", "Monthly", "Macro", None,
                 "The generation function itself doesn't exist yet — needs a design "
                 "decision on what a macro insight is first", None),
    ScheduledJob("macro_factor_classification", "Macro factors -> knowledge graph (Neo4j)", "Monthly", "Macro",
                 "macro_factor_classification", None, _run_macro_factor_classification),
    ScheduledJob("insights_companies", "Company insights", "Monthly", "Insights",
                 "key_insights_batch", None, _run_insights_companies),
    ScheduledJob("doc_analysis", "Document analysis (all uploaded PDFs/audio)", "Daily", "Documents",
                 "document_processing", None, _run_doc_analysis),
    ScheduledJob("investor_relations", "Investor relations documents (Q4/Berkshire)", "Quarterly", "Documents",
                 "investor_relations_fetch", None, _run_investor_relations),
    ScheduledJob("db_shard", "DB sharding", "Daily", "Maintenance",
                 "db_shard", None, _run_db_shard),
    ScheduledJob("raw_object_reconciliation", "Raw object catalog reconciliation (S3 <-> Postgres)", "Weekly",
                 "Maintenance", "raw_object_reconciliation", None, _run_raw_object_reconciliation),
    ScheduledJob("generated_report_reconciliation", "Research thread reconciliation (S3 <-> Postgres)", "Weekly",
                 "Maintenance", "generated_report_reconciliation", None, _run_generated_report_reconciliation),
]


def get_job(job_id: str) -> ScheduledJob | None:
    return next((j for j in SCHEDULED_JOBS if j.job_id == job_id), None)
