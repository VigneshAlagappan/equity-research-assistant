"""Section 14: continuous discovery + classification of every quarterly
financial-results filing for HDFCBANK/RELIANCE/ICICIBANK/TCS from
2015-01-01 to the most recent quarter — not 3 sample points like the
original spike, the full history.

Matching strategy (incorporates the Nifty 50 extension's learnings,
Section 12): rather than trusting text-marker matching alone (proven
unreliable across companies in Section 12.2 — HINDUNILVR/ETERNAL/TRENT all
filed genuine results with `attchmntText` that never says "financial
results"), this uses a DATE-WINDOW match as the primary signal: for each
expected fiscal-quarter end date, look at every `desc="Outcome of Board
Meeting"` (or one of the other recognized result-category) announcement
within 75 days after that quarter-end (SEBI's own disclosure deadline is 45
days for Q1-Q3, 60 for the audited Q4/annual — 75 gives slack) and take the
EARLIEST one in that window (Section 12.4's own recommendation: the primary
filing is reliably first; things that reference the same results — press
releases, presentations, newspaper ads — are reliably filed after it).
`desc="General Updates"` is excluded entirely from the candidate pool (the
Section 12.2 catch-all trap), not just filtered per-row.

Every candidate is ALSO checked against the original text-marker filter, so
the output records whether a match was text-confirmed or date-window-only —
full transparency about which signal found it, not a black box.

Every match is downloaded, unzipped where needed (Section 13's own
finding — pre-2019 zips are usually just a wrapped PDF, occasionally a
scanned one), and run through pypdf, classified extracted / needs_ocr.
Local scratch only — spikes/nse_pdf_feasibility/data/discovery_pdfs/ — never
the real S3 bucket, never production Neon (XBRL cross-check reuses the same
NSE listing endpoints already used elsewhere in this spike; no DB write
anywhere in this script).
"""
from __future__ import annotations

import csv
import json
import logging
import re
import sys
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # repo root, for normalization.periods

import nse_pdf_fetch as npf
from run_spike import _FIN_TEXT_MARKERS, _EXCLUDED_DESC_CATEGORIES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_2015_discovery")

DATA_DIR = Path(__file__).parent / "data"
PDF_DIR = DATA_DIR / "discovery_pdfs"

COMPANIES = ["HDFCBANK", "RELIANCE", "ICICIBANK", "TCS"]
START_DATE = date(2015, 1, 1)
TODAY = date(2026, 9, 17)
WINDOW_DAYS = 75
EXTRACTED_CHAR_THRESHOLD = 1000

# desc categories a candidate result filing can carry, beyond the
# date-window "Outcome of Board Meeting" match — same allowlist as
# run_spike.py's _is_fin_result_row, minus needing the text-marker OR here
# (date-window already does that job for Board Meeting rows).
_RESULT_DESC_ALLOWLIST_SUBSTR = ("Financial Result", "Results Update", "Result Update")
_RESULT_DESC_EXACT = {"Integrated Filing- Financial", "Outcome of Board Meeting"}


def _parse_sort_date(d: dict) -> datetime | None:
    try:
        return datetime.strptime(d["sort_date"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _quarter_end_dates(start: date, end: date) -> list[date]:
    """Every Mar/Jun/Sep/Dec 31(30) quarter-end from just before `start`
    through `end` — generated directly (not from any XBRL/production
    period-calc code), since this needs quarter ends independent of
    whether a filing for them was ever found."""
    ends = []
    year = start.year - 1
    while True:
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qe = date(year, month, day)
            if qe >= start - timedelta(days=100) and qe <= end:
                ends.append(qe)
        year += 1
        if date(year, 3, 31) > end:
            break
    return sorted(set(ends))


def _fiscal_year_quarter(qe: date) -> tuple[str, str]:
    """India fiscal year (Apr-Mar) label + quarter, computed directly here
    (self-contained, not importing normalization/periods.py, to keep this
    script fully standalone from the app's own period logic — an
    independent cross-check is more useful for a discovery/classification
    tool than reusing the same code it would be validated against)."""
    if qe.month in (4, 5, 6):
        fy_end_year, q = qe.year + 1, "Q1"
    elif qe.month in (7, 8, 9):
        fy_end_year, q = qe.year + 1, "Q2"
    elif qe.month in (10, 11, 12):
        fy_end_year, q = qe.year + 1, "Q3"
    else:  # Jan-Mar
        fy_end_year, q = qe.year, "Q4"
    return f"FY{fy_end_year}", q


def _is_text_confirmed(text: str) -> bool:
    tl = (text or "").lower()
    return any(m in tl for m in _FIN_TEXT_MARKERS)


def _attachment_format(url: str | None) -> str:
    if not url or url == "-":
        return "none"
    u = url.lower()
    if u.endswith(".pdf"):
        return "pdf"
    if u.endswith(".zip"):
        return "zip"
    if u.endswith(".html") or u.endswith(".htm"):
        return "html"
    return "other"


def _extract_pdf_text_len(path: Path) -> tuple[int, str | None]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return len(text), None
    except Exception as exc:
        return 0, str(exc)


def _handle_zip(zip_path: Path, extract_dir: Path) -> tuple[str, Path | None]:
    """Returns (inner_content_description, path_to_pdf_if_any)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        return "invalid_zip", None
    pdfs = [n for n in names if n.lower().endswith(".pdf")]
    if pdfs:
        return f"zip_contains_pdf({len(names)}_files)", extract_dir / pdfs[0]
    return f"zip_contains_other({names[:5]})", None


@dataclass
class QuarterRecord:
    company_id: str
    nse_symbol: str
    fiscal_year: str
    quarter: str
    period_end: str
    filing_date: str | None = None
    source_url: str | None = None
    match_confidence: str | None = None  # text_confirmed | date_window_only | none
    attachment_format: str = "none"
    extraction_status: str = "not_found"  # extracted | needs_ocr | not_found | not_attempted
    extracted_char_count: int = 0
    xbrl_available: bool = False
    notes: str = ""


def process_company(symbol: str, do_extraction: bool) -> list[QuarterRecord]:
    session = npf.new_session()
    if session is None:
        logger.warning("%s: bootstrap failed, skipping entirely", symbol)
        return [QuarterRecord(symbol, symbol, "", "", "", notes="bootstrap failed")]

    resp = npf._get(session, "https://www.nseindia.com/api/corporate-announcements",
                     params={"index": "equities", "symbol": symbol})
    if resp is None or resp.status_code in (403, 429):
        logger.warning("%s: announcements fetch blocked/failed", symbol)
        return [QuarterRecord(symbol, symbol, "", "", "", notes="announcements fetch blocked/failed")]
    ann_rows = resp.json()

    xbrl_rows = npf.fetch_filing_index_raw(session, symbol, "Quarterly") or []
    integrated_rows = npf.fetch_integrated_filing_index_raw(session, symbol) or []

    def _xbrl_dates() -> set[date]:
        out = set()
        for r in xbrl_rows + integrated_rows:
            raw = r.get("toDate") or r.get("qe_Date")
            if not raw:
                continue
            try:
                d = datetime.strptime(raw, "%d-%b-%Y").date()
            except Exception:
                continue
            xurl = r.get("xbrl")
            if xurl and xurl.rstrip("/").rsplit("/", 1)[-1] not in ("", "-"):
                out.add(d)
        return out

    real_xbrl_dates = _xbrl_dates()

    candidates = []
    for r in ann_rows:
        desc = r.get("desc", "")
        if desc in _EXCLUDED_DESC_CATEGORIES or desc == "General Updates":
            continue
        if desc not in _RESULT_DESC_EXACT and not any(s in desc for s in _RESULT_DESC_ALLOWLIST_SUBSTR):
            continue
        dt = _parse_sort_date(r)
        if dt is None:
            continue
        candidates.append((dt, r))
    candidates.sort(key=lambda x: x[0])

    records: list[QuarterRecord] = []
    for qe in _quarter_end_dates(START_DATE, TODAY):
        fy, q = _fiscal_year_quarter(qe)
        window_start = datetime.combine(qe, datetime.min.time())
        window_end = window_start + timedelta(days=WINDOW_DAYS)
        in_window = [(dt, r) for dt, r in candidates if window_start <= dt <= window_end]

        rec = QuarterRecord(company_id=symbol, nse_symbol=symbol, fiscal_year=fy, quarter=q,
                             period_end=qe.isoformat())
        rec.xbrl_available = any(abs((xd - qe).days) <= 20 for xd in real_xbrl_dates)

        if not in_window:
            rec.notes = "no Outcome-of-Board-Meeting/result announcement found in +75d window"
            records.append(rec)
            continue

        dt, row = in_window[0]  # earliest in window = primary filing (Section 12.4)
        rec.filing_date = row.get("an_dt") or row.get("sort_date")
        rec.source_url = row.get("attchmntFile")
        rec.match_confidence = "text_confirmed" if _is_text_confirmed(row.get("attchmntText") or "") else "date_window_only"
        rec.attachment_format = _attachment_format(row.get("attchmntFile"))

        if rec.attachment_format == "none":
            rec.extraction_status = "not_found"
            rec.notes = f"desc={row.get('desc')!r}, no real attachment"
            records.append(rec)
            continue

        if not do_extraction:
            rec.extraction_status = "not_attempted"
            rec.notes = "skipped extraction (time budget)"
            records.append(rec)
            continue

        company_dir = PDF_DIR / symbol
        company_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{qe.isoformat()}_{row.get('seq_id', 'noseq')}"

        if rec.attachment_format == "pdf":
            dest = company_dir / f"{safe_name}.pdf"
            ok = npf.download_pdf(session, row["attchmntFile"], dest)
            if not ok:
                rec.extraction_status = "not_found"
                rec.notes = "download failed"
                records.append(rec)
                continue
            chars, err = _extract_pdf_text_len(dest)
            rec.extracted_char_count = chars
            rec.extraction_status = "extracted" if chars > EXTRACTED_CHAR_THRESHOLD else "needs_ocr"
            if err:
                rec.notes = f"pypdf error: {err}"
        elif rec.attachment_format == "zip":
            zip_dest = company_dir / f"{safe_name}.zip"
            zresp = npf._get(session, row["attchmntFile"])
            if zresp is None or zresp.status_code in (403, 429):
                rec.extraction_status = "not_found"
                rec.notes = "zip download failed"
                records.append(rec)
                continue
            zip_dest.write_bytes(zresp.content)
            extract_dir = company_dir / f"{safe_name}_extracted"
            content_desc, pdf_path = _handle_zip(zip_dest, extract_dir)
            rec.notes = content_desc
            if pdf_path and pdf_path.exists():
                chars, err = _extract_pdf_text_len(pdf_path)
                rec.extracted_char_count = chars
                rec.extraction_status = "extracted" if chars > EXTRACTED_CHAR_THRESHOLD else "needs_ocr"
                if err:
                    rec.notes += f" | pypdf error: {err}"
            else:
                rec.extraction_status = "needs_ocr"  # zip has no PDF inside — treat as non-text-extractable
        else:
            rec.extraction_status = "not_attempted"
            rec.notes = f"attachment_format={rec.attachment_format}, not handled"

        records.append(rec)

    session.close()
    return records


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.monotonic()
    # Budget check: extraction (download+pypdf) is the expensive part. Do it
    # for all 4 companies unless we're clearly running long, in which case
    # later companies get classification-only (attachment_format from
    # metadata, no download) — exactly the graceful degradation the task
    # asked for, applied per-company rather than abandoning mid-run.
    all_records: list[QuarterRecord] = []
    for i, symbol in enumerate(COMPANIES):
        elapsed_min = (time.monotonic() - t_start) / 60
        do_extraction = elapsed_min < 35  # leave time for report-writing
        logger.info("=== %s (extraction=%s, %.1fmin elapsed) ===", symbol, do_extraction, elapsed_min)
        recs = process_company(symbol, do_extraction)
        all_records.extend(recs)
        logger.info("%s: %d quarters classified", symbol, len(recs))

    # JSON (full detail) + CSV (flat, for the coordinator's own Neon load)
    (DATA_DIR / "discovery_2015_now.json").write_text(
        json.dumps([asdict(r) for r in all_records], indent=2)
    )
    with open(DATA_DIR / "discovery_2015_now.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(all_records[0]).keys()))
        writer.writeheader()
        for r in all_records:
            writer.writerow(asdict(r))

    npf.dump_attempt_log(DATA_DIR / "discovery_2015_now_attempt_log.json")

    total_min = (time.monotonic() - t_start) / 60
    logger.info("Done in %.1f min. %d total quarter-records written.", total_min, len(all_records))
    for symbol in COMPANIES:
        sub = [r for r in all_records if r.nse_symbol == symbol]
        statuses = {}
        for r in sub:
            statuses[r.extraction_status] = statuses.get(r.extraction_status, 0) + 1
        logger.info("%s: %d quarters, %s", symbol, len(sub), statuses)


if __name__ == "__main__":
    main()
