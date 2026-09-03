"""retrieval/hybrid_search.py — the Hybrid Retriever (section 7, section
10). Combines FTS5 keyword search (retrieval/document_search.py, real
SQLite FTS5, no mocking needed) with semantic search (FakeEmbeddingProvider/
FakeVectorStore doubles, tests/conftest.py) to prove fusion, dedup, the
"both" confidence boost, company/as_of scoping, and graceful degradation
when the vector layer is unavailable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.document_chunker import chunk_and_index_document
from retrieval.hybrid_search import hybrid_search_documents
from retrieval.semantic_indexer import embed_and_index_document_chunks
from storage.repositories import save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _add_indexed_pdf(conn, tmp_path: Path, company_id: str, text: str, filename: str, **kwargs):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    doc = save_company_document(
        conn, company_id, document_type=kwargs.pop("document_type", "annual_report"),
        fiscal_year=kwargs.pop("fiscal_year", "FY2024"), quarter=kwargs.pop("quarter", "Q1"),
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(conn, doc)
    return doc


def test_keyword_only_match_is_found_and_labeled_keyword(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 15 case 1: exact terminology is FTS5's strength — a query
    term that appears verbatim, but that the fake embedding model treats as
    totally unrelated to anything indexed (so it's a keyword-only hit)."""
    _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Capital adequacy ratio was 16.2 percent", "report.pdf")

    results = hybrid_search_documents(
        company_conn, "capital adequacy ratio",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    assert results[0].retrieval_source == "keyword"
    assert results[0].hybrid_score is not None
    assert results[0].fts_rank == 1


def test_semantic_only_match_is_found_and_labeled_semantic(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 15 case 2: different terminology, no keyword overlap at
    all — only semantic search can find it."""
    doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Employee headcount fell during the year", "report.pdf")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    results = hybrid_search_documents(
        company_conn, "staff churn and turnover",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    assert results[0].retrieval_source == "semantic"
    assert results[0].fts_rank is None
    assert results[0].semantic_score is not None


def test_passage_found_by_both_methods_is_deduplicated_and_boosted(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 15 case 3 / section 7's confidence boost: a passage that both
    methods independently surface must appear exactly once, marked "both",
    and must outrank a passage found by only one method."""
    both_doc = _add_indexed_pdf(
        company_conn, tmp_path, "HDFCBANK", "Net earnings profit rose sharply this quarter", "both.pdf"
    )
    embed_and_index_document_chunks(company_conn, both_doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    keyword_only_doc = _add_indexed_pdf(
        company_conn, tmp_path, "HDFCBANK", "Profit margins across other unrelated widgets improved", "kw_only.pdf"
    )
    # Deliberately NOT embedded/upserted -- keyword-only candidate.

    results = hybrid_search_documents(
        company_conn, "profit earnings",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    chunk_ids = [r.chunk_id for r in results]
    assert len(chunk_ids) == len(set(chunk_ids)), "no duplicate passages"

    both_result = next(r for r in results if r.document_id == both_doc["document_id"])
    kw_only_result = next(r for r in results if r.document_id == keyword_only_doc["document_id"])
    assert both_result.retrieval_source == "both"
    assert kw_only_result.retrieval_source == "keyword"
    assert both_result.hybrid_score > kw_only_result.hybrid_score


def test_company_scoping_applies_to_the_hybrid_result(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 15 case 6: company scoping matters."""
    hdfc_doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Loan growth accelerated this year", "hdfc.pdf")
    icici_doc = _add_indexed_pdf(company_conn, tmp_path, "ICICIBANK", "Loan growth accelerated this year", "icici.pdf")
    embed_and_index_document_chunks(company_conn, hdfc_doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)
    embed_and_index_document_chunks(company_conn, icici_doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    results = hybrid_search_documents(
        company_conn, "loan growth", company_id="HDFCBANK",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert all(r.company_id == "HDFCBANK" for r in results)


def test_as_of_rejects_future_evidence_from_the_hybrid_result(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 15 case 7: a historical as_of investigation must reject newer
    evidence, whether it was found by keyword, semantic, or both."""
    doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Regulatory capital ratio improved", "report.pdf")
    company_conn.execute("UPDATE documents SET published_at = ? WHERE document_id = ?", ("2030-03-01", doc["document_id"]))
    company_conn.commit()
    refreshed = company_conn.execute("SELECT * FROM documents WHERE document_id = ?", (doc["document_id"],)).fetchone()
    embed_and_index_document_chunks(company_conn, refreshed, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    results = hybrid_search_documents(
        company_conn, "regulatory capital ratio", as_of="2029-12-31",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert results == []


def test_degrades_to_fts5_only_when_vector_store_unavailable(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 10: research must still work with no vector infrastructure."""
    _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Deposits grew steadily this quarter", "report.pdf")
    fake_vector_store.healthy = False

    results = hybrid_search_documents(
        company_conn, "deposits grew",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    assert len(results) == 1
    assert results[0].retrieval_source == "keyword"

    diagnostic = company_conn.execute(
        "SELECT degraded, degradation_reason FROM retrieval_diagnostics ORDER BY retrieval_id DESC LIMIT 1"
    ).fetchone()
    assert diagnostic["degraded"] == 1
    assert "vector store" in diagnostic["degradation_reason"]


def test_degrades_to_fts5_only_when_vector_store_backend_disabled(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Deposits grew steadily this quarter", "report.pdf")

    results = hybrid_search_documents(company_conn, "deposits grew", embedding_provider=fake_embedding_provider)

    assert len(results) == 1
    assert results[0].retrieval_source == "keyword"


def test_records_retrieval_diagnostics_row_on_a_healthy_call(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "report.pdf")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    hybrid_search_documents(
        company_conn, "revenue grew", company_id="HDFCBANK",
        embedding_provider=fake_embedding_provider, vector_store=fake_vector_store,
    )

    row = company_conn.execute("SELECT * FROM retrieval_diagnostics ORDER BY retrieval_id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["company_id"] == "HDFCBANK"
    assert row["degraded"] == 0
    assert row["returned_count"] >= 1
    assert row["keyword_candidate_count"] >= 1
    assert row["semantic_candidate_count"] >= 1
    assert "profit" not in (row["query_excerpt"] or "")  # sanity: excerpt is the actual query text, not garbage


def test_fusion_is_deterministic_across_repeated_calls(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 7: ranking must be deterministic/explainable, never an LLM's
    arbitrary call."""
    for i in range(3):
        doc = _add_indexed_pdf(company_conn, tmp_path, "HDFCBANK", f"Net interest income rose in period {i}", f"r{i}.pdf")
        embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    first = hybrid_search_documents(company_conn, "net interest income", embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)
    second = hybrid_search_documents(company_conn, "net interest income", embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    assert [(r.chunk_id, r.hybrid_score) for r in first] == [(r.chunk_id, r.hybrid_score) for r in second]
