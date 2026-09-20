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


def _resolve_symbol(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")
    symbol = company["nse_symbol"]
    if not symbol:
        raise ValueError(f"{company_id} has no nse_symbol on file")
    return symbol


def _resolve_companies_by_index(conn, index_name: str) -> list[str]:
    """company_id list for every Nifty 500 (or other index) member with an
    nse_symbol on file -- companies without one can't be attempted at all
    (there's no symbol to query the endpoint with), so they're excluded
    here rather than failing individually inside the loop."""
    rows = select_index_members_with_nse_symbol(conn, index_name)
    return [row["company_id"] for row in rows]


def _log_ref(ref: AnnualReportRef, *, action: str) -> None:
    logger.info(
        "  [%s->%s %s] %s file=%s submission=%s dttm=%s",
        ref.from_yr, ref.to_yr, "zip" if ref.is_zip else "pdf",
        action, ref.file_url, ref.submission_type, ref.dissemination_dttm,
    )


def dry_run_company(conn, company_id: str) -> CompanyBackfillResult:
    """Discovery + dedup + 2015 filter only -- one network call (the
    annual-reports listing), zero PDF/ZIP downloads, zero writes to S3 or
    raw_objects. Every ref that would be stored is logged so the output
    can be reviewed by a human before any real run."""
    symbol = _resolve_symbol(conn, company_id)
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


def backfill_company(conn, company_id: str) -> CompanyBackfillResult:
    """Discovery + dedup + real download/store, unwrapping a ZIP to its
    real annual-report PDF before storing. Every stored fiscal year is
    handed to store_raw_object(), which dedups by content hash (a
    byte-identical re-fetch across repeat runs writes nothing new) and
    leaves the new raw_objects row at state="stored" -- this function
    never calls update_raw_object_state(), so no row from this job is ever
    advanced past "stored" (see this module's docstring)."""
    symbol = _resolve_symbol(conn, company_id)
    logger.info("%s (%s): discovering via %s", company_id, symbol, annual_reports_url(symbol))

    session = _new_session()
    try:
        refs = discover_company_annual_reports(symbol, session=session)

        result = CompanyBackfillResult(company_id=company_id, symbol=symbol)
        result.total_rows_after_filter = len(refs)
        result.pdf_count = sum(1 for ref in refs if not ref.is_zip)
        result.zip_count = sum(1 for ref in refs if ref.is_zip)

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
            raw_result = store_raw_object(
                conn,
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
        session.close()

    logger.info("%s: run summary -- %s", company_id, result.summary())
    return result


def run_backfill(conn, companies: list[str], *, dry_run: bool) -> list[CompanyBackfillResult]:
    """The per-company loop, audited via BatchRun (same "one capability,
    Audit Log -> Job Runs gets every run" convention as
    scripts/batch_fetch_nse.py's run_nse_batch()) -- but ONLY in real-run
    mode. A dry run makes no writes of any kind, and a BatchRun itself
    writes batch_job_runs/batch_job_items rows, so dry-run mode
    intentionally skips it rather than logging a run that did nothing
    real."""
    results: list[CompanyBackfillResult] = []
    if dry_run:
        for company_id in companies:
            try:
                results.append(dry_run_company(conn, company_id))
            except (ValueError, NSEFetchError) as exc:
                logger.error("%s: dry-run failed: %s", company_id, exc)
        return results

    scope_label = f"nse_annual_reports_backfill ({len(companies)} companies)"
    with BatchRun(conn, JOB_NAME, scope_label) as run:
        logger.info("run_id=%s", run.run_id)
        for company_id in companies:
            with run.item(company_id) as item:
                result = backfill_company(conn, company_id)
                item.detail = result.summary()
                results.append(result)
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
            companies = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
            if not companies:
                raise SystemExit("--companies resolved to an empty list")
        else:
            companies = _resolve_companies_by_index(conn, args.index)
            if not companies:
                raise SystemExit(f"--index {args.index!r} resolved to an empty company list")
            logger.info("--index %r resolved to %d companies with an nse_symbol on file", args.index, len(companies))

        results = run_backfill(conn, companies, dry_run=args.dry_run)
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
