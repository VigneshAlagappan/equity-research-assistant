"""MacroDataAdapter — CSV parsing for non-company (RBI/IMD/...) series."""

from __future__ import annotations

from pathlib import Path

import pytest

from sources.macro import MACRO_SOURCE_IDS, MacroDataAdapter, MacroPeriodError, infer_period_type


def test_fred_is_a_registered_macro_source_id() -> None:
    """FRED (sources/fred.py) is the US macro-data source, alongside the
    India ones (rbi/imd/iitm/mospi/irda/mfin) — even though it's live-fetched
    rather than CSV-staged, it validates through the same set."""
    assert "fred" in MACRO_SOURCE_IDS


@pytest.mark.parametrize(
    "period,expected",
    [("2015", "annual"), ("2015-06", "monthly"), ("1999-12", "monthly")],
)
def test_infer_period_type_recognizes_valid_shapes(period: str, expected: str) -> None:
    assert infer_period_type(period) == expected


@pytest.mark.parametrize("bad_period", ["FY2015", "2015-13", "15", "2015-6", "", "not-a-period"])
def test_infer_period_type_rejects_invalid_shapes(bad_period: str) -> None:
    with pytest.raises(MacroPeriodError):
        infer_period_type(bad_period)


def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_parse_infers_series_key_from_filename(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "repo_rate.csv", "period,value,unit\n2015,6.75,PERCENT\n")
    observations = MacroDataAdapter("imd").parse(file_path)
    assert len(observations) == 1
    assert observations[0].series_key == "repo_rate"


def test_parse_series_key_override(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "data.csv", "period,value,unit\n2015,6.75,PERCENT\n")
    observations = MacroDataAdapter("imd").parse(file_path, series_key="repo_rate")
    assert observations[0].series_key == "repo_rate"


def test_parse_annual_and_monthly_rows(tmp_path: Path) -> None:
    file_path = _write_csv(
        tmp_path / "rainfall.csv",
        "period,value,unit\n2015,1108.9,MILLIMETRES\n2015-06,120.5,MILLIMETRES\n",
    )
    observations = MacroDataAdapter("imd").parse(file_path)
    assert [o.period_type for o in observations] == ["annual", "monthly"]
    assert [o.value for o in observations] == [1108.9, 120.5]


def test_parse_region_column_optional(tmp_path: Path) -> None:
    file_path = _write_csv(
        tmp_path / "rainfall.csv",
        "period,value,unit,region\n2015,1108.9,MILLIMETRES,\n2015,950.2,MILLIMETRES,Maharashtra\n",
    )
    observations = MacroDataAdapter("imd").parse(file_path)
    assert observations[0].region is None  # blank region -> all-India, not the empty string
    assert observations[1].region == "Maharashtra"


def test_parse_without_region_column_defaults_to_none(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "rainfall.csv", "period,value,unit\n2015,1108.9,MILLIMETRES\n")
    observations = MacroDataAdapter("imd").parse(file_path)
    assert observations[0].region is None


def test_parse_strips_commas_from_values(tmp_path: Path) -> None:
    """Reuses normalization/units.py's parse_numeric — same comma-stripping
    Screener's own numbers need, since RBI/IMD publish comma-formatted
    numbers too (e.g. "1,108.9")."""
    file_path = _write_csv(tmp_path / "series.csv", 'period,value,unit\n2015,"1,108.9",MILLIMETRES\n')
    observations = MacroDataAdapter("imd").parse(file_path)
    assert observations[0].value == 1108.9


def test_parse_skips_blank_value_row(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "series.csv", "period,value,unit\n2015,,MILLIMETRES\n2016,100,MILLIMETRES\n")
    observations = MacroDataAdapter("imd").parse(file_path)
    assert len(observations) == 1
    assert observations[0].period == "2016"


def test_parse_skips_malformed_value_row(tmp_path: Path) -> None:
    file_path = _write_csv(
        tmp_path / "series.csv", "period,value,unit\n2015,not_a_number,MILLIMETRES\n2016,100,MILLIMETRES\n"
    )
    observations = MacroDataAdapter("imd").parse(file_path)
    assert len(observations) == 1
    assert observations[0].period == "2016"


def test_parse_skips_malformed_period_row(tmp_path: Path) -> None:
    file_path = _write_csv(
        tmp_path / "series.csv", "period,value,unit\nFY2015,6.75,PERCENT\n2016,100,PERCENT\n"
    )
    observations = MacroDataAdapter("imd").parse(file_path)
    assert len(observations) == 1
    assert observations[0].period == "2016"


def test_parse_skips_row_missing_unit(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "series.csv", "period,value,unit\n2015,100,\n2016,100,PERCENT\n")
    observations = MacroDataAdapter("imd").parse(file_path)
    assert len(observations) == 1
    assert observations[0].period == "2016"


def test_parse_requires_period_and_value_columns(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "series.csv", "date,amount\n2015,100\n")
    with pytest.raises(ValueError, match="period"):
        MacroDataAdapter("imd").parse(file_path)


def test_parse_stamps_provenance(tmp_path: Path) -> None:
    file_path = _write_csv(tmp_path / "repo_rate.csv", "period,value,unit\n2015,6.75,PERCENT\n")
    observations = MacroDataAdapter("rbi").parse(file_path)
    obs = observations[0]
    assert obs.source == "rbi"  # the specific provider, not a generic "macro" placeholder
    assert obs.source_file == str(file_path)
    assert obs.parser_version


def test_unknown_source_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_id"):
        MacroDataAdapter("not_a_real_provider")
