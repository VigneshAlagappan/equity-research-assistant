"""research/documents.py::get_document_passage_evidence() — section 9's
"question -> hybrid retrieval -> top-K relevant passages -> LLM" evidence
source, additive alongside get_document_evidence()'s whole-document text.
Uses the FakeEmbeddingProvider/FakeVectorStore doubles (tests/conftest.py)
via monkeypatched retrieval defaults, same pattern as
tests/test_embedding_indexer_worker.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.document_chunker import chunk_and_index_document
from research.documents import get_document_evidence, get_document_passage_evidence
from retrieval.semantic_indexer import embed_and_index_document_chunks
from storage.repositories import save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


@pytest.fixture(autouse=True)
def _patch_hybrid_defaults(monkeypatch: pytest.MonkeyPatch, fake_embedding_provider, fake_vector_store):
    """get_document_passage_evidence() calls hybrid_search_documents()
    without an explicit embedding_provider/vector_store (same as the real
    research/capabilities.py binding does) — patch the defaults it falls
    back to so this test suite never depends on a running Qdrant or a real
    model load."""
    monkeypatch.setattr("retrieval.semantic_search.default_embedding_provider", lambda: fake_embedding_provider)
    monkeypatch.setattr("retrieval.semantic_search.default_vector_store", lambda: fake_vector_store)
    return fake_embedding_provider, fake_vector_store


def _add_indexed_document(conn, tmp_path: Path, company_id: str, text: str, filename: str = "report.pdf"):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    doc = save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(conn, doc)
    return doc


def test_finds_a_targeted_passage_via_semantic_retrieval(
    company_conn: sqlite3.Connection, tmp_path: Path, _patch_hybrid_defaults,
) -> None:
    provider, store = _patch_hybrid_defaults
    doc = _add_indexed_document(company_conn, tmp_path, "HDFCBANK", "Employee attrition declined this quarter")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=provider, vector_store=store)

    evidence = get_document_passage_evidence(company_conn, "HDFCBANK", "How is staff turnover trending?")

    assert len(evidence) == 1
    item = evidence[0]
    assert item.kind == "MANAGEMENT_STATEMENT"
    assert item.company_id == "HDFCBANK"
    assert "attrition" in item.value.lower()
    assert "retrieval" in item.citation


def test_returns_empty_when_nothing_relevant_is_indexed(company_conn: sqlite3.Connection) -> None:
    assert get_document_passage_evidence(company_conn, "HDFCBANK", "anything at all") == []


def test_is_additive_alongside_whole_document_evidence(
    company_conn: sqlite3.Connection, tmp_path: Path, _patch_hybrid_defaults,
) -> None:
    """Section 9: this must not remove get_document_evidence()'s
    whole-document access — both evidence sources are available side by
    side, and a caller (research/assistant.py) combines them."""
    provider, store = _patch_hybrid_defaults
    doc = _add_indexed_document(company_conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent this quarter")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=provider, vector_store=store)

    whole_document_evidence = get_document_evidence(company_conn, "HDFCBANK", "revenue growth")
    passage_evidence = get_document_passage_evidence(company_conn, "HDFCBANK", "revenue growth")

    assert len(whole_document_evidence) == 1
    assert len(passage_evidence) == 1
    assert whole_document_evidence[0].kind == passage_evidence[0].kind == "MANAGEMENT_STATEMENT"


def test_degrades_gracefully_when_vector_store_unavailable(
    company_conn: sqlite3.Connection, tmp_path: Path, _patch_hybrid_defaults,
) -> None:
    """Section 10: a Q&A answer must still get keyword-matched passage
    evidence even if the vector layer is down — never an exception
    propagating up into research/assistant.py::answer_question()."""
    provider, store = _patch_hybrid_defaults
    store.healthy = False
    doc = _add_indexed_document(company_conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent this quarter")
    # Not embedded (store is down) -- only FTS5 has this chunk.

    evidence = get_document_passage_evidence(company_conn, "HDFCBANK", "revenue grew")

    assert len(evidence) == 1
    assert "twelve percent" in evidence[0].value
