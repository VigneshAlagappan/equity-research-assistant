"""retrieval/structured_search.py never calls the LLM — pure DB/calculation
tests, same fixture pattern as tests/test_calculations.py and test_ratios.py."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from retrieval.structured_search import get_comparison_evidence, get_company_evidence
from storage.database import utcnow_iso
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _insert_quarterly(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    quarter: str,
    value: float,
    unit: str = "INR_CRORE",
    statement_type: str = "consolidated",
) -> None:
    """Directly populate a period_type='quarterly' canonical_financials row —
    same pattern as tests/test_charts.py's _insert_canonical, but for the
    quarterly series retrieval/structured_search.py now also reads."""
    conn.execute(
        """
        INSERT INTO canonical_financials (
            company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
            canonical_value, unit, chosen_observation_id, reconciliation_reason,
            normalization_version, decided_at
        ) VALUES (?, ?, 'quarterly', ?, ?, ?, ?, ?, NULL, 'test fixture', 'v1', ?)
        """,
        (company_id, metric_key, fiscal_year, quarter, statement_type, value, unit, utcnow_iso()),
    )
    conn.commit()


def test_get_company_evidence_empty_without_data(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert get_company_evidence(db_conn, "HDFCBANK") == []


def test_get_company_evidence_includes_fact_rows_for_reported_metrics(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    net_profit_fy24 = next(e for e in evidence if e.label == "Net Profit FY2024")
    assert net_profit_fy24.kind == "FACT"
    assert net_profit_fy24.value == "20,500.00 INR_CRORE"
    assert "only source available" in net_profit_fy24.citation


def test_get_company_evidence_includes_calculation_rows(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    calc_labels = {e.label for e in evidence if e.kind == "CALCULATION"}
    assert any("YoY growth" in label for label in calc_labels)
    assert any("CAGR" in label for label in calc_labels)
    assert any(label.startswith("ROA") for label in calc_labels)
    assert any(label.startswith("ROE") for label in calc_labels)


def test_get_company_evidence_includes_vendor_reported_facts(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    gnpa = next((e for e in evidence if "gross_npa_percent" in e.label), None)
    assert gnpa is not None
    assert gnpa.kind == "FACT"


def test_get_company_evidence_all_company_ids_match(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    assert evidence  # sanity: fixture actually produced evidence
    assert all(e.company_id == "HDFCBANK" for e in evidence)


def test_get_comparison_evidence_combines_multiple_companies(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_comparison_evidence(ingested_conn, ["HDFCBANK", "ICICIBANK"])
    company_ids = {e.company_id for e in evidence}
    # ICICIBANK is registered (seed_companies) but has no ingested data —
    # contributes nothing, doesn't error the whole comparison.
    assert company_ids == {"HDFCBANK"}


def test_get_comparison_evidence_unregistered_company_contributes_nothing(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_comparison_evidence(ingested_conn, ["HDFCBANK", "NOPE"])
    assert all(e.company_id == "HDFCBANK" for e in evidence)


def test_get_company_evidence_includes_quarterly_fact_rows(ingested_conn: sqlite3.Connection) -> None:
    """The Screener fixture workbook (tests/test_screener_adapter.py) already ingests a
    quarterly net_profit series (Q1-Q4 FY2024) alongside the annual one — get_company_evidence
    must surface it, not just the annual rows, for 'last N quarters' questions to work."""
    evidence = get_company_evidence(ingested_conn, "HDFCBANK")
    q1 = next(e for e in evidence if e.label == "Net Profit Q1 FY2024")
    assert q1.kind == "FACT"
    assert q1.value == "4,200.00 INR_CRORE"
    q4 = next(e for e in evidence if e.label == "Net Profit Q4 FY2024")
    assert q4.value == "5,000.00 INR_CRORE"


def test_get_company_evidence_quarterly_metric_with_partial_coverage_does_not_error(
    ingested_conn: sqlite3.Connection,
) -> None:
    """Balance-sheet metrics are typically only filed half-yearly (Q2/Q4), unlike
    income-statement metrics filed every quarter -- a metric with fewer quarterly rows
    than another must just emit however many it has, not error or get padded."""
    _insert_quarterly(ingested_conn, "HDFCBANK", "total_assets", "FY2025", "Q2", 2_650_000.0)
    _insert_quarterly(ingested_conn, "HDFCBANK", "total_assets", "FY2025", "Q4", 2_800_000.0)

    evidence = get_company_evidence(ingested_conn, "HDFCBANK")  # must not raise

    labels = {e.label for e in evidence}
    assert "Total Assets Q2 FY2025" in labels
    assert "Total Assets Q4 FY2025" in labels
    assert "Total Assets Q1 FY2025" not in labels
    assert "Total Assets Q3 FY2025" not in labels


def test_get_company_evidence_no_quarterly_data_behaves_as_before(db_conn: sqlite3.Connection) -> None:
    """A company with only annual canonical data (no quarterly rows at all) must see
    no regression: no quarterly FACT lines, and the annual evidence is unaffected."""
    seed_companies(db_conn)
    for fiscal_year, value in (("FY2023", 17_000.0), ("FY2024", 20_500.0)):
        db_conn.execute(
            """
            INSERT INTO canonical_financials (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                canonical_value, unit, chosen_observation_id, reconciliation_reason,
                normalization_version, decided_at
            ) VALUES ('HDFCBANK', 'net_profit', 'annual', ?, NULL, 'consolidated', ?, 'INR_CRORE', NULL, 'test fixture', 'v1', ?)
            """,
            (fiscal_year, value, utcnow_iso()),
        )
    db_conn.commit()

    evidence = get_company_evidence(db_conn, "HDFCBANK")

    assert any(e.label == "Net Profit FY2024" for e in evidence)
    assert not any(re.search(r"\bQ[1-4]\b", e.label) for e in evidence)


def test_get_company_evidence_as_of_truncates_quarterly_series(db_conn: sqlite3.Connection) -> None:
    """as_of must exclude quarters that hadn't ended yet as of the cutoff, the same way
    it already excludes future annual rows -- research/temporal.py's fiscal_year_visible
    resolves each quarter's own end date against the company's fiscal year end."""
    seed_companies(db_conn)
    for quarter, month_end in (("Q1", "2023-06-30"), ("Q2", "2023-09-30"), ("Q3", "2023-12-31"), ("Q4", "2024-03-31")):
        _insert_quarterly(db_conn, "HDFCBANK", "net_profit", "FY2024", quarter, 1000.0)

    evidence = get_company_evidence(db_conn, "HDFCBANK", as_of="2023-10-01")

    labels = {e.label for e in evidence}
    assert "Net Profit Q1 FY2024" in labels
    assert "Net Profit Q2 FY2024" in labels
    assert "Net Profit Q3 FY2024" not in labels
    assert "Net Profit Q4 FY2024" not in labels


def test_get_company_evidence_skips_ratio_for_zero_denominator_year(ingested_conn: sqlite3.Connection) -> None:
    """A genuinely-reported total_assets of 0.0 (not missing data — real ingested source
    data some companies have for early years, e.g. ICICIBANK FY2004-FY2013) makes ROA's
    average-assets denominator <=0. roa()/roe() (financials/ratios.py) raise ValueError
    for that, not MissingDataError — get_company_evidence must catch it too and just skip
    that year's ratio, the same as it already does for MissingDataError, rather than
    letting the whole evidence-gathering call blow up."""
    ingested_conn.execute(
        "UPDATE canonical_financials SET canonical_value = 0.0 "
        "WHERE company_id = 'HDFCBANK' AND metric_key = 'total_assets' AND fiscal_year IN ('FY2023', 'FY2024')"
    )
    ingested_conn.commit()

    evidence = get_company_evidence(ingested_conn, "HDFCBANK")  # must not raise

    roa_labels = [e.label for e in evidence if e.label.startswith("ROA (FY2024)")]
    assert roa_labels == []  # FY2024's prior-year (FY2023) assets are the degenerate 0.0
