"""Semantic indexing (embedding generation + VectorStore upsert) for a
document's already-chunked text — the embedding-side twin of
research/document_chunker.py's FTS5 indexing (section 5: "the same logical
chunk must have both a keyword-search representation and a semantic-search
representation — do not create a second independent chunking
implementation"). This module never re-derives chunks from a PDF; it only
reads what research/document_chunker.py already wrote to `document_chunks`
via storage/repositories.py's list_document_chunks().

Called from two places, both reusing this exact function so there is only
ever one embedding-generation implementation (section 12):
  * ingestion/workers/embedding_indexer_worker.py — automatically, on every
    future `document` DATASET_INGESTED event, right after the Chunk Indexer
    Worker has (re)written that document's chunks.
  * main.py's `vector-backfill` CLI command — the one-time backfill over
    every already-processed document (section 11).

Idempotent by construction (section 11, section 14): a chunk whose
document_chunks.embedding_status is already 'indexed' under the CURRENT
embedding provider's model_id is skipped without calling the embedding
provider or the vector store at all, unless force=True. A document being
reprocessed gets entirely new chunk_ids (storage/repositories.py's
replace_document_chunks() docstring), so its embedding_status columns come
back 'pending' automatically — this function then deletes the old vectors
for that document_id before upserting the new ones, so stale vectors under
now-orphaned chunk_ids are never left behind (section 14: "document
reprocessing replaces stale vectors").

Graceful degradation (section 10): a VectorStore that is unreachable raises
VectorStoreUnavailable, which this function lets propagate — callers decide
whether that means "skip this document for now, FTS5 still has it" (the
worker) or "stop the whole backfill run and say why" (the CLI). Either way,
nothing here ever touches document_chunks_fts or the FTS5-served text/rank
columns — a failed embedding attempt marks embedding_status='failed' on the
affected chunks and returns; it never deletes or corrupts the keyword index
those same chunks already serve.
"""

from __future__ import annotations

from dataclasses import dataclass

from storage.db_types import DBConnection, Row

from retrieval.embedding_provider import EmbeddingProvider, EmbeddingProviderUnavailable, default_embedding_provider
from retrieval.vector_store import VectorRecord, VectorStore, VectorStoreUnavailable, default_vector_store
from storage.database import utcnow_iso
from storage.fact_store import FactStore, default_fact_store


@dataclass(frozen=True)
class EmbeddingIndexResult:
    document_id: int
    chunks_total: int
    chunks_embedded: int
    chunks_already_indexed: int
    model_id: str | None


def _needs_embedding(chunk: Row, model_id: str, force: bool) -> bool:
    if force:
        return True
    return chunk["embedding_status"] != "indexed" or chunk["embedding_model"] != model_id


def embed_and_index_document_chunks(
    conn: DBConnection,
    document_row: Row,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    fact_store: FactStore | None = None,
    force: bool = False,
) -> EmbeddingIndexResult:
    """(Re-)embed and upsert one document's chunks. Returns a result with
    zero counts (not an error) for a document with no chunks yet — same
    "absence isn't an error" convention research/document_chunker.py's
    chunk_and_index_document() already follows. Raises
    VectorStoreUnavailable if the configured vector store is disabled or
    unreachable, and EmbeddingProviderUnavailable if the embedding provider
    cannot serve the request — callers decide how to handle each."""
    fs = fact_store or default_fact_store()
    document_id = document_row["document_id"]
    chunks = list(fs.list_document_chunks(conn, document_id))
    if not chunks:
        return EmbeddingIndexResult(document_id, 0, 0, 0, None)

    store = vector_store if vector_store is not None else default_vector_store()
    if store is None:
        raise VectorStoreUnavailable("VECTOR_STORE_BACKEND=none — semantic indexing is disabled")
    if not store.health_check():
        raise VectorStoreUnavailable("vector store health check failed — backend unreachable")

    provider = embedding_provider or default_embedding_provider()

    to_embed = [c for c in chunks if _needs_embedding(c, provider.model_id, force)]
    already_indexed = len(chunks) - len(to_embed)
    if not to_embed:
        return EmbeddingIndexResult(document_id, len(chunks), 0, already_indexed, provider.model_id)

    try:
        vectors = provider.embed_batch([c["text"] for c in to_embed])
    except EmbeddingProviderUnavailable:
        fs.set_document_chunks_embedding_status(
            conn, [c["chunk_id"] for c in to_embed], status="failed", model=None, embedded_at=None
        )
        raise

    records = [
        VectorRecord(
            chunk_id=chunk["chunk_id"],
            document_id=chunk["document_id"],
            company_id=chunk["company_id"],
            embedding=vector,
            page_number=chunk["page_number"],
            document_type=chunk["document_type"],
            fiscal_year=chunk["fiscal_year"],
            quarter=chunk["quarter"],
            source=chunk["source"],
            published_at=chunk["published_at"],
        )
        for chunk, vector in zip(to_embed, vectors)
    ]

    # Only delete this document's existing vectors when to_embed covers every
    # current chunk (already_indexed == 0) -- exactly the "brand new
    # document" and "reprocessed document" cases. Reprocessing
    # (research/document_chunker.py) replaces ALL of a document's rows with
    # fresh chunk_ids, so every chunk comes back embedding_status='pending'
    # and to_embed naturally equals the full set -- that's when stale
    # vectors under the now-orphaned OLD chunk_ids need cleaning up via this
    # payload-based delete (their chunk_ids no longer exist to upsert over).
    # When already_indexed > 0 this is a partial retry (e.g. some chunks
    # previously failed) over UNCHANGED chunk_ids -- deleting here would
    # wipe the good, already-indexed chunks' vectors with nothing to
    # re-upsert in their place, so it's skipped: upsert-by-chunk_id alone is
    # enough to add/replace just the chunks in this batch.
    if already_indexed == 0:
        store.delete_document(document_id)
    store.upsert(records)

    now = utcnow_iso()
    fs.set_document_chunks_embedding_status(
        conn, [r.chunk_id for r in records], status="indexed", model=provider.model_id, embedded_at=now
    )

    return EmbeddingIndexResult(document_id, len(chunks), len(records), already_indexed, provider.model_id)
