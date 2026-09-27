"""Discover, download, extract, and ingest NSE quarterly-results PDFs for a
SMALL, explicit list of companies — the nse-pdf-backfill task's Phases 1-3
wired together into one runnable script. Deliberately requires an explicit
`--company` list (no "run over every company" mode) — bulk/unsupervised
backfill against production is a separate, explicitly-approved next step
(the feasibility report's own Section 10/12.4 recommendation: spot-check a
small sample manually before trusting the discovery filter unsupervised).

Pipeline per company:
  1. sources/nse_pdf_filings.discover_result_filings() — date-window match
     every expected quarter in [--from-year, --to-year] to its primary
     result filing.
  2. Download the match's attachment (PDF or ZIP -- a ZIP is unwrapped,
     feasibility report Section 13.1: NSE's own pre-2019 ZIPs usually just
     wrap a single real PDF).
  3. sources/nse_pdf_extractor.extract_from_pdf() -- no usable text layer
     -> logged to nse_filing_discovery_log as needs_ocr, nothing else
     happens for that quarter (no OCR attempted, per scope).
  4. A real extraction -> the PDF is registered as a `documents` row (Docs
     tab, same as any other officially-sourced filing) and its facts are
     ingested via ingestion.pipeline.ingest_nse_pdf_observations() (which
     itself skips any (metric, period) canonical_financials already has --
     see that function's own docstring).
  5. Every quarter attempted -- extracted, needs_ocr, not_found, or failed
     -- gets one row in nse_filing_discovery_log either way.

--dry-run discovers and downloads but skips extraction/writes entirely
(prints what WOULD happen) -- use scripts/qa_nse_pdf_filings.py instead for
the full manual-review report this task's Phase 4 calls for; this flag is
a lighter smoke-test, not a substitute for that review.
"""

from __future__ import annotations

import argparse
import logging
import zipfile
from datetime import date
from pathlib import Path

from companies.registry import get_company
from config import settings
from config.settings import to_repo_relative
from ingestion.pipeline import ingest_nse_pdf_observations
from sources.nse_fetch import _new_session
from sources.nse_pdf_extractor import extract_from_pdf, facts_to_observations
from sources.nse_pdf_filings import discover_result_filings, download_filing, quarter_end_dates
from storage.database import init_db
from storage.document_store import default_document_store
from storage.repositories import save_company_document, upsert_nse_filing_discovery_log

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_nse_pdf_filings")

_XBRL_ERA_START_YEAR = 2019  # per feasibility report Section 8 -- P&L extraction is only in-scope before this


def _unwrap_zip_if_needed(path: Path) -> Path | None:
    if not zipfile.is_zipfile(path):
        return path
    extract_dir = path.parent / f"{path.stem}_extracted"
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        return None
    pdfs = [n for n in names if n.lower().endswith(".pdf")]
    if not pdfs:
        return None
    return extract_dir / pdfs[0]


def process_company(
    conn, session, company_id: str, nse_symbol: str, *, from_year: int, to_year: int, dry_run: bool,
) -> None:
    start, end = date(from_year, 1, 1), date(to_year, 12, 31)
    matches = discover_result_filings(nse_symbol, start=start, end=end, session=session)
    matched_quarters = {(m.fiscal_year, m.quarter) for m in matches}

    for qe in quarter_end_dates(start, end):
        match = next((m for m in matches if m.period_end == qe), None)
        if match is None:
            upsert_nse_filing_discovery_log(
                conn, company_id=company_id, nse_symbol=nse_symbol,
                fiscal_year="", quarter="", period_end=qe.isoformat(),
                extraction_status="not_found", notes="no result filing in the +75d date window",
            )
            continue

        logger.info(
            "%s %s%s: matched seq_id=%s desc=%r confidence=%s format=%s",
            company_id, match.fiscal_year, match.quarter, match.seq_id, match.desc,
            match.match_confidence, match.attachment_format,
        )

        if dry_run:
            continue

        if match.attachment_format not in ("pdf", "zip"):
            upsert_nse_filing_discovery_log(
                conn, company_id=company_id, nse_symbol=nse_symbol,
                fiscal_year=match.fiscal_year, quarter=match.quarter, period_end=qe.isoformat(),
                filing_date=match.filing_date, source_url=match.source_url, document_id=match.seq_id,
                match_confidence=match.match_confidence, attachment_format=match.attachment_format,
                extraction_status="not_found", notes=f"desc={match.desc!r}, no usable attachment",
            )
            continue

        scratch_dir = settings.RAW_DIR / company_id / "nse_pdf_scratch"
        raw_dest = scratch_dir / f"{match.fiscal_year}_{match.quarter}_{match.seq_id}.{match.attachment_format}"
        if not download_filing(session, match, raw_dest):
            upsert_nse_filing_discovery_log(
                conn, company_id=company_id, nse_symbol=nse_symbol,
                fiscal_year=match.fiscal_year, quarter=match.quarter, period_end=qe.isoformat(),
                filing_date=match.filing_date, source_url=match.source_url, document_id=match.seq_id,
                match_confidence=match.match_confidence, attachment_format=match.attachment_format,
                extraction_status="failed", notes="download failed",
            )
            continue

        pdf_path = _unwrap_zip_if_needed(raw_dest) if match.attachment_format == "zip" else raw_dest
        if pdf_path is None:
            upsert_nse_filing_discovery_log(
                conn, company_id=company_id, nse_symbol=nse_symbol,
                fiscal_year=match.fiscal_year, quarter=match.quarter, period_end=qe.isoformat(),
                filing_date=match.filing_date, source_url=match.source_url, document_id=match.seq_id,
                match_confidence=match.match_confidence, attachment_format=match.attachment_format,
                extraction_status="needs_ocr", notes="zip did not contain a usable PDF",
            )
            continue

        include_pnl = int(match.fiscal_year.replace("FY", "")) < _XBRL_ERA_START_YEAR
        facts = extract_from_pdf(
            pdf_path, expected_quarter_end=qe, expected_fiscal_year=match.fiscal_year,
            expected_quarter=match.quarter, include_pnl=include_pnl,
        )

        if not facts:
            upsert_nse_filing_discovery_log(
                conn, company_id=company_id, nse_symbol=nse_symbol,
                fiscal_year=match.fiscal_year, quarter=match.quarter, period_end=qe.isoformat(),
                filing_date=match.filing_date, source_url=match.source_url, document_id=match.seq_id,
                match_confidence=match.match_confidence, attachment_format=match.attachment_format,
                extraction_status="needs_ocr", notes="no usable text layer / no recognized balance-sheet section",
            )
            continue

        # Register the source PDF in the Docs tab, same as any other
        # officially-sourced filing (storage/document_store.py) -- content
        # goes through the active DocumentStore backend (S3 in production).
        store = default_document_store()
        storage_key = to_repo_relative(
            settings.DOCUMENTS_DIR / company_id / "nse_pdf" / f"{match.fiscal_year}_{match.quarter}_{match.seq_id}.pdf"
        )
        content = pdf_path.read_bytes()
        store.store(storage_key, content)
        doc_row = save_company_document(
            conn, company_id, document_type="financial_result", fiscal_year=match.fiscal_year,
            quarter=match.quarter, added_by_user=None, source_url=match.source_url,
            storage_object_key=storage_key,
        )
        document_id = doc_row["document_id"]

        observations = facts_to_observations(
            facts, company_id=company_id, source_file=str(pdf_path), source_document_id=document_id,
        )
        result = ingest_nse_pdf_observations(conn, company_id, observations, source_file=str(pdf_path))

        upsert_nse_filing_discovery_log(
            conn, company_id=company_id, nse_symbol=nse_symbol,
            fiscal_year=match.fiscal_year, quarter=match.quarter, period_end=qe.isoformat(),
            filing_date=match.filing_date, source_url=match.source_url, document_id=match.seq_id,
            match_confidence=match.match_confidence, attachment_format=match.attachment_format,
            extraction_status="extracted", extracted_char_count=sum(len(str(f.value)) for f in facts),
            registered_document_id=document_id,
            notes=f"inserted={result.inserted_count} skipped={result.skipped_count} reconciled={result.reconciled_count}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", action="append", required=True, help="company_id, e.g. HDFCBANK (repeatable)")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument("--dry-run", action="store_true", help="discover + log matches only, no download/extraction/writes")
    args = parser.parse_args()

    conn = init_db()
    session = _new_session()
    try:
        for company_id in args.company:
            company_id = company_id.upper()
            company = get_company(conn, company_id)
            if company is None or not company["nse_symbol"]:
                logger.warning("%s: not registered or no nse_symbol on file — skipping", company_id)
                continue
            logger.info("=== %s (%s) ===", company_id, company["nse_symbol"])
            process_company(
                conn, session, company_id, company["nse_symbol"],
                from_year=args.from_year, to_year=args.to_year, dry_run=args.dry_run,
            )
    finally:
        session.close()
        conn.close()


if __name__ == "__main__":
    main()
