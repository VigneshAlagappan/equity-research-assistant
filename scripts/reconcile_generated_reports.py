"""S3<->Postgres reconciliation for generated_reports.s3_key -- same idea
as scripts/reconcile_raw_objects.py's ADR-022 catalog reconciliation, but
for research threads (Ask AI/Quick Answer/Signals report artifacts saved to
S3, see web/app.py's own _persist_generated_report_s3) instead of ingested
raw documents. Built after a real production gap: research_thread() used
to 500 outright when a row's s3_key pointed at a missing S3 object (fixed
separately, web/app.py's own fallback-to-Postgres-copy), and 8 of 38 rows
with an s3_key turned out to have exactly that problem when checked by
hand. This is the automated, recurring version of that same check.

Reports two kinds of drift, exactly like reconcile_raw_objects.py:

  * orphaned S3 keys -- an object physically exists under threads/ but no
    generated_reports row points at it (e.g. a write that succeeded but
    whose s3_key-recording UPDATE then failed/never ran).
  * broken catalog rows -- a generated_reports row's s3_key doesn't
    resolve to anything in the store (the class of bug found live). Not
    fatal on its own -- research_thread() already falls back to the row's
    own report_markdown/evidence/followups columns -- but still worth
    surfacing, since that fallback exists to mask exactly this drift, not
    to make it invisible.

Never deletes or recreates anything -- report-only, a human decides what
to do with a finding, same as ADR-022 requires for raw objects.

Usage: python -m scripts.reconcile_generated_reports
"""

from __future__ import annotations

from ingestion.batch_log import BatchRun
from scripts.reconcile_raw_objects import ReconciliationResult
from storage.backend_bootstrap import open_db
from storage.document_store import DocumentStore, default_document_store

JOB_NAME = "generated_report_reconciliation"


def _list_generated_report_s3_keys(conn) -> list[dict]:
    """thread_id/s3_key for every generated_reports row that has an
    s3_key at all -- a row saved before ADR-021's S3 persistence split
    (or one whose _persist_generated_report_s3 call never completed) has
    s3_key IS NULL, which is the already-safe case (research_thread()'s
    plain else-branch), not something to flag here."""
    cur = conn.cursor()
    cur.execute("SELECT thread_id, s3_key FROM generated_reports WHERE s3_key IS NOT NULL")
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def reconcile_generated_reports(conn, document_store: DocumentStore | None = None) -> ReconciliationResult:
    """The actual comparison, factored out of run_generated_report_
    reconciliation() below so it's directly testable without the
    BatchRun/audit-log machinery around it -- same "one capability, two
    triggers" shape every other job in this app already uses."""
    store = document_store or default_document_store()
    catalog_rows = _list_generated_report_s3_keys(conn)
    catalog_keys = {row["s3_key"] for row in catalog_rows}

    store_keys = set(store.list_keys("threads/"))

    orphaned = sorted(store_keys - catalog_keys)
    broken = [row for row in catalog_rows if row["s3_key"] not in store_keys]

    return ReconciliationResult(
        checked_count=len(catalog_rows), orphaned_s3_keys=orphaned, broken_catalog_rows=broken,
    )


def run_generated_report_reconciliation(conn=None) -> int:
    """BatchRun-audited wrapper -- the Settings > Data Operations >
    Schedule panel's "Run now" button and the CLI below both drive this.
    Every finding becomes its own 'failed' batch_job_items row (the run
    itself finishes 'completed' regardless of what it finds -- reporting a
    problem isn't the job failing, see reconcile_raw_objects.py's own
    docstring for the same reasoning), so each drift shows up individually
    in Audit Log -> Job Runs rather than buried in one summary line."""
    owns_conn = conn is None
    if conn is None:
        conn = open_db()
    try:
        result = reconcile_generated_reports(conn)
        with BatchRun(conn, JOB_NAME, scope_label=f"{result.checked_count} threads with an s3_key") as run:
            for key in result.orphaned_s3_keys:
                with run.item(None) as item:
                    item.detail = f"orphaned S3 key (no generated_reports row): {key}"
                    raise RuntimeError(item.detail)
            for row in result.broken_catalog_rows:
                with run.item(row["thread_id"]) as item:
                    item.detail = (
                        f"broken s3_key for thread_id={row['thread_id']!r} "
                        f"s3_key={row['s3_key']!r} (not found in store; "
                        "research_thread() falls back to the Postgres copy for this row)"
                    )
                    raise RuntimeError(item.detail)
        print(
            f"Reconciliation done. checked={result.checked_count} "
            f"orphaned={result.orphan_count} broken={result.broken_count}",
            flush=True,
        )
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_generated_report_reconciliation()


if __name__ == "__main__":
    main()
