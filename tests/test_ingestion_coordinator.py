"""Ingestion Coordinator — Admin/Settings -> Ingest queue's discovery and
dispatch layer. Reuses ingestion/pipeline.py under the hood; these tests
exercise the new discovery/status-tracking behavior on top of it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.coordinator import (
    discover_pending_documents,
    discover_pending_financial_items,
    process_all_pending_documents,
    process_all_pending_financial_items,
    process_financial_items,
    retry_failed_financial_items,
)
from storage.repositories import (
    get_ingestion_queue_item_by_path,
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
    means registered + hashed + knowledge-extracted + indexed."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client

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
    assert worker_logs == {"knowledge_builder": "ok", "chunk_indexer": "ok", "financial_derivation": "skipped"}


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
