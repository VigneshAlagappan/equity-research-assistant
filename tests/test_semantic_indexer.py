"""retrieval/semantic_indexer.py — embedding generation + VectorStore upsert
over already-chunked documents (section 5, section 11, section 14). Uses
the FakeEmbeddingProvider/FakeVectorStore test doubles (tests/conftest.py) —
fast, deterministic, no ML model load or Qdrant server needed to prove the
indexing/idempotency/reprocessing/degradation logic is correct."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.document_chunker import chunk_and_index_document
from retrieval.embedding_provider import EmbeddingProviderUnavailable
from retrieval.semantic_indexer import embed_and_index_document_chunks
from retrieval.vector_store import VectorStoreUnavailable
from storage.repositories import save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _add_chunked_pdf(conn: sqlite3.Connection, tmp_path: Path, text: str, filename: str = "report.pdf"):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    doc = save_company_document(
        conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(conn, doc)
    return doc


def test_embeds_every_chunk_and_upserts_to_the_vector_store(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_chunked_pdf(company_conn, tmp_path, "Revenue grew twelve percent this quarter")

    result = embed_and_index_document_chunks(
        company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    )

    assert result.chunks_total == 1
    assert result.chunks_embedded == 1
    assert result.chunks_already_indexed == 0
    assert fake_vector_store.upsert_calls == 1
    assert len(fake_vector_store._records) == 1

    status_rows = company_conn.execute(
        "SELECT embedding_status, embedding_model FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchall()
    assert all(r["embedding_status"] == "indexed" for r in status_rows)
    assert all(r["embedding_model"] == fake_embedding_provider.model_id for r in status_rows)


def test_document_with_no_chunks_embeds_nothing(
    company_conn: sqlite3.Connection, fake_embedding_provider: FakeEmbeddingProvider, fake_vector_store: FakeVectorStore,
) -> None:
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="announcement", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/not-a-pdf-link",
    )
    # No chunk_and_index_document() call -- this document was never chunked (no extractable text).

    result = embed_and_index_document_chunks(
        company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    )

    assert result.chunks_total == 0
    assert result.chunks_embedded == 0
    assert fake_vector_store.upsert_calls == 0


def test_rerunning_is_idempotent_and_does_not_call_the_provider_or_store_again(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_chunked_pdf(company_conn, tmp_path, "Original report text")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)
    assert fake_vector_store.upsert_calls == 1

    result = embed_and_index_document_chunks(
        company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    )

    assert result.chunks_embedded == 0
    assert result.chunks_already_indexed == result.chunks_total
    assert fake_vector_store.upsert_calls == 1  # unchanged -- no second upsert call


def test_force_reembeds_even_when_already_indexed(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_chunked_pdf(company_conn, tmp_path, "Original report text")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)

    result = embed_and_index_document_chunks(
        company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store, force=True
    )

    assert result.chunks_embedded == result.chunks_total
    assert fake_vector_store.upsert_calls == 2


def test_reprocessing_a_document_replaces_stale_vectors(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """research/document_chunker.py's replace_document_chunks() gives a
    reprocessed document entirely new chunk_ids — this must not leave the
    OLD chunk_ids' vectors orphaned in the VectorStore (section 14: "document
    reprocessing replaces stale vectors")."""
    doc = _add_chunked_pdf(company_conn, tmp_path, "Original report text")
    embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store)
    assert len(fake_vector_store._records) == 1

    # Reprocess with different text -- research/document_chunker.py deletes
    # the old document_chunks rows and inserts fresh ones. Note: SQLite's
    # plain `INTEGER PRIMARY KEY` (no AUTOINCREMENT) can coincidentally
    # reuse the same chunk_id once a table is fully emptied, so this test
    # proves staleness is replaced by CONTENT, not by asserting chunk_ids
    # never repeat -- delete_document() is exactly what makes that safe
    # either way.
    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Completely different reprocessed content about exports")
    company_conn.execute(
        "UPDATE documents SET raw_file_path = ? WHERE document_id = ?", (str(pdf_path), doc["document_id"])
    )
    company_conn.commit()
    refreshed_doc = company_conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()
    chunk_and_index_document(company_conn, refreshed_doc)

    embed_and_index_document_chunks(
        company_conn, refreshed_doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
    )

    assert fake_vector_store.delete_document_calls == [doc["document_id"], doc["document_id"]]
    assert len(fake_vector_store._records) == 1  # old vector cleared, exactly the new chunk's vector present

    new_chunk_text = company_conn.execute(
        "SELECT text FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()["text"]
    (stored_record,) = fake_vector_store._records.values()
    assert stored_record.embedding == fake_embedding_provider.embed_text(new_chunk_text)
    assert stored_record.embedding != fake_embedding_provider.embed_text("Original report text")


def test_vector_store_unavailable_propagates_and_leaves_chunks_pending(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    fake_vector_store: FakeVectorStore,
) -> None:
    """Section 10: a down VectorStore must not corrupt or silently swallow
    anything — the caller (the worker, or the backfill CLI) decides what to
    do, and FTS5 is completely untouched either way."""
    doc = _add_chunked_pdf(company_conn, tmp_path, "Revenue grew twelve percent")
    fake_vector_store.healthy = False

    with pytest.raises(VectorStoreUnavailable):
        embed_and_index_document_chunks(
            company_conn, doc, embedding_provider=fake_embedding_provider, vector_store=fake_vector_store
        )

    status_rows = company_conn.execute(
        "SELECT embedding_status FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchall()
    assert all(r["embedding_status"] == "pending" for r in status_rows)

    # FTS5 is completely unaffected by the vector store outage.
    from retrieval.document_search import search_documents

    assert len(search_documents(company_conn, "revenue")) == 1


def test_vector_store_none_raises_vector_store_unavailable(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_embedding_provider: FakeEmbeddingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    doc = _add_chunked_pdf(company_conn, tmp_path, "Revenue grew twelve percent")

    with pytest.raises(VectorStoreUnavailable):
        embed_and_index_document_chunks(company_conn, doc, embedding_provider=fake_embedding_provider)


def test_embedding_provider_failure_marks_chunks_failed_without_touching_vector_store(
    company_conn: sqlite3.Connection, tmp_path: Path, fake_vector_store: FakeVectorStore,
) -> None:
    doc = _add_chunked_pdf(company_conn, tmp_path, "Revenue grew twelve percent")

    class _BrokenProvider:
        model_id = "broken"
        dimension = 4

        def embed_text(self, text):
            return self.embed_batch([text])[0]

        def embed_batch(self, texts):
            raise EmbeddingProviderUnavailable("simulated embedding failure")

    with pytest.raises(EmbeddingProviderUnavailable):
        embed_and_index_document_chunks(
            company_conn, doc, embedding_provider=_BrokenProvider(), vector_store=fake_vector_store
        )

    assert fake_vector_store.upsert_calls == 0
    status_rows = company_conn.execute(
        "SELECT embedding_status FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchall()
    assert all(r["embedding_status"] == "failed" for r in status_rows)

    from retrieval.document_search import search_documents

    assert len(search_documents(company_conn, "revenue")) == 1
