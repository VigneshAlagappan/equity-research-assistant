"""Discovery + download of NSE Annual Report PDFs -- a STORAGE-ONLY
companion to sources/nse_xbrl.py (facts) and sources/nse_filing_documents.py
(quarterly-result/presentation/transcript filings). This module never
parses a document's contents for facts; it only lists a company's official
annual reports and fetches the bytes, one PDF per fiscal year.

Built on the real, live-verified `/api/annual-reports` endpoint --
dramatically simpler than sources/nse_filing_documents.py's own
`/api/corporate-announcements` classification problem: this endpoint is
NSE's OWN dedicated annual-reports listing, one row per fiscal year, with
no desc-category ambiguity and no false-positive risk at all. Verified live
against HDFCBANK (18 rows, fromYr/toYr 2009-2010 through 2025-2026) and
AAVAS (9 rows, from its actual 2018-2019 NSE listing year onward -- normal,
expected variation by listing date, not a bug).

Two real wrinkles this module handles that a naive "one row per
fromYr/toYr" reading would miss, both confirmed against real HDFCBANK data:

1. A revision. NSE sometimes files a `submission_type="Revised"` row for a
   fiscal year some time after the original `submission_type="New"` row --
   verified live: HDFCBANK's 2024-2025 fiscal year has both, the Revised
   row's own disseminationDateTime later than the New row's. Only one PDF
   per (fromYr, toYr) is wanted here (this module's whole point is "one
   usable annual report per fiscal year"), so rows are grouped by
   (fromYr, toYr) and only the row with the latest disseminationDateTime
   within each group is kept -- the same "group + keep the latest" shape
   sources/nse_fetch.py's _integrated_rows_to_refs() already uses for its
   own analogous revision problem (Original vs Revision seq_Id), though
   the field compared here is disseminationDateTime (a real timestamp
   string, "DD-MON-YYYY HH:MM:SS"), not a numeric seq_Id, since that's
   what this endpoint actually reports as the ordering signal. A row
   carrying "-" for disseminationDateTime (verified live: older ZIP-era
   rows have no useful timestamp fields at all) sorts before any row with
   a real timestamp, since "-" never indicates a later revision of
   anything -- there is only ever one row per fiscal year that old anyway.

2. A ZIP wrapping the real PDF. Verified live: HDFCBANK's fileName is a
   direct .pdf for every fiscal year from 2023-2024 onward, and a .zip for
   2022-2023 and earlier, all the way back to 2009-2010. A real ZIP
   contains 1-3 files -- the main annual report, plus sometimes
   `FormA_*.pdf` (a short compliance form) and/or `BRR_SR_*.pdf` (Business
   Responsibility Report) -- verified live against HDFCBANK's 2015-2016
   ZIP: `AR_2015_2016.pdf` (5.96MB), `FormA_2015_2016.pdf` (55KB),
   `BRR_SR_2015_2016.pdf` (1.5MB). extract_annual_report_pdf() picks the
   one PDF that doesn't match either companion-file prefix, or -- if that
   leaves zero or more than one candidate, an unforeseen naming variant --
   falls back to the single largest PDF in the archive, per this task's
   own "just take the largest PDF if ambiguous" instruction.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime

import requests

from sources.nse_fetch import _BASE, _get_with_retries, _new_session

_ANNUAL_REPORTS_API_PATH = "/api/annual-reports"

#: Companion-file prefixes a real annual-report ZIP can also contain
#: (case-insensitive) -- never the main report itself. Verified live
#: against HDFCBANK's own ZIPs (see module docstring).
_COMPANION_FILE_PREFIXES = ("forma_", "brr_sr_")

_DISSEMINATION_DT_FORMAT = "%d-%b-%Y %H:%M:%S"


class AnnualReportZipError(ValueError):
    """Raised by extract_annual_report_pdf() when a downloaded ZIP's
    contents don't match the expected shape at all (no PDF inside) --
    callers should treat this the same as a download failure (skip, log,
    move on), never guess at non-PDF content."""


@dataclass(frozen=True)
class AnnualReportRef:
    """One fiscal year's annual report, already deduplicated across
    New/Revised submissions -- everything
    scripts/backfill_nse_annual_reports.py needs to download and catalog
    it. `raw` is kept for anything not modeled explicitly, same convention
    sources/nse_filing_documents.py's ClassifiedFiling follows."""

    symbol: str
    from_yr: str
    to_yr: str
    file_url: str
    is_zip: bool
    submission_type: str
    dissemination_dttm: str | None
    raw: dict


def annual_reports_url(symbol: str) -> str:
    """Public wrapper of the annual-reports endpoint, for recording as the
    raw object's source_url -- same role sources/nse_filing_documents.py's
    announcements_url() plays for its own endpoint."""
    return f"{_BASE}{_ANNUAL_REPORTS_API_PATH}?index=equities&symbol={symbol}"


def fiscal_year_label(to_yr: str) -> str:
    """"FY{toYr}" -- matches this app's own "FYyyyy" fiscal-year labeling
    convention (normalization/periods.py: fiscal_year_number() parses
    exactly this shape). NSE's own fromYr/toYr pair (e.g. "2024"/"2025" for
    the fiscal year running Apr 2024-Mar 2025) names its fiscal year after
    the closing calendar year, same as this app's own Apr-Mar convention --
    so "FY{toYr}" lines up directly, no offset needed."""
    return f"FY{to_yr}"


def has_downloadable_file(row: dict) -> bool:
    """True only for a row carrying a real PDF or ZIP file -- mirrors
    sources/nse_filing_documents.py's has_downloadable_attachment() for
    this endpoint's own `fileName` field."""
    file_name = row.get("fileName")
    if not file_name or file_name == "-":
        return False
    lowered = file_name.lower()
    return lowered.endswith(".pdf") or lowered.endswith(".zip")


def _parse_dissemination_dttm(row: dict) -> datetime | None:
    """None for NSE's own "-" placeholder (verified live: every pre-2019
    ZIP-era row) or an unparseable value -- never raises. Used only to pick
    the latest of several rows for the same fiscal year (_dedupe_by_year),
    where "no real timestamp" already implies "no revision to compare
    against" (see module docstring point 1)."""
    raw = row.get("disseminationDateTime")
    if not raw or raw == "-":
        return None
    try:
        return datetime.strptime(raw, _DISSEMINATION_DT_FORMAT)
    except ValueError:
        return None


def _dedupe_by_year(rows: list[dict]) -> list[dict]:
    """One row per (fromYr, toYr) -- the row with the latest
    disseminationDateTime wins (a None/unparseable timestamp is treated as
    earliest-possible, per _parse_dissemination_dttm's docstring); the
    first-seen row wins a tie or an all-None group, preserving the API's
    own response order. See module docstring point 1 for the real
    HDFCBANK New/Revised example this handles."""
    best: dict[tuple[str, str], dict] = {}
    best_dt: dict[tuple[str, str], datetime | None] = {}
    for row in rows:
        key = (row.get("fromYr", ""), row.get("toYr", ""))
        dt = _parse_dissemination_dttm(row)
        if key not in best:
            best[key] = row
            best_dt[key] = dt
            continue
        existing_dt = best_dt[key]
        if dt is not None and (existing_dt is None or dt > existing_dt):
            best[key] = row
            best_dt[key] = dt
    return list(best.values())


def filter_from_2015(rows: list[dict]) -> list[dict]:
    """Only fiscal years whose toYr is 2015 or later -- this task's
    deliberate "up to 2015 for now" scope boundary (not a technical
    limit: older years ARE available for e.g. HDFCBANK, they're just not
    pulled this pass). Plain string comparison is safe here: NSE's toYr is
    always a bare 4-digit year string, so lexicographic and numeric
    ordering agree."""
    return [row for row in rows if row.get("toYr", "") >= "2015"]


def parse_annual_reports_json(raw_bytes: bytes) -> list[dict]:
    import json

    return json.loads(raw_bytes).get("data", [])


def fetch_annual_reports_raw(symbol: str, *, session: requests.Session | None = None) -> bytes:
    """Just the network fetch -- full annual-report history in one
    response, no pagination. Split from parse_annual_reports_json() so a
    caller that already has the raw bytes can re-derive refs without a
    network call, same convention as
    sources/nse_filing_documents.py's fetch_announcements_raw()."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_ANNUAL_REPORTS_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    finally:
        if owns_session:
            session.close()
    return response.content


def rows_to_refs(rows: list[dict], symbol: str) -> list[AnnualReportRef]:
    """Full pipeline over already-fetched rows: filter to downloadable
    rows, dedupe New/Revised submissions per fiscal year, restrict to
    toYr >= 2015, and translate into AnnualReportRef -- pure, no network
    call, so tests exercise this directly against real captured row
    shapes."""
    downloadable = [row for row in rows if has_downloadable_file(row)]
    deduped = _dedupe_by_year(downloadable)
    scoped = filter_from_2015(deduped)
    refs: list[AnnualReportRef] = []
    for row in scoped:
        file_url = row["fileName"]
        dttm = row.get("disseminationDateTime")
        refs.append(
            AnnualReportRef(
                symbol=symbol,
                from_yr=row.get("fromYr", ""),
                to_yr=row.get("toYr", ""),
                file_url=file_url,
                is_zip=file_url.lower().endswith(".zip"),
                submission_type=row.get("submission_type") or "",
                dissemination_dttm=dttm if dttm and dttm != "-" else None,
                raw=row,
            )
        )
    # Newest fiscal year first -- matches the API's own natural response
    # order (verified live) and gives a human-reviewable dry-run log a
    # sensible order; not load-bearing for correctness.
    refs.sort(key=lambda ref: ref.to_yr, reverse=True)
    return refs


def discover_company_annual_reports(
    symbol: str, *, session: requests.Session | None = None,
) -> list[AnnualReportRef]:
    """One network call (fetch_annual_reports_raw) + pure translation --
    the composed convenience function
    scripts/backfill_nse_annual_reports.py calls per company."""
    raw_bytes = fetch_annual_reports_raw(symbol, session=session)
    rows = parse_annual_reports_json(raw_bytes)
    return rows_to_refs(rows, symbol)


def download_file(session: requests.Session, url: str) -> bytes:
    """Download one annual report's file (a PDF or ZIP). Raises
    NSEFetchError (via _get_with_retries) on exhausted retries/blocking --
    same "let the caller's per-company error handling decide what to do"
    convention as sources/nse_filing_documents.py's download_document()."""
    response = _get_with_retries(session, url)
    return response.content


def extract_annual_report_pdf(zip_bytes: bytes) -> bytes:
    """Given a downloaded ZIP's raw bytes, return the bytes of the single
    PDF that's the actual annual report -- never the companion FormA_/
    BRR_SR_ files (module docstring point 2). Picks the one PDF whose
    filename doesn't start with either companion prefix; if that leaves
    zero or more than one candidate (an unforeseen naming variant this
    module hasn't seen live), falls back to the largest PDF in the
    archive, per this task's own "take the largest if ambiguous"
    instruction. Raises AnnualReportZipError if the archive contains no
    PDF at all, OR if `zip_bytes` isn't a valid ZIP archive at all --
    verified live (real full-Nifty-500 run): NSE occasionally serves a
    corrupted/truncated response for a file whose URL is completely valid
    (a re-fetch of the identical URL minutes later succeeded cleanly), and
    Python's own zipfile.BadZipFile is a totally different exception
    class from this module's own AnnualReportZipError -- left
    uncaught here, it escaped scripts/backfill_nse_annual_reports.py's
    `except AnnualReportZipError` entirely, which (via
    ingestion/batch_log.py's BatchRun.item(), which swallows ANY
    exception from a company's own processing without printing it)
    silently aborted that entire company's remaining fiscal years with
    no visible error at all -- only a "File is not a zip file" string
    buried in the batch_job_items DB table's own `detail` column. Treating
    a bad archive the same as a valid-but-PDF-less one fixes that: it's
    now just one more counted, logged, skippable per-year failure."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            pdf_infos = [info for info in zf.infolist() if info.filename.lower().endswith(".pdf")]
            if not pdf_infos:
                raise AnnualReportZipError("ZIP archive contains no PDF file")

            candidates = [
                info for info in pdf_infos
                if not info.filename.lower().rsplit("/", 1)[-1].startswith(_COMPANION_FILE_PREFIXES)
            ]
            if len(candidates) != 1:
                # Zero (every PDF matched a companion prefix -- shouldn't
                # happen live, but not impossible) or more than one (a
                # naming variant this module hasn't seen) -- take the
                # largest PDF in the archive either way, per this task's
                # explicit fallback instruction.
                chosen = max(pdf_infos, key=lambda info: info.file_size)
            else:
                chosen = candidates[0]
            return zf.read(chosen)
    except zipfile.BadZipFile as exc:
        # Can be raised by ZipFile(...) itself (bad header) or by
        # infolist()/read() (a truncated body behind an otherwise-valid
        # header) -- both are the same "unusable archive" condition from
        # this function's caller's point of view, so both are folded into
        # one exception type here.
        raise AnnualReportZipError(f"not a valid/complete ZIP archive: {exc}") from exc
