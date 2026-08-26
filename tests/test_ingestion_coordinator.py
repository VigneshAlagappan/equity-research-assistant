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
