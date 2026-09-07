"""Quarterly job: run every document sitting at documents.processing_status
='pending' (transcripts, concall presentations, annual report docs, etc.)
through Step 1 registration + the Knowledge Builder extraction pipeline.

Closes the scheduling gap SCHEDULED_JOBS.md section 4 and web/app.py's
`doc_analysis` ScheduledJob row flagged: the extraction logic itself
(research/knowledge_builder.py, wired through ingestion/coordinator.py's
process_all_pending_documents()) was already real, it just had no trigger
except the Admin -> Ingest queue's "Process All Pending" button. There is
still no automated fetch source for new transcripts/presentations (no NSE/
BSE announcements scraper exists) -- this job only ever processes files a
human has already manually uploaded through the Docs tab by the time it
runs. That half of the gap is unchanged; this closes the scheduling half.

No new batch-loop needed here, unlike scripts/batch_fetch_nse.py or
scripts/batch_generate_insights.py: process_all_pending_documents() already
loops every pending document and already wraps itself in its own BatchRun
(ingestion/batch_log.py, job_name="document_processing", see
ingestion/coordinator.py's process_documents()) -- the per-document audit
trail this job needs already exists. Reusing it here rather than
duplicating its loop is exactly ADR-015's point: a scheduled trigger and
the existing manual "Process All Pending" button must converge on the same
job implementation, not grow a second one.

Usage: python -m scripts.process_pending_documents_batch
(a plain `python scripts/process_pending_documents_batch.py` fails on the
`ingestion`/`storage` imports below -- run as a module so the repo root,
not scripts/, lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

from ingestion.coordinator import process_all_pending_documents
from storage.database import init_db
from storage.repositories import get_latest_batch_job_run

JOB_NAME = "document_processing"


def run_document_processing_batch(conn=None) -> int:
    """The actual pending-documents sweep, factored out of main() so the
    Settings > Data Operations > Schedule panel's "Run now" button
    (web/app.py) can trigger the identical job on demand -- same
    one-capability-two-triggers shape every other job in this file's
    sibling scripts uses.

    process_all_pending_documents() opens its own BatchRun internally and
    doesn't hand the run_id back out (it returns a ProcessSummary instead,
    the same return shape the Admin -> Ingest queue route already consumes)
    -- since this whole call is synchronous and blocking (no concurrent
    trigger of the same job can be in flight), the row get_latest_batch_job_
    run() finds immediately afterward for JOB_NAME is guaranteed to be the
    one this call just created. That avoids reaching into
    process_all_pending_documents()/process_documents() to change what they
    return just for this caller's benefit -- every other caller of those
    (the Ingest queue routes) is left untouched.

    Returns the BatchRun's run_id."""
    owns_conn = conn is None
    if conn is None:
        conn = init_db()

    try:
        summary = process_all_pending_documents(conn)
        print(
            f"Done. attempted={summary.attempted} succeeded={summary.succeeded} "
            f"failed={summary.failed}",
            flush=True,
        )
        last_run = get_latest_batch_job_run(conn, JOB_NAME)
        return last_run["run_id"] if last_run else None
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_document_processing_batch()


if __name__ == "__main__":
    main()
