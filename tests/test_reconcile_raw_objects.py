"""scripts/reconcile_raw_objects.py -- ADR-022's S3<->Postgres catalog
reconciliation. Never deletes/recreates anything; report-only. Covers the
three states the ADR names: clean, orphaned S3 key, broken catalog row."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.reconcile_raw_objects import reconcile_raw_objects, run_raw_object_reconciliation
from storage import raw_object_repository as ror
from storage.document_store import LocalDocumentStore


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch) -> LocalDocumentStore:
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    return LocalDocumentStore()


def _catalog_and_store(conn, local_store, key: str, content: bytes = b"{}") -> int:
    """Insert a raw_objects row AND write the matching bytes -- a fully
    consistent object, the baseline every test starts from before
    introducing one specific kind of drift."""
    local_store.store(key, content)
    return ror.insert_raw_object(
        conn, source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", source_url=None, raw_prefix="market-data",
        s3_key=key, content_hash="deadbeef",
    )


def test_clean_state_reports_no_drift(db_conn, local_store) -> None:
    _catalog_and_store(db_conn, local_store, "raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json")

    result = reconcile_raw_objects(db_conn, document_store=local_store)

    assert result.clean
    assert result.checked_count == 1
    assert result.orphaned_s3_keys == []
    assert result.broken_catalog_rows == []


def test_orphaned_s3_key_detected(db_conn, local_store) -> None:
    _catalog_and_store(db_conn, local_store, "raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json")
    # An object written to the store with no catalog row at all -- e.g. a
    # write that succeeded but whose catalog INSERT then failed.
    local_store.store("raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/y/orphan.json", b"{}")

    result = reconcile_raw_objects(db_conn, document_store=local_store)

    assert not result.clean
    assert result.orphaned_s3_keys == ["raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/y/orphan.json"]
    assert result.broken_catalog_rows == []


def test_broken_catalog_row_detected(db_conn, local_store) -> None:
    object_id = _catalog_and_store(
        db_conn, local_store, "raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json",
    )
    # The object was removed from the store outside this app's normal
    # delete-never code paths (e.g. manual bucket cleanup) -- the catalog
    # row is now a broken reference.
    local_store.delete("raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json")

    result = reconcile_raw_objects(db_conn, document_store=local_store)

    assert not result.clean
    assert result.orphaned_s3_keys == []
    assert len(result.broken_catalog_rows) == 1
    assert result.broken_catalog_rows[0]["object_id"] == object_id


def test_reconciliation_never_deletes_or_recreates_anything(db_conn, local_store) -> None:
    """A drifted state stays exactly as drifted after reconciliation runs
    -- report-only, per ADR-022's explicit requirement."""
    _catalog_and_store(db_conn, local_store, "raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json")
    local_store.store("raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/y/orphan.json", b"{}")

    reconcile_raw_objects(db_conn, document_store=local_store)

    # The orphan is still there (not deleted); no new catalog row was
    # conjured up for it (not silently "fixed").
    assert local_store.exists("raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/y/orphan.json")
    rows = ror.list_all_raw_objects(db_conn)
    assert len(rows) == 1


def test_run_raw_object_reconciliation_audits_each_finding(db_conn, local_store, monkeypatch) -> None:
    """The BatchRun-wrapped entry point -- each drift becomes its own
    audited item, the run itself completes regardless of what it finds."""
    monkeypatch.setattr("scripts.reconcile_raw_objects.default_document_store", lambda: local_store)
    _catalog_and_store(db_conn, local_store, "raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/x/hash.json")
    local_store.store("raw/market-data/yfinance_prices/HDFCBANK/ohlcv_batch/y/orphan.json", b"{}")

    from storage.repositories import get_batch_job_run_live_progress, list_batch_job_items, list_batch_job_runs

    run_id = run_raw_object_reconciliation(conn=db_conn)

    runs = list_batch_job_runs(db_conn, job_name="raw_object_reconciliation")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"

    items = list_batch_job_items(db_conn, run_id)
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert "orphaned S3 key" in items[0]["detail"]
