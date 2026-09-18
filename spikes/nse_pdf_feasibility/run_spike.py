"""Spike orchestrator: for each target company, discover NSE financial-
results filings (PDF via corporate-announcements, XBRL via the existing
corporates-financial-results / integrated-filing-results listings), sample
a recent / ~5y-ago / ~10y-ago quarter, download whatever PDFs are available,
and record everything to spikes/nse_pdf_feasibility/data/results.json for
the report to read from.

One bootstrap + a small, fixed number of listing calls per company (each
listing call returns FULL history in one response — no per-period repeated
calls, no pagination loop). PDF downloads are one call each, only for the
3 sampled periods per company. This keeps total NSE traffic small and
respectful, consistent with the task's network-safety rule.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nse_pdf_fetch as npf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_spike")

DATA_DIR = Path(__file__).parent / "data"
PDF_DIR = DATA_DIR / "pdfs"
COMPANIES = ["HDFCBANK", "RELIANCE", "ICICIBANK", "TCS"]

TODAY = date(2026, 9, 17)


def _parse_sort_date(d: dict) -> datetime | None:
    try:
        return datetime.strptime(d["sort_date"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


_FIN_TEXT_MARKERS = ("financial results for the period ended", "unaudited financial results",
                     "audited financial results", "financial results of")


def _is_fin_result_row(d: dict) -> bool:
    """A genuine exchange-filed financial-results announcement, verified by
    CONTENT (attchmntText), not just NSE's own `desc` category bucket.

    Found live (HDFCBANK, period ended 30-Jun-2026): the real quarterly
    result PDF is filed under desc="Outcome of Board Meeting" — NSE bundles
    the board's results approval and the results submission into the same
    announcement — not under any of the dedicated "Financial Result
    Updates"/"Results Update .../"Integrated Filing- Financial" category
    labels a desc-only filter would look for. Those category labels DO
    still catch genuine filings in most other cases (verified: older
    filings, and administrative "Integrated Filing" resubmissions), so
    they're kept as a secondary signal, but attchmntText content is the
    primary, reliable check — this IS this spike's "how was this verified
    as a real financial result, not a presentation/transcript/press
    release" method (task requirement 5): attchmntText for a genuine
    filing explicitly names "financial results for the period ended ..."
    or "Unaudited/Audited Financial Results", whereas verified real
    exclusions (Investor Presentation, Analysts/Con.Call Updates, Press
    Release) never do — checked live for HDFCBANK 18-Jul-2026 same-day
    rows: "Investor Presentation" text reads "...informed the Exchange
    about Investor Presentation" (no financial-results phrase), "Analysts/
    Institutional Investor Meet/Con. Call Updates" text reads "...Link of
    Recording" — neither matches these markers.
    """
    desc = d.get("desc", "")
    # Verified live (RELIANCE/ICICIBANK): a Media Release, analyst-meet
    # transcript/presentation, or newspaper-publication notice all
    # legitimately MENTION "financial results for the period ended ..." in
    # their own attchmntText while pointing to a DIFFERENT attached document
    # (a press release PDF, a transcript, a newspaper clipping) — exactly
    # the excluded document types the task calls out. A text-marker match
    # alone is not sufficient; these desc categories are excluded even when
    # the text marker fires, because verified real examples in this exact
    # dataset are false positives on text alone.
    if desc in _EXCLUDED_DESC_CATEGORIES:
        return False
    text = (d.get("attchmntText") or "").lower()
    if any(marker in text for marker in _FIN_TEXT_MARKERS):
        return True
    return (
        "Financial Result" in desc
        or "Results Update" in desc
        or "Result Update" in desc
        or desc == "Integrated Filing- Financial"
    )


_EXCLUDED_DESC_CATEGORIES = {
    "Press Release", "Press Release (Revised)", "News Release",
    "Analysts Meet", "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Investor Presentation", "Recording of Analysts/Institutional Investor Meet/Con. Call",
    "Transcript of Analysts/Institutional Investor Meet/Con. Call",
    "Schedule of Analysts/Institutional Investor Meet/Con. Call",
    "Copy of Newspaper Publication", "Newspaper Advertisements",
    "Clarification of News", "News Clarification", "News Verification",
}


def _has_real_pdf(d: dict) -> bool:
    f = d.get("attchmntFile")
    return bool(f) and f != "-" and f.lower().endswith(".pdf")


def _target_dates(today: date) -> dict[str, date]:
    return {
        "recent": today,
        "~5y_ago": today.replace(year=today.year - 5),
        "~10y_ago": today.replace(year=today.year - 10),
    }


_PERIOD_END_RE = re.compile(
    r"(?:period|quarter|year)\s+ended\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})", re.IGNORECASE
)


def _extract_period_end(text: str) -> date | None:
    """Best-effort "reported quarter-end" from the announcement's own
    attchmntText (e.g. "...for the period ended June 30, 2021") — used to
    anchor the XBRL-existence match to the SAME period the PDF actually
    covers, rather than to the sample target_date. Needed because the
    announcement nearest target_date and the XBRL filing nearest
    target_date can legitimately be different quarters (verified live:
    HDFCBANK's nearest ~5y-ago PDF announcement was for the quarter ended
    30-Jun-2021, broadcast 17-Jul-2021, while the XBRL row nearest the same
    target_date was for the quarter ended 30-Sep-2021 — comparing those two
    would have silently compared different periods' figures)."""
    m = _PERIOD_END_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "").strip()
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _closest_row(rows: list[dict], target: date) -> dict | None:
    """The fin-result announcement whose sort_date is closest to (and not
    after, to avoid picking a filing that postdates our "as of" sample
    point) target — a simple, honest "nearest available filing" pick, not
    an assumption about exact quarter-end dates."""
    candidates = []
    for r in rows:
        dt = _parse_sort_date(r)
        if dt is None:
            continue
        candidates.append((dt.date(), r))
    if not candidates:
        return None
    on_or_before = [c for c in candidates if c[0] <= target]
    pool = on_or_before if on_or_before else candidates
    return min(pool, key=lambda c: abs((c[0] - target).days))[1]


def process_company(symbol: str) -> dict:
    result: dict = {"symbol": symbol, "periods": {}}

    session = npf.new_session()
    if session is None:
        result["blocked"] = "bootstrap failed"
        return result

    announcements = npf._get(session, "https://www.nseindia.com/api/corporate-announcements",
                              params={"index": "equities", "symbol": symbol})
    if announcements is None:
        result["blocked"] = "corporate-announcements fetch failed (network error)"
        return result
    if announcements.status_code in (403, 429):
        result["blocked"] = f"corporate-announcements blocked (status {announcements.status_code})"
        return result
    try:
        ann_rows = announcements.json()
    except ValueError:
        result["blocked"] = "corporate-announcements returned non-JSON"
        return result

    fin_rows = [r for r in ann_rows if _is_fin_result_row(r)]
    logger.info("%s: %d total announcements, %d financial-result-related", symbol, len(ann_rows), len(fin_rows))

    xbrl_rows = npf.fetch_filing_index_raw(session, symbol, "Quarterly") or []
    integrated_rows = npf.fetch_integrated_filing_index_raw(session, symbol) or []
    logger.info("%s: %d xbrl-listing rows, %d integrated-filing rows", symbol, len(xbrl_rows), len(integrated_rows))

    for label, target in _target_dates(TODAY).items():
        row = _closest_row(fin_rows, target)
        period_result: dict = {"target_date": target.isoformat()}
        if row is None:
            period_result["filing_found"] = False
            result["periods"][label] = period_result
            continue

        period_result["filing_found"] = True
        period_result["desc"] = row.get("desc")
        period_result["attchmnt_text"] = row.get("attchmntText")
        text_l = (row.get("attchmntText") or "").lower()
        period_result["verified_by_text_marker"] = any(m in text_l for m in _FIN_TEXT_MARKERS)
        period_result["broadcast_date"] = row.get("an_dt") or row.get("sort_date")
        period_result["nse_source_url"] = "https://www.nseindia.com/api/corporate-announcements"
        period_result["seq_id"] = row.get("seq_id")
        period_result["has_pdf_field"] = _has_real_pdf(row)
        period_result["attchmnt_file"] = row.get("attchmntFile")
        period_result["isin"] = row.get("sm_isin")

        reported_period_end = _extract_period_end(row.get("attchmntText") or "")
        period_result["reported_period_end"] = reported_period_end.isoformat() if reported_period_end else None
        xbrl_anchor = reported_period_end or target

        # Does XBRL exist for a period around this same date? (existence
        # check only, closest match by toDate.) Anchored to the PDF's own
        # reported period end when extractable (see _extract_period_end),
        # not to the sample target_date — the two can be different quarters.
        xbrl_match = None
        all_xbrl = xbrl_rows + integrated_rows
        if all_xbrl:
            def _to_date(r):
                # Older listing (fetch_filing_index_raw) uses "toDate";
                # Integrated Filing listing (fetch_integrated_filing_index_raw)
                # uses "qe_Date" instead (both "%d-%b-%Y", case-insensitive
                # month abbrev — verified datetime.strptime handles
                # "30-JUN-2026" fine). Missing this second field name meant
                # every integrated-filing row was silently dropped from the
                # XBRL-existence check below (caught while reviewing this
                # spike's own recent-quarter results: XBRL falsely showed
                # as absent for a period that in fact has an Integrated
                # Filing XBRL on record).
                raw = r.get("toDate") or r.get("qe_Date")
                if not raw:
                    return None
                try:
                    return datetime.strptime(raw, "%d-%b-%Y").date()
                except Exception:
                    return None
            dated = [(r, _to_date(r)) for r in all_xbrl]
            dated = [(r, d) for r, d in dated if d is not None]
            if dated:
                closest = min(dated, key=lambda rd: abs((rd[1] - xbrl_anchor).days))
                if abs((closest[1] - xbrl_anchor).days) <= 20:  # same reported quarter, not just "nearby"
                    xbrl_match = closest[0]
        period_result["xbrl_exists"] = xbrl_match is not None
        if xbrl_match is not None:
            period_result["xbrl_period_to_date"] = xbrl_match.get("toDate") or xbrl_match.get("qe_Date")
            period_result["xbrl_url"] = xbrl_match.get("xbrl")

        # Download PDF if available
        if period_result["has_pdf_field"]:
            pdf_url = row["attchmntFile"]
            dest = PDF_DIR / symbol / f"{label}_{row.get('seq_id','noseq')}.pdf"
            ok = npf.download_pdf(session, pdf_url, dest)
            period_result["pdf_downloaded"] = ok
            period_result["local_path"] = str(dest) if ok else None
        else:
            period_result["pdf_downloaded"] = False
            period_result["local_path"] = None

        result["periods"][label] = period_result

    return result


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    all_results = {}
    for symbol in COMPANIES:
        logger.info("=== %s ===", symbol)
        all_results[symbol] = process_company(symbol)

    (DATA_DIR / "results.json").write_text(json.dumps(all_results, indent=2, default=str))
    npf.dump_attempt_log(DATA_DIR / "attempt_log.json")
    logger.info("Done. Wrote %s and attempt_log.json (%d total NSE requests logged)",
                DATA_DIR / "results.json", len(npf.ATTEMPT_LOG))


if __name__ == "__main__":
    main()
