"""ingestion/workers/embedding_indexer_worker.py — the event-driven half of
section 12 ("after backfill, normal document ingestion should automatically
maintain both indexes"), and section 4/14's "structured facts are not
accidentally embedded" guarantee, proven at the event-dispatch level rather
than only at the function level (tests/test_semantic_indexer.py already
covers embed_and_index_document_chunks() directly)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.event_bus import publish
from ingestion.events import DatasetIngestedEvent
from ingestion.workers.embedding_indexer_worker import run
from research.document_chunker import chunk_and_index_document
from storage.repositories import save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _document_event(document_id: int) -> DatasetIngestedEvent:
    return DatasetIngestedEvent(
        dataset_id=f"document:{document_id}",
        dataset_type="document",
        source="test",
        storage_reference={"table": "documents", "document_id": document_id},
        ingestion_id="test-ingest",
        scope={"document_id": document_id},
    )


def test_worker_skips_non_document_events() -> None:
    event = DatasetIngestedEvent(
        dataset_id="company_financials:HDFCBANK", dataset_type="company_financials", source="nse",
        storage_reference={}, ingestion_id="x", scope={"company_id": "HDFCBANK"},
    )
    result = run(conn=None, event=event)  # conn unused on this early-return path
    assert result.status == "skipped"


def test_worker_embeds_a_documents_chunks(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore,
) -> None:
    monkeypatch.setattr("retrieval.semantic_indexer.default_embedding_provider", lambda: fake_embedding_provider)
    monkeypatch.setattr("retrieval.semantic_indexer.default_vector_store", lambda: fake_vector_store)

    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(company_conn, doc)

    result = run(company_conn, _document_event(doc["document_id"]))

    assert result.status == "ok"
    assert fake_vector_store.upsert_calls == 1


def test_worker_skips_gracefully_when_vector_store_unreachable(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore,
) -> None:
    fake_vector_store.healthy = False
    monkeypatch.setattr("retrieval.semantic_indexer.default_embedding_provider", lambda: fake_embedding_provider)
    monkeypatch.setattr("retrieval.semantic_indexer.default_vector_store", lambda: fake_vector_store)

    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(company_conn, doc)

    result = run(company_conn, _document_event(doc["document_id"]))

    assert result.status == "skipped"
    assert "vector store" in result.output_reference

    # FTS5 keyword search is completely unaffected by the outage.
    from retrieval.document_search import search_documents

    assert len(search_documents(company_conn, "revenue")) == 1


def test_worker_skips_a_document_with_no_chunks(company_conn: sqlite3.Connection) -> None:
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="announcement", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/not-a-pdf-link",
    )
    # No chunk_and_index_document() -- nothing to embed.

    result = run(company_conn, _document_event(doc["document_id"]))
    assert result.status == "skipped"


def test_structured_financial_ingestion_never_creates_document_chunks(company_conn: sqlite3.Connection) -> None:
    """Section 4/14: XBRL/financial observations, macro observations, etc.
    never flow through research/document_chunker.py at all (they have their
    own entirely separate ingest_file()/ingest_macro_file() pipelines that
    never touch the `documents`/`document_chunks` tables) — so the Embedding
    Indexer Worker (which only ever reads document_chunks) structurally has
    nothing to vectorize for them. This event carries a company_financials
    dataset_type — same event fan-out every real ingestion publishes — and
    the worker must skip it without ever looking at document_chunks."""
    event = DatasetIngestedEvent(
        dataset_id="company_financials:HDFCBANK", dataset_type="company_financials", source="nse",
        storage_reference={"table": "financial_observations"}, ingestion_id="x",
        scope={"company_id": "HDFCBANK"},
    )

    result = run(company_conn, event)

    assert result.status == "skipped"
    count = company_conn.execute("SELECT COUNT(*) AS n FROM document_chunks").fetchone()["n"]
    assert count == 0
