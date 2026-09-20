"""Pull a company's full NSE quarterly-result filing PDFs, investor/concall
presentations, and concall transcripts, and store them RAW in S3 (or local
disk under DOCUMENT_STORE_BACKEND=local) via storage/raw_object_store.py --
deliberately WITHOUT running any of it through this app's document-
processing/knowledge-extraction pipeline. This is a storage-only step;
parsing/extraction is a separate, later decision (see this script's own
module docstring boundary below).

Built on sources/nse_filing_documents.py's discovery + classification, and
storage/raw_object_store.py's existing dedup-by-hash raw-object catalog
(docs/ADR/022) -- same "one shared mechanism, not per-source ad hoc" and
"land raw bytes under raw/ before any parsing" discipline every other NSE
source (nse_corporate_actions, nse_shareholding) already follows via
scripts/batch_fetch_nse.py.

CRITICAL BOUNDARY -- this script must NEVER:
  * parse/extract facts from a downloaded document,
  * write to canonical_financials or financial_observations,
  * advance a raw_objects row's state past "stored" (no "validated"/
    "parsed"/"ingested" -- store_raw_object() already leaves new rows at
    exactly "stored", and this script calls nothing that would move them
    further).

Usage:
  python -m scripts.backfill_nse_filing_documents --companies BANKBARODA,AAVAS --dry-run
  python -m scripts.backfill_nse_filing_documents --companies BANKBARODA,AAVAS

--dry-run discovers + classifies and logs what WOULD be stored, without any
network download beyond the one discovery call, and without touching S3 or
raw_objects at all. Real-run mode does the same discovery, then downloads
and stores every confidently-classified filing.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from companies.registry import get_company
from ingestion.batch_log import BatchRun
from sources.nse_filing_documents import (
    DOCUMENT_TYPES,
    ClassifiedFiling,
    announcements_url,
    attachment_extension,
    discover_company_filings,
    download_document,
)
from sources.nse_fetch import NSEFetchError, _new_session
from storage.backend_bootstrap import open_db
from storage.raw_object_store import store_raw_object

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_nse_filing_documents")

JOB_NAME = "nse_filing_documents_backfill"

#: This is the one source_url/raw_prefix ADR-022 assigns for
#: company-originated NSE disclosures (see that ADR's bucket-layout table:
#: "raw/companies/ -- filings, annual/quarterly PDFs, XBRL, disclosures...").
_RAW_PREFIX = "companies"
_SOURCE = "nse"


@dataclass
class CompanyBackfillResult:
    company_id: str
    symbol: str
    total_announcements: int = 0
    classified_counts: dict[str, int] = field(default_factory=dict)
    unclassified_count: int = 0
    stored_new: int = 0
    stored_duplicate: int = 0
    download_errors: int = 0

    def summary(self) -> str:
        classified_str = ", ".join(f"{k}={v}" for k, v in sorted(self.classified_counts.items())) or "none"
        return (
            f"announcements={self.total_announcements} classified=[{classified_str}] "
            f"unclassified={self.unclassified_count} stored_new={self.stored_new} "
            f"stored_duplicate={self.stored_duplicate} download_errors={self.download_errors}"
        )


def _resolve_symbol(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")
    symbol = company["nse_symbol"]
    if not symbol:
        raise ValueError(f"{company_id} has no nse_symbol on file")
    return symbol


def _log_classified(filing: ClassifiedFiling, *, action: str) -> None:
    logger.info(
        "  [%s] %s desc=%r period=%s broadcast=%s url=%s",
        filing.document_type, action, filing.desc, filing.period, filing.broadcast_date, filing.attchmnt_file,
    )


def dry_run_company(conn, company_id: str) -> CompanyBackfillResult:
    """Discovery + classification only -- one network call (the
    corporate-announcements listing), zero PDF/ZIP downloads, zero writes
    to S3 or raw_objects. Every classified row is logged so the output can
    be reviewed by a human before any real run."""
    symbol = _resolve_symbol(conn, company_id)
    logger.info("%s (%s): discovering via %s", company_id, symbol, announcements_url(symbol))

    session = _new_session()
    try:
        classified, unclassified = discover_company_filings(symbol, session=session)
    finally:
        session.close()

    result = CompanyBackfillResult(company_id=company_id, symbol=symbol)
    result.total_announcements = len(classified) + len(unclassified)
    for doc_type in DOCUMENT_TYPES:
        matches = [f for f in classified if f.document_type == doc_type]
        result.classified_counts[doc_type] = len(matches)
        logger.info("%s: %s -- %d row(s) would be stored", company_id, doc_type, len(matches))
        for filing in matches:
            _log_classified(filing, action="WOULD STORE")

    result.unclassified_count = len(unclassified)
    if unclassified:
        logger.info("%s: %d row(s) had an attachment but could not be confidently classified -- skipped:",
                     company_id, len(unclassified))
        for row in unclassified:
            logger.info(
                "  [unclassified] desc=%r attchmntText=%r url=%s",
                row.get("desc"), (row.get("attchmntText") or "")[:160], row.get("attchmntFile"),
            )

    logger.info("%s: dry-run summary -- %s", company_id, result.summary())
    return result


def backfill_company(conn, company_id: str) -> CompanyBackfillResult:
    """Discovery + classification + real download/store. Every
    confidently-classified filing is downloaded once and handed to
    store_raw_object(), which dedups by content hash (a byte-identical
    re-fetch across repeat runs writes nothing new) and leaves the new
    raw_objects row at state="stored" -- this function never calls
    update_raw_object_state(), so no row from this job is ever advanced
    past "stored" (see this module's docstring)."""
    symbol = _resolve_symbol(conn, company_id)
    logger.info("%s (%s): discovering via %s", company_id, symbol, announcements_url(symbol))

    session = _new_session()
    try:
        classified, unclassified = discover_company_filings(symbol, session=session)

        result = CompanyBackfillResult(company_id=company_id, symbol=symbol)
        result.total_announcements = len(classified) + len(unclassified)
        result.unclassified_count = len(unclassified)

        for filing in classified:
            result.classified_counts[filing.document_type] = result.classified_counts.get(filing.document_type, 0) + 1
            try:
                content = download_document(session, filing.attchmnt_file)
            except NSEFetchError as exc:
                result.download_errors += 1
                logger.warning("%s: failed to download %s (%s): %s",
                                company_id, filing.document_type, filing.attchmnt_file, exc)
                continue

            raw_result = store_raw_object(
                conn,
                source=_SOURCE,
                entity=company_id,
                object_type=filing.document_type,
                period=filing.period,
                source_url=filing.attchmnt_file,
                raw_prefix=_RAW_PREFIX,
                content=content,
                extension=attachment_extension(filing.attchmnt_file),
            )
            if raw_result.is_new:
                result.stored_new += 1
                _log_classified(filing, action=f"STORED object_id={raw_result.object_id} key={raw_result.s3_key}")
            else:
                result.stored_duplicate += 1
                _log_classified(filing, action=f"DUPLICATE (existing object_id={raw_result.object_id})")
    finally:
        session.close()

    logger.info("%s: run summary -- %s", company_id, result.summary())
    return result


def run_backfill(conn, companies: list[str], *, dry_run: bool) -> list[CompanyBackfillResult]:
    """The per-company loop, audited via BatchRun (same "one capability,
    Audit Log -> Job Runs gets every run" convention as
    scripts/batch_fetch_nse.py's run_nse_batch()) -- but ONLY in real-run
    mode. A dry run makes no writes of any kind (per this task's explicit
    "dry-run the discovery+classification step first ... don't write
    anything" instruction), and a BatchRun itself writes batch_job_runs/
    batch_job_items rows, so dry-run mode intentionally skips it rather
    than logging a run that did nothing real."""
    results: list[CompanyBackfillResult] = []
    if dry_run:
        for company_id in companies:
            results.append(dry_run_company(conn, company_id))
        return results

    scope_label = f"nse_filing_documents_backfill ({len(companies)} companies)"
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
    parser.add_argument("--companies", required=True, help="comma-separated company_id list (e.g. BANKBARODA,AAVAS)")
    parser.add_argument("--dry-run", action="store_true",
                         help="discover + classify only; log what would be stored, write nothing")
    args = parser.parse_args()

    companies = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
    if not companies:
        raise SystemExit("--companies resolved to an empty list")

    conn = open_db()
    try:
        results = run_backfill(conn, companies, dry_run=args.dry_run)
    finally:
        conn.close()

    print()
    print(f"{'DRY RUN' if args.dry_run else 'REAL RUN'} complete -- {len(results)} company(ies)")
    for result in results:
        print(f"  {result.company_id}: {result.summary()}")


if __name__ == "__main__":
    main()
