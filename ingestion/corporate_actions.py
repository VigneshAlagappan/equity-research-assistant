"""Classify corporate_actions_raw rows into the display-ready
corporate_actions table -- the processing half of the "store the raw feed,
decide how to process it separately" split sources/nse_corporate_actions.py
uses (same reasoning as financial_observations -> canonical_financials
here: re-classifying never requires re-fetching from NSE, it just re-runs
this over whatever's already on disk).

`subject` is NSE's own freeform text (e.g. "Bonus 1:1", "Dividend - Rs 13
Per Share", " Face Value Split (Sub-Division) - From Rs 2 Per Share To Rs 1
Per Share") -- classify_action_type() below was verified against HDFCBANK's
real 20-row history (2011-2026: 18 dividend incl. 5 AGM-combined subjects,
1 bonus, 2 fv_split) before this module was written. No Rights-type
subject has been seen in real data yet -- that branch is a plain substring
match, unverified against a real example.
"""

from __future__ import annotations

from storage.company_repository import (
    insert_corporate_action,
    mark_corporate_actions_raw_processed,
    select_all_corporate_actions_raw,
    select_unprocessed_corporate_actions_raw,
)
from storage.database import utcnow_iso
from storage.db_types import DBConnection

CLASSIFIER_VERSION = "v2"  # v2: recognizes older filings' abbreviated "Div"/"Rhs" alongside "Dividend"/"Rights"


def classify_action_type(subject: str) -> str:
    """Ordered keyword rules, closed vocabulary + "other" fallback -- same
    shape as research/aggregate_query.py's free-text-to-metric mapping.
    Order matters: face-value-split is checked before the plainer "split"
    rule, and bonus/rights before dividend, so a subject naming more than
    one action type report doesn't fall through in the wrong precedence.

    Verified against real Nifty 50 history (ingestion run 2026-09-10):
    older filings abbreviate -- "Agm/Div-Rs.12/- Per Share" (AXISBANK),
    "Rhs 3:7@Prem Rs95/Div185%" (HINDALCO) -- "div" and "rhs" are the same
    dividend/rights actions as their spelled-out modern equivalents, not a
    different type. "div" is checked as a substring rather than a whole
    word because it already covers "dividend" itself (no need for two
    checks) -- the one collision risk, "(Sub-Division)"/"Sub-Division"
    containing "div", is already resolved by fv_split/split being checked
    earlier in this same order."""
    s = subject.strip().lower()
    if "face value" in s and ("split" in s or "sub-division" in s or "subdivision" in s):
        return "fv_split"
    if "bonus" in s:
        return "bonus"
    if "rights" in s or "rhs" in s:
        return "rights"
    if "split" in s or "sub-division" in s or "subdivision" in s:
        return "split"
    if "div" in s:
        return "dividend"
    return "other"


def process_company_corporate_actions(conn: DBConnection, company_id: str) -> str:
    """Classify every not-yet-processed corporate_actions_raw row for one
    company and insert the result into corporate_actions, stamping
    processed_at on the raw row so a re-run only touches what's new.
    Returns a human-readable detail string for the batch audit log."""
    raw_rows = select_unprocessed_corporate_actions_raw(conn, company_id)
    now = utcnow_iso()
    counts: dict[str, int] = {}
    for row in raw_rows:
        action_type = classify_action_type(row["subject"])
        counts[action_type] = counts.get(action_type, 0) + 1
        insert_corporate_action(
            conn,
            raw_id=row["raw_id"],
            company_id=company_id,
            action_type=action_type,
            subject=row["subject"],
            ex_date=row["ex_date"],
            record_date=row["record_date"],
            face_value=row["face_value"],
            classifier_version=CLASSIFIER_VERSION,
            now=now,
        )
        mark_corporate_actions_raw_processed(conn, row["raw_id"], now=now)

    if not raw_rows:
        return "classified=0 (nothing pending)"
    breakdown = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"classified={len(raw_rows)} ({breakdown})"


def reclassify_company_corporate_actions(conn: DBConnection, company_id: str) -> str:
    """Re-run classify_action_type() over EVERY raw row for this company,
    including ones already processed under an older CLASSIFIER_VERSION --
    the counterpart to process_company_corporate_actions' "only what's
    new". Use this after a classify_action_type() rule change (e.g. the
    v1->v2 "div"/"rhs" abbreviation fix) to correct history without
    re-fetching from NSE. insert_corporate_action's upsert-on-raw_id means
    this is safe to call repeatedly and safe to call on rows that were
    already correct (no-op update). Returns how many of this company's
    rows actually changed action_type, not just how many were touched."""
    raw_rows = select_all_corporate_actions_raw(conn, company_id)
    now = utcnow_iso()
    changed = 0
    for row in raw_rows:
        new_type = classify_action_type(row["subject"])
        existing = conn.execute(
            "SELECT action_type FROM corporate_actions WHERE raw_id = ?", (row["raw_id"],)
        ).fetchone()
        if existing is not None and existing["action_type"] == new_type:
            continue
        insert_corporate_action(
            conn,
            raw_id=row["raw_id"],
            company_id=company_id,
            action_type=new_type,
            subject=row["subject"],
            ex_date=row["ex_date"],
            record_date=row["record_date"],
            face_value=row["face_value"],
            classifier_version=CLASSIFIER_VERSION,
            now=now,
        )
        if row["processed_at"] is None:
            mark_corporate_actions_raw_processed(conn, row["raw_id"], now=now)
        changed += 1
    return f"reclassified={len(raw_rows)} changed={changed}"
