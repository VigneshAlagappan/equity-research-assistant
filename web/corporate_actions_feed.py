"""Builds the Corporate Actions-tab JSON feed (see
web/static/js/corporate_actions_panel.js) from real data:
storage.company_repository.select_corporate_actions's already-classified
rows (ingestion/corporate_actions.py's classify_action_type() output) --
no reshaping needed beyond picking the fields the table/filter-bar use,
since NSE's own `subject` text already reads naturally as a table's
"Detail" column (e.g. "Bonus 1:1", "Dividend - Rs 13 Per Share") and
select_corporate_actions already orders newest-first.
"""

from __future__ import annotations

from storage.company_repository import select_corporate_actions
from storage.db_types import DBConnection


def build_corporate_actions_feed(conn: DBConnection, company_id: str) -> dict:
    """`years` is the actual span of ex_dates on file (e.g. 20, matching the
    mockup's "LAST 20 YEARS" header), not a fixed lookback window -- a
    company with only 3 years of NSE corporate-actions history shouldn't
    claim 20."""
    rows = select_corporate_actions(conn, company_id)  # newest first
    actions = [
        {
            "ex_date": row["ex_date"],
            "action_type": row["action_type"],
            "subject": row["subject"],
            "record_date": row["record_date"],
            "face_value": row["face_value"],
        }
        for row in rows
    ]
    years = 0
    if actions:
        newest_year = int(actions[0]["ex_date"][:4])
        oldest_year = int(actions[-1]["ex_date"][:4])
        years = newest_year - oldest_year
    return {"actions": actions, "years": years}
