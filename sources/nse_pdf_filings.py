"""NSE quarterly-results PDF discovery — the "new, separate NSE source
adapter" the feasibility spike's own recommendation calls for
(docs/nse-pdf-feasibility/FEASIBILITY_REPORT.md, Section 9 point 1), built
on top of an endpoint this repo's XBRL fetch path (sources/nse_fetch.py)
never uses at all: `/api/corporate-announcements`. One call per company
returns that company's FULL disclosure history in one response (no
per-period pagination needed) — every row carries `desc` (NSE's own
category), `attchmntText` (a human-readable description NSE/the filer
writes), and `attchmntFile` (a direct PDF/ZIP url) or "-" if none.

Session/pacing: reuses sources/nse_fetch.py's exact bootstrap/session/
pacing/backoff machinery (`_new_session`, `_get_with_retries`,
`_REQUEST_PACING_SECONDS`) rather than re-implementing NSE's anti-bot
cookie dance a second time — this module is a sibling fetch path against
the same NSE host, not a different one. That module's own docstring
documents why the bootstrap step exists at all (a cold API call is
rejected without it) and why every call goes through paced retries with a
403-triggered re-bootstrap.

Matching method — date-window, not text-marker matching (feasibility
report Section 14.1, itself built on Section 12's Nifty-50-scale finding
that text-marker matching alone misses real filings — HINDUNILVR/ETERNAL/
TRENT all file genuine results whose `attchmntText` never says "financial
results" at all): for every expected fiscal-quarter-end date, look at
every `desc` in the recognized result-category set within
`_DISCLOSURE_WINDOW_DAYS` (75 — SEBI's own deadline is 45 days for Q1-Q3,
60 for the audited Q4/annual; 75 gives slack) days AFTER that quarter-end,
excluding `desc="General Updates"` from the candidate pool entirely (the
Section 12.2 "General Updates" catch-all trap — Investor Presentations and
Newspaper Publications share that exact desc with genuine filings, so it
can't be safely included even with per-row text disambiguation), and take
the EARLIEST match in that window as the primary filing (Section 12.4: the
primary filing is reliably first; press releases/presentations/newspaper
ads about the same results are reliably filed after it — this is exactly
what fixed the spike's own BHARTIARTL mis-pick in Section 12.3).

Every match is also checked against the original text-marker filter,
purely for transparency (not as a second filter stage) — `match_confidence`
records "text_confirmed" (both signals agree) or "date_window_only" (found
by timing alone) so a QA reviewer can see which signal found it, not a
black box. This mirrors spikes/nse_pdf_feasibility/run_2015_discovery.py's
own approach, retroactively validated there against 192 real quarters (0
`not_found`) — this module is that method promoted to a real, reusable
production adapter (not a modification of the spike script itself, which
stays untouched as a reference).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from sources.nse_fetch import _BASE, _get_with_retries, _new_session

logger = logging.getLogger(__name__)

_ANNOUNCEMENTS_API_PATH = "/api/corporate-announcements"

#: SEBI LODR Reg 33's own disclosure deadline is 45 days (Q1-Q3) / 60 days
#: (audited Q4/annual) after quarter-end — 75 gives slack for a late filer
#: without reaching into the NEXT quarter's own disclosure window (India's
#: quarters are ~90 days apart, so 75 never overlaps the following
#: quarter's primary filing). Verified against 192 real quarters across 4
#: companies (feasibility report Section 14.2): 0 quarters missed at this
#: window width.
_DISCLOSURE_WINDOW_DAYS = 75

#: Same text-marker list as spikes/nse_pdf_feasibility/run_spike.py's
#: _FIN_TEXT_MARKERS — kept here as an independent copy (not an import from
#: spike code, which the task explicitly keeps untouched/unimported by
#: production code) since this module uses it only for the transparency
#: match_confidence flag, never as a filtering gate on its own (Section
#: 12.2's whole point: text-marker-only filtering misses real filings).
_FIN_TEXT_MARKERS = (
    "financial results for the period ended",
    "unaudited financial results",
    "audited financial results",
    "financial results of",
)

#: `desc` categories verified live (feasibility report Section 4/12.2) to
#: produce false positives even though they sometimes reference "financial
#: results" in their own attchmntText — a press release/analyst deck/
#: newspaper clipping ABOUT results is not the result filing itself.
#: "General Updates" is deliberately NOT in this set: unlike these, it also
#: contains genuine filings for some companies (Section 12.2), so it can't
#: be blanket-excluded — it's excluded from the date-window candidate pool
#: below by a separate, explicit check instead (Section 14.1's fix).
_EXCLUDED_DESC_CATEGORIES = frozenset({
    "Press Release", "Press Release (Revised)", "News Release",
    "Analysts Meet", "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Investor Presentation", "Recording of Analysts/Institutional Investor Meet/Con. Call",
    "Transcript of Analysts/Institutional Investor Meet/Con. Call",
    "Schedule of Analysts/Institutional Investor Meet/Con. Call",
    "Copy of Newspaper Publication", "Newspaper Advertisements",
    "Clarification of News", "News Clarification", "News Verification",
    "Clarification - Financial Results",  # SEBI-initiated clarification request, not a filing (Section 12.2)
})

#: `desc` values a genuine result filing can carry (Section 14.1's
#: allowlist) — "General Updates" is excluded from the pool entirely
#: (checked separately, not via this set) even though it's not in
#: _EXCLUDED_DESC_CATEGORIES, since some of ITS rows genuinely are results.
_RESULT_DESC_EXACT = frozenset({"Integrated Filing- Financial", "Outcome of Board Meeting"})
_RESULT_DESC_ALLOWLIST_SUBSTR = ("Financial Result", "Results Update", "Result Update")

_GENERAL_UPDATES_DESC = "General Updates"


class NSEPDFDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class FilingMatch:
    """One discovered primary result filing for one (symbol, fiscal_year,
    quarter) — enough to download, register, and provenance-tag, before
    the PDF is ever fetched. Mirrors the fields the feasibility report's
    Section 9 point 3 calls out as directly available from this endpoint:
    seq_id -> document_id, an_dt/sort_date -> filing_date."""

    nse_symbol: str
    fiscal_year: str
    quarter: str
    period_end: date
    filing_date: str | None
    source_url: str | None
    seq_id: str | None
    desc: str
    attchmnt_text: str
    match_confidence: str  # "text_confirmed" | "date_window_only"
    attachment_format: str  # "pdf" | "zip" | "html" | "other" | "none"


def _parse_sort_date(row: dict) -> datetime | None:
    raw = row.get("sort_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _is_text_confirmed(attchmnt_text: str) -> bool:
    text = (attchmnt_text or "").lower()
    return any(marker in text for marker in _FIN_TEXT_MARKERS)


def _is_recognized_result_desc(desc: str) -> bool:
    if desc == _GENERAL_UPDATES_DESC or desc in _EXCLUDED_DESC_CATEGORIES:
        return False
    return desc in _RESULT_DESC_EXACT or any(s in desc for s in _RESULT_DESC_ALLOWLIST_SUBSTR)


def _attachment_format(url: str | None) -> str:
    if not url or url == "-":
        return "none"
    lowered = url.lower()
    if lowered.endswith(".pdf"):
        return "pdf"
    if lowered.endswith(".zip"):
        return "zip"
    if lowered.endswith((".html", ".htm")):
        return "html"
    return "other"


def quarter_end_dates(start: date, end: date) -> list[date]:
    """Every Mar/Jun/Sep/Dec quarter-end from just before `start` through
    `end` — generated directly (independent of whether a filing for it was
    ever found), same approach as the spike's own
    run_2015_discovery.py:_quarter_end_dates()."""
    ends: list[date] = []
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


def fetch_announcements_raw(
    symbol: str, *, session: requests.Session | None = None
) -> list[dict]:
    """Raw JSON rows from `/api/corporate-announcements` for `symbol` — full
    disclosure history in one call. Reuses sources/nse_fetch.py's session/
    retry machinery unchanged; raises NSEPDFDiscoveryError (wrapping
    whatever sources.nse_fetch.NSEFetchError/requests exception occurred)
    rather than returning an ambiguous empty list on a real failure, so a
    caller can tell "no announcements" from "couldn't fetch" — the earlier
    corporate-announcements-based discovery only had to distinguish this at
    the spike-script level; this is the one production entry point every
    caller (ingest script, QA tool) goes through, so it needs to be
    unambiguous."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_ANNOUNCEMENTS_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as this module's own error type
        raise NSEPDFDiscoveryError(f"failed to fetch corporate-announcements for {symbol}: {exc}") from exc
    finally:
        if owns_session:
            session.close()
    try:
        return response.json()
    except ValueError as exc:
        raise NSEPDFDiscoveryError(f"non-JSON corporate-announcements response for {symbol}") from exc


def discover_result_filings(
    symbol: str,
    *,
    start: date,
    end: date,
    session: requests.Session | None = None,
    announcements: list[dict] | None = None,
) -> list[FilingMatch]:
    """One FilingMatch per expected fiscal quarter in [start, end] that has a
    discoverable primary result filing — None (no match) is simply absent
    from the returned list rather than represented as a placeholder,
    letting the caller distinguish "found" from "not found" positionally
    against quarter_end_dates(start, end).

    `announcements`, when given, skips the live fetch entirely (used by
    tests / the QA tool against the spike's own already-downloaded
    discovery_2015_now.json-shaped data or a cached fixture) — the matching
    logic itself never depends on how the rows were obtained.
    """
    rows = announcements if announcements is not None else fetch_announcements_raw(symbol, session=session)

    candidates: list[tuple[datetime, dict]] = []
    for row in rows:
        desc = row.get("desc", "")
        if not _is_recognized_result_desc(desc):
            continue
        dt = _parse_sort_date(row)
        if dt is None:
            continue
        candidates.append((dt, row))
    candidates.sort(key=lambda pair: pair[0])

    matches: list[FilingMatch] = []
    for qe in quarter_end_dates(start, end):
        from normalization.periods import fiscal_year_and_quarter_from_date

        fiscal_year, quarter = fiscal_year_and_quarter_from_date(qe, "quarterly")
        window_start = datetime.combine(qe, datetime.min.time())
        window_end = window_start + timedelta(days=_DISCLOSURE_WINDOW_DAYS)
        in_window = [(dt, row) for dt, row in candidates if window_start <= dt <= window_end]
        if not in_window:
            continue

        _, row = in_window[0]  # earliest in window = primary filing
        source_url = row.get("attchmntFile")
        matches.append(
            FilingMatch(
                nse_symbol=symbol,
                fiscal_year=fiscal_year,
                quarter=quarter or "Q4",
                period_end=qe,
                filing_date=row.get("an_dt") or row.get("sort_date"),
                source_url=source_url,
                seq_id=str(row["seq_id"]) if row.get("seq_id") is not None else None,
                desc=row.get("desc", ""),
                attchmnt_text=row.get("attchmntText") or "",
                match_confidence="text_confirmed" if _is_text_confirmed(row.get("attchmntText") or "") else "date_window_only",
                attachment_format=_attachment_format(source_url),
            )
        )

    return matches


def download_filing(
    session: requests.Session, filing: FilingMatch, dest_path,
) -> bool:
    """Download `filing`'s attachment (PDF or ZIP) to dest_path. False
    (logged) on any failure — never raises, matching every other NSE
    download helper's "log and let the caller move on" contract."""
    if filing.attachment_format not in ("pdf", "zip") or not filing.source_url:
        logger.warning("%s %s%s: nothing to download (format=%s)", filing.nse_symbol, filing.fiscal_year, filing.quarter, filing.attachment_format)
        return False
    try:
        response = _get_with_retries(session, filing.source_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Download failed for %s: %s", filing.source_url, exc)
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(response.content)
    return True
