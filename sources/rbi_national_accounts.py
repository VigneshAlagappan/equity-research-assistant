"""Parses RBI's quarterly National Accounts Statistics workbooks (Handbook
of Statistics tables 2 and 3, distributed as standalone downloads rather
than folded into the "50 Macroeconomic Indicators" workbook):
- "Quarterly Estimates of Gross Domestic Product (At Constant Prices)" --
  Real GDP, expenditure side (PFCE, GFCE, GFCF, exports, imports, ...).
- "Quarterly Estimates of Gross Value Added At Basic Price (At Current
  Prices)" -- the nominal-side counterpart, industry-wise (agriculture,
  manufacturing, ...). Note this is GVA at Basic Prices / GDP at Factor
  Cost, not headline GDP at Market Prices -- RBI's own download does not
  publish a current-price GDP-at-market-prices quarterly series, so this
  is the closest nominal counterpart available, kept under its own
  series-key family rather than mislabeled as "nominal_gdp".

Both workbooks repeat the same series across one sheet per base-year
revision (1999-2000, 2004-05, 2011-12, 2022-23, ...) -- CSO/MoSPI's base
year changes roughly once a decade and each revision is its own
methodology, not a drop-in continuation of the previous one. Every sheet
is ingested as its own series family, tagged by base year in the
series_key (e.g. "gdp_real_base_2022_23"), rather than spliced into one
continuous series -- splicing would require a judgment call about where
to join two different methodologies that RBI itself does not make.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import openpyxl

from sources.macro import MacroNormalizedObservation

logger = logging.getLogger(__name__)

PARSER_VERSION = "rbi-national-accounts-v1-xlsx"

_BASE_YEAR_RE = re.compile(r"Base\s*:\s*(\d{4}-\d{2,4})")
_FISCAL_YEAR_RE = re.compile(r"^(\d{4})-(\d{2,4})")
_LEADING_NUMBER_RE = re.compile(r"^\d+\.\s*")

# Indian fiscal year (Apr-Mar): Q1-Q3 fall in the fiscal year's first
# calendar year, Q4 in its second.
_QUARTER_END = {
    "Q1": (0, 6, 30),
    "Q2": (0, 9, 30),
    "Q3": (0, 12, 31),
    "Q4": (1, 3, 31),
}

# (substring to match in the lowercased, number-stripped header, canonical
# component key) -- order matters, first match wins. Headers vary slightly
# across base-year sheets ("Export of goods & services" vs "Exports") so
# this is a keyword match, not an exact lookup.
_GDP_COMPONENTS = [
    ("pfce", "pfce"),
    ("gfce", "gfce"),
    ("gfcf", "gfcf"),
    ("change in stock", "change_in_stock"),
    ("valuables", "valuables"),
    ("export", "exports"),
    ("import", "imports"),
    ("discrepancies", "discrepancies"),
    ("gross domestic product", "gdp"),
]

_GVA_COMPONENTS = [
    ("agricult", "agriculture"),
    ("mining", "mining_quarrying"),
    ("manufactur", "manufacturing"),
    ("electric", "electricity_gas_water"),
    ("construct", "construction"),
    ("trade", "trade_hotels_transport"),
    ("financ", "finance_real_estate"),
    ("community", "public_admin_other_services"),
    ("public administration", "public_admin_other_services"),
    ("gross value added at basic price", "gva_basic_prices"),
    ("gross domestic product at factor cost", "gdp_factor_cost"),
]


def _component_key(header: str, components: list[tuple[str, str]]) -> str | None:
    cleaned = _LEADING_NUMBER_RE.sub("", header).strip().lower()
    for substring, key in components:
        if substring in cleaned:
            return key
    return None


def _find_base_year(ws) -> str | None:
    for row in ws.iter_rows(min_row=1, max_row=4, values_only=True):
        for value in row:
            if isinstance(value, str):
                match = _BASE_YEAR_RE.search(value)
                if match:
                    return match.group(1)
    return None


def _find_header_row(ws) -> int | None:
    for row in ws.iter_rows(min_row=1, max_row=10):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.strip() == "Quarter":
                return cell.row
    return None


def _quarter_end_period(fiscal_year: str, quarter: str) -> str | None:
    match = _FISCAL_YEAR_RE.match(fiscal_year.strip())
    if not match or quarter not in _QUARTER_END:
        return None
    start_year, end_raw = match.groups()
    end_year = int(end_raw) if len(end_raw) == 4 else int(start_year[:2] + end_raw)
    year_offset, month, day = _QUARTER_END[quarter]
    # Q1-Q3 fall in the fiscal year's start_year, Q4 in its end_year.
    year = int(start_year) if year_offset == 0 else end_year
    return f"{year}-{month:02d}-{day:02d}"


def _parse_workbook(file_path: Path, components: list[tuple[str, str]], series_suffix: str) -> list[MacroNormalizedObservation]:
    wb = openpyxl.load_workbook(file_path, data_only=True)
    observations: list[MacroNormalizedObservation] = []

    for ws in wb.worksheets:
        base_year = _find_base_year(ws)
        header_row = _find_header_row(ws)
        if base_year is None or header_row is None:
            continue
        base_tag = base_year.replace("-", "_")

        columns = [
            (cell.column, _component_key(str(cell.value), components))
            for cell in ws[header_row][2:]  # column D onward (C is "Quarter")
            if cell.value is not None
        ]
        columns = [(col, key) for col, key in columns if key is not None]

        current_fiscal_year: str | None = None
        for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row):
            year_cell = row[1].value  # column C: "Item/Year" or "Year/Industry"
            quarter_cell = row[2].value  # column D: "Quarter"
            if isinstance(year_cell, str) and year_cell.strip():
                current_fiscal_year = year_cell
            if not isinstance(quarter_cell, str) or current_fiscal_year is None:
                continue
            period = _quarter_end_period(current_fiscal_year, quarter_cell.strip())
            if period is None:
                continue
            for col_index, component_key in columns:
                value = row[col_index - 1].value
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                observations.append(MacroNormalizedObservation(
                    series_key=f"{component_key}_{series_suffix}_base_{base_tag}",
                    period_type="quarterly", period=period, value=float(value), unit="INR_CRORE",
                    source="rbi", source_file=str(file_path), parser_version=PARSER_VERSION,
                ))
    return observations


def looks_like_rbi_gdp_workbook(file_path: Path) -> bool:
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True)
    except Exception:
        return False
    for ws in wb.worksheets:
        if _find_header_row(ws) is not None and _find_base_year(ws) is not None:
            title_cells = [c for row in ws.iter_rows(min_row=1, max_row=4, values_only=True) for c in row if isinstance(c, str)]
            if any("gross domestic product" in t.lower() and "constant prices" in t.lower() for t in title_cells):
                return True
    return False


def looks_like_rbi_gva_workbook(file_path: Path) -> bool:
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True)
    except Exception:
        return False
    for ws in wb.worksheets:
        if _find_header_row(ws) is not None and _find_base_year(ws) is not None:
            title_cells = [c for row in ws.iter_rows(min_row=1, max_row=4, values_only=True) for c in row if isinstance(c, str)]
            if any(("gross value added" in t.lower() or "gross domestic product at factor cost" in t.lower()) and "current prices" in t.lower() for t in title_cells):
                return True
    return False


def parse_rbi_gdp_workbook(file_path: Path) -> list[MacroNormalizedObservation]:
    return _parse_workbook(file_path, _GDP_COMPONENTS, "real")


def parse_rbi_gva_workbook(file_path: Path) -> list[MacroNormalizedObservation]:
    return _parse_workbook(file_path, _GVA_COMPONENTS, "nominal")
