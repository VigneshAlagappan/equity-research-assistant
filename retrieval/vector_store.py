"""VectorStore — the backend-independent abstraction over "wherever chunk
embeddings are indexed for semantic search" (architecture.md "Hybrid Document
Retrieval"). Mirrors this codebase's two existing swappable-backend patterns:

  * storage/ (SQLite today; a repository/data-access seam, dependency-
    injected, never a global singleton import) for structured facts.
  * config.settings.GRAPH_BACKEND / context/graph.py + context/graph_neo4j.py
    for the knowledge graph (SQLite traversal by default, Neo4j opt-in,
    graceful fallback on connectivity failure).

STRICT RULE (architecture.md, this feature's spec): no business logic,
research module, planner, Flask route, ingestion worker, or investigation
code may import or call a specific vector-database SDK. Only
retrieval/vector_store_qdrant.py (the concrete implementation selected by
config.settings.VECTOR_STORE_BACKEND) imports qdrant_client. Everywhere else
depends on this module's Protocol/dataclasses/factory, or receives a
VectorStore via dependency injection — same "capability, not a direct
import" seam research/capabilities.py already established for every other
Planner dependency. Swapping Qdrant for pgvector/Chroma/a managed service
later means adding one new retrieval/vector_store_<backend>.py module and
changing VECTOR_STORE_BACKEND — no other file in this codebase changes.

The vector DB is NOT a source of truth (architecture.md's invariant: "FTS5,
Vector DB, and Neo4j are retrieval/projection structures. They must never
silently become competing sources of truth."). Every VectorRecord upserted
here is fully rebuildable from storage/repositories.py's `documents` +
`document_chunks` tables — retrieval/semantic_indexer.py is the only writer,
and it always re-derives records from those tables, never from anything
cached only in the vector store itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class VectorStoreUnavailable(Exception):
    """Raised by a VectorStore method when the backend cannot serve this
    request right now — unreachable service, timeout, auth failure, missing
    collection that can't be created. Every concrete backend translates its
    own SDK-specific exceptions into this ONE type, so callers
    (retrieval/hybrid_search.py, retrieval/semantic_search.py, the embedding
    indexer worker) never need to know which backend is configured to
    degrade gracefully (section 10) — same role
    llm/providers/base.py's ProviderUnavailable plays for LLM providers."""


@dataclass(frozen=True)
class VectorRecord:
    """One chunk's embedding plus the provenance needed to turn a raw vector
    hit back into a properly cited Signal evidence object without a second
    database round-trip. `chunk_id` doubles as the vector's ID in every
    backend — document_chunks.chunk_id is already a stable, unique integer,
    so there is no reason to mint a second identifier space."""

    chunk_id: int
    document_id: int
    company_id: str | None
    embedding: list[float]
    page_number: int | None = None
    document_type: str | None = None
    fiscal_year: str | None = None
    quarter: str | None = None
    source: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class VectorMatch:
    """One search hit — just enough to rank and then hydrate. Full
    provenance is re-read from document_chunks/documents by chunk_id
    (retrieval/semantic_search.py), not trusted from the vector store's own
    payload, so the vector store staying authoritative for nothing is a
    property of every caller, not just a design intention."""

    chunk_id: int
    score: float


class VectorStore(Protocol):
    """Every concrete backend satisfies this shape structurally — no base
    class needed, same minimal pattern llm/providers/base.py's
    Provider(Protocol) already uses for swappable LLM providers."""

    def upsert(self, records: list[VectorRecord]) -> None:
        """Index (or re-index) the given records. Upsert semantics by
        chunk_id — calling this again with the same chunk_id replaces that
        vector rather than duplicating it, which is what makes
        retrieval/semantic_indexer.py's backfill idempotent."""
        ...

    def delete_document(self, document_id: int) -> None:
        """Remove every vector belonging to this document_id. Called before
        re-upserting a reprocessed document's fresh chunks (their chunk_ids
        change on reprocess — storage/repositories.py's
        replace_document_chunks() docstring — so stale vectors under the old
        chunk_ids would otherwise never be reachable again but would also
        never be cleaned up)."""
        ...

    def search(
        self, query_embedding: list[float], *, company_id: str | None = None, limit: int = 10
    ) -> list[VectorMatch]:
        """Nearest-neighbour search by cosine similarity, optionally scoped
        to one company. Returns [] (not an error) when the collection
        doesn't exist yet (nothing has been indexed) — same "absence isn't
        an error" convention retrieval/document_search.py's search_documents
        already follows for FTS5."""
        ...

    def health_check(self) -> bool:
        """True if the backend is reachable right now. Never raises — a
        connectivity problem is exactly what this method exists to report,
        not to propagate. retrieval/hybrid_search.py calls this before
        relying on search()/upsert() so a down vector store degrades to
        FTS5/BM25-only instead of raising mid-investigation."""
        ...


def default_vector_store() -> VectorStore | None:
    """The only place that imports a concrete backend module directly —
    everywhere else routes through the VectorStore seam (dependency
    injection, or this factory). Returns None when
    config.settings.VECTOR_STORE_BACKEND="none" — every caller treats a None
    store exactly like an unreachable one (section 10: continue with
    FTS5/BM25 only). Reads settings at call time, not import time, so tests
    can monkeypatch the backend choice."""
    from config import settings

    backend = settings.VECTOR_STORE_BACKEND
    if backend in ("none", "disabled", ""):
        return None
    if backend == "qdrant":
        from retrieval.vector_store_qdrant import QdrantVectorStore

        return QdrantVectorStore()
    raise ValueError(f"Unknown VECTOR_STORE_BACKEND={backend!r} (expected 'qdrant' or 'none')")
