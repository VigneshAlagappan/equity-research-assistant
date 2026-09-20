"""Discovery + classification + download of NSE quarterly-result filing
PDFs, investor/concall presentations, and concall transcripts -- a
STORAGE-ONLY companion to sources/nse_xbrl.py (which parses XBRL facts into
canonical_financials). This module never parses a document's contents for
facts; it only decides which of a company's `corporate-announcements` rows
are one of three document types worth keeping raw, and fetches the bytes.

Built from the real findings of the NSE PDF feasibility spike
(docs/nse-pdf-feasibility/FEASIBILITY_REPORT.md, code under
spikes/nse_pdf_feasibility/): `/api/corporate-announcements` returns a
company's full disclosure history in one call, each row carrying `desc`
(NSE's own category), `attchmntText` (a human-written description), and
`attchmntFile` (a direct PDF/ZIP URL under nsearchives.nseindia.com, or
"-" when there's no attachment). Reuses sources/nse_fetch.py's session
bootstrap/pacing/retry machinery directly (same WAF/anti-bot cookie dance,
same host) rather than duplicating it -- same reuse convention
sources/nse_corporate_actions.py and sources/nse_shareholding.py already
follow for this endpoint family.

Classification (see FEASIBILITY_REPORT.md Section 4/12 for the full
evidence trail this is built from):

- quarterly_result_filing -- desc is one of NSE's own genuine
  financial-results categories ("Financial Result Updates", "Results
  Update...", "Result Update...", "Integrated Filing- Financial") OR
  desc == "Outcome of Board Meeting" AND the announcement's own
  attchmntText actually mentions results (verified live: a Board Meeting
  outcome can legitimately be about something else entirely -- a buyback,
  a director appointment -- so that one desc category alone isn't
  sufficient signal, unlike the others).
- investor_presentation -- desc == "Investor Presentation" (NSE's own
  dedicated category), OR desc == "General Updates" (a catch-all bucket,
  Section 12.2's confirmed false-positive leak) AND attchmntText mentions
  an investor/analyst presentation.
- concall_transcript -- desc is a transcript or recording of an
  analysts/institutional-investor call ("Transcript of Analysts/
  Institutional Investor Meet/Con. Call", "Recording of Analysts/
  Institutional Investor Meet/Con. Call" -- Section 4 references this
  bucket as "Con. Call transcripts/recordings"), OR desc == "General
  Updates" AND attchmntText explicitly mentions a concall/earnings-call
  transcript.

Deliberately unlike the feasibility spike's own `_is_fin_result_row`: no
attempt is made here to pick ONE correct filing per quarter (date-window
matching, "earliest wins", XBRL-proximity) -- this module collects EVERY
row it can confidently classify into one of the three types above, across
a company's full history. A row that doesn't confidently match one of
these three patterns is left unclassified (skipped, logged) rather than
guessed at, per this task's own "skip rather than guess" requirement --
this deliberately accepts lower recall on ambiguously-worded rows (e.g. a
"General Updates"/"Outcome of Board Meeting" row that never spells out
what it's about, a real limitation the spike's Section 12.2 already
documented for a handful of Nifty 50 companies) in exchange for never
mis-filing a document under the wrong type.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

import requests

from normalization.periods import fiscal_year_and_quarter_from_date
from sources.nse_fetch import _BASE, _get_with_retries, _new_session

_ANNOUNCEMENTS_API_PATH = "/api/corporate-announcements"

#: The three document types this module ever classifies a row as -- kept as
#: a tuple (not just implied by the classifier's return values) so callers
#: (scripts/backfill_nse_filing_documents.py, tests) have one place to
#: validate a returned document_type against, rather than restating the
#: literal strings.
DOCUMENT_TYPES = ("quarterly_result_filing", "investor_presentation", "concall_transcript")

#: NSE's own dedicated "this is unambiguously an investor presentation"
#: category -- verified real across companies (Section 4/12).
_INVESTOR_PRESENTATION_DESC = "Investor Presentation"

#: NSE's own dedicated concall transcript/recording categories -- verified
#: real (Section 4: "Con. Call transcripts/recordings" is one of the
#: categories the original spike had to actively exclude for its narrower
#: task; here they're exactly what's wanted).
_CONCALL_DESC_CATEGORIES = frozenset({
    "Transcript of Analysts/Institutional Investor Meet/Con. Call",
    "Recording of Analysts/Institutional Investor Meet/Con. Call",
})

#: NSE's own genuinely mixed-bag desc labels -- confirmed live against BOTH
#: validation companies' real full history (not just the feasibility
#: spike's own Section 12.2 "General Updates" finding): "Updates" (BANKBARODA,
#: 470 rows spanning everything from a plain director appointment to a
#: genuine "Transcript of Analyst and Media Meet"), "General Updates" /
#: "General updates" (both capitalizations seen live), and "Analysts/
#: Institutional Investor Meet/Con. Call Updates" (409 rows, spanning plain
#: meeting-schedule intimations to a genuine "Earning Conference Call
#: transcript" attachment) ALL co-mingle unrelated content under one label.
#: None of these desc values is itself evidence of anything -- compared
#: case-insensitively since NSE's own data isn't consistently cased.
_MIXED_BAG_DESC_VALUES = frozenset({
    "general updates", "updates", "analysts/institutional investor meet/con. call updates",
})

#: Phrases confirming a genuine financial-results announcement, taken
#: verbatim from the feasibility spike's own verified real-data findings
#: (run_spike.py's _FIN_TEXT_MARKERS, Section 4).
_RESULT_TEXT_MARKERS = (
    "financial results for the period ended",
    "unaudited financial results",
    "audited financial results",
    "financial results of",
)

#: Verified live: "investor presentation" (the common case) plus "analyst
#: presentation"/"analysts presentation" (BANKBARODA's own real older
#: phrasing, e.g. "Analyst Presentation Q2 Results FY 2016-17") and the
#: "presentation for ... conference call" variants (KOTAKBANK, Section 12.2).
_INVESTOR_PRESENTATION_TEXT_MARKERS = (
    "investor presentation", "analyst presentation", "analysts presentation",
    "presentation for the earnings conference call", "presentation for earnings conference call",
)

#: Verified live: real mixed-bag rows describe a transcript/recording far
#: more tersely than the feasibility spike's own dedicated-category
#: wording ("Bank Of Baroda has informed the Exchange about Transcript",
#: "...Transcript and Link of Recording", "Earning Call Transcript" --
#: note "Earning" singular, unlike the spike's "Earnings Call Transcript"
#: marker) -- a bare "transcript"/"recording" substring is used here
#: instead of a longer phrase, since it's only ever applied within an
#: already-narrowed mixed-bag desc bucket (see _disambiguate_mixed_bag_by_
#: content), not globally.
_CONCALL_TEXT_MARKERS = ("transcript", "recording")

#: A "Schedule of ..." announcement (a notice about an UPCOMING meet/call)
#: is not itself a presentation/transcript document, even when its own
#: text happens to mention "Presentation" or a call -- verified live
#: (BANKBARODA: "Schedule of Analysts Meet / Presentation and Media Meet on
#: Bank's Financial Results..." under both desc="Analysts/Institutional
#: Investor Meet/Con. Call Updates" and desc="Analysts Meet", attached file
#: literally named "...MeetSchedule...") -- this guard exists specifically
#: because "Presentation"/"transcript" alone would otherwise mis-fire on
#: these schedule notices.
_SCHEDULE_NOTICE_MARKER = "schedule of"


def _disambiguate_mixed_bag_by_content(text_l: str) -> str | None:
    """Shared content-only disambiguation for NSE's confirmed mixed-bag
    desc labels (_MIXED_BAG_DESC_VALUES) -- none of them is itself
    evidence of anything, so attchmntText is the only signal, and a
    schedule/notice about a future event is excluded up front rather than
    risking a false match on incidental wording."""
    if _SCHEDULE_NOTICE_MARKER in text_l:
        return None
    if any(marker in text_l for marker in _INVESTOR_PRESENTATION_TEXT_MARKERS):
        return "investor_presentation"
    if any(marker in text_l for marker in _CONCALL_TEXT_MARKERS):
        return "concall_transcript"
    return None


def _is_result_desc_category(desc: str) -> bool:
    """Same substring rule the feasibility spike's _is_fin_result_row used
    for its secondary desc-based signal (Section 4) -- "Financial Result
    Updates", "Results Update For The Quarter Ended ...", "Result Update",
    "Integrated Filing- Financial" all satisfy this."""
    return (
        "Financial Result" in desc
        or "Results Update" in desc
        or "Result Update" in desc
        or desc == "Integrated Filing- Financial"
    )


def classify_announcement_row(row: dict) -> str | None:
    """One of DOCUMENT_TYPES, or None if this row can't be confidently
    classified (including: no attachment at all, an attachment that isn't
    a PDF/ZIP, or a desc/attchmntText combination that doesn't match any
    recognized pattern). Never raises on a malformed/missing field --
    treats it the same as "not classifiable"."""
    if not has_downloadable_attachment(row):
        return None

    desc = (row.get("desc") or "").strip()
    text_l = (row.get("attchmntText") or "").lower()

    if desc == _INVESTOR_PRESENTATION_DESC:
        return "investor_presentation"
    if desc in _CONCALL_DESC_CATEGORIES:
        return "concall_transcript"
    if desc == "Outcome of Board Meeting":
        # Verified live (FEASIBILITY_REPORT.md Section 12.2): "Outcome of
        # Board Meeting" is NSE's single most common vehicle for the real
        # quarterly result PDF, but the same desc also covers board
        # meetings about buybacks, appointments, and other non-results
        # business -- attchmntText content is the only reliable signal for
        # this one desc value specifically (every other recognized result
        # category is unambiguous by desc alone).
        if any(marker in text_l for marker in _RESULT_TEXT_MARKERS) or "result" in text_l:
            return "quarterly_result_filing"
        return None
    if _is_result_desc_category(desc):
        return "quarterly_result_filing"
    if desc.lower() in _MIXED_BAG_DESC_VALUES:
        # Verified live across both validation companies' full history:
        # every one of these desc labels is a genuine mixed bag (Section
        # 12.2 for "General Updates"; BANKBARODA's/AAVAS's own real
        # history, found during this task's own dry run, for "Updates" and
        # "Analysts/Institutional Investor Meet/Con. Call Updates") --
        # content is the only usable signal for any of them.
        return _disambiguate_mixed_bag_by_content(text_l)
    return None


def has_downloadable_attachment(row: dict) -> bool:
    """True only for a row carrying a real PDF or ZIP attachment -- NSE
    reports "-" (or omits the field) for announcements with nothing
    attached, and Section 13 confirmed some pre-2019 filings arrive as a
    ZIP wrapping a real PDF rather than a bare .pdf URL, so both
    extensions are accepted here (this module downloads the ZIP as-is,
    same storage-only scope as everything else it does -- unzipping is a
    processing step, out of scope per this task)."""
    attachment = row.get("attchmntFile")
    if not attachment or attachment == "-":
        return False
    lowered = attachment.lower()
    return lowered.endswith(".pdf") or lowered.endswith(".zip")


def attachment_extension(attchmnt_file: str) -> str:
    """"pdf" or "zip" -- has_downloadable_attachment() already guarantees
    one of these two suffixes for anything reaching this function."""
    return attchmnt_file.lower().rsplit(".", 1)[-1]


_PERIOD_END_RE = re.compile(
    r"(?:period|quarter|year)\s+ended\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})", re.IGNORECASE
)


def _extract_period_end(text: str) -> date | None:
    """Best-effort "reported quarter-end" straight from the announcement's
    own attchmntText (e.g. "...for the period ended June 30, 2021") --
    same regex the feasibility spike's run_spike.py used
    (_extract_period_end), reused verbatim since it was already verified
    against real NSE phrasing. Returns None (never raises) when the text
    doesn't contain this exact phrasing -- callers must treat a None
    period as "not determinable", not an error, per this task's own
    "best-effort -- don't block on this" instruction."""
    match = _PERIOD_END_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1).replace(",", "").strip()
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def period_string(period_end: date | None) -> str | None:
    """"FY2022Q1"-style period string for raw_objects.period, or None when
    no period-end date was determinable. Uses fiscal_year_and_quarter_
    from_date's India (Apr-Mar) default, the fiscal-year convention every
    other NSE source in this app already assumes."""
    if period_end is None:
        return None
    fiscal_year, quarter = fiscal_year_and_quarter_from_date(period_end, "quarterly")
    return f"{fiscal_year}{quarter}" if quarter else fiscal_year


def _parse_broadcast_date(row: dict) -> date | None:
    """an_dt / sort_date are both "YYYY-MM-DD HH:MM:SS" on this endpoint
    (verified against real rows in spikes/nse_pdf_feasibility/data/) --
    unlike sources/nse_fetch.py's "DD-Mon-YYYY" listings, this is a
    different endpoint with its own date format."""
    raw = row.get("an_dt") or row.get("sort_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw.split(" ")[0], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class ClassifiedFiling:
    """One announcement row this module confidently classified into one of
    DOCUMENT_TYPES, with everything scripts/backfill_nse_filing_documents.py
    needs to download and catalog it -- `raw` is kept for anything not
    modeled explicitly (same convention sources/nse_corporate_actions.py's
    CorporateActionRef already follows)."""

    symbol: str
    seq_id: str
    document_type: str
    desc: str
    attchmnt_text: str
    attchmnt_file: str
    broadcast_date: date | None
    period: str | None
    raw: dict


def announcements_url(symbol: str) -> str:
    """Public wrapper of the corporate-announcements endpoint, for
    recording as the raw object's source_url (ADR-022) -- same role
    sources/nse_corporate_actions.py's corporate_actions_url() plays for
    its own endpoint."""
    return f"{_BASE}{_ANNOUNCEMENTS_API_PATH}?index=equities&symbol={symbol}"


def fetch_announcements_raw(symbol: str, *, session: requests.Session | None = None) -> bytes:
    """Just the network fetch -- full disclosure history in one response,
    no pagination, no server-side date filtering (verified by the
    feasibility spike). Split from parse_announcements_json() so a caller
    that already has the raw bytes (e.g. replaying a cataloged raw/
    companies/ object) can classify without a network call."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_ANNOUNCEMENTS_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    finally:
        if owns_session:
            session.close()
    return response.content


def parse_announcements_json(raw_bytes: bytes) -> list[dict]:
    return json.loads(raw_bytes)


def classify_announcements(rows: list[dict], symbol: str) -> tuple[list[ClassifiedFiling], list[dict]]:
    """Splits `rows` into (confidently classified, everything else) --
    the second list is every row that either has no usable attachment or
    couldn't be matched to one of DOCUMENT_TYPES, for the caller to log
    and skip rather than silently drop."""
    classified: list[ClassifiedFiling] = []
    unclassified: list[dict] = []
    for row in rows:
        document_type = classify_announcement_row(row)
        if document_type is None:
            if has_downloadable_attachment(row):
                # Only rows with SOMETHING attached are worth flagging for
                # manual review -- a row with no attachment at all was
                # never a candidate for any of the three document types.
                unclassified.append(row)
            continue
        period_end = _extract_period_end(row.get("attchmntText") or "")
        classified.append(
            ClassifiedFiling(
                symbol=symbol,
                seq_id=str(row.get("seq_id") or row.get("seqId") or ""),
                document_type=document_type,
                desc=row.get("desc") or "",
                attchmnt_text=row.get("attchmntText") or "",
                attchmnt_file=row["attchmntFile"],
                broadcast_date=_parse_broadcast_date(row),
                period=period_string(period_end),
                raw=row,
            )
        )
    return classified, unclassified


def discover_company_filings(
    symbol: str, *, session: requests.Session | None = None,
) -> tuple[list[ClassifiedFiling], list[dict]]:
    """One network call (fetch_announcements_raw) + pure classification --
    the composed convenience function scripts/backfill_nse_filing_documents.py
    calls per company."""
    raw_bytes = fetch_announcements_raw(symbol, session=session)
    rows = parse_announcements_json(raw_bytes)
    return classify_announcements(rows, symbol)


def download_document(session: requests.Session, url: str) -> bytes:
    """Download one classified filing's attachment (a PDF or ZIP). Raises
    NSEFetchError (via _get_with_retries) on exhausted retries/blocking --
    same "let the caller's per-company error handling decide what to do"
    convention as sources/nse_shareholding.py's fetch_shareholding_detail_raw."""
    response = _get_with_retries(session, url)
    return response.content
