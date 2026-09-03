"""Chunk Indexer Worker -- wraps research/document_chunker.py's Step 2D
chunking/indexing as an independent subscriber to `document`
DATASET_INGESTED events, instead of ingestion/coordinator.py calling it
inline right after the Knowledge Builder.

Independent of the Knowledge Builder Worker on purpose -- event_bus.publish()
dispatches every registered worker separately, so a chunking failure here
never undoes an already-successful extraction, same graceful-degradation
behavior ingestion/coordinator.py's process_documents() had before this
worker existed.
"""

from __future__ import annotations

from ingestion.event_bus import WorkerResult, register_worker
from ingestion.events import DatasetIngestedEvent
from research.document_chunker import chunk_and_index_document
from storage.repositories import get_document

WORKER_NAME = "chunk_indexer"
WORKER_VERSION = "1"


def run(conn, event: DatasetIngestedEvent) -> WorkerResult:
    if event.dataset_type != "document":
        return WorkerResult(status="skipped")

    document_id = event.scope.get("document_id")
    row = get_document(conn, document_id)
    if row is None:
        return WorkerResult(status="skipped")

    try:
        chunk_count = chunk_and_index_document(conn, row)
    except Exception as exc:  # noqa: BLE001 -- best-effort, same tolerance coordinator.py had inline
        return WorkerResult(status="failed", error=str(exc))

    return WorkerResult(
        status="ok", output_reference=f"chunk_count={chunk_count}", data={"chunk_count": chunk_count}
    )


register_worker(WORKER_NAME, WORKER_VERSION, run)
