"""Company-list batch loop for NSE fetch jobs -- the gap SCHEDULED_JOBS.md
flagged for both the Financials and Shareholding-pattern jobs ("no
company-list batch loop exists yet ... one invocation is one company").
Wraps the same per-company fetch+ingest logic scripts/fetch_nse_xbrl.py /
scripts/fetch_nse_shareholding.py already use (nothing new about how a
single company gets fetched), looped over a company list, with every run
and every company's outcome recorded to the batch job audit log
(ingestion/batch_log.py -> batch_job_runs/batch_job_items) instead of a
scratch stdout log nobody can query later.

Usage:
  python -m scripts.batch_fetch_nse financials --companies WIPRO,TITAN --scope "Nifty 50 remaining"
  python -m scripts.batch_fetch_nse shareholding --companies-file nifty50.txt
  python -m scripts.batch_fetch_nse shareholding --index "Nifty 50" --scope "Nifty 50 shareholding"

--companies-file / --index are alternate ways to supply the company list --
exactly one of --companies/--companies-file/--index is required. --index
resolves via storage.company_repository.select_company_ids_by_index()
rather than querying company_index_membership directly.
"""

from __future__ import annotations

import argparse
import uuid

from companies.registry import get_company
from config import settings as app_settings
from ingestion.batch_log import BatchRun
from ingestion.event_bus import publish
from ingestion.events import DatasetIngestedEvent
from ingestion.pipeline import ingest_file
from sources.nse_fetch import NSEFetchError, refresh_company_filings
from sources.nse_shareholding import fetch_shareholding_detail, fetch_shareholding_master
from storage.company_repository import select_company_ids_by_index
from storage.database import init_db
from storage.repositories import (
    insert_shareholding_holders,
    insert_shareholding_observations,
    update_shareholding_category_breakdown,
)


def _run_financials(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")
    symbol = company["nse_symbol"]
    if not symbol:
        raise ValueError(f"{company_id} has no nse_symbol on file")

    dest_dir = app_settings.RAW_DIR / company_id / "nse"
    result = refresh_company_filings(symbol, dest_dir)
    if not result.downloaded_files and result.error_count:
        # Nothing usable came back at all -- a real failure, not "0 new
        # filings, up to date" (that case has error_count == 0).
        raise NSEFetchError(f"{result.error_count} NSE request(s) failed, nothing downloaded")

    reconciled = 0
    for path in result.downloaded_files:
        statement_type = path.stem.split("_")[1]
        reconciled += ingest_file(conn, path, company_id=company_id, source_id="nse", statement_type=statement_type).reconciled_count

    detail = f"downloaded={len(result.downloaded_files)} reconciled={reconciled}"
    if result.error_count:
        detail += f" errors={result.error_count}"
    return detail


def _run_shareholding(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")
    symbol = company["nse_symbol"]
    if not symbol:
        raise ValueError(f"{company_id} has no nse_symbol on file")

    summaries = fetch_shareholding_master(symbol)
    if not summaries:
        return "no shareholding submissions on NSE for this symbol"
    summaries.sort(key=lambda s: s.period_end)
    insert_shareholding_observations(conn, company_id, summaries)

    holder_total, quarter_errors = 0, 0
    for s in summaries:
        if not s.source_url:
            continue
        try:
            holdings, breakdown = fetch_shareholding_detail(s.source_url)
        except NSEFetchError:
            quarter_errors += 1
            continue
        holder_total += insert_shareholding_holders(
            conn, company_id, s.fiscal_year, s.quarter, holdings,
            source_url=s.source_url, submission_date=s.submission_date,
        )
        if breakdown is not None:
            update_shareholding_category_breakdown(conn, company_id, s.fiscal_year, s.quarter, breakdown)

    publish(
        conn,
        DatasetIngestedEvent(
            dataset_id=f"nse_shareholding:{company_id}",
            dataset_type="shareholding",
            source="nse",
            scope={"company_id": company_id},
            storage_reference={"table": "shareholding_observations", "company_id": company_id},
            ingestion_id=str(uuid.uuid4()),
            metadata={"summary_count": len(summaries), "named_holder_count": holder_total},
        ),
    )

    detail = f"summaries={len(summaries)} named_holders={holder_total}"
    if quarter_errors:
        detail += f" quarter_errors={quarter_errors}"
    return detail


_RUNNERS = {"financials": _run_financials, "shareholding": _run_shareholding}
_JOB_NAMES = {"financials": "nse_xbrl_fetch", "shareholding": "nse_shareholding_fetch"}


def _resolve_companies(conn, args: argparse.Namespace) -> list[str]:
    if args.companies:
        return [c.strip().upper() for c in args.companies.split(",") if c.strip()]
    if args.companies_file:
        with open(args.companies_file) as f:
            return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
    if args.index:
        rows = select_company_ids_by_index(conn, args.index)
        return [r["company_id"] for r in rows]
    raise SystemExit("one of --companies / --companies-file / --index is required")


def run_nse_batch(conn, kind: str, companies: list[str], scope_label: str | None = None, job_name: str | None = None) -> int:
    """The actual company-list loop, factored out of main() so the Settings >
    Data Operations > Schedule panel's "Run now" button (web/app.py) can
    drive the exact same batch -- one capability (loop a company list
    through the NSE financials/shareholding fetch, with a BatchRun audit
    trail), two triggers (this CLI's main() below, and the admin route) --
    same reuse shape as admin_refresh_company()'s own docstring describes
    for the single-company refresh action. Returns the BatchRun's run_id so
    the caller can report/link back to it (e.g. a flash message pointing at
    Audit Log -> Job Runs).

    `job_name` defaults to `_JOB_NAMES[kind]` (the CLI never needs to
    override it), but the Schedule panel passes a distinct value for its
    "Nifty 500 remaining" jobs -- those run the same `kind` as the Nifty 50
    jobs, and get_latest_batch_job_run() looks "last run" up by job_name
    alone, so two scopes sharing one job_name would make each Schedule row
    show whichever scope happened to run more recently instead of its own
    history."""
    if not companies:
        raise ValueError("company list is empty")

    scope_label = scope_label or f"{kind} ({len(companies)} companies)"
    runner = _RUNNERS[kind]
    job_name = job_name or _JOB_NAMES[kind]

    print(f"{job_name}: {len(companies)} companies, scope={scope_label!r}", flush=True)
    ok = failed = 0
    with BatchRun(conn, job_name, scope_label) as run:
        print(f"run_id={run.run_id}", flush=True)
        for company_id in companies:
            with run.item(company_id) as item:
                try:
                    item.detail = runner(conn, company_id)
                    ok += 1
                    print(f"{company_id}: OK -- {item.detail}", flush=True)
                except Exception as exc:  # noqa: BLE001 -- let run.item() record it, then keep looping
                    failed += 1
                    print(f"{company_id}: FAILED -- {exc}", flush=True)
                    raise

    print(f"\nDone. run_id={run.run_id} ok={ok} failed={failed}", flush=True)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=sorted(_RUNNERS))
    parser.add_argument("--companies", help="comma-separated company_id list")
    parser.add_argument("--companies-file", help="path to a file, one company_id per line")
    parser.add_argument("--index", help='company_index_membership index_name, e.g. "Nifty 50"')
    parser.add_argument("--scope", help="human label for the audit log (defaults to the kind + count)")
    args = parser.parse_args()

    conn = init_db()
    companies = _resolve_companies(conn, args)
    if not companies:
        raise SystemExit("resolved company list is empty")

    run_nse_batch(conn, args.kind, companies, scope_label=args.scope)
    conn.close()


if __name__ == "__main__":
    main()
