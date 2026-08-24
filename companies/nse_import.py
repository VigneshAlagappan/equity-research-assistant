"""Bulk-import the NSE company master list into `companies` +
`company_index_membership`.

Source is an "NSE_Companies_..._Metadata" export (see e.g.
data/raw/NSE_Companies_Industry_Metadata_Enhanced_Aug_2026.xlsx and its own
"Notes"/"Classification Summary" sheets for provenance/as-of date/coverage) —
a periodically-refreshed reference list of company name/NSE symbol/BSE code/
NIFTY index membership/NSE's 4-level sector classification, not a
financial-observations source. It goes straight through
register_company()/set_company_index_tags() rather than the ingestion
pipeline (ingestion/pipeline.py + sources/ adapters exist for
financial_observations, which this isn't).

Classification coverage is partial by design (NSE's own "Pending detailed
classification" / "Macro + Sector only" / "4-level classification" rows,
see the Notes sheet) and improves release to release — each of the 4
classification fields is merged independently (file value wins if present,
otherwise whatever's already on the row is kept) rather than overwriting a
fuller, previously-curated value with this file's gap. The older
NSE_Companies_with_BSE_and_Nifty_Metadata export (name/symbol/BSE
code/index membership only, no classification columns) still works through
the same function — those columns are read defensively, not assumed present.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from companies.registry import get_company, register_company
from normalization.companies import InvalidCompanyIdError, normalize_company_id
from storage.repositories import set_company_index_tags

_ID_STRIP_RE = re.compile(r"[^A-Za-z0-9&]")
_LEGAL_SUFFIX_RE = re.compile(r"\s+(Limited|Ltd\.?)$", re.IGNORECASE)

# Workbook column header -> INDEX_NAMES entry (storage/repositories.py).
# Only the per-index Y/blank flag columns are read — "Index Membership" (a
# comma-joined duplicate of the same information as free text) and "Nifty
# Size Bucket" (a convenience bucket derived from these flags, not itself an
# index) carry no information the flag columns don't already have.
_INDEX_COLUMNS = {
    "NIFTY 50": "Nifty 50",
    "NIFTY Next 50": "Nifty Next 50",
    "NIFTY 100": "Nifty 100",
    "NIFTY 200": "Nifty 200",
    "NIFTY 500": "Nifty 500",
    "NIFTY Midcap 50": "Nifty Midcap 50",
    "NIFTY Midcap 100": "Nifty Midcap 100",
    "NIFTY Midcap 150": "Nifty Midcap 150",
    "NIFTY Smallcap 50": "Nifty Smallcap 50",
    "NIFTY Smallcap 100": "Nifty Smallcap 100",
    "NIFTY Smallcap 250": "Nifty Smallcap 250",
}


@dataclass
class NSEImportResult:
    total_rows: int = 0
    registered: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)


def _display_name(legal_name: str) -> str:
    """Strip a trailing "Limited"/"Ltd" the way every hand-curated company in
    this database already does (HDFC Bank Limited -> HDFC Bank, Poonawalla
    Fincorp Limited -> Poonawalla Fincorp, ...) — the one suffix pattern
    consistently stripped across every existing legal_name/display_name
    pair, so it's the only one safe to automate here."""
    return _LEGAL_SUFFIX_RE.sub("", legal_name).strip()


def _cell(row: tuple, col: dict[str, int], header: str) -> str | None:
    """Read one column by header name, or None if this row's file doesn't
    have that column at all (older export) or the cell itself is blank."""
    pos = col.get(header)
    if pos is None:
        return None
    value = row[pos]
    return value.strip() or None if isinstance(value, str) else value


def import_nse_companies(conn: sqlite3.Connection, file_path: Path) -> NSEImportResult:
    """Register every row in the workbook's "Companies" sheet, tag its NIFTY
    index membership, and return counts. Idempotent — safe to re-run against
    a refreshed export; company_id is derived from NSE Symbol (stripped of
    any character company_id doesn't allow, e.g. "BAJAJ-AUTO" ->
    "BAJAJAUTO") so the same company always lands on the same row.

    Fields this file doesn't carry (ISIN, website, sector, industry, listed
    date) are preserved from whatever's already on the row instead of being
    wiped to NULL — same pass-through register_company() callers already do
    elsewhere (see web/app.py's admin_update_company)."""
    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook["Companies"]
    rows = sheet.iter_rows(min_row=1, values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    index_col_positions = {col[h]: index_name for h, index_name in _INDEX_COLUMNS.items() if h in col}

    result = NSEImportResult()
    for row in rows:
        result.total_rows += 1
        legal_name = (row[col["Company Name"]] or "").strip()
        raw_symbol = (row[col["NSE Symbol"]] or "").strip()
        bse_code = (row[col["BSE Code"]] or "").strip() or None

        if not legal_name or not raw_symbol:
            result.skipped.append(f"row {result.total_rows + 1}: missing company name or NSE symbol")
            continue
        try:
            company_id = normalize_company_id(_ID_STRIP_RE.sub("", raw_symbol))
        except InvalidCompanyIdError as exc:
            result.skipped.append(f"{raw_symbol!r}: {exc}")
            continue

        existing = get_company(conn, company_id)

        def _merged(header: str, db_column: str) -> str | None:
            """File value wins if this row has one; otherwise keep whatever
            classification is already on the row (don't let a file with
            partial coverage wipe out a fuller, previously-curated value)."""
            file_value = _cell(row, col, header)
            if file_value:
                return file_value
            return existing[db_column] if existing else None

        register_company(
            conn,
            company_id,
            legal_name,
            _display_name(legal_name),
            nse_symbol=raw_symbol.upper(),
            bse_code=bse_code,
            isin=existing["isin"] if existing else None,
            country=existing["country"] if existing else "IN",
            currency=existing["currency"] if existing else "INR",
            website=existing["website"] if existing else None,
            macro_economic_sector=_merged("Macro Economic Sector", "macro_economic_sector"),
            sector=_merged("Sector", "sector"),
            industry=_merged("Industry", "industry"),
            basic_industry=_merged("Basic Industry", "basic_industry"),
            listed_date=existing["listed_date"] if existing else None,
        )
        if existing is None:
            result.registered += 1
        else:
            result.updated += 1

        tags = [index_name for pos, index_name in index_col_positions.items() if row[pos] == "Y"]
        set_company_index_tags(conn, company_id, tags)

    return result
