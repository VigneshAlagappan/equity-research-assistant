"""Company-list batch loop for the SEC EDGAR financials fetch -- the
company-list-loop half of sources/sec_edgar.py's own "USA equivalent of
sources/nse_fetch.py" story (that module's docstring already flags that a
future batch script covering many companies should pace itself against
SEC's fair-access policy; this is that script). Wraps the same per-company
ingest_sec_edgar_company() main.py's `ingest-sec-edgar` CLI command
already uses (nothing new about how a single company gets fetched), looped
over a company list, with every run and every company's outcome recorded
to the batch job audit log (ingestion/batch_log.py -> batch_job_runs/
batch_job_items) -- same shape as scripts/batch_fetch_nse.py.

Usage:
  python -m scripts.batch_fetch_sec_edgar --companies AAPL,MSFT
  python -m scripts.batch_fetch_sec_edgar --companies-file sp500.txt
  python -m scripts.batch_fetch_sec_edgar --index "S&P 500" --scope "S&P 500 financials"
  python -m scripts.batch_fetch_sec_edgar --country US

--companies-file / --index / --country are alternate ways to supply the
company list -- exactly one of --companies/--companies-file/--index/
--country is required. --country mirrors scripts/fetch_daily_prices_usa.py's
"every US company on file" convention (select_active_companies_by_country);
--index resolves via storage.company_repository.select_company_ids_by_index()
the same way scripts/batch_fetch_nse.py's own --index flag does.
"""

from __future__ import annotations

import argparse
import time

from companies.registry import get_company
from ingestion.batch_log import BatchRun
from ingestion.pipeline import ingest_sec_edgar_company
from sources.sec_edgar import SECFetchError, get_cik_for_ticker
from storage.company_repository import select_active_companies_by_country, select_company_ids_by_index
from storage.database import init_db

# SEC's fair-access policy caps automated traffic at 10 req/s; one company
# here is one companyfacts request (get_cik_for_ticker's own ticker->CIK
# map is fetched once per process and cached in-process), so this pacing
# is generously under that cap rather than tuned to it.
REQUEST_DELAY_SECONDS = 0.3

_JOB_NAME = "sec_edgar_financials_fetch"


def _run_financials(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")

    cik = get_cik_for_ticker(company_id)
    if cik is None:
        raise SECFetchError(f"could not resolve a SEC CIK for ticker {company_id!r}")

    result = ingest_sec_edgar_company(conn, company_id, cik, currency=company["currency"])
    detail = (
        f"cik={cik} parsed={result.parsed_count} inserted={result.inserted_count} "
        f"skipped={result.skipped_count} reconciled={result.reconciled_count}"
    )
    if result.skip_reasons:
        detail += f" ({len(result.skip_reasons)} skip reason(s) logged)"
    return detail


def _resolve_companies(conn, args: argparse.Namespace) -> list[str]:
    if args.companies:
        return [c.strip().upper() for c in args.companies.split(",") if c.strip()]
    if args.companies_file:
        with open(args.companies_file) as f:
            return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
    if args.index:
        rows = select_company_ids_by_index(conn, args.index)
        return [r["company_id"] for r in rows]
    if args.country:
        rows = select_active_companies_by_country(conn, args.country)
        return [r["company_id"] for r in rows]
    raise SystemExit("one of --companies / --companies-file / --index / --country is required")


def run_sec_edgar_batch(conn, companies: list[str], scope_label: str | None = None, job_name: str | None = None) -> int:
    """The actual company-list loop, factored out of main() so the Settings >
    Data Operations > Schedule panel's "Run now" button (web/app.py) can
    drive the exact same batch -- same one-capability-two-triggers shape
    scripts/batch_fetch_nse.py's run_nse_batch() already uses. Returns the
    BatchRun's run_id."""
    if not companies:
        raise ValueError("company list is empty")

    scope_label = scope_label or f"sec_edgar financials ({len(companies)} companies)"
    job_name = job_name or _JOB_NAME

    print(f"{job_name}: {len(companies)} companies, scope={scope_label!r}", flush=True)
    ok = failed = 0
    with BatchRun(conn, job_name, scope_label) as run:
        print(f"run_id={run.run_id}", flush=True)
        for i, company_id in enumerate(companies):
            if i > 0:
                time.sleep(REQUEST_DELAY_SECONDS)
            with run.item(company_id) as item:
                try:
                    item.detail = _run_financials(conn, company_id)
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
    parser.add_argument("--companies", help="comma-separated company_id list")
    parser.add_argument("--companies-file", help="path to a file, one company_id per line")
    parser.add_argument("--index", help='company_index_membership index_name, e.g. "S&P 500"')
    parser.add_argument("--country", help='companies.country, e.g. "US" -- every active company on file')
    parser.add_argument("--scope", help="human label for the audit log (defaults to the count)")
    args = parser.parse_args()

    conn = init_db()
    companies = _resolve_companies(conn, args)
    if not companies:
        raise SystemExit("resolved company list is empty")

    run_sec_edgar_batch(conn, companies, scope_label=args.scope)
    conn.close()


if __name__ == "__main__":
    main()
