"""scripts/fetch_daily_prices.py::run_price_history_update() -- proof-of-
concept wiring for docs/ADR/022-s3-raw-processed-object-store-with-
lineage-catalog.md: every fetched OHLCV batch lands in raw/market-data/
(cataloged in raw_objects) BEFORE daily_prices is touched, and a re-run
with identical bars dedups instead of creating a second raw object."""

from __future__ import annotations

from pathlib import Path

import pytest

from companies.registry import seed_companies
from scripts.fetch_daily_prices import run_price_history_update
from sources.yfinance_prices import PriceBar
from storage import raw_object_repository as ror
from storage.company_repository import tag_companies_index
from storage.price_database import init_price_db
from storage.price_repository import get_price_history


@pytest.fixture
def price_conn(tmp_path: Path):
    conn = init_price_db(db_path=tmp_path / "prices.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _local_document_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    monkeypatch.setattr("config.settings.DOCUMENT_STORE_BACKEND", "local")


_BARS = [
    PriceBar(trade_date="2026-09-10", open=100.0, high=102.0, low=99.0, close=101.0, volume=1000),
    PriceBar(trade_date="2026-09-11", open=101.0, high=103.0, low=100.5, close=102.5, volume=1200),
]


def test_fetch_lands_raw_object_before_daily_prices(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK", "ICICIBANK"], "Nifty 500")
    monkeypatch.setattr(
        "scripts.fetch_daily_prices.fetch_daily_bars",
        lambda symbol, period=None: _BARS if symbol in ("HDFCBANK", "ICICIBANK") else [],
    )

    run_price_history_update(main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", job_name="test_ph")

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_prices", entity="HDFCBANK")
    assert len(raw_rows) == 1
    assert raw_rows[0]["state"] == "ingested"
    assert raw_rows[0]["object_type"] == "ohlcv_batch"
    assert raw_rows[0]["period"] == "2026-09-10..2026-09-11"
    assert raw_rows[0]["processed_at"] is not None

    # daily_prices got the same two bars, existing behavior unchanged.
    history = get_price_history(price_conn, "HDFCBANK", "2026-09-01", "2026-09-30")
    assert len(history) == 2

    # Lineage links the raw object to the daily_prices write.
    lineage = ror.get_lineage_for_object(db_conn, raw_rows[0]["object_id"])
    assert len(lineage) == 1
    assert lineage[0]["derived_table"] == "daily_prices"


def test_rerun_with_identical_bars_dedups_raw_object(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK", "ICICIBANK"], "Nifty 500")
    monkeypatch.setattr(
        "scripts.fetch_daily_prices.fetch_daily_bars",
        lambda symbol, period=None: _BARS if symbol in ("HDFCBANK", "ICICIBANK") else [],
    )

    run_price_history_update(main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", job_name="test_ph")
    run_price_history_update(main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", job_name="test_ph")

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_prices", entity="HDFCBANK")
    assert len(raw_rows) == 1, "identical bars on the second run must not create a second raw object"


def test_rerun_with_changed_bars_creates_new_raw_object(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK", "ICICIBANK"], "Nifty 500")
    call_count = {"n": 0}

    def _fetch(symbol, period=None):
        if symbol not in ("HDFCBANK", "ICICIBANK"):
            return []
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return _BARS
        # A later "day" appears -- genuinely new content for the same company.
        return _BARS + [PriceBar(trade_date="2026-09-12", open=102.5, high=104.0, low=102.0, close=103.5, volume=900)]

    monkeypatch.setattr("scripts.fetch_daily_prices.fetch_daily_bars", _fetch)

    run_price_history_update(main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", job_name="test_ph")
    run_price_history_update(main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", job_name="test_ph")

    raw_rows = ror.list_raw_objects(db_conn, source="yfinance_prices", entity="HDFCBANK")
    assert len(raw_rows) == 2, "genuinely changed content must create a second, distinct raw object"
