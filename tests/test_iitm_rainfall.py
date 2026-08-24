"""sources/iitm_rainfall.py — fixed-width IITM rainfall text parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sources.iitm_rainfall import (
    UnsupportedIitmFileError,
    parse_iitm_file,
    parse_regional_series_file,
    parse_subdivision_rainfall_file,
)

# Column layout for the 8 "<n>-<code>.txt" files: YEAR(4) then 17 x 7-char
# columns (Jan..Dec, ANN, JF, MAM, JJAS, OND). Built from real header/data
# widths in data/raw/_macro/iitm/*.txt (see module docstring).
_REGIONAL_HEADER = (
    "YEAR      J      F      M      A      M      J     JU      A      S      O      N      D"
    "    ANN     JF    MAM   JJAS    OND"
)
_REGIONAL_ROW = (
    "1844    9.0   12.3   14.2   22.5   44.3  133.1  242.4  246.1  215.8   55.8   16.2    9.7"
    " 1021.4   21.3   81.1  837.3   81.7"
)
_REGIONAL_ROW_WITH_BLANKS = (
    "1845                               65.7  111.5  290.4  352.4   42.9    5.5    6.3   13.3"
    "                       797.2   25.1"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_parse_regional_series_file_reads_title_and_rows(tmp_path: Path) -> None:
    text = (
        "ALL-INDIA MONTHLY, SEASONAL AND ANNUAL RAINFALL SERIES FOR THE PERIOD 1813-2006. "
        "THE RAINFALL FIGURES ARE IN MM.\n"
        f"{_REGIONAL_HEADER}\n{_REGIONAL_ROW}\n"
    )
    file_path = _write(tmp_path / "8-all_ind.txt", text)
    observations = parse_regional_series_file(file_path)

    assert all(o.region is None for o in observations)  # ALL-INDIA -> region None
    assert all(o.source == "iitm" for o in observations)
    assert all(o.period == "1844" and o.period_type == "annual" for o in observations)
    by_key = {o.series_key: o.value for o in observations}
    assert by_key["rainfall_regional_jan"] == 9.0
    assert by_key["rainfall_regional_dec"] == 9.7
    assert by_key["rainfall_regional_annual"] == 1021.4
    assert by_key["rainfall_regional_ond"] == 81.7
    assert len(observations) == 17


def test_parse_regional_series_file_derives_region_from_title(tmp_path: Path) -> None:
    text = (
        "NORTH MOUNTAINOUS INDIA MONTHLY, SEASONAL AND ANNUAL RAINFALL SERIES FOR THE PERIOD "
        "1844-2006. THE RAINFALL FIGURES ARE IN MM.\n"
        f"{_REGIONAL_HEADER}\n{_REGIONAL_ROW}\n"
    )
    file_path = _write(tmp_path / "1-nmi.txt", text)
    observations = parse_regional_series_file(file_path)
    assert all(o.region == "north_mountainous_india" for o in observations)


def test_parse_regional_series_file_skips_blank_fields(tmp_path: Path) -> None:
    text = (
        "NORTH MOUNTAINOUS INDIA MONTHLY, SEASONAL AND ANNUAL RAINFALL SERIES FOR THE PERIOD "
        "1844-2006. THE RAINFALL FIGURES ARE IN MM.\n"
        f"{_REGIONAL_HEADER}\n{_REGIONAL_ROW_WITH_BLANKS}\n"
    )
    file_path = _write(tmp_path / "1-nmi.txt", text)
    observations = parse_regional_series_file(file_path)
    by_key = {o.series_key: o.value for o in observations}
    assert "rainfall_regional_jan" not in by_key  # blank month, skipped
    assert "rainfall_regional_annual" not in by_key  # blank ANN column, skipped
    assert by_key["rainfall_regional_may"] == 65.7
    assert by_key["rainfall_regional_jjas"] == 797.2


def test_parse_regional_series_typo_label_does_not_misalign_columns(tmp_path: Path) -> None:
    """5-wpi.txt's real header spells "MAM" one column narrower than the other
    files, but the data rows use the same fixed width throughout — hardcoded
    boundaries (not header-derived) must still land on the right values."""
    text = (
        "WEST PENINSIULAR INDIA MONTHLY, SEASONAL AND ANNUAL RAINFALL SERIES FOR THE PERIOD "
        "1817-2006. THE RAINFALL FIGURES ARE IN MM.\n"
        "YEAR      J      F      M      A      M      J     JU      A      S      O      N      D"
        "    ANN     JF   MAM    JJAS    OND\n"
        "1826    2.1    2.6    2.9    4.6   61.6  166.8  282.3  130.0  216.3   50.2   39.9    9.7"
        "  969.1    4.8   69.1  795.5   99.8\n"
    )
    file_path = _write(tmp_path / "5-wpi.txt", text)
    observations = parse_regional_series_file(file_path)
    by_key = {o.series_key: o.value for o in observations}
    assert by_key["rainfall_regional_mam"] == 69.1
    assert by_key["rainfall_regional_jjas"] == 795.5
    assert by_key["rainfall_regional_ond"] == 99.8


def test_parse_regional_series_file_rejects_unrecognized_title(tmp_path: Path) -> None:
    file_path = _write(tmp_path / "1-nmi.txt", "not a rainfall title line\n")
    with pytest.raises(UnsupportedIitmFileError):
        parse_regional_series_file(file_path)


def test_parse_subdivision_rainfall_file_multiple_blocks(tmp_path: Path) -> None:
    text = (
        "146  ALL-INDIA  RAINFALL    (1871-2016)          30 SUBDIVISIONS AREA 2880324 SQ.KM.\n"
        " Monthly,Seasonal and Annual rainfall (in 10th of mm) 1871-2016\n"
        "----------------------------------------------------------------------------------\n"
        "YEAR    JAN   FEB   MAR   APR   MAY   JUN   JUL   AUG   SEP   OCT   NOV   DEC    JF"
        "   MAM  JJAS   OND   ANN\n"
        "----------------------------------------------------------------------------------\n"
        "1871    196   107   145   339   636  2080  2778  1794  1836   368   324    67   303"
        "  1120  8487   758 10670\n"
        "  MEAN    99    97   146   271   551  1683  2843  2555  1614   611   259    88   196"
        "   968  9695   957 11734\n"
        "146 NORTHWEST INDIA RAINFALL    (1871-2016)       6 SUBDIVISIONS AREA  634272 SQ.KM.\n"
        "YEAR    JAN   FEB   MAR   APR   MAY   JUN   JUL   AUG   SEP   OCT   NOV   DEC    JF"
        "   MAM  JJAS   OND   ANN\n"
        "1871      1     4     1     2    22   109   169   119    88    12     8     1     5"
        "    25   484    21   536\n"
    )
    file_path = _write(tmp_path / "iitm-regionrf.txt", text)
    observations = parse_subdivision_rainfall_file(file_path)

    all_india = [o for o in observations if o.region is None]
    northwest = [o for o in observations if o.region == "northwest_india"]

    assert {o.series_key for o in all_india} == {
        f"rainfall_subdivision_{s}"
        for s in ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                  "nov", "dec", "jf", "mam", "jjas", "ond", "annual"]
    }
    by_key = {o.series_key: o.value for o in all_india}
    assert by_key["rainfall_subdivision_jan"] == pytest.approx(19.6)  # 196 (10th mm) -> 19.6 mm
    assert by_key["rainfall_subdivision_annual"] == pytest.approx(1067.0)

    by_key_nw = {o.series_key: o.value for o in northwest}
    assert by_key_nw["rainfall_subdivision_annual"] == pytest.approx(53.6)

    # the "MEAN" summary row must not be parsed as a year row
    assert all(o.period == "1871" for o in observations)


def test_parse_iitm_file_dispatches_and_rejects_unsupported(tmp_path: Path) -> None:
    unsupported = _write(tmp_path / "iitm-imr-stn.txt", "District Sub-Division WMO No\n")
    with pytest.raises(UnsupportedIitmFileError):
        parse_iitm_file(unsupported)

    unsupported2 = _write(tmp_path / "NEW-TNREGION.TXT", "107 ALLIN: All-INDIA\n")
    with pytest.raises(UnsupportedIitmFileError):
        parse_iitm_file(unsupported2)
