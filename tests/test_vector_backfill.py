"""main.py's `vector-backfill` CLI command (section 11) — one-time,
idempotent embedding backfill over already-processed documents.

Exercised only against a tmp_path SQLite database with a couple of
synthetic PDFs and the FakeEmbeddingProvider/FakeVectorStore test doubles
(tests/conftest.py) — never the real data/equity_research.db, and never a
real embeddings API call. This is exactly this feature's cost guardrail in
test form: the backfill command is proven correct here; running it for real
against the actual document archive is a separate, explicit, user-approved
action (see this feature's final report)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import main
from companies.registry import seed_companies
from research.document_chunker import chunk_and_index_document
from storage.database import init_db
from storage.repositories import list_batch_job_items, list_batch_job_runs, save_company_document
from tests.conftest import FakeEmbeddingProvider, FakeVectorStore
from tests.test_documents import _make_minimal_pdf


def _prepare_processed_document(
    conn: sqlite3.Connection, tmp_path: Path, company_id: str, text: str, filename: str, *,
    document_type: str = "annual_report",
):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    doc = save_company_document(
        conn, company_id, document_type=document_type, fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    chunk_and_index_document(conn, doc)
    # Simulates having already gone through the Knowledge Builder pipeline
    # (ingestion/coordinator.py's process_documents()) without needing a
    # mocked Anthropic client just to reach processing_status='processed'.
    conn.execute("UPDATE documents SET processing_status = 'processed' WHERE document_id = ?", (doc["document_id"],))
    conn.commit()
    return doc


@pytest.fixture
def backfill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    conn = init_db(db_path=db_path)
    seed_companies(conn)

    fake_store = FakeVectorStore()
    fake_provider = FakeEmbeddingProvider()
    monkeypatch.setattr("retrieval.vector_store.default_vector_store", lambda: fake_store)
    monkeypatch.setattr("retrieval.embedding_provider.default_embedding_provider", lambda: fake_provider)
    yield conn, tmp_path, fake_store, fake_provider
    conn.close()


def _run_backfill(
    *, company_id: str | None = None, document_type: str | None = None, limit: int | None = None,
    force: bool = False,
) -> None:
    flags = ["vector-backfill"]
    if company_id:
        flags += ["--company-id", company_id]
    if document_type:
        flags += ["--document-type", document_type]
    if limit is not None:
        flags += ["--limit", str(limit)]
    if force:
        flags.append("--force")
    args = main.build_parser().parse_args(flags)
    args.func(args)


def test_backfill_embeds_every_processed_document(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "a.pdf")
    _prepare_processed_document(conn, tmp_path, "ICICIBANK", "Deposits grew steadily this quarter", "b.pdf")

    _run_backfill()

    assert len(fake_store._records) == 2
    statuses = conn.execute("SELECT embedding_status FROM document_chunks").fetchall()
    assert statuses and all(r["embedding_status"] == "indexed" for r in statuses)


def test_backfill_records_a_queryable_audit_run(backfill_env) -> None:
    """Every backfill run and its per-document outcome must land in
    batch_job_runs/batch_job_items (ingestion/batch_log.py) — the same
    durable audit trail scripts/batch_fetch_nse.py uses — not just
    stdout/logs/app.log, so `main.py list-batch-runs`/`show-batch-run` can
    answer "did this actually run, and what happened" after the fact."""
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "a.pdf")

    _run_backfill(document_type="annual_report")

    runs = list_batch_job_runs(conn)
    assert runs and runs[0]["job_name"] == "vector_backfill"
    assert runs[0]["status"] == "completed"
    assert runs[0]["items_succeeded"] == 1
    assert "document_type=annual_report" in runs[0]["scope_label"]

    items = list_batch_job_items(conn, runs[0]["run_id"])
    assert len(items) == 1
    assert items[0]["company_id"] == "HDFCBANK"
    assert items[0]["status"] == "ok"
    assert "embedded=" in items[0]["detail"]


def test_backfill_is_idempotent_no_duplicate_vectors_on_rerun(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "a.pdf")

    _run_backfill()
    first_upsert_calls = fake_store.upsert_calls
    first_record_count = len(fake_store._records)

    _run_backfill()

    assert fake_store.upsert_calls == first_upsert_calls  # nothing new to embed the second time
    assert len(fake_store._records) == first_record_count  # no duplicates


def test_backfill_force_reembeds_everything(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "a.pdf")
    _run_backfill()
    first_upsert_calls = fake_store.upsert_calls

    _run_backfill(force=True)

    assert fake_store.upsert_calls > first_upsert_calls


def test_backfill_respects_company_id_filter(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue grew twelve percent", "a.pdf")
    _prepare_processed_document(conn, tmp_path, "ICICIBANK", "Deposits grew steadily this quarter", "b.pdf")

    _run_backfill(company_id="HDFCBANK")

    assert len(fake_store._records) == 1
    (record,) = fake_store._records.values()
    assert record.company_id == "HDFCBANK"


def test_backfill_respects_document_type_filter(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(
        conn, tmp_path, "HDFCBANK", "Management discussed margins on the call", "a.pdf",
        document_type="transcript",
    )
    _prepare_processed_document(
        conn, tmp_path, "HDFCBANK", "Full-year results and balance sheet", "b.pdf",
        document_type="annual_report",
    )

    _run_backfill(document_type="transcript")

    assert len(fake_store._records) == 1
    (record,) = fake_store._records.values()
    embedded_doc = conn.execute(
        "SELECT document_type FROM documents WHERE document_id = ?", (record.document_id,)
    ).fetchone()
    assert embedded_doc["document_type"] == "transcript"


def test_backfill_respects_limit_as_the_cost_guardrail(backfill_env) -> None:
    conn, tmp_path, fake_store, _provider = backfill_env
    _prepare_processed_document(conn, tmp_path, "HDFCBANK", "Revenue number one", "a.pdf")
    _prepare_processed_document(conn, tmp_path, "ICICIBANK", "Revenue number two", "b.pdf")

    _run_backfill(limit=1)

    embedded_documents = {r.document_id for r in fake_store._records.values()}
    assert len(embedded_documents) == 1


def test_backfill_exits_cleanly_when_vector_store_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    init_db(db_path=db_path).close()

    with pytest.raises(SystemExit):
        _run_backfill()


def test_backfill_exits_cleanly_when_vector_store_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    init_db(db_path=db_path).close()

    unhealthy_store = FakeVectorStore()
    unhealthy_store.healthy = False
    monkeypatch.setattr("retrieval.vector_store.default_vector_store", lambda: unhealthy_store)

    with pytest.raises(SystemExit):
        _run_backfill()
