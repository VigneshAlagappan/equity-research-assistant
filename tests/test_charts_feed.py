"""web/charts_feed.py — the live feed backing the Financials tab (and the
Charts tab) for every company, including the 21 companies that used to read
a static, pre-generated web/static/data/*.json instead (see web/app.py's
company_report() and the coverage-comparison migration writeup). Covers the
balanceSheet/incomeStatement shape and the per-cell XBRL-vs-NSE-PDF
provenance marker (_classify_provenance/_provenance_by_period)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from companies.registry import register_company
from storage.database import utcnow_iso
from storage.repositories import company_has_canonical_financials, get_canonical_series_provenance
from web.charts_feed import _classify_provenance, build_charts_feed


def _insert_document(
    conn: sqlite3.Connection, company_id: str, source: str, parser_version: str | None
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (company_id, source, document_type, retrieved_at, parser_version) "
        "VALUES (?, ?, 'financial_result', ?, ?)",
        (company_id, source, utcnow_iso(), parser_version),
    )
    conn.commit()
    return cur.lastrowid


def _insert_observation(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    value: float,
    source: str,
    source_document_id: int | None = None,
    statement_type: str = "consolidated",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO financial_observations (
            company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
            value, unit, currency, source, source_document_id, retrieved_at,
            parser_version, normalization_version, created_at
        ) VALUES (?, ?, 'annual', ?, NULL, ?, ?, 'INR_CRORE', 'INR', ?, ?, ?, 'v1', 'v1', ?)
        """,
        (company_id, metric_key, fiscal_year, statement_type, value, source, source_document_id, utcnow_iso(), utcnow_iso()),
    )
    conn.commit()
    return cur.lastrowid


def _insert_canonical(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    value: float,
    chosen_observation_id: int | None = None,
    statement_type: str = "consolidated",
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_financials (
            company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
            canonical_value, unit, chosen_observation_id, reconciliation_reason,
            normalization_version, decided_at
        ) VALUES (?, ?, 'annual', ?, NULL, ?, ?, 'INR_CRORE', ?, 'test fixture', 'v1', ?)
        """,
        (company_id, metric_key, fiscal_year, statement_type, value, chosen_observation_id, utcnow_iso()),
    )
    conn.commit()


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    register_company(db_conn, "TESTCO", legal_name="Test Co", display_name="Test Co")
    yield db_conn


def test_build_charts_feed_balance_sheet_and_income_statement(company_conn: sqlite3.Connection) -> None:
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0)
    _insert_canonical(company_conn, "TESTCO", "deposits", "FY2023", 500.0)
    _insert_canonical(company_conn, "TESTCO", "net_profit", "FY2023", 50.0)
    _insert_canonical(company_conn, "TESTCO", "total_revenue", "FY2023", 200.0)

    feed = build_charts_feed(company_conn, "TESTCO")

    assert feed["PERIODS"] == ["FY2023"]
    assert feed["PERIOD_KEYS"] == [[2023, 0]]
    assert "balanceSheet" in feed["METRICS"]
    assert "incomeStatement" in feed["METRICS"]

    networth_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "networth")
    assert networth_row["values"] == [100.0]
    assert networth_row["type"] == "fact"

    deposits_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "deposits")
    assert deposits_row["values"] == [500.0]

    net_profit_row = next(r for r in feed["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    assert net_profit_row["values"] == [50.0]

    # "she" is a calc row (fill_missing of two other series) — never carries
    # a "sources" array, even when its own ingredients do.
    she_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "she")
    assert she_row["type"] == "calc"
    assert "sources" not in she_row


def test_build_charts_feed_empty_company_has_no_periods(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "EMPTYCO", legal_name="Empty Co", display_name="Empty Co")
    feed = build_charts_feed(db_conn, "EMPTYCO")
    assert feed["PERIODS"] == []
    assert feed["PERIOD_KEYS"] == []
    for section in feed["METRICS"].values():
        for row in section:
            assert row["values"] == []


def test_classify_provenance_xbrl_when_no_pdf_document() -> None:
    assert _classify_provenance("nse", None) == "xbrl"
    assert _classify_provenance("sec_edgar", None) == "xbrl"


def test_classify_provenance_nse_pdf_when_parser_version_matches() -> None:
    assert _classify_provenance("nse", "nse_pdf_spike_v1") == "nse_pdf"
    assert _classify_provenance("nse", "nse_pdf_v2") == "nse_pdf"


def test_classify_provenance_none_for_unclassified_sources() -> None:
    assert _classify_provenance("screener", None) is None
    assert _classify_provenance("proprietary", None) is None
    assert _classify_provenance(None, None) is None


def test_build_charts_feed_networth_row_carries_xbrl_provenance(company_conn: sqlite3.Connection) -> None:
    obs_id = _insert_observation(company_conn, "TESTCO", "reserves", "FY2023", 100.0, source="nse")
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0, chosen_observation_id=obs_id)

    feed = build_charts_feed(company_conn, "TESTCO")
    networth_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "networth")
    assert networth_row["sources"] == ["xbrl"]


def test_build_charts_feed_networth_row_carries_nse_pdf_provenance(company_conn: sqlite3.Connection) -> None:
    doc_id = _insert_document(company_conn, "TESTCO", source="nse", parser_version="nse_pdf_spike_v1")
    obs_id = _insert_observation(company_conn, "TESTCO", "reserves", "FY2023", 100.0, source="nse", source_document_id=doc_id)
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0, chosen_observation_id=obs_id)

    feed = build_charts_feed(company_conn, "TESTCO")
    networth_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "networth")
    assert networth_row["sources"] == ["nse_pdf"]


def test_build_charts_feed_networth_row_unclassified_for_screener_source(company_conn: sqlite3.Connection) -> None:
    obs_id = _insert_observation(company_conn, "TESTCO", "reserves", "FY2023", 100.0, source="screener")
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0, chosen_observation_id=obs_id)

    feed = build_charts_feed(company_conn, "TESTCO")
    networth_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "networth")
    assert networth_row["sources"] == [None]


def test_build_charts_feed_no_sources_array_when_no_provenance_data(company_conn: sqlite3.Connection) -> None:
    # A canonical value with no chosen_observation_id at all (never happens
    # in practice, but the LEFT JOIN chain must degrade gracefully).
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0, chosen_observation_id=None)
    feed = build_charts_feed(company_conn, "TESTCO")
    networth_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "networth")
    assert networth_row["sources"] == [None]


def test_get_canonical_series_provenance_returns_source_and_parser_version(company_conn: sqlite3.Connection) -> None:
    doc_id = _insert_document(company_conn, "TESTCO", source="nse", parser_version="nse_pdf_spike_v1")
    obs_id = _insert_observation(company_conn, "TESTCO", "reserves", "FY2023", 100.0, source="nse", source_document_id=doc_id)
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0, chosen_observation_id=obs_id)

    rows = get_canonical_series_provenance(company_conn, "TESTCO", "reserves")
    assert len(rows) == 1
    assert rows[0]["fiscal_year"] == "FY2023"
    assert rows[0]["source"] == "nse"
    assert rows[0]["parser_version"] == "nse_pdf_spike_v1"


def test_company_has_canonical_financials_false_when_empty(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "EMPTYCO", legal_name="Empty Co", display_name="Empty Co")
    assert company_has_canonical_financials(db_conn, "EMPTYCO") is False


def test_company_has_canonical_financials_true_with_any_row(company_conn: sqlite3.Connection) -> None:
    _insert_canonical(company_conn, "TESTCO", "reserves", "FY2023", 100.0)
    assert company_has_canonical_financials(company_conn, "TESTCO") is True
