"""Pull each company's official NSE Annual Report PDFs (fiscal years
covering 2015 onward) and store them RAW in S3 (or local disk under
DOCUMENT_STORE_BACKEND=local) via storage/raw_object_store.py --
deliberately WITHOUT running any of it through this app's document-
processing/knowledge-extraction pipeline. This is a storage-only step;
deciding how to extract facts from these PDFs is a separate, later effort.

Built on sources/nse_annual_reports.py's discovery/dedup/ZIP-extraction and
storage/raw_object_store.py's existing dedup-by-hash raw-object catalog
(docs/ADR/022) -- same shape as scripts/backfill_nse_filing_documents.py
(the sibling "quarterly filing docs" backfill), simplified: this endpoint
needs no desc/text classification at all.

CRITICAL BOUNDARY -- this script must NEVER:
  * parse/extract facts from a downloaded document,
  * write to canonical_financials or financial_observations,
  * advance a raw_objects row's state past "stored" (no "validated"/
    "parsed"/"ingested" -- store_raw_object() already leaves new rows at
    exactly "stored", and this script calls nothing that would move them
    further).

Usage:
  python -m scripts.backfill_nse_annual_reports --companies HDFCBANK,AAVAS --dry-run
  python -m scripts.backfill_nse_annual_reports --companies HDFCBANK,AAVAS
  python -m scripts.backfill_nse_annual_reports --index "Nifty 500" --dry-run
  python -m scripts.backfill_nse_annual_reports --index "Nifty 500"

--dry-run discovers + dedupes + filters only and logs what WOULD be
stored (how many rows after the 2015 filter, PDF vs ZIP counts), without
any download beyond the one discovery call, and without touching S3 or
raw_objects at all. Real-run mode does the same discovery, then downloads
(unwrapping a ZIP to its real annual-report PDF where needed) and stores
every resulting PDF.

--companies and --index are mutually exclusive; --index resolves the
company list from company_index_membership (the same source
scripts/batch_fetch_nse.py's own --index flag already reads), restricted
to companies with an nse_symbol on file.
"""

from __future__ import annotations

import argparse
import logging

# Must run before any other import in this file touches storage.repositories/
# company_repository/raw_object_repository -- see storage/backend_bootstrap.py's
# own docstring and scripts/run_job.py's identical top-of-file comment: a
# module that does `from storage import company_repository as repo` at
# import time (companies/registry.py does exactly this) binds to the
# pre-swap SQLite module forever if install() runs after that import.
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from dataclasses import dataclass, field

from companies.registry import get_company
from ingestion.batch_log import BatchRun
from sources.nse_annual_reports import (
    AnnualReportRef,
    AnnualReportZipError,
    annual_reports_url,
    discover_company_annual_reports,
    download_file,
    extract_annual_report_pdf,
    fiscal_year_label,
)
from sources.nse_fetch import NSEFetchError, _new_session
from storage.backend_bootstrap import open_db
from storage.company_repository import select_index_members_with_nse_symbol
from storage.raw_object_store import store_raw_object

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_nse_annual_reports")

JOB_NAME = "nse_annual_reports_backfill"

#: This is the one source_url/raw_prefix ADR-022 assigns for
#: company-originated NSE disclosures (see that ADR's bucket-layout table:
#: "raw/companies/ -- filings, annual/quarterly PDFs, XBRL, disclosures...").
_RAW_PREFIX = "companies"
_SOURCE = "nse"
_OBJECT_TYPE = "annual_report"


@dataclass
class CompanyBackfillResult:
    company_id: str
    symbol: str
    total_rows_after_filter: int = 0
    pdf_count: int = 0
    zip_count: int = 0
    stored_new: int = 0
    stored_duplicate: int = 0
    download_errors: int = 0
    zip_extraction_errors: int = 0

    def summary(self) -> str:
        return (
            f"rows={self.total_rows_after_filter} (pdf={self.pdf_count} zip={self.zip_count}) "
            f"stored_new={self.stored_new} stored_duplicate={self.stored_duplicate} "
            f"download_errors={self.download_errors} zip_extraction_errors={self.zip_extraction_errors}"
        )


def _is_stale_connection_error(exc: BaseException) -> bool:
    """True for a Postgres connection that's gone bad mid-run -- Neon
    closing/recycling a long-lived connection during a multi-hundred-
    company batch. Observed live during this job's own full-Nifty-500
    dry run: succeeded cleanly for 109 companies (each one a single quick
    DB lookup interleaved with NSE network calls), then died on company
    110 with psycopg2's own "server closed the connection unexpectedly".
    Matched by exception class name/message rather than a hard `import
    psycopg2` at module level, since this script (like storage/database.py's
    init_postgres_db) must stay importable in a SQLite-only environment
    with psycopg2 not installed at all -- the SQLite path never raises
    this shape of error in the first place, so a false-negative match
    there is harmless."""
    name = type(exc).__name__
    text = str(exc)
    return name in ("OperationalError", "InterfaceError") and (
        "server closed the connection" in text
        or "connection already closed" in text
        or "terminat" in text.lower()
    )


def _resolve_symbol_map(conn, companies: list[str]) -> dict[str, str]:
    """company_id -> nse_symbol for every company that has one on file,
    resolved ONCE up front (a handful of quick, closely-spaced DB calls)
    rather than inside the per-company discovery loop -- that loop's own
    DB touch is exactly what triggered the stale-connection failure above:
    a single shared connection sitting through a hundred-plus NSE network
    round-trips, each one a fresh opportunity for Neon to have recycled it
    since the loop's last (equally brief) query. A company with no
    nse_symbol on file is logged and left out of the returned map, same
    "skip rather than fail the whole run" contract _resolve_companies_by_
    index() already applies for the --index path."""
    symbols: dict[str, str] = {}
    for company_id in companies:
        company = get_company(conn, company_id)
        if company is None:
            logger.warning("%s: no company registered under this id -- skipping", company_id)
            continue
        symbol = company["nse_symbol"]
        if not symbol:
            logger.warning("%s: no nse_symbol on file -- skipping", company_id)
            continue
        symbols[company_id] = symbol
    return symbols


def _resolve_companies_by_index(conn, index_name: str) -> dict[str, str]:
    """company_id -> nse_symbol for every Nifty 500 (or other index) member
    with an nse_symbol on file -- one single query already returns both
    columns, so (unlike _resolve_symbol_map's --companies path) there's
    nothing further to resolve per company."""
    rows = select_index_members_with_nse_symbol(conn, index_name)
    return {row["company_id"]: row["nse_symbol"] for row in rows}


def _log_ref(ref: AnnualReportRef, *, action: str) -> None:
    logger.info(
        "  [%s->%s %s] %s file=%s submission=%s dttm=%s",
        ref.from_yr, ref.to_yr, "zip" if ref.is_zip else "pdf",
        action, ref.file_url, ref.submission_type, ref.dissemination_dttm,
    )


def dry_run_company(company_id: str, symbol: str) -> CompanyBackfillResult:
    """Discovery + dedup + 2015 filter only -- one network call (the
    annual-reports listing), zero PDF/ZIP downloads, zero DB reads/writes
    of any kind (symbol is resolved once up front by the caller, not here
    -- see _resolve_symbol_map()'s docstring for why that matters at
    Nifty-500 scale). Every ref that would be stored is logged so the
    output can be reviewed by a human before any real run."""
    logger.info("%s (%s): discovering via %s", company_id, symbol, annual_reports_url(symbol))

    session = _new_session()
    try:
        refs = discover_company_annual_reports(symbol, session=session)
    finally:
        session.close()

    result = CompanyBackfillResult(company_id=company_id, symbol=symbol)
    result.total_rows_after_filter = len(refs)
    result.pdf_count = sum(1 for ref in refs if not ref.is_zip)
    result.zip_count = sum(1 for ref in refs if ref.is_zip)
    for ref in refs:
        _log_ref(ref, action="WOULD STORE")

    if not refs:
        logger.info("%s: zero rows after the 2015 filter -- no annual-report history on file at all, or "
                     "none since 2015 (verify against the live endpoint if this company should have some)",
                     company_id)

    logger.info("%s: dry-run summary -- %s", company_id, result.summary())
    return result


def backfill_company(company_id: str, symbol: str) -> CompanyBackfillResult:
    """Discovery + dedup + real download/store, unwrapping a ZIP to its
    real annual-report PDF before storing. Every stored fiscal year is
    handed to store_raw_object(), which dedups by content hash (a
    byte-identical re-fetch across repeat runs writes nothing new) and
    leaves the new raw_objects row at state="stored" -- this function
    never calls update_raw_object_state(), so no row from this job is ever
    advanced past "stored" (see this module's docstring).

    Opens (and closes) its OWN short-lived DB connection for the
    store_raw_object() calls, rather than sharing one connection across
    the whole multi-hundred-company run -- deliberately, per the same
    stale-connection finding _resolve_symbol_map() documents: a
    connection that only needs to be alive for one company's own
    (typically few-second) download+store sequence, opened right before
    it's needed, is far less likely to have gone stale than one held for
    the entire run's wall-clock duration. Also retried once via
    _is_stale_connection_error() as a further backstop, in case even a
    single company's own processing time is enough to trip it."""
    logger.info("%s (%s): discovering via %s", company_id, symbol, annual_reports_url(symbol))

    session = _new_session()
    try:
        refs = discover_company_annual_reports(symbol, session=session)

        result = CompanyBackfillResult(company_id=company_id, symbol=symbol)
        result.total_rows_after_filter = len(refs)
        result.pdf_count = sum(1 for ref in refs if not ref.is_zip)
        result.zip_count = sum(1 for ref in refs if ref.is_zip)

        store_conn = open_db() if refs else None
        try:
            for ref in refs:
                try:
                    downloaded = download_file(session, ref.file_url)
                except NSEFetchError as exc:
                    result.download_errors += 1
                    logger.warning("%s: failed to download %s->%s (%s): %s",
                                    company_id, ref.from_yr, ref.to_yr, ref.file_url, exc)
                    continue

                if ref.is_zip:
                    try:
                        content = extract_annual_report_pdf(downloaded)
                    except AnnualReportZipError as exc:
                        result.zip_extraction_errors += 1
                        logger.warning("%s: ZIP for %s->%s (%s) has no usable PDF: %s",
                                        company_id, ref.from_yr, ref.to_yr, ref.file_url, exc)
                        continue
                else:
                    content = downloaded

                period = fiscal_year_label(ref.to_yr)
                try:
                    raw_result = store_raw_object(
                        store_conn,
                        source=_SOURCE,
                        entity=company_id,
                        object_type=_OBJECT_TYPE,
                        period=period,
                        source_url=ref.file_url,
                        raw_prefix=_RAW_PREFIX,
                        content=content,
                        extension="pdf",
                    )
                except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry-once backstop
                    if not _is_stale_connection_error(exc):
                        raise
                    logger.warning("%s: DB connection went stale mid-company (%s) -- reopening and retrying once",
                                    company_id, exc)
                    store_conn.close()
                    store_conn = open_db()
                    raw_result = store_raw_object(
                        store_conn,
                        source=_SOURCE,
                        entity=company_id,
                        object_type=_OBJECT_TYPE,
                        period=period,
                        source_url=ref.file_url,
                        raw_prefix=_RAW_PREFIX,
                        content=content,
                        extension="pdf",
                    )
                if raw_result.is_new:
                    result.stored_new += 1
                    _log_ref(ref, action=f"STORED object_id={raw_result.object_id} key={raw_result.s3_key}")
                else:
                    result.stored_duplicate += 1
                    _log_ref(ref, action=f"DUPLICATE (existing object_id={raw_result.object_id})")
        finally:
            if store_conn is not None:
                store_conn.close()
    finally:
        session.close()

    logger.info("%s: run summary -- %s", company_id, result.summary())
    return result


def run_dry_run(symbol_map: dict[str, str]) -> list[CompanyBackfillResult]:
    """The dry-run per-company loop -- zero DB reads/writes of any kind
    (symbol_map is already fully resolved by the caller), zero PDF/ZIP
    downloads. A dry run makes no writes at all, and BatchRun itself
    writes batch_job_runs/batch_job_items rows, so dry-run mode
    intentionally never touches BatchRun -- logging a run that did
    nothing real would misrepresent the audit log."""
    results: list[CompanyBackfillResult] = []
    for company_id, symbol in symbol_map.items():
        try:
            results.append(dry_run_company(company_id, symbol))
        except NSEFetchError as exc:
            logger.error("%s: dry-run failed: %s", company_id, exc)
    return results


def run_real_backfill(conn, symbol_map: dict[str, str]) -> list[CompanyBackfillResult]:
    """The real-run per-company loop, audited via BatchRun (same "one
    capability, Audit Log -> Job Runs gets every run" convention as
    scripts/batch_fetch_nse.py's run_nse_batch()). `conn` is used ONLY for
    BatchRun's own lightweight start/finish bookkeeping -- backfill_company()
    opens its own short-lived connection per company for the actual
    store_raw_object() writes (see that function's docstring).

    Wrapped in an outer retry loop that resumes with the remaining
    (not-yet-attempted) companies under a fresh run_id if BatchRun's own
    bookkeeping connection goes stale mid-run -- a genuinely long run
    across Nifty 500's full company list is exactly the shape of run that
    hit this in dry-run form (109 companies in before the connection
    died), so a real run (much slower per company, real downloads) must
    not let one blip lose everything already done."""
    results: list[CompanyBackfillResult] = []
    remaining = dict(symbol_map)
    while remaining:
        scope_label = f"nse_annual_reports_backfill ({len(remaining)} companies remaining)"
        done: list[str] = []
        try:
            with BatchRun(conn, JOB_NAME, scope_label) as run:
                logger.info("run_id=%s", run.run_id)
                for company_id, symbol in remaining.items():
                    with run.item(company_id) as item:
                        result = backfill_company(company_id, symbol)
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
            remaining = {cid: sym for cid, sym in remaining.items() if cid not in done}
            logger.warning(
                "BatchRun bookkeeping connection went stale (%s) -- reopened it and resuming "
                "under a new run_id with %d company(ies) remaining", exc, len(remaining),
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--companies", help="comma-separated company_id list (e.g. HDFCBANK,AAVAS)")
    group.add_argument("--index", help='company_index_membership index_name, e.g. "Nifty 500"')
    parser.add_argument("--dry-run", action="store_true",
                         help="discover + dedupe + filter only; log what would be stored, write nothing")
    args = parser.parse_args()

    conn = open_db()
    try:
        if args.companies:
            requested = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
            if not requested:
                raise SystemExit("--companies resolved to an empty list")
            symbol_map = _resolve_symbol_map(conn, requested)
        else:
            symbol_map = _resolve_companies_by_index(conn, args.index)
            logger.info("--index %r resolved to %d companies with an nse_symbol on file", args.index, len(symbol_map))
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
    total_rows = sum(r.total_rows_after_filter for r in results)
    total_pdf = sum(r.pdf_count for r in results)
    total_zip = sum(r.zip_count for r in results)
    total_stored_new = sum(r.stored_new for r in results)
    total_stored_dup = sum(r.stored_duplicate for r in results)
    total_download_errors = sum(r.download_errors for r in results)
    total_zip_errors = sum(r.zip_extraction_errors for r in results)
    for result in results:
        print(f"  {result.company_id}: {result.summary()}")
    print()
    print(
        f"TOTALS: rows={total_rows} (pdf={total_pdf} zip={total_zip}) "
        f"stored_new={total_stored_new} stored_duplicate={total_stored_dup} "
        f"download_errors={total_download_errors} zip_extraction_errors={total_zip_errors}"
    )


if __name__ == "__main__":
    main()
