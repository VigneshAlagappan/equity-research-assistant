"""Knowledge Builder Worker -- wraps research/knowledge_builder.py's Step 2A
extraction as an independent subscriber to `document` DATASET_INGESTED
events, instead of ingestion/coordinator.py calling it inline.

Re-fetches the documents row by event.scope["document_id"] rather than
having it passed through the event -- the event only carries a pointer
(README: Signals Dataset-Centric Ingestion), and re-fetching means
event_bus.replay() can rerun extraction for an old document purely from the
Event Store, with no re-upload needed.
"""

from __future__ import annotations

from ingestion.event_bus import WorkerResult, register_worker
from ingestion.events import DatasetIngestedEvent
from research.knowledge_builder import KnowledgeExtractionError, extract_document_knowledge
from storage.repositories import get_document

WORKER_NAME = "knowledge_builder"
WORKER_VERSION = "1"


def run(conn, event: DatasetIngestedEvent) -> WorkerResult:
    if event.dataset_type != "document":
        return WorkerResult(status="skipped")

    document_id = event.scope.get("document_id")
    row = get_document(conn, document_id)
    if row is None:
        return WorkerResult(status="skipped")

    try:
        extraction = extract_document_knowledge(conn, row)
    except KnowledgeExtractionError as exc:
        return WorkerResult(status="failed", error=str(exc))

    return WorkerResult(
        status="ok",
        output_reference=f"claims_created={extraction.claims_created}",
        data={"claims_created": extraction.claims_created},
    )


register_worker(WORKER_NAME, WORKER_VERSION, run)
