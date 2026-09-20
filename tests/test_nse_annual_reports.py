"""sources/nse_annual_reports.py -- pure discovery/dedup/extraction logic
only (no real network calls). Row shapes and ZIP contents below are taken
verbatim from real NSE `/api/annual-reports?index=equities&symbol=HDFCBANK`
data fetched live while building this module (18 real rows, fromYr/toYr
2009-2010 through 2025-2026; the 2015-2016 ZIP genuinely contains
AR_2015_2016.pdf/FormA_2015_2016.pdf/BRR_SR_2015_2016.pdf at real sizes)."""

from __future__ import annotations

import io
import zipfile

import pytest

from sources.nse_annual_reports import (
    AnnualReportZipError,
    extract_annual_report_pdf,
    fiscal_year_label,
    filter_from_2015,
    has_downloadable_file,
    rows_to_refs,
)

# Real HDFCBANK rows (trimmed to the fields this module reads), newest first.
_HDFCBANK_ROWS = [
    {
        "fromYr": "2025", "toYr": "2026", "submission_type": "New",
        "disseminationDateTime": "11-JUL-2026 00:10:56",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_29735_HDFCBANK_2025_2026_A_12667766_11072026001055.pdf",
    },
    {
        "fromYr": "2024", "toYr": "2025", "submission_type": "Revised",
        "disseminationDateTime": "25-JUL-2025 22:00:57",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_27115_HDFCBANK_2024_2025_U_25072025220054.pdf",
    },
    {
        "fromYr": "2024", "toYr": "2025", "submission_type": "New",
        "disseminationDateTime": "14-JUL-2025 07:56:28",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_26889_HDFCBANK_2024_2025_A_14072025075626.pdf",
    },
    {
        "fromYr": "2015", "toYr": "2016", "submission_type": "-",
        "disseminationDateTime": "-",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_9108_HDFCBANK_2015_2016_26072016101219.zip",
    },
    {
        "fromYr": "2014", "toYr": "2015", "submission_type": "-",
        "disseminationDateTime": "-",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_6180_HDFCBANK_2014_2015_29062015190351.zip",
    },
    {
        "fromYr": "2013", "toYr": "2014", "submission_type": "-",
        "disseminationDateTime": "-",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_3393_HDFCBANK_2013_2014_12062014114216.zip",
    },
    # Pre-2015 -- must be filtered out by filter_from_2015 / the toYr>=2015 scope.
    {
        "fromYr": "2009", "toYr": "2010", "submission_type": "-",
        "disseminationDateTime": "-",
        "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_HDFCBANK_2009_2010_18082010121500.zip",
    },
]


def test_has_downloadable_file_accepts_pdf_and_zip() -> None:
    assert has_downloadable_file({"fileName": "https://x/y.pdf"}) is True
    assert has_downloadable_file({"fileName": "https://x/y.zip"}) is True


def test_has_downloadable_file_rejects_placeholder() -> None:
    assert has_downloadable_file({"fileName": "-"}) is False
    assert has_downloadable_file({}) is False


def test_fiscal_year_label_matches_app_convention() -> None:
    """"FY{toYr}" -- normalization/periods.py's own "FYyyyy" shape."""
    assert fiscal_year_label("2025") == "FY2025"


def test_filter_from_2015_excludes_older_years() -> None:
    rows = [{"toYr": "2014"}, {"toYr": "2015"}, {"toYr": "2026"}]
    assert filter_from_2015(rows) == [{"toYr": "2015"}, {"toYr": "2026"}]


def test_rows_to_refs_deduplicates_revised_submission_by_latest_dissemination() -> None:
    """Real HDFCBANK 2024-2025 fiscal year has both a New (14-Jul-2025) and
    a later Revised (25-Jul-2025) row -- only the Revised one (later
    disseminationDateTime) should survive, exactly one ref for that
    fiscal year."""
    refs = rows_to_refs(_HDFCBANK_ROWS, "HDFCBANK")
    fy2025_refs = [r for r in refs if r.to_yr == "2025"]
    assert len(fy2025_refs) == 1
    assert fy2025_refs[0].submission_type == "Revised"
    assert "27115" in fy2025_refs[0].file_url


def test_rows_to_refs_excludes_years_before_2015() -> None:
    refs = rows_to_refs(_HDFCBANK_ROWS, "HDFCBANK")
    assert all(ref.to_yr >= "2015" for ref in refs)
    assert not any(ref.to_yr == "2010" for ref in refs)


def test_rows_to_refs_flags_zip_vs_pdf_correctly() -> None:
    refs = rows_to_refs(_HDFCBANK_ROWS, "HDFCBANK")
    by_year = {ref.to_yr: ref for ref in refs}
    assert by_year["2026"].is_zip is False
    assert by_year["2016"].is_zip is True


def test_rows_to_refs_sets_symbol_and_period_fields() -> None:
    refs = rows_to_refs(_HDFCBANK_ROWS, "HDFCBANK")
    ref = next(r for r in refs if r.to_yr == "2026")
    assert ref.symbol == "HDFCBANK"
    assert ref.from_yr == "2025"
    assert fiscal_year_label(ref.to_yr) == "FY2026"


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extract_annual_report_pdf_picks_the_non_companion_file() -> None:
    """Real HDFCBANK 2015-2016 ZIP shape: AR_2015_2016.pdf (main report),
    FormA_2015_2016.pdf, BRR_SR_2015_2016.pdf -- only the main report's
    bytes should come back."""
    zip_bytes = _make_zip({
        "AR_2015_2016.pdf": b"main report bytes" * 100,
        "FormA_2015_2016.pdf": b"form a",
        "BRR_SR_2015_2016.pdf": b"brr sr",
    })
    extracted = extract_annual_report_pdf(zip_bytes)
    assert extracted == b"main report bytes" * 100


def test_extract_annual_report_pdf_falls_back_to_largest_when_ambiguous() -> None:
    """No file avoids both companion prefixes -- an unforeseen naming
    variant -- so the largest PDF wins, per this task's explicit
    fallback rule."""
    zip_bytes = _make_zip({
        "FormA_report.pdf": b"small",
        "BRR_SR_report.pdf": b"much much bigger than the other one here",
    })
    extracted = extract_annual_report_pdf(zip_bytes)
    assert extracted == b"much much bigger than the other one here"


def test_extract_annual_report_pdf_raises_when_no_pdf_present() -> None:
    zip_bytes = _make_zip({"readme.txt": b"no pdf here"})
    with pytest.raises(AnnualReportZipError):
        extract_annual_report_pdf(zip_bytes)


def test_extract_annual_report_pdf_single_pdf_no_companions() -> None:
    """The common recent-year shape once ZIPs stop appearing isn't relevant
    here (those are plain .pdf fileNames, no ZIP at all) -- but an older
    ZIP with just the one PDF and no companion files at all should also
    work cleanly."""
    zip_bytes = _make_zip({"AR_2013_2014.pdf": b"only file"})
    assert extract_annual_report_pdf(zip_bytes) == b"only file"
