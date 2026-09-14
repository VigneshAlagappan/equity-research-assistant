"""scripts/batch_fetch_nse.py::_run_shareholding() -- ADR-022 raw/
regulatory/ wiring for BOTH the master listing (one raw object per
company) and the per-quarter XBRL detail (one raw object per quarter).
Mirrors the established raw-before-ingest / dedup-on-rerun / new-object-
on-changed-content test shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companies.registry import seed_companies
from scripts.batch_fetch_nse import _run_shareholding
from storage import raw_object_repository as ror

_MINIMAL_XBRL = b'<?xml version="1.0"?><root/>'


def _master_payload(*, date_str: str = "30-JUN-2026", xbrl_url: str = "https://example.test/shp.xml") -> bytes:
    return json.dumps(
        [
            {
                "symbol": "HDFCBANK", "date": date_str, "pr_and_prgrp": "0", "public_val": "100",
                "employeeTrusts": "0", "submissionDate": date_str, "xbrl": xbrl_url,
            }
        ]
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def _local_document_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    monkeypatch.setattr("config.settings.DOCUMENT_STORE_BACKEND", "local")


def _mock_fetch(monkeypatch, *, master_body: bytes, detail_body: bytes = _MINIMAL_XBRL) -> None:
    monkeypatch.setattr("scripts.batch_fetch_nse.fetch_shareholding_master_raw", lambda symbol: master_body)
    monkeypatch.setattr("scripts.batch_fetch_nse.fetch_shareholding_detail_raw", lambda url: detail_body)


def test_run_shareholding_lands_master_and_detail_raw_objects(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_fetch(monkeypatch, master_body=_master_payload())

    _run_shareholding(db_conn, "HDFCBANK")

    master_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_master")
    assert len(master_rows) == 1
    assert master_rows[0]["state"] == "ingested"
    assert master_rows[0]["raw_prefix"] == "regulatory"

    detail_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_detail")
    assert len(detail_rows) == 1
    assert detail_rows[0]["state"] == "ingested"

    lineage = ror.get_lineage_for_object(db_conn, master_rows[0]["object_id"])
    assert len(lineage) == 1
    detail_lineage = ror.get_lineage_for_object(db_conn, detail_rows[0]["object_id"])
    assert len(detail_lineage) == 1


def test_run_shareholding_rerun_with_identical_master_dedups(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_fetch(monkeypatch, master_body=_master_payload())

    _run_shareholding(db_conn, "HDFCBANK")
    _run_shareholding(db_conn, "HDFCBANK")

    master_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_master")
    assert len(master_rows) == 1, "an unchanged master listing re-fetch must not create a second raw object"

    # The detail fetch itself is also naturally skipped on rerun (existing
    # detail_fetched_at idempotency, unchanged) -- so still exactly one
    # detail raw object too, not because of dedup but because the detail
    # HTTP call was never re-issued for an already-fetched quarter.
    detail_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_detail")
    assert len(detail_rows) == 1


def test_run_shareholding_new_quarter_creates_new_master_raw_object(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_fetch(monkeypatch, master_body=_master_payload(date_str="30-JUN-2026"))
    _run_shareholding(db_conn, "HDFCBANK")

    # NSE published a new quarter's submission -- genuinely different master listing.
    _mock_fetch(monkeypatch, master_body=_master_payload(date_str="30-SEP-2026"))
    _run_shareholding(db_conn, "HDFCBANK")

    master_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_master")
    assert len(master_rows) == 2, "a genuinely changed master listing must create a second, distinct raw object"

    # A new quarter also means a new (not-yet-fetched) detail XBRL raw object.
    detail_rows = ror.list_raw_objects(db_conn, source="nse_shareholding", entity="HDFCBANK", object_type="shareholding_detail")
    assert len(detail_rows) == 2
