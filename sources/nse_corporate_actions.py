"""Live fetch of NSE's per-symbol corporate actions feed (Bonus, Dividend,
Split, Face-Value Split, Rights) -- a separate domain from
sources/nse_xbrl.py's quarterly-results financials and sources/
nse_shareholding.py's SHP filings, with its own listing API.

Raw fetch only: this module returns exactly what NSE's own
corporates-corporateActions endpoint reports, one CorporateActionRef per
row, with dates parsed but the freeform `subject` field left untouched --
classifying that text into a Bonus/Dividend/Split/etc. type happens later,
in ingestion, not here (same "store the raw feed, decide how to process it
separately" split this app already uses for financial_observations vs
canonical_financials).

Reuses sources/nse_fetch.py's session bootstrap/pacing/retry machinery
directly (same WAF/anti-bot cookie dance, same host, same "25-Jan-2025"
date format on this endpoint too -- verified live against HDFCBANK) rather
than duplicating it -- this module owns nothing about HTTP session
mechanics of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import requests

from sources.nse_fetch import _BASE, _get_with_retries, _new_session, _parse_nse_date

_CORPORATE_ACTIONS_API_PATH = "/api/corporates-corporateActions"


@dataclass(frozen=True)
class CorporateActionRef:
    """One row exactly as NSE's corporate-actions listing reports it --
    `subject` is kept verbatim (freeform text, e.g. "Bonus 1:1", "Dividend
    - Rs 13 Per Share"); classifying it into a type is an ingestion-layer
    concern, not this module's."""

    symbol: str
    subject: str
    ex_date: date
    record_date: date | None       # None when NSE reports "-" (older filings use book-closure dates instead)
    face_value: float | None
    bc_start_date: date | None     # book-closure window, when this action used one instead of a record date
    bc_end_date: date | None
    raw: dict                      # the full NSE row, verbatim, for anything not modeled above


def _dash_to_none(value: str | None) -> str | None:
    """NSE reports an absent date/value as the literal string "-", not
    null/empty -- verified live (older HDFCBANK dividends carry
    recDate="-" and instead use bcStartDate/bcEndDate)."""
    if not value or value == "-":
        return None
    return value


def corporate_actions_url(symbol: str) -> str:
    """Public wrapper of the corporate-actions endpoint -- scripts/
    batch_fetch_nse.py's _run_corporate_actions() records this as the raw
    object's source_url (ADR-022). Query params aren't included (only the
    base path) since `symbol` alone identifies the fetch and the fixed
    `index=equities` param never varies."""
    return f"{_BASE}{_CORPORATE_ACTIONS_API_PATH}?symbol={symbol}"


def fetch_corporate_actions_raw(
    symbol: str, *, session: requests.Session | None = None,
) -> bytes:
    """Just the network fetch, extracted out of fetch_corporate_actions()
    below so a caller that needs the raw bytes themselves (ADR-022's
    raw/regulatory/ persistence -- ingestion/corporate_actions.py stores
    this exact payload before any parsing happens) doesn't have to issue a
    second HTTP request. fetch_corporate_actions() calls this internally
    -- one network call either way, same as before this split."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_CORPORATE_ACTIONS_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    finally:
        if owns_session:
            session.close()
    return response.content


def parse_corporate_actions_json(raw_bytes: bytes, symbol: str) -> list[CorporateActionRef]:
    """The parsing half of fetch_corporate_actions() below, extracted so a
    caller that already has the raw bytes (e.g. replaying a cataloged
    raw/regulatory/ object, per ADR-022) can parse them without a network
    call."""
    rows = json.loads(raw_bytes)
    refs: list[CorporateActionRef] = []
    for row in rows:
        ex_date_str = _dash_to_none(row.get("exDate"))
        subject = row.get("subject")
        if not ex_date_str or not subject:
            # No ex-date or no subject text -- nothing usable to act on or
            # to classify later, same "skip rather than guess" discipline
            # as sources/nse_fetch.py's own row filtering.
            continue
        record_date_str = _dash_to_none(row.get("recDate"))
        bc_start_str = _dash_to_none(row.get("bcStartDate"))
        bc_end_str = _dash_to_none(row.get("bcEndDate"))
        face_val = row.get("faceVal")
        refs.append(
            CorporateActionRef(
                symbol=row.get("symbol", symbol),
                subject=subject,
                ex_date=_parse_nse_date(ex_date_str),
                record_date=_parse_nse_date(record_date_str) if record_date_str else None,
                face_value=float(face_val) if face_val not in (None, "", "-") else None,
                bc_start_date=_parse_nse_date(bc_start_str) if bc_start_str else None,
                bc_end_date=_parse_nse_date(bc_end_str) if bc_end_str else None,
                raw=row,
            )
        )
    return refs


def fetch_corporate_actions(
    symbol: str,
    *,
    session: requests.Session | None = None,
) -> list[CorporateActionRef]:
    """List every corporate action NSE has on file for `symbol` -- full
    history in one response, same "no server-side date-range filtering,
    caller filters client-side if it wants a window" convention as
    sources/nse_fetch.py's fetch_filing_index(). Unchanged public
    behavior/signature, now composed of fetch_corporate_actions_raw() +
    parse_corporate_actions_json() rather than doing the HTTP GET and JSON
    parse inline."""
    raw_bytes = fetch_corporate_actions_raw(symbol, session=session)
    return parse_corporate_actions_json(raw_bytes, symbol)
