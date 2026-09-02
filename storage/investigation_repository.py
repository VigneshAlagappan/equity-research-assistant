"""Repository layer for the one-investigation-to-many-companies association
(`investigation_companies`) — every raw SQL statement the "Company ->
Investigations" surface needs, so `web/app.py` and `research/*` never build
that join themselves (same contract `storage/indicator_repository.py` and
`storage/company_repository.py` establish for their own tables).

Why a join table at all, when `investigations.company_ids` already holds a
JSON array: the JSON column answers "which companies is THIS investigation
about?" (a render-time detail, ordered as the question was asked), but it
cannot answer the question the product actually asks on every company page —
"which investigations touch THIS company?" — without a full-table LIKE scan
that would also match substrings (`HDFCBANK` inside `HDFCBANKX`). The join
table makes that an indexed lookup, and does it without duplicating the
investigation record: a cross-company investigation is still exactly one row
in `investigations`, listed under each of its companies.

Written once, at save time, alongside the investigation itself
(storage/repositories.py::save_investigation) — never edited afterwards,
same write-once discipline the rest of the 2E-2H tables follow.
"""

from __future__ import annotations

import json

from storage.db_types import DBConnection, Row


def insert_investigation_companies(conn: DBConnection, investigation_id: str, company_ids: list[str]) -> None:
    """Associate one investigation with every company it covers, preserving
    the order they were asked about. `INSERT OR IGNORE` because
    company_ids can legitimately repeat a company (a caller passing the same
    id twice shouldn't fail the whole save), and because backfilling an
    already-associated investigation must stay idempotent.

    Does not commit — the caller owns the transaction, so the investigation
    row and its associations land together or not at all.
    """
    if not company_ids:
        return
    seen: set[str] = set()
    rows = []
    for position, company_id in enumerate(company_ids):
        if company_id in seen:
            continue
        seen.add(company_id)
        rows.append((investigation_id, company_id, position))
    conn.executemany(
        "INSERT OR IGNORE INTO investigation_companies (investigation_id, company_id, position) VALUES (?, ?, ?)",
        rows,
    )


def select_company_ids_for_investigation(conn: DBConnection, investigation_id: str) -> list[str]:
    return [
        row["company_id"]
        for row in conn.execute(
            "SELECT company_id FROM investigation_companies WHERE investigation_id = ? ORDER BY position, company_id",
            (investigation_id,),
        ).fetchall()
    ]


def select_investigations_for_company(conn: DBConnection, company_id: str) -> list[Row]:
    """Every structured investigation (research/investigation.py) associated
    with this company, newest first — the query behind the company page's
    Investigations section. A cross-company investigation is returned here
    for each of its companies, from the single shared record."""
    return conn.execute(
        """
        SELECT i.*
        FROM investigations AS i
        JOIN investigation_companies AS ic ON ic.investigation_id = i.investigation_id
        WHERE ic.company_id = ?
        ORDER BY i.generated_at DESC
        """,
        (company_id,),
    ).fetchall()


def count_investigation_hypotheses(conn: DBConnection, investigation_id: str) -> int:
    """How many hypotheses this investigation produced — the one number the
    company-page card needs, so the page doesn't fetch every hypothesis row
    of every investigation just to call len() on them."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM investigation_hypotheses WHERE investigation_id = ?", (investigation_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


def select_investigations_missing_company_rows(conn: DBConnection) -> list[Row]:
    """Investigations with no `investigation_companies` rows yet — the input
    to the one-time backfill in storage/database.py. An investigation that
    genuinely has no companies (a purely macro question) is included here and
    simply produces no rows, which is correct and stays cheap: the backfill
    runs once per process start and this query is an index-covered
    anti-join."""
    return conn.execute(
        """
        SELECT investigation_id, company_ids
        FROM investigations
        WHERE investigation_id NOT IN (SELECT investigation_id FROM investigation_companies)
        """
    ).fetchall()


def backfill_investigation_companies(conn: DBConnection) -> int:
    """Populate `investigation_companies` from the pre-existing
    `investigations.company_ids` JSON for any investigation saved before the
    join table existed. Idempotent; returns how many investigations were
    backfilled. Skips a company_id with no `companies` row (the FK would
    reject it) rather than aborting the whole backfill — an investigation
    naming a since-deleted company keeps its other associations."""
    known = {row["company_id"] for row in conn.execute("SELECT company_id FROM companies").fetchall()}
    backfilled = 0
    for row in select_investigations_missing_company_rows(conn):
        try:
            company_ids = json.loads(row["company_ids"] or "[]")
        except (TypeError, ValueError):
            continue
        usable = [c for c in company_ids if isinstance(c, str) and c in known]
        if not usable:
            continue
        insert_investigation_companies(conn, row["investigation_id"], usable)
        backfilled += 1
    return backfilled
