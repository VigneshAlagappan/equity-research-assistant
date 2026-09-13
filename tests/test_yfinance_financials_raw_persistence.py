"""ingestion/pipeline.py::ingest_yfinance_company() -- ADR-022 raw/
companies/ wiring. Mirrors the established raw-before-ingest / dedup-on-
rerun / new-object-on-changed-content test shape, using a fake yf.Ticker
(same pattern tests/test_web.py's own yfinance test already establishes)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from companies.registry import register_company
from ingestion.pipeline import ingest_yfinance_company
from storage import raw_object_repository as ror


class _FakeTicker:
    def __init__(self, revenue: float) -> None:
        self.financials = pd.DataFrame({pd.Timestamp("2024-09-30"): [revenue]}, index=["Total Revenue"])
        self.balance_sheet = pd.DataFrame()
        self.cashflow = pd.DataFrame()


@pytest.fixture(autouse=True)
def _local_document_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    monkeypatch.setattr("config.settings.DOCUMENT_STORE_BACKEND", "local")


@pytest.fixture
def apple(db_conn) -> str:
    register_company(db_conn, "AAPL", "Apple Inc.", "Apple", country="US", currency="USD", fiscal_year_end_month=12)
    return "AAPL"


def _mock_ticker(monkeypatch, revenue: float) -> None:
    monkeypatch.setattr("sources.yfinance_financials.yf.Ticker", lambda *a, **k: _FakeTicker(revenue))


def test_ingest_yfinance_lands_raw_object_before_financial_observations(db_conn, apple, monkeypatch) -> None:
    _mock_ticker(monkeypatch, 1000.0)

    result = ingest_yfinance_company(db_conn, apple, "AAPL", currency="USD")
    assert result.inserted_count > 0

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_financials", entity="AAPL")
    assert len(raw_rows) == 1
    assert raw_rows[0]["state"] == "ingested"
    assert raw_rows[0]["object_type"] == "annual_statements"
    assert raw_rows[0]["raw_prefix"] == "companies"

    lineage = ror.get_lineage_for_object(db_conn, raw_rows[0]["object_id"])
    assert len(lineage) == 1
    assert lineage[0]["derived_table"] == "financial_observations"


def test_ingest_yfinance_rerun_with_identical_statements_dedups(db_conn, apple, monkeypatch) -> None:
    _mock_ticker(monkeypatch, 1000.0)

    ingest_yfinance_company(db_conn, apple, "AAPL", currency="USD")
    ingest_yfinance_company(db_conn, apple, "AAPL", currency="USD")

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_financials", entity="AAPL")
    assert len(raw_rows) == 1, "byte-identical statements on a re-run must not create a second raw object"


def test_ingest_yfinance_updated_statements_creates_new_raw_object(db_conn, apple, monkeypatch) -> None:
    _mock_ticker(monkeypatch, 1000.0)
    ingest_yfinance_company(db_conn, apple, "AAPL", currency="USD")

    # A restated/updated figure -- genuinely different content.
    _mock_ticker(monkeypatch, 1100.0)
    ingest_yfinance_company(db_conn, apple, "AAPL", currency="USD")

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_financials", entity="AAPL")
    assert len(raw_rows) == 2, "genuinely changed statement content must create a second, distinct raw object"
