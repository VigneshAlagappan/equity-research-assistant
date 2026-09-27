"""Pull each US company's official SEC 10-K filings and store them RAW in
S3 (or local disk under DOCUMENT_STORE_BACKEND=local) via
storage/raw_object_store.py -- deliberately WITHOUT running any of it
through this app's document-processing/knowledge-extraction pipeline.
This is a storage-only step; deciding how to extract narrative content
from these filings is a separate, later effort.

Unlike the NSE annual-report backfill (scripts/backfill_nse_annual_reports.py),
this is NOT filling a financial-data gap: sources/sec_edgar.py's existing
XBRL company-facts ingestion already gives this app real balance-sheet/
cash-flow/income-statement facts for US companies back to ~2006-2008
(verified live against production Neon for AAPL/MSFT/AMZN before this
script was written). What this pulls instead is the narrative content of
each 10-K itself (MD&A, risk factors, business description) for future
RAG/evidence use -- a distinct, additive capability, not a backfill of
something already covered.

CRITICAL BOUNDARY -- this script must NEVER:
  * parse/extract facts from a downloaded document,
  * write to canonical_financials or financial_observations,
  * advance a raw_objects row's state past "stored" (no "validated"/
    "parsed"/"ingested" -- store_raw_object() already leaves new rows at
    exactly "stored", and this script calls nothing that would move them
    further).

Usage:
  python -m scripts.backfill_sec_edgar_documents --companies AAPL,MSFT --dry-run
  python -m scripts.backfill_sec_edgar_documents --companies AAPL,MSFT
  python -m scripts.backfill_sec_edgar_documents --country US --dry-run
  python -m scripts.backfill_sec_edgar_documents --country US

--dry-run discovers only (one submissions call plus any paginated older-
filing pages) and logs what WOULD be stored -- how many 10-Ks on file --
without downloading a single filing document or touching S3/raw_objects.
Real-run mode does the same discovery, then downloads and stores every
10-K's primary document.

--companies and --country are mutually exclusive.
"""

from __future__ import annotations

import argparse
import logging

# Must run before any other import in this file touches storage.repositories/
# company_repository/raw_object_repository -- see storage/backend_bootstrap.py's
# own docstring: a module that does `from storage import company_repository as
# repo` at import time (companies/registry.py does exactly this) binds to the
# pre-swap SQLite module forever if install() runs after that import.
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from companies.registry import get_company, list_companies
from ingestion.batch_log import BatchRun
from sources.sec_edgar import SECFetchError
from sources.sec_edgar_documents import discover_10k_filings, download_filing
from storage.backend_bootstrap import open_db
from storage.raw_object_store import store_raw_object

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_sec_edgar_documents")

JOB_NAME = "sec_edgar_documents_backfill"

_RAW_PREFIX = "companies"
_SOURCE = "sec_edgar"
_OBJECT_TYPE = "annual_report_10k"


def _is_stale_connection_error(exc: BaseException) -> bool:
    """Same class of failure the NSE annual-report backfill's identically
    named function documents in full -- Neon closing/recycling a long-lived
    Postgres connection during a multi-company batch. Matched by exception
    class name/message rather than a hard `import psycopg2` at module
    level, so this script stays importable in a SQLite-only environment."""
    name = type(exc).__name__
    text = str(exc)
    return name in ("OperationalError", "InterfaceError") and (
        "server closed the connection" in text
        or "connection already closed" in text
        or "terminat" in text.lower()
    )


def _resolve_symbol_map(conn, companies: list[str]) -> dict[str, str]:
    """company_id -> ticker (usually this app's US company_id IS the
    ticker, e.g. "AAPL", but a handful are disambiguated from a
    pre-existing Indian company_id -- e.g. "PNC_US" vs the real ticker
    "PNC" -- so the real ticker is read from companies.fetch_symbol,
    falling back to company_id, rather than assumed. Resolved through
    get_company() rather than a bare lookup, so a typo'd/unregistered
    ticker is skipped with a clear log line instead of silently hitting
    SEC's ticker map with something never checked against this app's own
    company registry)."""
    symbols: dict[str, str] = {}
    for company_id in companies:
        company = get_company(conn, company_id)
        if company is None:
            logger.warning("%s: no company registered under this id -- skipping", company_id)
            continue
        symbols[company_id] = company["fetch_symbol"] or company_id
    return symbols


def _resolve_us_companies(conn) -> dict[str, str]:
    return {c["company_id"]: (c["fetch_symbol"] or c["company_id"]) for c in list_companies(conn) if c["country"] == "US"}


class CompanyResult:
    def __init__(self, company_id: str) -> None:
        self.company_id = company_id
        self.total_10ks = 0
        self.stored_new = 0
        self.stored_duplicate = 0
        self.download_errors = 0

    def summary(self) -> str:
        return (
            f"10-Ks={self.total_10ks} stored_new={self.stored_new} "
            f"stored_duplicate={self.stored_duplicate} download_errors={self.download_errors}"
        )


def dry_run_company(company_id: str, ticker: str) -> CompanyResult:
    logger.info("%s (%s): discovering 10-K filings", company_id, ticker)
    filings = discover_10k_filings(ticker)
    result = CompanyResult(company_id)
    result.total_10ks = len(filings)
    for f in filings:
        logger.info("  [%s, filed %s] WOULD STORE %s", f["report_date"], f["filing_date"], f["doc_url"])
    if not filings:
        logger.info("%s: zero 10-K filings found (verify ticker->CIK resolution if this looks wrong)", company_id)
    logger.info("%s: dry-run summary -- %s", company_id, result.summary())
    return result


def backfill_company(company_id: str, ticker: str) -> CompanyResult:
    """Opens its OWN short-lived DB connection for store_raw_object() calls
    per company (not one shared across the whole run) -- same stale-
    connection defense the NSE annual-report backfill's backfill_company()
    uses, for the same reason: a connection only alive for one company's
    brief download+store sequence is far less likely to have gone stale
    than one held for a run's entire wall-clock duration."""
    logger.info("%s (%s): discovering 10-K filings", company_id, ticker)
    filings = discover_10k_filings(ticker)

    result = CompanyResult(company_id)
    result.total_10ks = len(filings)

    store_conn = open_db() if filings else None
    try:
        for f in filings:
            try:
                content = download_filing(f["doc_url"])
            except SECFetchError as exc:
                result.download_errors += 1
                logger.warning("%s: failed to download %s (%s): %s", company_id, f["accession_number"], f["doc_url"], exc)
                continue

            period = f["report_date"][:4] if f["report_date"] else f["filing_date"][:4]
            extension = f["primary_document"].rsplit(".", 1)[-1] if "." in f["primary_document"] else "htm"
            try:
                raw_result = store_raw_object(
                    store_conn, source=_SOURCE, entity=company_id, object_type=_OBJECT_TYPE,
                    period=f"FY{period}", source_url=f["doc_url"], raw_prefix=_RAW_PREFIX,
                    content=content, extension=extension,
                )
            except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry-once backstop
                if not _is_stale_connection_error(exc):
                    raise
                logger.warning("%s: DB connection went stale mid-company (%s) -- reopening and retrying once", company_id, exc)
                store_conn.close()
                store_conn = open_db()
                raw_result = store_raw_object(
                    store_conn, source=_SOURCE, entity=company_id, object_type=_OBJECT_TYPE,
                    period=f"FY{period}", source_url=f["doc_url"], raw_prefix=_RAW_PREFIX,
                    content=content, extension=extension,
                )

            if raw_result.is_new:
                result.stored_new += 1
                logger.info("  [FY%s] STORED object_id=%s key=%s", period, raw_result.object_id, raw_result.s3_key)
            else:
                result.stored_duplicate += 1
                logger.info("  [FY%s] DUPLICATE (existing object_id=%s)", period, raw_result.object_id)
    finally:
        if store_conn is not None:
            store_conn.close()

    logger.info("%s: run summary -- %s", company_id, result.summary())
    return result


def run_dry_run(symbol_map: dict[str, str]) -> list[CompanyResult]:
    results: list[CompanyResult] = []
    for company_id, ticker in symbol_map.items():
        try:
            results.append(dry_run_company(company_id, ticker))
        except SECFetchError as exc:
            logger.error("%s: dry-run failed: %s", company_id, exc)
    return results


def run_real_backfill(conn, symbol_map: dict[str, str]) -> list[CompanyResult]:
    """Same outer retry-and-resume shape as the NSE annual-report backfill's
    run_real_backfill() -- if BatchRun's own bookkeeping connection goes
    stale mid-run, resume under a fresh run_id with only the not-yet-
    attempted companies, rather than losing everything already done."""
    results: list[CompanyResult] = []
    remaining = dict(symbol_map)
    while remaining:
        scope_label = f"sec_edgar_documents_backfill ({len(remaining)} companies remaining)"
        done: list[str] = []
        try:
            with BatchRun(conn, JOB_NAME, scope_label) as run:
                logger.info("run_id=%s", run.run_id)
                for company_id, ticker in remaining.items():
                    with run.item(company_id) as item:
                        result = backfill_company(company_id, ticker)
                        item.detail = result.summary()
                        results.append(result)
                    done.append(company_id)
            remaining = {}
        except Exception as exc:  # noqa: BLE001 -- reconnect-and-resume backstop, see docstring
            if not _is_stale_connection_error(exc):
                raise
            try:
                conn.close()
            except Exception:  # noqa: BLE001 -- best-effort close of an already-broken connection
                pass
            conn = open_db()
            remaining = {cid: t for cid, t in remaining.items() if cid not in done}
            logger.warning(
                "BatchRun bookkeeping connection went stale (%s) -- reopened it and resuming "
                "under a new run_id with %d company(ies) remaining", exc, len(remaining),
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--companies", help="comma-separated company_id/ticker list (e.g. AAPL,MSFT)")
    group.add_argument("--country", help='resolve every registered company with this country code, e.g. "US"')
    parser.add_argument("--dry-run", action="store_true",
                         help="discover only; log what would be stored, write nothing")
    args = parser.parse_args()

    conn = open_db()
    try:
        if args.companies:
            requested = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
            if not requested:
                raise SystemExit("--companies resolved to an empty list")
            symbol_map = _resolve_symbol_map(conn, requested)
        else:
            symbol_map = _resolve_us_companies(conn)
            logger.info("--country %r resolved to %d companies", args.country, len(symbol_map))
        if not symbol_map:
            raise SystemExit("company selection resolved to an empty list")

        if args.dry_run:
            results = run_dry_run(symbol_map)
        else:
            results = run_real_backfill(conn, symbol_map)
    finally:
        conn.close()

    print()
    print(f"{'DRY RUN' if args.dry_run else 'REAL RUN'} complete -- {len(results)} company(ies)")
    total_10ks = sum(r.total_10ks for r in results)
    total_stored_new = sum(r.stored_new for r in results)
    total_stored_dup = sum(r.stored_duplicate for r in results)
    total_download_errors = sum(r.download_errors for r in results)
    for result in results:
        print(f"  {result.company_id}: {result.summary()}")
    print()
    print(
        f"TOTALS: 10-Ks={total_10ks} stored_new={total_stored_new} "
        f"stored_duplicate={total_stored_dup} download_errors={total_download_errors}"
    )


if __name__ == "__main__":
    main()
