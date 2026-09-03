"""analytics/patterns.py — deterministic YoY-spike detection (Tools tab's
Analytics panel). Uses the same fixture tests/test_calculations.py ingests:
HDFCBANK net_profit 17,000 (FY2023) -> 20,500 (FY2024), a real +20.6% YoY
move — under the default 25% threshold, over a lower one, letting one
fixture cover both the "flagged" and "not flagged" cases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from analytics.patterns import detect_yoy_spikes
from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_no_pattern_at_default_threshold(ingested_conn: sqlite3.Connection) -> None:
    """+20.6% net_profit YoY doesn't clear the default 25% bar."""
    patterns = detect_yoy_spikes(ingested_conn)
    assert not any(p.company_id == "HDFCBANK" and p.metric_key == "net_profit" for p in patterns)


def test_pattern_flagged_at_lower_threshold(ingested_conn: sqlite3.Connection) -> None:
    patterns = detect_yoy_spikes(ingested_conn, threshold_percent=15.0)
    net_profit_patterns = [p for p in patterns if p.company_id == "HDFCBANK" and p.metric_key == "net_profit"]
    assert len(net_profit_patterns) == 1
    p = net_profit_patterns[0]
    assert p.metric_label == "Net Profit"
    assert p.fiscal_year == "FY2024"
    assert p.yoy_percent == pytest.approx(20.588, abs=0.01)


def test_sorted_by_magnitude_descending(ingested_conn: sqlite3.Connection) -> None:
    patterns = detect_yoy_spikes(ingested_conn, threshold_percent=10.0)
    magnitudes = [abs(p.yoy_percent) for p in patterns]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_company_with_no_financial_data_is_never_iterated(ingested_conn: sqlite3.Connection) -> None:
    """ICICIBANK is seeded (seed_companies) but has no canonical_financials
    rows — the list_company_ids_with_financial_data prefilter must exclude
    it, not iterate it and hit a MissingDataError-driven skip."""
    from storage.repositories import list_company_ids_with_financial_data

    company_ids = list_company_ids_with_financial_data(ingested_conn)
    assert company_ids == ["HDFCBANK"]


def test_no_data_at_all_returns_empty(db_conn: sqlite3.Connection) -> None:
    assert detect_yoy_spikes(db_conn) == []
