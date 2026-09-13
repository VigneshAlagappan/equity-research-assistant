"""scripts/batch_fetch_nse.py::_run_corporate_actions() -- ADR-022 raw/
regulatory/ wiring. Mirrors the FRED/SEC EDGAR raw-persistence test shape
(raw-before-ingest, dedup-on-rerun, new-object-on-changed-content)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companies.registry import seed_companies
from scripts.batch_fetch_nse import _run_corporate_actions
from storage import raw_object_repository as ror


def _actions_payload(*subjects: str) -> bytes:
    return json.dumps(
        [
            {
                "symbol": "HDFCBANK", "subject": subject, "exDate": "25-Jan-2025", "recDate": "27-Jan-2025",
                "faceVal": "1", "bcStartDate": "-", "bcEndDate": "-",
            }
            for subject in subjects
        ]
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def _local_document_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    monkeypatch.setattr("config.settings.DOCUMENT_STORE_BACKEND", "local")


def _mock_actions(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr("scripts.batch_fetch_nse.fetch_corporate_actions_raw", lambda symbol: body)


def test_run_corporate_actions_lands_raw_object_before_insert(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_actions(monkeypatch, _actions_payload("Dividend - Rs 13 Per Share"))

    detail = _run_corporate_actions(db_conn, "HDFCBANK")
    assert "fetched=1" in detail

    raw_rows = ror.list_raw_objects(db_conn, source="nse_corporate_actions", entity="HDFCBANK")
    assert len(raw_rows) == 1
    assert raw_rows[0]["state"] == "ingested"
    assert raw_rows[0]["object_type"] == "corporate_actions_feed"
    assert raw_rows[0]["raw_prefix"] == "regulatory"

    lineage = ror.get_lineage_for_object(db_conn, raw_rows[0]["object_id"])
    assert len(lineage) == 1
    assert lineage[0]["derived_table"] == "corporate_actions_raw"


def test_run_corporate_actions_rerun_with_identical_feed_dedups(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_actions(monkeypatch, _actions_payload("Dividend - Rs 13 Per Share"))

    _run_corporate_actions(db_conn, "HDFCBANK")
    _run_corporate_actions(db_conn, "HDFCBANK")

    raw_rows = ror.list_raw_objects(db_conn, source="nse_corporate_actions", entity="HDFCBANK")
    assert len(raw_rows) == 1, "an unchanged corporate-actions feed re-fetch must not create a second raw object"


def test_run_corporate_actions_new_action_creates_new_raw_object(db_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    _mock_actions(monkeypatch, _actions_payload("Dividend - Rs 13 Per Share"))
    _run_corporate_actions(db_conn, "HDFCBANK")

    # NSE published a new corporate action -- genuinely different content.
    _mock_actions(monkeypatch, _actions_payload("Dividend - Rs 13 Per Share", "Bonus 1:1"))
    _run_corporate_actions(db_conn, "HDFCBANK")

    raw_rows = ror.list_raw_objects(db_conn, source="nse_corporate_actions", entity="HDFCBANK")
    assert len(raw_rows) == 2, "a genuinely changed feed must create a second, distinct raw object"
