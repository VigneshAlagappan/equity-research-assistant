"""retrieval/semantic_search.py — embedding/vector passage search (section
6). Most tests use the FakeEmbeddingProvider/FakeVectorStore doubles
(tests/conftest.py) for fast, deterministic coverage of company scoping,
as_of filtering, and provenance hydration; one test uses the REAL local
(sentence-transformers) embedding provider against an in-memory vector store
to prove genuine semantic paraphrase matching — no exact keyword overlap
required (section 14's core semantic-search requirement) — rather than only
proving it against a synonym-map fake."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.document_chunker import chunk_and_index_document
from retrieval.embedding_provider_local import LocalEmbeddingProvider
from retrieval.semantic_search import semantic_search_documents
from retrieval.vector_store import VectorStoreUnavailable
from storage.repositories import save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _add_indexed_pdf(
    conn: sqlite3.Connection, tmp_path: Path, company_id: str, text: str, filename: str,
    *, fiscal_year: str = "FY2024", quarter: str | None = "Q1",
):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    doc = save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year=fiscal_year, quarter=quarter,
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(conn, doc)
    return doc


def _embed_all_chunks(conn: sqlite3.Connection, provider: FakeEmbeddingProvider, store: FakeVectorStore, doc) -> None:
    from retrieval.semantic_indexer import embed_and_index_document_chunks

    embed_and_index_document_chunks(conn, doc, embedding_provider=provider, vector_store=store)


def test_finds_paraphrased_concept_with_no_literal_keyword_overlap(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_indexed_pdf(
        company_conn, tmp_path, "HDFCBANK",
        "Net earnings for the bank improved significantly this year", "report.pdf",
    )
    _embed_all_chunks(company_conn, fake_embedding_provider, fake_vector_store, doc)

    # Query shares zero literal words with the indexed text (fake provider's
    # synonym map treats "profitability"/"bottomline" as the same concept as
    # "earnings" — tests/conftest.py's FAKE_EMBEDDING_SYNONYMS).
    results = semantic_search_documents(
        company_conn, "How is bottomline profitability trending?",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    assert results[0].document_id == doc["document_id"]
    assert results[0].retrieval_source == "semantic"
    assert results[0].semantic_score is not None


def test_finds_paraphrased_concept_with_real_local_embeddings() -> None:
    """No fakes at all here -- the real sentence-transformers model, proving
    section 14's requirement genuinely, not just against a hand-built
    synonym map."""
    from retrieval.vector_store import VectorRecord

    provider = LocalEmbeddingProvider()
    relevant_text = "The bank's net interest margin expanded due to lower cost of funds"
    unrelated_text = "The company inaugurated a new regional distribution warehouse"

    class _InMemoryStore:
        def __init__(self):
            self.records = {}

        def upsert(self, records):
            for r in records:
                self.records[r.chunk_id] = r

        def delete_document(self, document_id):
            pass

        def search(self, query_embedding, *, company_id=None, limit=10):
            from retrieval.vector_store import VectorMatch

            def cosine(a, b):
                dot = sum(x * y for x, y in zip(a, b))
                na = sum(x * x for x in a) ** 0.5
                nb = sum(x * x for x in b) ** 0.5
                return dot / (na * nb) if na and nb else 0.0

            scored = sorted(
                ((cosine(query_embedding, r.embedding), r) for r in self.records.values()),
                key=lambda t: t[0], reverse=True,
            )
            return [VectorMatch(chunk_id=r.chunk_id, score=s) for s, r in scored[:limit]]

        def health_check(self):
            return True

    store = _InMemoryStore()
    vectors = provider.embed_batch([relevant_text, unrelated_text])
    store.upsert([
        VectorRecord(chunk_id=1, document_id=100, company_id="ACME", embedding=vectors[0]),
        VectorRecord(chunk_id=2, document_id=101, company_id="ACME", embedding=vectors[1]),
    ])

    # The query never says "net interest margin", "cost of funds", or
    # "bank" -- it asks about the same idea in different words entirely.
    query_vector = provider.embed_text("Did lending profitability improve because funding got cheaper?")
    from retrieval.vector_store import VectorMatch

    matches = store.search(query_vector, limit=2)
    best = matches[0]
    assert best.chunk_id == 1, "the semantically related passage must rank above the unrelated one"
    assert best.score > matches[1].score


def test_company_scoping(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    hdfc_doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Employee attrition declined this quarter", "hdfc.pdf")
    icici_doc = _add_indexed_pdf(company_conn, tmp_path, "ICICIBANK", "Employee attrition declined this quarter", "icici.pdf")
    _embed_all_chunks(company_conn, fake_embedding_provider, fake_vector_store, hdfc_doc)
    _embed_all_chunks(company_conn, fake_embedding_provider, fake_vector_store, icici_doc)

    results = semantic_search_documents(
        company_conn, "staff turnover trends", company_id="HDFCBANK",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    assert results[0].company_id == "HDFCBANK"


def test_as_of_excludes_documents_published_after_the_cutoff(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Non-performing loans rose sharply", "report.pdf")
    company_conn.execute(
        "UPDATE documents SET published_at = ? WHERE document_id = ?", ("2030-01-15", doc["document_id"])
    )
    company_conn.commit()
    _embed_all_chunks(company_conn, fake_embedding_provider, fake_vector_store, doc)

    # A historical investigation as of 2029 must never see this 2030 document,
    # even though it's the closest semantic match on file (section 6, 14).
    historical_results = semantic_search_documents(
        company_conn, "bad loans and delinquencies", as_of="2029-12-31",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )
    assert historical_results == []

    current_results = semantic_search_documents(
        company_conn, "bad loans and delinquencies", as_of="2030-06-30",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )
    assert len(current_results) == 1


def test_returns_empty_for_blank_query(
    company_conn: sqlite3.Connection, fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore
) -> None:
    assert semantic_search_documents(
        company_conn, "   ", embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    ) == []


def test_returns_empty_when_nothing_indexed_yet(
    company_conn: sqlite3.Connection, fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore
) -> None:
    assert semantic_search_documents(
        company_conn, "anything at all", embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    ) == []


def test_raises_vector_store_unavailable_when_store_is_unhealthy(
    company_conn: sqlite3.Connection, fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore
) -> None:
    fake_vector_store.healthy = False
    with pytest.raises(VectorStoreUnavailable):
        semantic_search_documents(
            company_conn, "some query", embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
        )


def test_raises_vector_store_unavailable_when_backend_disabled(
    company_conn: sqlite3.Connection, fake_embedding_provider: FakeEmbeddingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    with pytest.raises(VectorStoreUnavailable):
        semantic_search_documents(company_conn, "some query", embedding_provider=fake_embedding_provider)


def test_preserves_full_provenance(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_indexed_pdf(
        company_conn, tmp_path, "HDFCBANK", "Guidance raised for the fiscal year", "report.pdf",
        fiscal_year="FY2025", quarter="Q2",
    )
    _embed_all_chunks(company_conn, fake_embedding_provider, fake_vector_store, doc)

    results = semantic_search_documents(
        company_conn, "outlook raised for the year",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    passage = results[0]
    assert passage.document_id == doc["document_id"]
    assert passage.company_id == "HDFCBANK"
    assert passage.fiscal_year == "FY2025"
    assert passage.quarter == "Q2"
    assert passage.document_type == "annual_report"
    assert passage.page_number == 1
