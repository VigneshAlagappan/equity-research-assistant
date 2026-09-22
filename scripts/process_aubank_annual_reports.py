"""One-off: process AU Small Finance Bank's already-downloaded NSE Annual
Report PDFs (raw_objects, source='nse', object_type='annual_report',
entity='AUBANK') through the new sources/nse_pdf_annual_report.py adapter
via this app's standard ingest_file() pipeline, and register each PDF in
the `documents` table so it appears on the company's Docs tab.

Deliberately scoped to ONE company, run once, by hand -- not a repeatable
CLI job like scripts/backfill_nse_annual_reports.py. Explicit user request:
"Process downloaded annual reports of AU Small Finance Bank and fill
canonical financial data ... (dont pull new info from NSE). Also
reference respective Annual reports with Docs section."

Does NOT touch NSE at all -- every PDF byte comes from the S3 object
already on file (storage/document_store.py's retrieve(), using the exact
raw_objects.s3_key from the earlier backfill), never re-downloaded.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from ingestion.pipeline import ingest_file
from sources.nse_pdf_annual_report import PARSER_VERSION
from storage.backend_bootstrap import open_db
from storage.document_store import default_document_store
from storage.raw_object_repository import list_raw_objects
from storage.repositories import list_company_documents, save_company_document

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("process_aubank_annual_reports")

COMPANY_ID = "AUBANK"


def _already_extracted_fiscal_years(conn) -> set[str]:
    """Fiscal years this exact parser has already written observations for
    -- an earlier partial run of this script (before two real page-
    detection bugs were found and fixed) already processed FY2023
    successfully with correct output; financial_observations has no
    uniqueness constraint beyond its own identity column, so re-running an
    already-correct year would just insert duplicate rows, not fix or
    change anything. Skipped, not deleted-and-redone -- the existing
    FY2023 rows are already right."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT fiscal_year FROM financial_observations "
            "WHERE company_id = %s AND parser_version = %s",
            (COMPANY_ID, PARSER_VERSION),
        )
        return {row["fiscal_year"] for row in cur.fetchall()}


def _is_stale_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return name in ("OperationalError", "InterfaceError") and (
        "server closed the connection" in text
        or "connection already closed" in text
        or "terminat" in text.lower()
    )


def main() -> None:
    conn = open_db()
    store = default_document_store()

    raw_rows = list_raw_objects(conn, source="nse", entity=COMPANY_ID, object_type="annual_report", limit=100)
    raw_rows = sorted(raw_rows, key=lambda r: r["period"])
    logger.info("Found %d raw annual-report objects for %s", len(raw_rows), COMPANY_ID)

    already_registered = {
        row["source_url"] for row in list_company_documents(conn, COMPANY_ID) if row["source_url"]
    }
    already_extracted = _already_extracted_fiscal_years(conn)
    if already_extracted:
        logger.info("Already has extracted facts for: %s -- will skip financial extraction for these "
                    "(Docs tab registration still runs)", sorted(already_extracted))

    total_reconciled = 0
    for row in raw_rows:
        period = row["period"]  # e.g. "FY2023"
        s3_key = row["s3_key"]
        source_url = row["source_url"]
        content_hash = row["content_hash"]

        logger.info("=== %s (%s) ===", period, s3_key)

        if period in already_extracted:
            logger.info("%s: already has extracted facts from a previous run -- skipping extraction", period)
        else:
            # 1. Extract facts -> financial_observations -> reconcile(), via
            #    the standard ingest_file() pipeline (this app's normal path
            #    for every other source too). Downloads the PDF bytes from S3
            #    to a local temp file first -- ingest_file()'s adapter
            #    interface takes a real file path, not raw bytes.
            content = store.retrieve(s3_key)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            try:
                try:
                    result = ingest_file(
                        conn, tmp_path, company_id=COMPANY_ID, source_id="nse_pdf_annual_report",
                        statement_type="standalone",
                    )
                except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry-once, same as every other script this session
                    if not _is_stale_connection_error(exc):
                        raise
                    logger.warning("%s: DB connection went stale -- reopening and retrying once", period)
                    conn.close()
                    conn = open_db()
                    result = ingest_file(
                        conn, tmp_path, company_id=COMPANY_ID, source_id="nse_pdf_annual_report",
                        statement_type="standalone",
                    )
                logger.info(
                    "%s: parsed=%d inserted=%d skipped=%d reconciled=%d",
                    period, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
                )
                if result.skip_reasons:
                    for reason in result.skip_reasons:
                        logger.warning("  skipped: %s", reason)
                total_reconciled += result.reconciled_count
            finally:
                tmp_path.unlink(missing_ok=True)

        # 2. Register the document for the Docs tab -- reuses the SAME S3
        #    key/content_hash already on file (no re-upload), skipped if
        #    this exact source_url is already registered (idempotent,
        #    same "skip what's already on file" convention
        #    scripts/fetch_investor_relations.py's own docstring uses).
        if source_url in already_registered:
            logger.info("%s: document already registered for Docs tab -- skipping", period)
            continue
        save_company_document(
            conn, COMPANY_ID,
            document_type="annual_report",
            fiscal_year=period,
            quarter=None,
            added_by_user=None,  # officially sourced -- same convention as investor_relations/NSE fetches
            raw_file_path=None,  # S3-only, no local path
            source_url=source_url,
            storage_object_key=s3_key,
            content_hash=content_hash,
        )
        logger.info("%s: registered in Docs tab", period)

    conn.close()
    print()
    print(f"Done. {len(raw_rows)} annual reports processed, {total_reconciled} total canonical facts reconciled.")


if __name__ == "__main__":
    main()
