"""sources/rbi_national_accounts.py — parsing RBI's quarterly GDP (constant
prices) and GVA (current prices) workbooks. Tests build small synthetic
workbooks with the same multi-base-year-sheet shape as the real
downloads."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from sources.rbi_national_accounts import (
    looks_like_rbi_gdp_workbook,
    looks_like_rbi_gva_workbook,
    parse_rbi_gdp_workbook,
    parse_rbi_gva_workbook,
)


def _make_gdp_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    old = wb.create_sheet("NAS - 2011-12")
    old.append([None] * 11)
    old.append([None, "Quarterly Estimates of Gross Domestic Product (At Constant Prices) New Series (Base : 2011-12)"])
    old.append([None, "(Rupees Crores)"])
    old.append([None])
    old.append([None, "Item/ Year", "Quarter", "1. PFCE", "2. GFCE", "3. GFCF", "4. Change in Stock", "5. Valuables", "6. Export of goods & services", "7. Import of goods & services", "8. Discrepancies", "9. Gross Domestic Product"])
    old.append([None, "2011-12   ", "Q1", 100, 20, 60, 5, 6, 40, 45, -1, 185])
    old.append([None, None, "Q4", 110, 25, 65, 6, 7, 42, 47, 2, 211])

    new = wb.create_sheet("NAS - 2022-23")
    new.append([None] * 11)
    new.append([None, "Quarterly Estimates of Gross Domestic Product (At Constant Prices) New Series (Base : 2022-23)"])
    new.append([None, "(Rupees Crores)"])
    new.append([None])
    new.append([None, "Item/ Year", "Quarter", "1. PFCE", "2. GFCE", "3. GFCF", "4. Change in Stock", "5. Valuables", "6. Exports", "7. Imports", "8. Discrepancies", "9. Gross Domestic Product"])
    new.append([None, "2022-23   ", "Q1", 300, 70, 190, 5, 10, 150, 160, -6, 600])

    wb.save(path)


def _make_gva_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("NAS - 2022-23")
    ws.append([None] * 10)
    ws.append([None, "Quarterly Estimates of Gross Value Added At Basic Price (At Current Prices) New Series (Base : 2022-23)"])
    ws.append([None, "(₹Crores)"])
    ws.append([None])
    ws.append([None, "Year/ Industry", "Quarter", "1. Agriculture, Livestock, Forestry and Fishing", "2. Mining & Quarrying", "3. Manufacturing", "Gross Value Added at Basic Prices"])
    ws.append([None, "2022-23   ", "Q1", 1000, 100, 700, 5000])
    wb.save(path)


@pytest.fixture
def gdp_workbook_path(tmp_path: Path) -> Path:
    path = tmp_path / "Quarterly GDP (Constant Prices).xlsx"
    _make_gdp_workbook(path)
    return path


@pytest.fixture
def gva_workbook_path(tmp_path: Path) -> Path:
    path = tmp_path / "Quarterly GVA (Current Prices).xlsx"
    _make_gva_workbook(path)
    return path


def test_looks_like_rbi_gdp_workbook_true_for_matching_shape(gdp_workbook_path: Path) -> None:
    assert looks_like_rbi_gdp_workbook(gdp_workbook_path) is True
    assert looks_like_rbi_gva_workbook(gdp_workbook_path) is False


def test_looks_like_rbi_gva_workbook_true_for_matching_shape(gva_workbook_path: Path) -> None:
    assert looks_like_rbi_gva_workbook(gva_workbook_path) is True
    assert looks_like_rbi_gdp_workbook(gva_workbook_path) is False


def test_gdp_components_are_tagged_by_base_year_not_spliced(gdp_workbook_path: Path) -> None:
    obs = parse_rbi_gdp_workbook(gdp_workbook_path)
    keys = {o.series_key for o in obs}
    assert "gdp_real_base_2011_12" in keys
    assert "gdp_real_base_2022_23" in keys
    assert all(o.unit == "INR_CRORE" for o in obs)
    assert all(o.period_type == "quarterly" for o in obs)


def test_fiscal_year_quarter_maps_to_correct_calendar_quarter_end(gdp_workbook_path: Path) -> None:
    """FY2011-12 Q1 = Apr-Jun 2011 (ends 2011-06-30); Q4 = Jan-Mar 2012
    (ends 2012-03-31, in the fiscal year's SECOND calendar year)."""
    obs = parse_rbi_gdp_workbook(gdp_workbook_path)
    gdp = {o.period: o.value for o in obs if o.series_key == "gdp_real_base_2011_12"}
    assert gdp["2011-06-30"] == 185
    assert gdp["2012-03-31"] == 211


def test_gva_components_normalize_across_header_variants(gva_workbook_path: Path) -> None:
    obs = parse_rbi_gva_workbook(gva_workbook_path)
    keys = {o.series_key: o.value for o in obs}
    assert keys["agriculture_nominal_base_2022_23"] == 1000
    assert keys["mining_quarrying_nominal_base_2022_23"] == 100
    assert keys["gva_basic_prices_nominal_base_2022_23"] == 5000
