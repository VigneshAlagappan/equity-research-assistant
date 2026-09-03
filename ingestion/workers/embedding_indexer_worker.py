"""Embedding Indexer Worker — wraps retrieval/semantic_indexer.py's embedding
generation + VectorStore upsert as an independent subscriber to `document`
DATASET_INGESTED events (section 12: "after backfill, normal document
ingestion should automatically maintain both indexes").

Registered AFTER chunk_indexer_worker in ingestion/workers/__init__.py's
import order — ingestion/event_bus.py dispatches registered workers in
registration order, so this worker always runs after research/
document_chunker.py has (re)written the document's chunks within the same
publish() call, and can read them straight back via
retrieval/semantic_indexer.py's embed_and_index_document_chunks() without
any extra coordination.

Independent of both the Knowledge Builder and Chunk Indexer workers on
purpose (same reasoning chunk_indexer_worker.py's own docstring gives for
being independent of the Knowledge Builder): event_bus.publish() dispatches
every registered worker separately in its own try/except, so an embedding
failure here never undoes an already-successful chunk/FTS5 index, and a
chunking failure (zero chunks) just means this worker has nothing to embed
(section 5, section 10).

A VectorStore that is disabled or unreachable is treated as "skipped", not
"failed" — section 10's graceful degradation: research must still work with
FTS5/BM25 alone, so a missing/down Qdrant is an expected, loggable, retryable
condition (retry via `python main.py replay-events --worker embedding_indexer`
once the vector store is back), not an ingestion error.
"""

from __future__ import annotations

import logging

from ingestion.event_bus import WorkerResult, register_worker
from ingestion.events import DatasetIngestedEvent
from retrieval.embedding_provider import EmbeddingProviderUnavailable
from retrieval.semantic_indexer import embed_and_index_document_chunks
from retrieval.vector_store import VectorStoreUnavailable
from storage.repositories import get_document

logger = logging.getLogger(__name__)

WORKER_NAME = "embedding_indexer"
WORKER_VERSION = "1"


def run(conn, event: DatasetIngestedEvent) -> WorkerResult:
    if event.dataset_type != "document":
        return WorkerResult(status="skipped")

    document_id = event.scope.get("document_id")
    row = get_document(conn, document_id)
    if row is None:
        return WorkerResult(status="skipped")

    try:
        result = embed_and_index_document_chunks(conn, row)
    except VectorStoreUnavailable as exc:
        logger.warning(
            "Embedding indexing skipped for document %s (vector store unavailable, FTS5 unaffected): %s",
            document_id, exc,
        )
        return WorkerResult(status="skipped", output_reference=f"vector store unavailable: {exc}")
    except EmbeddingProviderUnavailable as exc:
        logger.warning("Embedding indexing failed for document %s (embedding provider): %s", document_id, exc)
        return WorkerResult(status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - best-effort, same tolerance chunk_indexer_worker has inline
        return WorkerResult(status="failed", error=str(exc))

    if result.chunks_total == 0:
        return WorkerResult(status="skipped", output_reference="no chunks to embed")

    return WorkerResult(
        status="ok",
        output_reference=f"chunks_embedded={result.chunks_embedded} already_indexed={result.chunks_already_indexed}",
        data={"chunks_embedded": result.chunks_embedded, "chunks_already_indexed": result.chunks_already_indexed},
    )


register_worker(WORKER_NAME, WORKER_VERSION, run)
