"""ingestion.pipeline.ingest_nse_pdf_observations() — the "check what's
already in canonical_financials before writing" rule the production task
calls for: XBRL keeps trust-rank priority regardless of period_type, so a
PDF-extracted fact for a (metric, period) canonical_financials already has
a value for must be skipped, never overwritten. Also verifies the
provenance shape (source="nse", parser_version starting "nse_pdf",
source_document_id set) actually lands in financial_observations, and that
web/charts_feed.py's _classify_provenance() reads it back as "nse_pdf".
"""

from __future__ import annotations

import sqlite3

from companies.registry import register_company
from ingestion.pipeline import ingest_nse_pdf_observations
from sources.base import NormalizedObservation
from storage.repositories import get_canonical_value, insert_financial_observations, reconcile, save_company_document
from web.charts_feed import _classify_provenance


def _pdf_obs(**overrides) -> NormalizedObservation:
    defaults = dict(
        company_id="HDFCBANK", metric_key="deposits", period_type="quarterly", fiscal_year="FY2017",
        quarter="Q1", statement_type="standalone", value=36787.7, unit="INR_CRORE",
        source="nse", source_file="x.pdf", parser_version="nse_pdf_v1",
        retrieved_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return NormalizedObservation(**defaults)


def test_writes_a_new_fact_when_nothing_canonical_exists_yet(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")

    result = ingest_nse_pdf_observations(db_conn, "HDFCBANK", [_pdf_obs()], source_file="x.pdf")

    assert result.inserted_count == 1
    assert result.skipped_count == 0
    canonical = get_canonical_value(db_conn, "HDFCBANK", "deposits", "quarterly", "FY2017", quarter="Q1", statement_type="standalone")
    assert canonical is not None
    assert canonical["canonical_value"] == 36787.7


def test_skips_when_canonical_already_has_a_value_for_that_exact_key(db_conn: sqlite3.Connection) -> None:
    """The core rule: an XBRL-sourced value already on file for this exact
    (metric, period, statement_type) must not be replaced by a PDF-derived
    one, even though both are stamped source="nse"."""
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")
    xbrl_obs = NormalizedObservation(
        company_id="HDFCBANK", metric_key="deposits", period_type="quarterly", fiscal_year="FY2017",
        quarter="Q1", statement_type="standalone", value=99999.0, unit="INR_CRORE",
        source="nse", source_file="real.xml", parser_version="nse-xbrl-v1",
        retrieved_at="2025-01-01T00:00:00+00:00",
    )
    insert_financial_observations(db_conn, [xbrl_obs])
    reconcile(db_conn, "HDFCBANK", "deposits", "quarterly", "FY2017", "Q1", "standalone")

    result = ingest_nse_pdf_observations(db_conn, "HDFCBANK", [_pdf_obs(value=1234.5)], source_file="x.pdf")

    assert result.inserted_count == 0
    assert result.skipped_count == 1
    canonical = get_canonical_value(db_conn, "HDFCBANK", "deposits", "quarterly", "FY2017", quarter="Q1", statement_type="standalone")
    assert canonical["canonical_value"] == 99999.0  # untouched


def test_different_metric_in_same_period_is_still_written(db_conn: sqlite3.Connection) -> None:
    """The skip is scoped to the exact (metric, period, statement_type)
    key, not the whole period — a metric XBRL/an earlier PDF pass didn't
    cover for this same quarter must still get written."""
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")
    xbrl_obs = NormalizedObservation(
        company_id="HDFCBANK", metric_key="net_profit", period_type="quarterly", fiscal_year="FY2017",
        quarter="Q1", statement_type="standalone", value=500.0, unit="INR_CRORE",
        source="nse", source_file="real.xml", parser_version="nse-xbrl-v1",
        retrieved_at="2025-01-01T00:00:00+00:00",
    )
    insert_financial_observations(db_conn, [xbrl_obs])
    reconcile(db_conn, "HDFCBANK", "net_profit", "quarterly", "FY2017", "Q1", "standalone")

    result = ingest_nse_pdf_observations(db_conn, "HDFCBANK", [_pdf_obs(metric_key="deposits")], source_file="x.pdf")

    assert result.inserted_count == 1
    canonical = get_canonical_value(db_conn, "HDFCBANK", "deposits", "quarterly", "FY2017", quarter="Q1", statement_type="standalone")
    assert canonical is not None


def test_source_document_id_and_parser_version_round_trip_for_provenance(db_conn: sqlite3.Connection) -> None:
    """web/charts_feed.py's _classify_provenance() must read a PDF-sourced
    fact back as "nse_pdf" — the exact provenance shape the production
    task specifies (source="nse", parser_version starting "nse_pdf")."""
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")
    doc = save_company_document(
        db_conn, "HDFCBANK", document_type="financial_result", fiscal_year="FY2017", quarter="Q1",
        added_by_user=None, storage_object_key="data/documents/HDFCBANK/nse_pdf/x.pdf",
    )

    ingest_nse_pdf_observations(db_conn, "HDFCBANK", [_pdf_obs(source_document_id=doc["document_id"])], source_file="x.pdf")

    row = db_conn.execute(
        "SELECT source, parser_version, source_document_id FROM financial_observations WHERE company_id = 'HDFCBANK'"
    ).fetchone()
    assert row["source"] == "nse"
    assert row["parser_version"] == "nse_pdf_v1"
    assert row["source_document_id"] == doc["document_id"]
    assert _classify_provenance(row["source"], row["parser_version"]) == "nse_pdf"


def test_invalid_observation_is_skipped_not_raised(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")
    bad_obs = _pdf_obs(value=float("nan"))

    result = ingest_nse_pdf_observations(db_conn, "HDFCBANK", [bad_obs], source_file="x.pdf")

    assert result.inserted_count == 0
    assert result.skipped_count == 1
