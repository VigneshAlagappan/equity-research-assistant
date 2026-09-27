"""storage.repositories.upsert_nse_filing_discovery_log() / list_nse_filing_discovery_log()."""

from __future__ import annotations

import sqlite3

from companies.registry import register_company
from storage.repositories import list_nse_filing_discovery_log, upsert_nse_filing_discovery_log


def test_insert_then_list(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")

    row = upsert_nse_filing_discovery_log(
        db_conn, company_id="HDFCBANK", nse_symbol="HDFCBANK", fiscal_year="FY2022", quarter="Q1",
        period_end="2021-06-30", extraction_status="extracted", filing_date="2021-07-17",
        source_url="https://nsearchives.nseindia.com/x.pdf", document_id="142732",
        match_confidence="text_confirmed", attachment_format="pdf", extracted_char_count=1234,
    )
    assert row["extraction_status"] == "extracted"

    rows = list_nse_filing_discovery_log(db_conn, company_id="HDFCBANK")
    assert len(rows) == 1
    assert rows[0]["document_id"] == "142732"


def test_upsert_updates_existing_row_for_same_natural_key(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")

    upsert_nse_filing_discovery_log(
        db_conn, company_id="HDFCBANK", nse_symbol="HDFCBANK", fiscal_year="FY2022", quarter="Q1",
        period_end="2021-06-30", extraction_status="not_attempted",
    )
    upsert_nse_filing_discovery_log(
        db_conn, company_id="HDFCBANK", nse_symbol="HDFCBANK", fiscal_year="FY2022", quarter="Q1",
        period_end="2021-06-30", extraction_status="extracted", extracted_char_count=5000,
    )

    rows = list_nse_filing_discovery_log(db_conn, company_id="HDFCBANK")
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0]["extraction_status"] == "extracted"
    assert rows[0]["extracted_char_count"] == 5000


def test_filter_by_extraction_status(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank")
    upsert_nse_filing_discovery_log(
        db_conn, company_id="HDFCBANK", nse_symbol="HDFCBANK", fiscal_year="FY2017", quarter="Q1",
        period_end="2016-06-30", extraction_status="needs_ocr",
    )
    upsert_nse_filing_discovery_log(
        db_conn, company_id="HDFCBANK", nse_symbol="HDFCBANK", fiscal_year="FY2022", quarter="Q1",
        period_end="2021-06-30", extraction_status="extracted",
    )

    needs_ocr = list_nse_filing_discovery_log(db_conn, extraction_status="needs_ocr")
    assert len(needs_ocr) == 1
    assert needs_ocr[0]["fiscal_year"] == "FY2017"
