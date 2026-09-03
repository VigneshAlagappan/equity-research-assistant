"""Shared fixtures for Phase 2 tests."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from normalization.financials import ensure_metric_vocabulary
from retrieval.vector_store import VectorMatch, VectorRecord, VectorStoreUnavailable
from storage.database import init_db


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A freshly initialized database with the metric vocabulary seeded, no companies."""
    conn = init_db(db_path=tmp_path / "test.db")
    ensure_metric_vocabulary(conn)
    yield conn
    conn.close()


# ------------------------------------------------------------------
# Hybrid retrieval test doubles (section 14: "VectorStore is accessed only
# through its abstraction" / "EmbeddingProvider is independent of
# VectorStore"). Neither fake imports qdrant_client, sentence_transformers,
# or voyageai — they exist purely so retrieval/hybrid_search.py,
# retrieval/semantic_search.py, and retrieval/semantic_indexer.py's tests
# run instantly and deterministically, without a running Qdrant or a loaded
# ML model, the same way research/capabilities.py's tests inject fake
# lambdas instead of the real FTS5/SQL-backed capabilities.
# ------------------------------------------------------------------

#: word -> canonical concept. FakeEmbeddingProvider hashes each word's
#: canonical form into a bucket, so two sentences sharing a *concept* (e.g.
#: "profit" and "earnings") land close in vector space even with zero literal
#: word overlap -- a controllable stand-in for what a real embedding model's
#: semantic similarity provides, used to test retrieval/ranking logic without
#: depending on a real model being loaded in every test.
FAKE_EMBEDDING_SYNONYMS = {
    "profit": "earnings", "profitability": "earnings", "earnings": "earnings",
    "bottomline": "earnings", "netincome": "earnings",
    "nonperforming": "badloans", "npas": "badloans", "badloans": "badloans",
    "delinquencies": "badloans",
    "workforce": "staff", "employees": "staff", "headcount": "staff", "staff": "staff",
    "attrition": "turnover", "turnover": "turnover", "churn": "turnover",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


class FakeEmbeddingProvider:
    """Deterministic EmbeddingProvider — same Protocol shape as
    retrieval.embedding_provider.EmbeddingProvider, no ML dependency."""

    model_id = "fake-embedding-v1"
    dimension = 32

    def embed_text(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in _WORD_RE.findall(text.lower()):
            canonical = FAKE_EMBEDDING_SYNONYMS.get(word, word)
            bucket = int(hashlib.md5(canonical.encode()).hexdigest(), 16) % self.dimension
            vector[bucket] += 1.0
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return [v / norm for v in vector]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """In-memory VectorStore — same Protocol shape as
    retrieval.vector_store.VectorStore, no Qdrant/network dependency.
    `healthy` toggles to simulate an outage (section 10's graceful
    degradation tests)."""

    def __init__(self) -> None:
        self._records: dict[int, VectorRecord] = {}
        self.healthy = True
        self.upsert_calls = 0
        self.delete_document_calls: list[int] = []

    def upsert(self, records: list[VectorRecord]) -> None:
        if not self.healthy:
            raise VectorStoreUnavailable("FakeVectorStore forced unhealthy")
        self.upsert_calls += 1
        for record in records:
            self._records[record.chunk_id] = record

    def delete_document(self, document_id: int) -> None:
        if not self.healthy:
            raise VectorStoreUnavailable("FakeVectorStore forced unhealthy")
        self.delete_document_calls.append(document_id)
        self._records = {cid: r for cid, r in self._records.items() if r.document_id != document_id}

    def search(self, query_embedding: list[float], *, company_id: str | None = None, limit: int = 10) -> list[VectorMatch]:
        if not self.healthy:
            raise VectorStoreUnavailable("FakeVectorStore forced unhealthy")
        candidates = list(self._records.values())
        if company_id is not None:
            candidates = [r for r in candidates if r.company_id == company_id]
        scored = sorted(
            ((_cosine_similarity(query_embedding, r.embedding), r) for r in candidates),
            key=lambda pair: pair[0], reverse=True,
        )
        return [VectorMatch(chunk_id=r.chunk_id, score=score) for score, r in scored[:limit]]

    def health_check(self) -> bool:
        return self.healthy


@pytest.fixture
def fake_embedding_provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


@pytest.fixture
def fake_vector_store() -> FakeVectorStore:
    return FakeVectorStore()
