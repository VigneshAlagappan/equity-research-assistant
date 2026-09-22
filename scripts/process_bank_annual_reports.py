"""Process already-downloaded NSE Annual Report PDFs for a list of bank
companies through sources/nse_pdf_annual_report.py via this app's standard
ingest_file() pipeline, and register each PDF in the `documents` table so
it appears on the company's Docs tab.

Generalization of scripts/process_aubank_annual_reports.py (AU Small
Finance Bank, done first as the single-company pilot that built/fixed the
parser) to the rest of the NSE-listed banks (industry LIKE 'Banks%') that
already have annual-report PDFs on file in S3. Explicit user request:
"repeat AU Small finance Bank steps for rest of the downloaded annual
reports from S3 (do not pull new from NSE)" -- scoped to banks only,
since sources/nse_pdf_annual_report.py's line-item labels (Deposits/
Advances/Interest Earned/Capital, RBI Third Schedule format) only match a
bank's own statement layout, not a general Ind-AS company's.

Does NOT touch NSE at all -- every PDF byte comes from the S3 object
already on file (storage/document_store.py's retrieve(), using the exact
raw_objects.s3_key from the earlier NSE backfill), never re-downloaded.

Usage: python3 -m scripts.process_bank_annual_reports COMPANY_ID [COMPANY_ID ...]
"""

from __future__ import annotations

import logging
import sys
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
logger = logging.getLogger("process_bank_annual_reports")


def _already_extracted_fiscal_years(conn, company_id: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT fiscal_year FROM financial_observations "
            "WHERE company_id = %s AND parser_version = %s",
            (company_id, PARSER_VERSION),
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


def process_company(conn, store, company_id: str) -> dict:
    """Returns a per-company summary dict for the caller's final report --
    same shape whether every year extracted cleanly or every year was
    skipped (a company whose PDFs simply don't match this parser's
    line-item labels still finishes without raising, just with zero
    reconciled facts, exactly like AU SFB's FY2017/FY2018 NBFC-era
    reports)."""
    raw_rows = list_raw_objects(conn, source="nse", entity=company_id, object_type="annual_report", limit=100)
    raw_rows = sorted(raw_rows, key=lambda r: r["period"])
    logger.info("=== %s: %d raw annual-report objects ===", company_id, len(raw_rows))

    already_registered = {
        row["source_url"] for row in list_company_documents(conn, company_id) if row["source_url"]
    }
    already_extracted = _already_extracted_fiscal_years(conn, company_id)
    if already_extracted:
        logger.info("%s: already has extracted facts for %s -- skipping extraction for these",
                     company_id, sorted(already_extracted))

    total_reconciled = 0
    years_extracted: list[str] = []
    years_no_data = []
    years_failed = []

    for row in raw_rows:
        period = row["period"]
        s3_key = row["s3_key"]
        source_url = row["source_url"]
        content_hash = row["content_hash"]

        if period in already_extracted:
            logger.info("%s %s: already extracted -- skipping", company_id, period)
        else:
            content = store.retrieve(s3_key)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            try:
                try:
                    result = ingest_file(
                        conn, tmp_path, company_id=company_id, source_id="nse_pdf_annual_report",
                        statement_type="standalone",
                    )
                except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry-once, same as AU SFB's script
                    if not _is_stale_connection_error(exc):
                        raise
                    logger.warning("%s %s: DB connection went stale -- reopening and retrying once", company_id, period)
                    conn.close()
                    conn = open_db()
                    result = ingest_file(
                        conn, tmp_path, company_id=company_id, source_id="nse_pdf_annual_report",
                        statement_type="standalone",
                    )
                logger.info(
                    "%s %s: parsed=%d inserted=%d skipped=%d reconciled=%d",
                    company_id, period, result.parsed_count, result.inserted_count,
                    result.skipped_count, result.reconciled_count,
                )
                if result.skip_reasons:
                    for reason in result.skip_reasons:
                        logger.warning("  skipped: %s", reason)
                total_reconciled += result.reconciled_count
                if result.parsed_count > 0:
                    years_extracted.append(period)
                else:
                    years_no_data.append(period)
            except Exception as exc:  # noqa: BLE001 -- one bad PDF/company must not abort the rest of the batch
                logger.error("%s %s: extraction failed with an unexpected error (%s: %s) -- continuing",
                              company_id, period, type(exc).__name__, exc)
                years_failed.append(period)
            finally:
                tmp_path.unlink(missing_ok=True)

        if source_url in already_registered:
            logger.info("%s %s: document already registered for Docs tab -- skipping", company_id, period)
            continue
        save_company_document(
            conn, company_id,
            document_type="annual_report",
            fiscal_year=period,
            quarter=None,
            added_by_user=None,
            raw_file_path=None,
            source_url=source_url,
            storage_object_key=s3_key,
            content_hash=content_hash,
        )
        logger.info("%s %s: registered in Docs tab", company_id, period)

    return {
        "company_id": company_id,
        "n_reports": len(raw_rows),
        "reconciled": total_reconciled,
        "years_extracted": years_extracted,
        "years_no_data": years_no_data,
        "years_failed": years_failed,
    }


def main(company_ids: list[str]) -> None:
    conn = open_db()
    store = default_document_store()

    summaries = []
    for company_id in company_ids:
        summaries.append(process_company(conn, store, company_id))

    conn.close()
    print()
    print("=== Summary ===")
    grand_total = 0
    for s in summaries:
        grand_total += s["reconciled"]
        print(f"{s['company_id']}: {s['n_reports']} reports, {s['reconciled']} facts reconciled, "
              f"extracted={s['years_extracted']}, no_data={s['years_no_data']}, failed={s['years_failed']}")
    print(f"\nTotal: {grand_total} facts reconciled across {len(summaries)} companies.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -m scripts.process_bank_annual_reports COMPANY_ID [COMPANY_ID ...]")
        sys.exit(1)
    main(sys.argv[1:])
