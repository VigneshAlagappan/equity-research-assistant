"""sources/nse_pdf_extractor.py, tested two ways:

1. Unit tests against synthetic page-text fixtures (fast, no file I/O),
   covering the label-accumulation/period-column-matching logic directly.
2. Real-data tests against the feasibility spike's own already-downloaded
   sample PDFs (spikes/nse_pdf_feasibility/data/{pdfs,zips}/ — copied into
   this worktree's own gitignored spike data dir, same files the
   feasibility report's Section 7/13.2 quote figures from) — these assert
   the EXACT values the report already verified by hand, so a real
   regression here is caught against ground truth, not just against this
   module's own synthetic fixtures. Skipped (not failed) if that data
   isn't present locally (e.g. a fresh clone that hasn't fetched it).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from sources.nse_pdf_extractor import extract_balance_sheet_from_text, extract_from_pdf

_SPIKE_DATA = Path(__file__).resolve().parent.parent / "spikes" / "nse_pdf_feasibility" / "data"

pytestmark_real_data = pytest.mark.skipif(
    not _SPIKE_DATA.exists(), reason="spike sample PDFs not present locally"
)


# ------------------------------------------------------------------
# Unit tests: synthetic page text, HDFCBANK-style layout (one label per
# line, numbers on the same line) -- text copied verbatim from the
# feasibility report Section 7's own quoted extraction.
# ------------------------------------------------------------------

_HDFCBANK_STYLE_PAGE = """Notes :
1 Statement of Assets and Liabilities as at June 30, 2021 is given below:
( in lac)
As at As at As at
Particulars 30.06.2021 30.06.2020 31.03.2021
Unaudited Unaudited Audited
CAPITAL AND LIABILITIES
Capital 55267 54903 55128
Reserves and Surplus 21193527 17740564 20316953
Deposits 134582934 118938729 133506022
Borrowings 13127502 11638900 13548733
Other Liabilities and Provisions 6434878 6137235 7260216
Total 175394108 154510331 174687052
ASSETS
Cash and Balances with Reserve Bank of India 10462511 9662537 9734073
Balances with Banks and Money at Call and Short notice 1535458 1301793 2212966
Investments 43613164 37935041 44372829
Advances 114765164 100329886 113283663
Fixed Assets 500538 446411 490932
Other Assets 4517273 4834663 4592589
Total 175394108 154510331 174687052
2 The above financial results have been approved by the Board of Directors
"""


def test_hdfcbank_style_extraction_matches_report_figures() -> None:
    """Section 7's own quoted figures, in lakh in the PDF, must come out in
    crore (this app's canonical balance-sheet unit) — 21193527 lac = 211935.27 crore."""
    facts = extract_balance_sheet_from_text(
        [_HDFCBANK_STYLE_PAGE],
        expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    by_metric = {f.metric_key: f.value for f in facts if f.period_type == "quarterly"}
    assert by_metric["equity_share_capital"] == pytest.approx(552.67)
    assert by_metric["reserves"] == pytest.approx(211935.27)
    assert by_metric["deposits"] == pytest.approx(1345829.34)
    assert by_metric["borrowings"] == pytest.approx(131275.02)
    assert by_metric["other_liabilities"] == pytest.approx(64348.78)
    assert by_metric["cash_and_bank"] == pytest.approx(104625.11)
    assert by_metric["investments"] == pytest.approx(436131.64)
    assert by_metric["advances"] == pytest.approx(1147651.64)
    assert by_metric["net_block"] == pytest.approx(5005.38)
    assert by_metric["other_assets"] == pytest.approx(45172.73)
    assert by_metric["total_assets"] == pytest.approx(1753941.08)
    # The liabilities-side "Total" (same value) must NOT also produce a
    # second total_liabilities/duplicate fact -- only one total_assets.
    assert sum(1 for f in facts if f.metric_key == "total_assets" and f.period_type == "quarterly") == 1
    for f in facts:
        assert f.statement_type == "standalone"  # no explicit qualifier in this fixture -- defaults sanely


def test_only_the_matching_quarter_column_is_extracted_not_comparatives() -> None:
    """Three columns (current quarter, year-ago quarter, prior fiscal
    year-end) -- only the column matching expected_quarter_end is kept;
    the other two (comparative periods this app gets from its own
    separately-filed documents) must never appear."""
    facts = extract_balance_sheet_from_text(
        [_HDFCBANK_STYLE_PAGE],
        expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    deposits_values = [f.value for f in facts if f.metric_key == "deposits"]
    assert deposits_values == [pytest.approx(1345829.34)]  # not 118938729/1e5 or 133506022/1e5


def test_consolidated_heading_tagged_correctly() -> None:
    page = "1 Consolidated Statement of Assets and Liabilities as at June 30, 2021\n( in lac)\n" + _HDFCBANK_STYLE_PAGE.split("\n", 2)[2]
    facts = extract_balance_sheet_from_text(
        [page], expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    assert facts
    assert all(f.statement_type == "consolidated" for f in facts)


def test_no_matching_period_column_produces_no_facts() -> None:
    """A filing for a totally different quarter than expected must produce
    nothing -- never guess at a comparative column instead."""
    facts = extract_balance_sheet_from_text(
        [_HDFCBANK_STYLE_PAGE],
        expected_quarter_end=date(2023, 6, 30), expected_fiscal_year="FY2024", expected_quarter="Q1",
    )
    assert facts == []


def test_no_balance_sheet_heading_produces_no_facts() -> None:
    facts = extract_balance_sheet_from_text(
        ["Just some narrative text about the quarter, no tables here."],
        expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    assert facts == []


def test_q4_filing_stamps_balance_sheet_into_both_quarterly_and_annual() -> None:
    """Same convention as sources/nse_xbrl.py's OneI instant context: a Q4
    quarter-end IS the fiscal-year-end, so one balance-sheet snapshot
    serves both period framings."""
    page = _HDFCBANK_STYLE_PAGE.replace("June 30, 2021", "March 31, 2022").replace("30.06.2021", "31.03.2022")
    facts = extract_balance_sheet_from_text(
        [page], expected_quarter_end=date(2022, 3, 31), expected_fiscal_year="FY2022", expected_quarter="Q4",
    )
    quarterly = {f.metric_key: f.value for f in facts if f.period_type == "quarterly"}
    annual = {f.metric_key: f.value for f in facts if f.period_type == "annual"}
    assert quarterly["total_assets"] == annual["total_assets"] == pytest.approx(1753941.08)
    assert annual["deposits"] == pytest.approx(1345829.34)


# ------------------------------------------------------------------
# Real-data tests against the feasibility spike's own downloaded PDFs.
# ------------------------------------------------------------------


@pytestmark_real_data
def test_real_hdfcbank_pdf_extracts_report_section_7_figures() -> None:
    pdf_path = _SPIKE_DATA / "pdfs" / "HDFCBANK" / "~5y_ago_142732.pdf"
    if not pdf_path.exists():
        pytest.skip("HDFCBANK sample PDF not present")
    facts = extract_from_pdf(
        pdf_path, expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    standalone = {f.metric_key: f.value for f in facts if f.statement_type == "standalone" and f.period_type == "quarterly"}
    assert standalone["deposits"] == pytest.approx(1345829.34, rel=1e-3)
    assert standalone["advances"] == pytest.approx(1147651.64, rel=1e-3)
    assert standalone["total_assets"] == pytest.approx(1753941.08, rel=1e-3)
    consolidated = {f.metric_key: f.value for f in facts if f.statement_type == "consolidated" and f.period_type == "quarterly"}
    assert "total_assets" in consolidated  # a second, distinct consolidated section also extracted


@pytestmark_real_data
def test_real_icicibank_pdf_extracts_summary_balance_sheet() -> None:
    """ICICIBANK's own "Summary Balance Sheet" layout (feasibility report
    Section 13.2), already in crore (no lakh rescale needed)."""
    pdf_path = _SPIKE_DATA / "pdfs" / "ICICIBANK" / "~5y_ago_142873.pdf"
    if not pdf_path.exists():
        pytest.skip("ICICIBANK sample PDF not present")
    facts = extract_from_pdf(
        pdf_path, expected_quarter_end=date(2021, 6, 30), expected_fiscal_year="FY2022", expected_quarter="Q1",
    )
    by_metric = {f.metric_key: f.value for f in facts if f.period_type == "quarterly"}
    assert by_metric["deposits"] == pytest.approx(926224, rel=1e-3)
    assert by_metric["advances"] == pytest.approx(738598, rel=1e-3)
    assert by_metric["total_assets"] == pytest.approx(1220654, rel=1e-3)


@pytestmark_real_data
def test_real_icicibank_2016_zip_pdf_extracts_pre_2019_balance_sheet() -> None:
    """The one real pre-2019 case the feasibility report's Section 13.2
    confirmed has a genuine text layer (unzipped from NSE's own ZIP
    attachment) -- values already in crore."""
    pdf_path = _SPIKE_DATA / "zips" / "ICICIBANK" / "extracted" / "BSE_NSE_29072016_f.pdf"
    if not pdf_path.exists():
        pytest.skip("ICICIBANK 2016 sample PDF not present")
    facts = extract_from_pdf(
        pdf_path, expected_quarter_end=date(2016, 6, 30), expected_fiscal_year="FY2017", expected_quarter="Q1",
    )
    by_metric = {f.metric_key: f.value for f in facts if f.period_type == "quarterly"}
    assert by_metric["deposits"] == pytest.approx(424086, rel=1e-3)
    assert by_metric["advances"] == pytest.approx(449427, rel=1e-3)
    assert by_metric["total_assets"] == pytest.approx(727223, rel=1e-3)


@pytestmark_real_data
def test_real_scanned_zip_pdf_produces_no_facts_not_a_crash() -> None:
    """The HDFCBANK 2016 ZIP unwraps to a scanned image with no text layer
    (feasibility report Section 13.2) -- must return [] cleanly, the
    caller's own signal to log this as needs_ocr, never an exception."""
    pdf_path = _SPIKE_DATA / "zips" / "HDFCBANK" / "extracted" / "Result30062016.pdf"
    if not pdf_path.exists():
        pytest.skip("HDFCBANK 2016 sample PDF not present")
    facts = extract_from_pdf(
        pdf_path, expected_quarter_end=date(2016, 6, 30), expected_fiscal_year="FY2017", expected_quarter="Q1",
    )
    assert facts == []
