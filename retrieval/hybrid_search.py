"""Hybrid Retriever (section 7) — runs FTS5/BM25 keyword search AND
embedding/vector semantic search for the same query, merges and
deduplicates the results, and ranks them with Reciprocal Rank Fusion (RRF).
This is the module architecture.md's retrieval flow diagram calls
"Hybrid Retriever":

    FTS5 Results + Vector Results -> Hybrid Retriever -> Ranked Top-K Evidence

Deterministic and explainable on purpose (section 7: "make it deterministic/
explainable — do not let an LLM arbitrarily pick the winner"). RRF scores a
passage by 1/(k + rank) summed across whichever method(s) ranked it, so a
passage found near the top by BOTH methods naturally outranks one found by
only one — the "confidence boost" section 7 asks for — without any tunable
weights to hand-pick per query.

This is what research/capabilities.py's `document_search` Planner capability
is bound to by default (section 8) — research/investigation_planner.py's
Protocol/call site never changes; only what backs it does. Calling
retrieval/document_search.py's search_documents() directly still works
unchanged (FTS5 is never replaced, section 1's core rule), and
retrieval/semantic_search.py's semantic_search_documents() still works
standalone too — this module is purely an additive composition on top of
both, not a replacement for either.

Graceful degradation (section 10): if the VectorStore is disabled or
unreachable, or the EmbeddingProvider can't serve a query, this function
logs the degradation and returns FTS5/BM25-only results — research must
still work with no vector infrastructure running at all. A failure here
never touches document_chunks/document_chunks_fts; it only affects this
one retrieval call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from storage.db_types import DBConnection

from retrieval.document_search import DocumentPassage, search_documents
from retrieval.embedding_provider import EmbeddingProvider, EmbeddingProviderUnavailable
from retrieval.semantic_search import search_documents_semantic_timed
from retrieval.vector_store import VectorStore, VectorStoreUnavailable
from storage.fact_store import FactStore, default_fact_store

logger = logging.getLogger(__name__)

#: Over-fetch factor for the keyword leg — RRF needs a real candidate pool
#: from both methods to fuse over, not just `limit` from each.
_OVER_FETCH_FACTOR = 3


@dataclass
class HybridRetrievalDiagnostics:
    """What one hybrid_search_documents() call is worth recording (section
    13) — retrieval/observability.py turns this into a log line + a
    retrieval_diagnostics row. `passages` is a compact per-passage summary
    (ids/ranks/scores only, never text) suitable for json.dumps()."""

    query: str
    company_id: str | None
    as_of: str | None
    keyword_candidate_count: int = 0
    semantic_candidate_count: int = 0
    returned_count: int = 0
    embedding_latency_ms: float | None = None
    vector_store_latency_ms: float | None = None
    keyword_latency_ms: float | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    passages: list[dict] = field(default_factory=list)


def _reciprocal_rank_fusion(
    keyword_passages: list[DocumentPassage], semantic_passages: list[DocumentPassage], *, k: int
) -> list[DocumentPassage]:
    """Standard RRF: score(chunk) = sum over each method that found it of
    1/(k + rank_in_that_method), rank starting at 1. Deduplicates by
    chunk_id — a chunk found by both methods is emitted once, with
    retrieval_source="both" and its semantic_score preserved."""
    scores: dict[int, float] = {}
    merged: dict[int, DocumentPassage] = {}

    for rank, passage in enumerate(keyword_passages, start=1):
        scores[passage.chunk_id] = scores.get(passage.chunk_id, 0.0) + 1.0 / (k + rank)
        merged[passage.chunk_id] = passage

    for rank, passage in enumerate(semantic_passages, start=1):
        scores[passage.chunk_id] = scores.get(passage.chunk_id, 0.0) + 1.0 / (k + rank)
        existing = merged.get(passage.chunk_id)
        if existing is not None:
            merged[passage.chunk_id] = replace(
                existing, retrieval_source="both", semantic_score=passage.semantic_score
            )
        else:
            merged[passage.chunk_id] = passage

    fused = [replace(passage, hybrid_score=scores[chunk_id]) for chunk_id, passage in merged.items()]
    fused.sort(key=lambda p: p.hybrid_score, reverse=True)
    return fused


def hybrid_search_documents(
    conn: DBConnection,
    query: str,
    *,
    company_id: str | None = None,
    limit: int = 10,
    as_of: str | None = None,
    fact_store: FactStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    rrf_k: int | None = None,
) -> list[DocumentPassage]:
    """The Hybrid Retriever's public entry point — same signature shape as
    retrieval/document_search.py's search_documents() (Protocol-compatible
    with research/capabilities.py's DocumentSearchCapability), so it is a
    drop-in replacement for what backs the Planner's `document_search`
    capability.

    Diagnostics are recorded as a best-effort side effect (retrieval/
    observability.py) — a logging failure must never take down a research
    answer, so it is swallowed with a warning rather than raised."""
    from config import settings

    fs = fact_store or default_fact_store()
    k = rrf_k if rrf_k is not None else settings.HYBRID_RETRIEVAL_RRF_K
    diagnostics = HybridRetrievalDiagnostics(query=query, company_id=company_id, as_of=as_of)

    t0 = time.monotonic()
    keyword_passages = search_documents(
        conn, query, company_id=company_id, limit=limit * _OVER_FETCH_FACTOR, fact_store=fs, as_of=as_of
    )
    diagnostics.keyword_latency_ms = (time.monotonic() - t0) * 1000
    diagnostics.keyword_candidate_count = len(keyword_passages)

    semantic_passages: list[DocumentPassage] = []
    try:
        semantic_passages, embed_ms, vector_ms = search_documents_semantic_timed(
            conn, query, company_id=company_id, limit=limit * _OVER_FETCH_FACTOR, as_of=as_of,
            fact_store=fs, embedding_provider=embedding_provider, vector_store=vector_store,
        )
        diagnostics.embedding_latency_ms = embed_ms
        diagnostics.vector_store_latency_ms = vector_ms
    except VectorStoreUnavailable as exc:
        diagnostics.degraded = True
        diagnostics.degradation_reason = f"vector store unavailable: {exc}"
        logger.warning("Hybrid retrieval degraded to FTS5-only (vector store unavailable): %s", exc)
    except EmbeddingProviderUnavailable as exc:
        diagnostics.degraded = True
        diagnostics.degradation_reason = f"embedding provider unavailable: {exc}"
        logger.warning("Hybrid retrieval degraded to FTS5-only (embedding provider unavailable): %s", exc)

    diagnostics.semantic_candidate_count = len(semantic_passages)

    fused = _reciprocal_rank_fusion(keyword_passages, semantic_passages, k=k)[:limit]
    diagnostics.returned_count = len(fused)
    diagnostics.passages = [
        {
            "chunk_id": p.chunk_id, "document_id": p.document_id, "page_number": p.page_number,
            "retrieval_source": p.retrieval_source, "fts_rank": p.fts_rank,
            "semantic_score": p.semantic_score, "hybrid_score": p.hybrid_score,
        }
        for p in fused
    ]

    try:
        from retrieval.observability import record

        record(conn, diagnostics)
    except Exception:  # noqa: BLE001 - observability must never break a research answer
        logger.warning("Failed to record retrieval diagnostics", exc_info=True)

    return fused
