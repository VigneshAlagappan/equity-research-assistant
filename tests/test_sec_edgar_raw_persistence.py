"""ingestion/pipeline.py::ingest_sec_edgar_company() -- ADR-022 raw/
companies/ wiring. Mirrors tests/test_pipeline.py's FRED raw-persistence
tests (raw-before-ingest, dedup-on-rerun, new-object-on-changed-content),
without touching the existing SEC EDGAR parsing logic itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companies.registry import register_company
from ingestion.pipeline import ingest_sec_edgar_company
from storage import raw_object_repository as ror


def _facts(total_assets: float, *, end: str = "2024-12-31", fy: int = 2024) -> bytes:
    return json.dumps(
        {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {"end": end, "val": total_assets, "fy": fy, "fp": "FY", "form": "10-K", "filed": end},
                            ]
                        }
                    }
                }
            }
        }
    ).encode("utf-8")


@pytest.fixture
def apple(db_conn) -> str:
    register_company(
        db_conn, "AAPL", "Apple Inc.", "Apple", country="US", currency="USD", fiscal_year_end_month=12,
    )
    return "AAPL"


@pytest.fixture(autouse=True)
def _local_document_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    monkeypatch.setattr("config.settings.DOCUMENT_STORE_BACKEND", "local")


def _mock_companyfacts(monkeypatch, body: bytes) -> None:
    class _FakeResponse:
        status_code = 200

        def __init__(self, content: bytes) -> None:
            self.content = content

        def json(self):
            return json.loads(self.content)

        def raise_for_status(self):
            return None

    monkeypatch.setattr("sources.sec_edgar.requests.get", lambda *a, **k: _FakeResponse(body))


def test_ingest_sec_edgar_lands_raw_object_before_financial_observations(db_conn, apple, monkeypatch) -> None:
    _mock_companyfacts(monkeypatch, _facts(1_000_000_000))

    result = ingest_sec_edgar_company(db_conn, apple, cik=320193)
    assert result.inserted_count > 0  # existing parse/insert behavior unaffected

    raw_rows = ror.list_raw_objects(db_conn, source="sec_edgar", entity="AAPL")
    assert len(raw_rows) == 1
    assert raw_rows[0]["state"] == "ingested"
    assert raw_rows[0]["object_type"] == "companyfacts"
    assert raw_rows[0]["raw_prefix"] == "companies"
    assert raw_rows[0]["processed_at"] is not None

    lineage = ror.get_lineage_for_object(db_conn, raw_rows[0]["object_id"])
    assert len(lineage) == 1
    assert lineage[0]["derived_table"] == "financial_observations"


def test_ingest_sec_edgar_rerun_with_identical_companyfacts_dedups(db_conn, apple, monkeypatch) -> None:
    _mock_companyfacts(monkeypatch, _facts(1_000_000_000))

    ingest_sec_edgar_company(db_conn, apple, cik=320193)
    ingest_sec_edgar_company(db_conn, apple, cik=320193)

    raw_rows = ror.list_raw_objects(db_conn, source="sec_edgar", entity="AAPL")
    assert len(raw_rows) == 1, "byte-identical companyfacts on a re-run must not create a second raw object"


def test_ingest_sec_edgar_updated_companyfacts_creates_new_raw_object(db_conn, apple, monkeypatch) -> None:
    _mock_companyfacts(monkeypatch, _facts(1_000_000_000))
    ingest_sec_edgar_company(db_conn, apple, cik=320193)

    # SEC published a restated/updated figure -- genuinely different content.
    _mock_companyfacts(monkeypatch, _facts(1_100_000_000))
    ingest_sec_edgar_company(db_conn, apple, cik=320193)

    raw_rows = ror.list_raw_objects(db_conn, source="sec_edgar", entity="AAPL")
    assert len(raw_rows) == 2, "genuinely changed companyfacts content must create a second, distinct raw object"
