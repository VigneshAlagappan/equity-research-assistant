"""Semantic (embedding/vector) passage search — the vector-side twin of
retrieval/document_search.py's FTS5 keyword search (section 6). Answers
"where was something *conceptually* similar discussed?", finding a passage
even when the question's wording never appears in the source text (e.g. a
question about "profitability" surfacing a passage that only ever says
"bottom line" or "net income").

No LLM call anywhere in this path (section 6: "No LLM required to perform
the search itself") — embedding a query is a deterministic vector lookup,
identical in spirit to retrieval/document_search.py's FTS5 MATCH query, just
over a different index.

Dependency shape (STRICT, section 2):

    semantic_search_documents()
        -> EmbeddingProvider interface (retrieval/embedding_provider.py)
        -> VectorStore interface (retrieval/vector_store.py)
        -> FactStore interface (storage/fact_store.py) to hydrate hits back
           into full-provenance DocumentPassage rows

This module never imports sentence_transformers, voyageai, or qdrant_client
directly — only the concrete provider/store modules do.

Standalone and directly testable, same as retrieval/document_search.py's
search_documents() — retrieval/hybrid_search.py composes this WITH keyword
search rather than this module knowing anything about FTS5.
"""

from __future__ import annotations

import time
from storage.db_types import DBConnection

from research.temporal import date_visible
from retrieval.document_search import DocumentPassage
from retrieval.embedding_provider import EmbeddingProvider, default_embedding_provider
from retrieval.vector_store import VectorStore, VectorStoreUnavailable, default_vector_store
from storage.fact_store import FactStore, default_fact_store

#: How many raw vector hits to request before company/as_of filtering and
#: hydration — mirrors retrieval/document_search.py's own "ask the index for
#: more than `limit`, since some may be filtered out after the fact" shape,
#: needed here because as_of filtering (unlike company scoping) happens
#: after the vector store's own search, not inside it.
_OVER_FETCH_FACTOR = 3


def semantic_search_documents(
    conn: DBConnection,
    query: str,
    *,
    company_id: str | None = None,
    limit: int = 10,
    as_of: str | None = None,
    fact_store: FactStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
) -> list[DocumentPassage]:
    """Embed `query`, search the VectorStore, hydrate hits into typed
    DocumentPassage evidence with full provenance preserved. Returns []
    (never raises) for a blank query or when the vector store has nothing
    indexed yet — same "absence isn't an error" convention
    retrieval/document_search.py's search_documents() already follows.

    Raises VectorStoreUnavailable if the configured vector store is disabled
    or unreachable — retrieval/hybrid_search.py is the caller that catches
    this and degrades to FTS5/BM25-only (section 10); a caller that wants
    semantic search in isolation (this function, called directly) sees the
    failure rather than a silently empty result, so it is never mistaken for
    "found nothing."

    `as_of` (ISO date) drops passages from documents published after the
    cutoff, applied after the vector search rather than as a payload filter
    — research/temporal.py's date_visible(), the exact same convention
    retrieval/document_search.py's search_documents() uses, so a historical
    investigation cannot see a semantically-similar-but-future document
    (section 6, section 14: "future documents cannot leak into historical
    investigations")."""
    passages, _embedding_ms, _vector_ms = search_documents_semantic_timed(
        conn, query, company_id=company_id, limit=limit, as_of=as_of,
        fact_store=fact_store, embedding_provider=embedding_provider, vector_store=vector_store,
    )
    return passages


def search_documents_semantic_timed(
    conn: DBConnection,
    query: str,
    *,
    company_id: str | None = None,
    limit: int = 10,
    as_of: str | None = None,
    fact_store: FactStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
) -> tuple[list[DocumentPassage], float | None, float | None]:
    """Same behavior as semantic_search_documents(), plus (embedding_latency_ms,
    vector_store_latency_ms) — split out so retrieval/hybrid_search.py can
    record both independently (section 13) without this module's public,
    timing-free function growing extra return values everyone else has to
    ignore."""
    if not query or not query.strip():
        return [], None, None

    fs = fact_store or default_fact_store()
    store = vector_store if vector_store is not None else default_vector_store()
    if store is None:
        raise VectorStoreUnavailable("VECTOR_STORE_BACKEND=none — semantic search is disabled")

    provider = embedding_provider or default_embedding_provider()
    t0 = time.monotonic()
    query_vector = provider.embed_text(query)
    embedding_latency_ms = (time.monotonic() - t0) * 1000

    t1 = time.monotonic()
    matches = store.search(query_vector, company_id=company_id, limit=limit * _OVER_FETCH_FACTOR)
    vector_store_latency_ms = (time.monotonic() - t1) * 1000

    if not matches:
        return [], embedding_latency_ms, vector_store_latency_ms

    score_by_chunk_id = {m.chunk_id: m.score for m in matches}
    rows = {row["chunk_id"]: row for row in fs.get_document_chunks_by_ids(conn, list(score_by_chunk_id))}

    passages: list[DocumentPassage] = []
    # Preserve the VectorStore's own ranking (best match first) rather than
    # dict/row order — a stale vector pointing at a since-deleted chunk
    # (row missing from `rows`) is dropped silently, same convention
    # storage/repositories.py's get_document_chunks_by_ids() docstring
    # describes.
    for match in matches:
        row = rows.get(match.chunk_id)
        if row is None:
            continue
        if as_of and not date_visible(row["published_at"], as_of):
            continue
        passages.append(
            DocumentPassage(
                chunk_id=row["chunk_id"], document_id=row["document_id"], company_id=row["company_id"],
                text=row["text"], page_number=row["page_number"], document_type=row["document_type"],
                fiscal_year=row["fiscal_year"], quarter=row["quarter"], source=row["source"],
                published_at=row["published_at"], retrieval_source="semantic", semantic_score=match.score,
            )
        )
        if len(passages) >= limit:
            break
    return passages, embedding_latency_ms, vector_store_latency_ms
