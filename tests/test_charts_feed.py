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

    # Lending-book lines are only shown for financial companies.
    bs_keys = {r["key"] for r in feed["METRICS"]["balanceSheet"]}
    assert not bs_keys & {"deposits", "borrowings", "advances"}

    net_profit_row = next(r for r in feed["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    assert net_profit_row["values"] == [50.0]

    # "she" is a calc row (fill_missing of two other series) — never carries
    # a "sources" array, even when its own ingredients do.
    she_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "she")
    assert she_row["type"] == "calc"
    assert "sources" not in she_row


def test_build_charts_feed_financial_company_keeps_lending_rows(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "BANKCO", legal_name="Bank Co", display_name="Bank Co")
    db_conn.execute("UPDATE companies SET sector = 'Financial Services' WHERE company_id = 'BANKCO'")
    db_conn.commit()
    _insert_canonical(db_conn, "BANKCO", "deposits", "FY2023", 500.0)

    feed = build_charts_feed(db_conn, "BANKCO")

    deposits_row = next(r for r in feed["METRICS"]["balanceSheet"] if r["key"] == "deposits")
    assert deposits_row["values"] == [500.0]


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


def test_dividend_amount_parses_nse_subject_styles() -> None:
    from web.charts_feed import _dividend_amount_per_share as parse

    assert parse("Dividend - Rs 13 Per Share") == 13.0
    assert parse("Interim Dividend - Re 0.70 Per Share") == 0.7
    assert parse("Annual General Meeting/Dividend - Rs 2.50 Per Share") == 2.5
    assert parse("Agm/Div-Rs.12/- Per Share") == 12.0
    assert parse("Dividend - Rs 5 Per Share And Special Dividend - Rs 2 Per Share") == 7.0
    assert parse("Div185%") is None


def test_dividend_row_filled_from_corporate_actions_when_canonical_missing(company_conn: sqlite3.Connection) -> None:
    _insert_canonical(company_conn, "TESTCO", "net_profit", "FY2024", 50.0)
    _insert_canonical(company_conn, "TESTCO", "dividend_per_share", "FY2023", 3.0)
    company_conn.execute(
        "INSERT INTO corporate_actions_raw (company_id, subject, ex_date, raw_json, retrieved_at) VALUES "
        "('TESTCO', 'Interim Dividend - Rs 2 Per Share', '2023-11-10', '{}', 'x'), "
        "('TESTCO', 'Dividend - Rs 4.50 Per Share', '2024-07-20', '{}', 'x')"
    )
    company_conn.execute(
        "INSERT INTO corporate_actions (raw_id, company_id, action_type, subject, ex_date, classifier_version, created_at) "
        "SELECT raw_id, company_id, 'dividend', subject, ex_date, 'v3', 'x' FROM corporate_actions_raw"
    )
    company_conn.commit()
    fye = company_conn.execute("SELECT fiscal_year_end_month FROM companies WHERE company_id='TESTCO'").fetchone()[0]

    feed = build_charts_feed(company_conn, "TESTCO")

    row = next(r for r in feed["METRICS"]["perShare"] if r["key"] == "dividend")
    by_period = dict(zip(feed["PERIODS"], row["values"]))
    assert by_period["FY2023"] == 3.0  # canonical value wins, never overwritten
    assert fye == 3
    assert by_period["FY2024"] == 2.0  # only the Nov-2023 interim falls in Apr23-Mar24
    assert row["type"] == "calc"


@pytest.mark.parametrize(
    "fye_month, ex_date, expected_fy",
    [
        (3, "2024-02-15", 2024),   # India, Apr-Mar: Feb 2024 is in FY2024
        (3, "2024-04-02", 2025),   # ...and Apr 2024 starts FY2025
        (12, "2024-12-31", 2024),  # US calendar-year filer
        (12, "2025-01-02", 2025),
        (9, "2024-09-30", 2024),   # Apple-style Oct-Sep
        (9, "2024-10-01", 2025),
        (6, "2024-07-01", 2025),   # Microsoft-style Jul-Jun
        (1, "2024-01-31", 2024),   # NVIDIA-style Feb-Jan
        (1, "2024-02-01", 2025),
    ],
)
def test_dividend_fill_uses_each_companys_own_fiscal_year(fye_month: int, ex_date: str, expected_fy: int) -> None:
    from web.charts_feed import _period_date_range

    periods = [(2024, 0), (2025, 0)]
    hit = [
        y for y, q in periods
        if (lambda r: r[0].isoformat() <= ex_date <= r[1].isoformat())(_period_date_range(fye_month, y, q))
    ]
    assert hit == [expected_fy]


def test_dividend_amount_parses_dollar_subjects() -> None:
    from web.charts_feed import _dividend_amount_per_share as parse

    assert parse("Cash Dividend - $0.24 Per Share") == 0.24
    assert parse("Dividend - USD 1.10 Per Share") == 1.1
