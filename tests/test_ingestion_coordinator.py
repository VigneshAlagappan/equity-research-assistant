"""Ingestion Coordinator — Admin/Settings -> Ingest queue's discovery and
dispatch layer. Reuses ingestion/pipeline.py under the hood; these tests
exercise the new discovery/status-tracking behavior on top of it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.coordinator import (
    archive_documents,
    discover_pending_documents,
    discover_pending_financial_items,
    process_all_pending_documents,
    process_all_pending_financial_items,
    process_financial_items,
    retry_failed_financial_items,
    unarchive_documents,
)
from storage.repositories import (
    get_ingestion_queue_item_by_path,
    list_batch_job_items,
    list_batch_job_runs,
    list_ingestion_queue_items,
    save_company_document,
)
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def db_conn_with_companies(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


@pytest.fixture
def raw_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "data" / "raw"
    directory.mkdir(parents=True)
    monkeypatch.setattr("config.settings.RAW_DIR", directory)
    return directory


def test_discover_flags_a_new_valid_file_as_pending(raw_dir: Path, db_conn_with_companies: sqlite3.Connection) -> None:
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)

    touched = discover_pending_financial_items(db_conn_with_companies)
    assert touched == 1

    items = list_ingestion_queue_items(db_conn_with_companies)
    assert len(items) == 1
    assert items[0]["status"] == "PENDING"
    assert items[0]["item_kind"] == "financial_file"
    assert items[0]["company_id"] == "HDFCBANK"
    assert items[0]["source_id"] == "screener"
    assert items[0]["content_hash"]


def test_discover_flags_unregistered_company_as_needs_review(raw_dir: Path, db_conn: sqlite3.Connection) -> None:
    """No seed_companies here — HDFCBANK is not registered."""
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)

    discover_pending_financial_items(db_conn)

    item = list_ingestion_queue_items(db_conn)[0]
    assert item["status"] == "NEEDS_REVIEW"
    assert "not registered" in item["status_reason"]


def test_discover_flags_undetectable_path_as_needs_review(raw_dir: Path, db_conn: sqlite3.Connection) -> None:
    file_path = raw_dir / "some_stray_file.txt"
    file_path.write_text("not under a company/source folder")

    discover_pending_financial_items(db_conn)

    item = list_ingestion_queue_items(db_conn)[0]
    assert item["status"] == "NEEDS_REVIEW"


def test_discover_never_queues_dotfiles(raw_dir: Path, db_conn_with_companies: sqlite3.Connection) -> None:
    """.DS_Store/.gitkeep are OS/editor artifacts, not ingestible data — they
    used to get queued and then fail (openpyxl format errors, "missing
    period/value columns"), polluting the Ingest -> Failed Items list with
    noise no admin action could ever resolve."""
    (raw_dir / "HDFCBANK" / "screener").mkdir(parents=True)
    (raw_dir / "HDFCBANK" / "screener" / ".DS_Store").write_bytes(b"junk")
    (raw_dir / ".gitkeep").touch()

    touched = discover_pending_financial_items(db_conn_with_companies)

    assert touched == 0
    assert list_ingestion_queue_items(db_conn_with_companies) == []


def test_discover_never_queues_mfin_reference_pdfs(raw_dir: Path, db_conn: sqlite3.Connection) -> None:
    """data/raw/_macro/mfin/ is archive-only reference material (config.settings
    .DEFAULT_SOURCES' "mfin" entry) — nothing calls ingest_macro_file() on it,
    so it should never enter the queue at all, let alone fail as an
    unparseable macro series file."""
    mfin_dir = raw_dir / "_macro" / "mfin"
    mfin_dir.mkdir(parents=True)
    (mfin_dir / "some-guidance-note.pdf").write_bytes(b"%PDF-1.4 not a csv")

    touched = discover_pending_financial_items(db_conn)

    assert touched == 0
    assert list_ingestion_queue_items(db_conn) == []


def test_discover_is_idempotent_for_an_unchanged_processed_file(
    raw_dir: Path, db_conn_with_companies: sqlite3.Connection
) -> None:
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)

    discover_pending_financial_items(db_conn_with_companies)
    item = list_ingestion_queue_items(db_conn_with_companies)[0]
    process_financial_items(db_conn_with_companies, [item["item_id"]])
    processed = get_ingestion_queue_item_by_path(db_conn_with_companies, str(file_path.resolve()))
    assert processed["status"] == "PROCESSED"

    touched = discover_pending_financial_items(db_conn_with_companies)
    assert touched == 0  # unchanged, already-processed file — not re-flagged

    still_processed = get_ingestion_queue_item_by_path(db_conn_with_companies, str(file_path.resolve()))
    assert still_processed["status"] == "PROCESSED"


def test_discover_reflags_a_changed_processed_file_as_pending(
    raw_dir: Path, db_conn_with_companies: sqlite3.Connection
) -> None:
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)

    discover_pending_financial_items(db_conn_with_companies)
    item = list_ingestion_queue_items(db_conn_with_companies)[0]
    process_financial_items(db_conn_with_companies, [item["item_id"]])

    with file_path.open("ab") as handle:  # guarantee different bytes -> different content_hash
        handle.write(b"\x00trailing-bytes-to-force-a-hash-change")

    discover_pending_financial_items(db_conn_with_companies)
    refreshed = get_ingestion_queue_item_by_path(db_conn_with_companies, str(file_path.resolve()))
    assert refreshed["status"] == "PENDING"
    assert "changed" in refreshed["status_reason"]


def test_process_all_pending_ingests_through_the_real_pipeline(
    raw_dir: Path, db_conn_with_companies: sqlite3.Connection
) -> None:
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)
    discover_pending_financial_items(db_conn_with_companies)

    summary = process_all_pending_financial_items(db_conn_with_companies)
    assert summary.attempted == 1
    assert summary.succeeded == 1
    assert summary.failed == 0

    row = db_conn_with_companies.execute(
        "SELECT COUNT(*) AS n FROM financial_observations WHERE company_id = 'HDFCBANK'"
    ).fetchone()
    assert row["n"] > 0  # actually landed in the same canonical pipeline ingest_file() uses


def test_needs_review_item_is_not_processed(raw_dir: Path, db_conn: sqlite3.Connection) -> None:
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)
    discover_pending_financial_items(db_conn)  # HDFCBANK not registered -> NEEDS_REVIEW
    item = list_ingestion_queue_items(db_conn)[0]

    summary = process_financial_items(db_conn, [item["item_id"]])
    assert summary.failed == 1
    assert summary.succeeded == 0

    still = get_ingestion_queue_item_by_path(db_conn, str(file_path.resolve()))
    assert still["status"] == "NEEDS_REVIEW"  # not silently flipped to FAILED


def test_retry_failed_only_reattempts_failed_rows_and_can_succeed(
    raw_dir: Path, db_conn_with_companies: sqlite3.Connection
) -> None:
    """A row already marked FAILED (e.g. a transient error on a real
    previous attempt) is exactly what Retry Failed re-attempts — using the
    real pipeline again, not a second parallel mechanism."""
    file_path = raw_dir / "HDFCBANK" / "screener" / "HDFCBANK.xlsx"
    file_path.parent.mkdir(parents=True)
    _make_screener_workbook(file_path)
    discover_pending_financial_items(db_conn_with_companies)
    item = list_ingestion_queue_items(db_conn_with_companies)[0]

    from storage.repositories import update_ingestion_queue_item_result

    update_ingestion_queue_item_result(db_conn_with_companies, item["item_id"], status="FAILED", error_message="simulated transient failure")

    summary = retry_failed_financial_items(db_conn_with_companies)
    assert summary.attempted == 1
    assert summary.succeeded == 1

    refreshed = get_ingestion_queue_item_by_path(db_conn_with_companies, str(file_path.resolve()))
    assert refreshed["status"] == "PROCESSED"


def test_discover_pending_documents_counts_unprocessed_rows(db_conn_with_companies: sqlite3.Connection) -> None:
    save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", source_url="https://example.com/investor-relations",
    )
    assert discover_pending_documents(db_conn_with_companies) == 1


def test_process_all_pending_documents_marks_them_processed(db_conn_with_companies: sqlite3.Connection) -> None:
    doc = save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", source_url="https://example.com/investor-relations",
    )
    summary = process_all_pending_documents(db_conn_with_companies)
    assert summary.succeeded == 1

    row = db_conn_with_companies.execute(
        "SELECT processing_status, processed_at FROM documents WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()
    assert row["processing_status"] == "processed"
    assert row["processed_at"] is not None
    assert discover_pending_documents(db_conn_with_companies) == 0


def test_processing_a_document_also_chunks_and_indexes_it(
    db_conn_with_companies: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """Step 2D wiring: processing a document (Step 1's action, now also
    running Step 2A's extraction) also runs the chunker — "processed"
    means registered + hashed + knowledge-extracted + indexed.

    VECTOR_STORE_BACKEND is forced to "none" so the Embedding Indexer
    Worker's outcome (added alongside chunk_indexer/knowledge_builder once
    hybrid retrieval shipped) is deterministic regardless of whether a real
    Qdrant happens to be reachable wherever this test runs — section 10's
    graceful degradation means "skipped", not "failed" or "ok", either way."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client

    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    doc = save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    summary = process_all_pending_documents(db_conn_with_companies)

    assert summary.succeeded == 1
    assert "chunk(s) indexed" in summary.outcomes[0].detail
    chunk_count = db_conn_with_companies.execute(
        "SELECT COUNT(*) AS n FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()["n"]
    assert chunk_count >= 1

    # Both Step 2A/2D calls now go through a published `document` event,
    # fanning out to two independent workers -- confirm both actually ran.
    event_row = db_conn_with_companies.execute(
        "SELECT * FROM dataset_events WHERE dataset_type = 'document'"
    ).fetchone()
    assert event_row is not None
    assert event_row["dataset_id"] == f"document:{doc['document_id']}"
    worker_logs = {
        row["worker_name"]: row["status"]
        for row in db_conn_with_companies.execute(
            "SELECT worker_name, status FROM worker_processing_log WHERE event_id = ?", (event_row["event_id"],)
        ).fetchall()
    }
    assert worker_logs == {
        "knowledge_builder": "ok", "chunk_indexer": "ok", "financial_derivation": "skipped",
        "embedding_indexer": "skipped",  # VECTOR_STORE_BACKEND=none for this test — FTS5/knowledge extraction unaffected
    }


def test_extraction_failure_via_the_event_bus_marks_the_document_failed(
    db_conn_with_companies: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """The Knowledge Builder Worker's failure (surfaced through publish())
    must still fail the document exactly like the old inline call did --
    routing extraction through the event bus doesn't change that contract."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _install_fake_client

    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    doc = save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, "I'm not going to respond in JSON.")

    summary = process_all_pending_documents(db_conn_with_companies)

    assert summary.failed == 1
    row = db_conn_with_companies.execute(
        "SELECT processing_status, error_message FROM documents WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()
    assert row["processing_status"] == "failed"
    assert row["error_message"]


def test_process_documents_records_a_queryable_audit_run(
    db_conn_with_companies: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """process_documents() (and everything that funnels through it --
    Admin -> Ingest queue, retry_failed_documents(),
    process_all_pending_documents()) must record every document's outcome to
    batch_job_runs/batch_job_items (ingestion/batch_log.py) -- the same
    durable audit trail scripts/batch_fetch_nse.py and main.py's
    vector-backfill/graph-backfill use -- not just the in-memory
    ProcessSummary this call returns."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client

    monkeypatch.setattr("config.settings.VECTOR_STORE_BACKEND", "none")
    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    doc = save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    process_all_pending_documents(db_conn_with_companies)

    runs = list_batch_job_runs(db_conn_with_companies)
    assert runs and runs[0]["job_name"] == "document_processing"
    assert runs[0]["status"] == "completed"
    assert runs[0]["items_succeeded"] == 1

    items = list_batch_job_items(db_conn_with_companies, runs[0]["run_id"])
    assert len(items) == 1
    assert items[0]["company_id"] == "HDFCBANK"
    assert items[0]["status"] == "ok"
    assert f"document_id={doc['document_id']}" in items[0]["detail"]
    assert "chunk(s) indexed" in items[0]["detail"]


def test_process_documents_records_a_failed_item_in_the_audit_run(
    db_conn_with_companies: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """The audit trail must reflect a real failure, not just successes --
    same document/error the in-memory ProcessSummary already reports."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _install_fake_client

    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "Revenue grew twelve percent this quarter")
    save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="annual_report",
        fiscal_year="FY2024", quarter=None, added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, "I'm not going to respond in JSON.")

    process_all_pending_documents(db_conn_with_companies)

    runs = list_batch_job_runs(db_conn_with_companies)
    assert runs[0]["items_failed"] == 1
    items = list_batch_job_items(db_conn_with_companies, runs[0]["run_id"])
    assert items[0]["status"] == "failed"
    assert items[0]["detail"]


def test_archive_and_unarchive_documents_record_audit_runs(db_conn_with_companies: sqlite3.Connection) -> None:
    """archive_documents()/unarchive_documents() must each land their own
    queryable batch_job_runs entry -- these had zero test coverage of any
    kind before this, core behavior included."""
    doc = save_company_document(
        db_conn_with_companies, "HDFCBANK", document_type="concall_recording",
        fiscal_year="FY2025", quarter="Q1", added_by_user="tester", source_url="https://example.com/call.mp3",
    )
    document_id = doc["document_id"]

    archived_count = archive_documents(db_conn_with_companies, [document_id])
    assert archived_count == 1
    status = db_conn_with_companies.execute(
        "SELECT processing_status FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()["processing_status"]
    assert status == "archived"

    archive_run = list_batch_job_runs(db_conn_with_companies)[0]
    assert archive_run["job_name"] == "document_archive"
    assert archive_run["items_succeeded"] == 1
    archive_items = list_batch_job_items(db_conn_with_companies, archive_run["run_id"])
    assert archive_items[0]["company_id"] == "HDFCBANK"
    assert "archived" in archive_items[0]["detail"]

    # Re-archiving an already-archived document is a no-op -- the row is
    # skipped before ever reaching `with run.item(...)`, so this second run
    # is recorded with zero items, not a phantom "archived" outcome.
    assert archive_documents(db_conn_with_companies, [document_id]) == 0
    no_op_run = list_batch_job_runs(db_conn_with_companies)[0]
    assert no_op_run["items_total"] == 0

    unarchived_count = unarchive_documents(db_conn_with_companies, [document_id])
    assert unarchived_count == 1
    status = db_conn_with_companies.execute(
        "SELECT processing_status FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()["processing_status"]
    assert status == "pending"

    runs = list_batch_job_runs(db_conn_with_companies)
    unarchive_run = next(r for r in runs if r["job_name"] == "document_unarchive")
    assert unarchive_run["items_succeeded"] == 1
    unarchive_items = list_batch_job_items(db_conn_with_companies, unarchive_run["run_id"])
    assert "unarchived" in unarchive_items[0]["detail"]
