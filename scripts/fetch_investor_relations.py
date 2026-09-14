"""Fetch + register a company's investor-relations documents (earnings
presentations, annual reports/shareholder letters, concall recordings)
straight from their own IR website — see sources/investor_relations.py's
own module docstring for the two site architectures this covers (Q4 Inc.
platform vs. Berkshire's static site) and why 10-K/10-Q/proxy PDFs are
deliberately skipped.

Downloads each new document to data/documents/investor_relations/<company_id>/
and registers it via storage.repositories.save_company_document() with
added_by_user=None (officially sourced, same as an SEC EDGAR or NSE fetch —
see that field's own docstring), same "download once, skip what's already
on file" idempotency as scripts/fetch_nse_xbrl.py's download_filing().
Safe to re-run: a document already registered (matched by source_url) is
never re-inserted.

Usage (run as a module):
    python -m scripts.fetch_investor_relations AMZN
    python -m scripts.fetch_investor_relations GOOGL BRKB
"""

from __future__ import annotations

import argparse
import asyncio

from companies.registry import get_company
from ingestion.batch_log import BatchRun
from sources.investor_relations import (
    BERKSHIRE_COMPANY_IDS,
    DEFAULT_IR_DOCUMENTS_DIR,
    Q4_COMPANIES,
    IRFetchError,
    download_document,
    fetch_company_documents,
)
from storage.database import init_db
from storage.repositories import list_company_documents, save_company_document

SUPPORTED_COMPANY_IDS = sorted(set(Q4_COMPANIES) | set(BERKSHIRE_COMPANY_IDS))


async def fetch_one_company(conn, company_id: str) -> str:
    company = get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company registered as {company_id!r}")

    refs = await fetch_company_documents(company_id)
    already_registered = {row["source_url"] for row in list_company_documents(conn, company_id) if row["source_url"]}

    dest_dir = DEFAULT_IR_DOCUMENTS_DIR / company_id
    downloaded = linked = skipped = errors = 0
    for ref in refs:
        if ref.url in already_registered:
            skipped += 1
            continue

        # Audio (concall_recording) is link-only for now -- deliberately
        # not downloaded (raw_file_path=None, same "a plain link, no
        # uploaded file" case the documents table's own raw_file_path
        # column comment already describes). Revisit once this app has a
        # transcription step to actually do something with the audio
        # file; until then, downloading tens of MB of .mp3/.wav per
        # quarter has nothing to feed.
        if ref.url.lower().endswith((".mp3", ".wav")):
            save_company_document(
                conn, company_id,
                document_type=ref.document_type,
                fiscal_year=ref.fiscal_year or "FY0000",
                quarter=ref.quarter,
                added_by_user=None,
                raw_file_path=None,
                source_url=ref.url,
            )
            linked += 1
            print(f"  {ref.document_type:22s} {ref.fiscal_year or '?':8s} {ref.quarter or '':3s} {ref.title[:50]} (link only)", flush=True)
            continue

        try:
            storage_key = download_document(ref, dest_dir)
        except IRFetchError as exc:
            errors += 1
            print(f"  ERROR downloading {ref.url}: {exc}", flush=True)
            continue
        save_company_document(
            conn, company_id,
            document_type=ref.document_type,
            fiscal_year=ref.fiscal_year or "FY0000",
            quarter=ref.quarter,
            added_by_user=None,  # officially sourced -- see documents.added_by_user's own docstring
            raw_file_path=storage_key,
            source_url=ref.url,
            storage_object_key=storage_key,
        )
        downloaded += 1
        print(f"  {ref.document_type:22s} {ref.fiscal_year or '?':8s} {ref.quarter or '':3s} {ref.title[:50]}", flush=True)

    detail = f"found={len(refs)} downloaded={downloaded} linked={linked} skipped={skipped} (already on file)"
    if errors:
        detail += f" errors={errors}"
    return detail


def run_investor_relations_batch(conn, company_ids: list[str] | None = None, scope_label: str | None = None) -> int:
    """The same per-company fetch_one_company() loop as main_async() below,
    but audited via BatchRun (batch_job_runs/batch_job_items) instead of
    plain stdout -- the counterpart to scripts/batch_fetch_nse.py's
    run_nse_batch()/scripts/batch_fetch_sec_edgar.py's run_sec_edgar_batch(),
    so this can be wired into scheduling/jobs.py's registry (CLI + Schedule
    panel "Run now" + cron trigger, all three call paths) the same way
    those already are. `company_ids` defaults to every supported company
    (Q4 + Berkshire) -- the scheduled-job case; the CLI below still accepts
    an explicit subset for ad-hoc single-company runs.

    fetch_one_company() is async (fetch_company_documents() does concurrent
    HTTP requests per source) -- run via asyncio.run() once per company
    inside the loop, same as main_async() already does, just wrapped in
    run.item() for the audit trail instead of a bare try/except."""
    company_ids = company_ids or SUPPORTED_COMPANY_IDS
    job_name = "investor_relations_fetch"
    scope_label = scope_label or f"investor relations ({len(company_ids)} companies)"

    with BatchRun(conn, job_name, scope_label) as run:
        for company_id in company_ids:
            with run.item(company_id) as item:
                item.detail = asyncio.run(fetch_one_company(conn, company_id))
    return run.run_id


async def main_async(company_ids: list[str]) -> None:
    conn = init_db()
    for company_id in company_ids:
        print(f"{company_id}:", flush=True)
        try:
            detail = await fetch_one_company(conn, company_id)
        except (IRFetchError, ValueError) as exc:
            print(f"  FAILED -- {exc}", flush=True)
            continue
        print(f"  {detail}", flush=True)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("company_id", nargs="+", help=f"one of: {', '.join(SUPPORTED_COMPANY_IDS)}")
    args = parser.parse_args()

    unknown = [c for c in args.company_id if c.upper() not in SUPPORTED_COMPANY_IDS]
    if unknown:
        raise SystemExit(f"no investor_relations fetch config for: {unknown} -- supported: {SUPPORTED_COMPANY_IDS}")

    asyncio.run(main_async([c.upper() for c in args.company_id]))


if __name__ == "__main__":
    main()
