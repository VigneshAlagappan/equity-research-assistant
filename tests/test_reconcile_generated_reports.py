"""scripts/reconcile_generated_reports.py -- S3<->Postgres reconciliation
for generated_reports.s3_key, the same ADR-022-style report-only check
reconcile_raw_objects.py does for ingested documents. Covers the three
states: clean, orphaned S3 key, broken catalog row (a real production gap
found live -- research_thread() used to 500 on exactly this)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.reconcile_generated_reports import (
    reconcile_generated_reports,
    run_generated_report_reconciliation,
)
from storage.document_store import LocalDocumentStore
from storage.repositories import save_generated_report, update_generated_report_s3_metadata


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch) -> LocalDocumentStore:
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    return LocalDocumentStore()


def _thread_with_s3(conn, local_store, thread_id: str, content: bytes = b"{}") -> str:
    """A fully consistent generated_reports row + its matching S3 artifact
    -- the baseline every test starts from before introducing one specific
    kind of drift."""
    save_generated_report(conn, thread_id, "test question", ["HDFCBANK"], "consolidated", "test answer")
    s3_key = f"threads/{thread_id}/v1.json"
    local_store.store(s3_key, content)
    update_generated_report_s3_metadata(conn, thread_id, s3_key=s3_key, abstract=None, version=1)
    return s3_key


def test_clean_state_reports_no_drift(db_conn, local_store) -> None:
    _thread_with_s3(db_conn, local_store, "th1")

    result = reconcile_generated_reports(db_conn, document_store=local_store)

    assert result.clean
    assert result.checked_count == 1
    assert result.orphaned_s3_keys == []
    assert result.broken_catalog_rows == []


def test_orphaned_s3_key_detected(db_conn, local_store) -> None:
    _thread_with_s3(db_conn, local_store, "th1")
    # An object written to the store with no generated_reports row pointing
    # at it -- e.g. a write that succeeded but whose s3_key-recording
    # UPDATE then failed.
    local_store.store("threads/orphan/v1.json", b"{}")

    result = reconcile_generated_reports(db_conn, document_store=local_store)

    assert not result.clean
    assert result.orphaned_s3_keys == ["threads/orphan/v1.json"]
    assert result.broken_catalog_rows == []


def test_broken_catalog_row_detected(db_conn, local_store) -> None:
    s3_key = _thread_with_s3(db_conn, local_store, "th1")
    # The object is gone from the store even though the row's s3_key still
    # points at it -- the exact class of bug found live (research_thread()
    # used to 500 on this; it now falls back to the row's own
    # report_markdown/evidence/followups columns).
    local_store.delete(s3_key)

    result = reconcile_generated_reports(db_conn, document_store=local_store)

    assert not result.clean
    assert result.orphaned_s3_keys == []
    assert len(result.broken_catalog_rows) == 1
    assert result.broken_catalog_rows[0]["thread_id"] == "th1"


def test_row_with_no_s3_key_is_not_flagged(db_conn, local_store) -> None:
    # A row saved before ADR-021's S3 persistence split (or one whose
    # _persist_generated_report_s3 call never completed) has s3_key IS
    # NULL -- research_thread()'s own plain else-branch already handles
    # this safely, so it's not drift for this job to report.
    save_generated_report(db_conn, "th-no-s3", "test question", ["HDFCBANK"], "consolidated", "test answer")

    result = reconcile_generated_reports(db_conn, document_store=local_store)

    assert result.clean
    assert result.checked_count == 0


def test_run_generated_report_reconciliation_audits_each_finding(db_conn, local_store, monkeypatch) -> None:
    """The BatchRun-wrapped entry point -- each drift becomes its own
    audited item, the run itself completes regardless of what it finds."""
    monkeypatch.setattr("scripts.reconcile_generated_reports.default_document_store", lambda: local_store)
    s3_key = _thread_with_s3(db_conn, local_store, "th1")
    local_store.delete(s3_key)

    from storage.repositories import list_batch_job_items, list_batch_job_runs

    run_id = run_generated_report_reconciliation(conn=db_conn)

    runs = list_batch_job_runs(db_conn, job_name="generated_report_reconciliation")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"

    items = list_batch_job_items(db_conn, run_id)
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert "broken s3_key" in items[0]["detail"]
