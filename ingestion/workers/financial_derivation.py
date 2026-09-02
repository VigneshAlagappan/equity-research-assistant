"""Financial Derivation Worker -- re-derives canonical_financials from
whatever's already in financial_observations, keyed by the
(company, metric, period) keys a company_financials DATASET_INGESTED event's
storage_reference names.

Deliberately downstream of ingestion, not inside it (README: Signals
Dataset-Centric Ingestion -- ingestion is complete once data is stored;
derived calculations are a worker's job). Reuses storage/repositories.py's
existing reconcile() one-key-at-a-time function unchanged -- no new
reconciliation logic here, just relocating who calls it and when.

Idempotent by construction: reconcile() always re-derives the same
canonical_value from whatever's currently in financial_observations for
that exact key, so running it again (a replay, or a duplicate dispatch)
never produces a different or duplicated result.
"""

from __future__ import annotations

from ingestion.event_bus import WorkerResult, register_worker
from ingestion.events import DatasetIngestedEvent
from storage.repositories import reconcile

WORKER_NAME = "financial_derivation"
WORKER_VERSION = "1"


def run(conn, event: DatasetIngestedEvent) -> WorkerResult:
    if event.dataset_type != "company_financials":
        return WorkerResult(status="skipped")

    keys = event.storage_reference.get("reconcile_keys") or []
    if not keys:
        return WorkerResult(status="skipped")

    count = sum(1 for key in keys if reconcile(conn, *key) is not None)
    return WorkerResult(
        status="ok", output_reference=f"reconciled_count={count}", data={"reconciled_count": count}
    )


register_worker(WORKER_NAME, WORKER_VERSION, run)
