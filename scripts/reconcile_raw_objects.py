"""S3<->Postgres catalog reconciliation (docs/ADR/022-s3-raw-processed-
object-store-with-lineage-catalog.md) -- compares what's actually stored
under `raw/` (S3, or local disk when DOCUMENT_STORE_BACKEND=local) against
what `raw_objects` says should be there, and reports two kinds of drift:

  * orphaned S3 keys -- an object physically exists but no catalog row
    points at it (e.g. a write that succeeded but whose catalog INSERT
    then failed/rolled back).
  * broken catalog rows -- a raw_objects row's s3_key doesn't resolve to
    anything in the store (e.g. manual deletion outside this app, or a
    catalog row whose write preceded the object write and the object
    write never actually happened).

Never deletes or recreates anything, per ADR-022's explicit requirement --
this is report-only. A human decides what to do with a finding (Audit Log
-> Job Runs shows every one, same as any other job here).

Usage: python -m scripts.reconcile_raw_objects
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ingestion.batch_log import BatchRun
from storage.backend_bootstrap import open_db
from storage.document_store import DocumentStore, default_document_store
from storage.raw_object_repository import RAW_PREFIXES, list_all_raw_objects

JOB_NAME = "raw_object_reconciliation"


@dataclass
class ReconciliationResult:
    checked_count: int
    orphaned_s3_keys: list[str] = field(default_factory=list)
    broken_catalog_rows: list[dict] = field(default_factory=list)

    @property
    def orphan_count(self) -> int:
        return len(self.orphaned_s3_keys)

    @property
    def broken_count(self) -> int:
        return len(self.broken_catalog_rows)

    @property
    def clean(self) -> bool:
        return self.orphan_count == 0 and self.broken_count == 0


def reconcile_raw_objects(conn, document_store: DocumentStore | None = None) -> ReconciliationResult:
    """The actual comparison -- factored out of run_raw_object_reconciliation()
    below so it's directly testable without the BatchRun/audit-log
    machinery around it, same "one capability, two triggers" shape every
    other job in this app already uses."""
    store = document_store or default_document_store()
    catalog_rows = list_all_raw_objects(conn)
    catalog_keys = {row["s3_key"] for row in catalog_rows}

    store_keys: set[str] = set()
    for prefix in RAW_PREFIXES:
        store_keys.update(store.list_keys(f"raw/{prefix}/"))

    orphaned = sorted(store_keys - catalog_keys)
    broken = [dict(row) for row in catalog_rows if row["s3_key"] not in store_keys]

    return ReconciliationResult(
        checked_count=len(catalog_rows), orphaned_s3_keys=orphaned, broken_catalog_rows=broken,
    )


def run_raw_object_reconciliation(conn=None) -> int:
    """BatchRun-audited wrapper -- the Settings > Data Operations >
    Schedule panel's "Run now" button (once wired into scheduling/jobs.py)
    and the CLI below both drive this. Every finding becomes its own
    'failed' batch_job_items row (not because the reconciliation itself
    failed -- the run finishes 'completed' regardless of what it finds --
    but so each drift shows up individually in Audit Log -> Job Runs,
    exactly the way a real per-object problem should, not buried in one
    summary line)."""
    owns_conn = conn is None
    if conn is None:
        conn = open_db()
    try:
        result = reconcile_raw_objects(conn)
        with BatchRun(conn, JOB_NAME, scope_label=f"{result.checked_count} cataloged objects") as run:
            for key in result.orphaned_s3_keys:
                with run.item(None) as item:
                    item.detail = f"orphaned S3 key (no catalog row): {key}"
                    raise RuntimeError(item.detail)
            for row in result.broken_catalog_rows:
                with run.item(row.get("entity")) as item:
                    item.detail = (
                        f"broken catalog row object_id={row['object_id']} "
                        f"s3_key={row['s3_key']!r} (not found in store)"
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
    run_raw_object_reconciliation()


if __name__ == "__main__":
    main()
