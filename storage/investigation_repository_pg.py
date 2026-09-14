"""Postgres (Neon) port of `storage/investigation_repository.py`.

Checkpoint port: every function in `investigation_repository.py`, same
names/signatures, targeting a `psycopg2` connection (from
`storage.database.init_postgres_db()`) instead of `sqlite3.Connection`. All
three tables it touches (`investigation_companies`, `investigations`,
`investigation_hypotheses`) and the `companies` table `backfill_...` reads
are part of `schemas/postgres_schema.sql`, so all 6 functions are ported --
none skipped.

Translation notes (see `storage/company_repository_pg.py` /
`storage/repositories_pg.py` for the established patterns this reuses):
- `?` -> `%s`; every query goes through an explicit `conn.cursor()`.
- `insert_investigation_companies()`'s bulk `INSERT OR IGNORE` via
  `conn.executemany()` -> `psycopg2.extras.execute_values()` with
  `ON CONFLICT (investigation_id, company_id) DO NOTHING` (the table's real
  PK, per `schemas/postgres_schema.sql`) -- never bare `executemany()`,
  whose rowcount/ignore semantics aren't reliable across drivers for this
  use, per the established checkpoint guidance. Like the original, this
  function does not commit -- the caller owns the transaction so an
  investigation row and its associations land together.
- No `IS ?` NULL-safety idiom anywhere in this file -- checked; every
  comparison here is a plain non-nullable equality/JOIN.
"""

from __future__ import annotations

import json

from psycopg2.extras import execute_values

from storage.db_types import DBConnection, Row


def insert_investigation_companies(conn: DBConnection, investigation_id: str, company_ids: list[str]) -> None:
    """Associate one investigation with every company it covers, preserving
    the order they were asked about. `ON CONFLICT ... DO NOTHING` because
    company_ids can legitimately repeat a company (a caller passing the same
    id twice shouldn't fail the whole save), and because backfilling an
    already-associated investigation must stay idempotent.

    Does not commit -- the caller owns the transaction, so the investigation
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
    cursor = conn.cursor()
    execute_values(
        cursor,
        """
        INSERT INTO investigation_companies (investigation_id, company_id, position)
        VALUES %s
        ON CONFLICT (investigation_id, company_id) DO NOTHING
        """,
        rows,
    )


def select_company_ids_for_investigation(conn: DBConnection, investigation_id: str) -> list[str]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT company_id FROM investigation_companies WHERE investigation_id = %s ORDER BY position, company_id",
        (investigation_id,),
    )
    return [row["company_id"] for row in cursor.fetchall()]


def select_investigations_for_company(conn: DBConnection, company_id: str) -> list[Row]:
    """Every structured investigation (research/investigation.py) associated
    with this company, newest first -- the query behind the company page's
    Investigations section. A cross-company investigation is returned here
    for each of its companies, from the single shared record."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT i.*
        FROM investigations AS i
        JOIN investigation_companies AS ic ON ic.investigation_id = i.investigation_id
        WHERE ic.company_id = %s
        ORDER BY i.generated_at DESC
        """,
        (company_id,),
    )
    return cursor.fetchall()


def count_investigation_hypotheses(conn: DBConnection, investigation_id: str) -> int:
    """How many hypotheses this investigation produced -- the one number the
    company-page card needs, so the page doesn't fetch every hypothesis row
    of every investigation just to call len() on them."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS n FROM investigation_hypotheses WHERE investigation_id = %s", (investigation_id,)
    )
    row = cursor.fetchone()
    return int(row["n"]) if row else 0


def select_investigations_missing_company_rows(conn: DBConnection) -> list[Row]:
    """Investigations with no `investigation_companies` rows yet -- the input
    to the one-time backfill in storage/database.py. An investigation that
    genuinely has no companies (a purely macro question) is included here and
    simply produces no rows, which is correct and stays cheap: the backfill
    runs once per process start and this query is an index-covered
    anti-join."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT investigation_id, company_ids
        FROM investigations
        WHERE investigation_id NOT IN (SELECT investigation_id FROM investigation_companies)
        """
    )
    return cursor.fetchall()


def backfill_investigation_companies(conn: DBConnection) -> int:
    """Populate `investigation_companies` from the pre-existing
    `investigations.company_ids` JSON for any investigation saved before the
    join table existed. Idempotent; returns how many investigations were
    backfilled. Skips a company_id with no `companies` row (the FK would
    reject it) rather than aborting the whole backfill -- an investigation
    naming a since-deleted company keeps its other associations."""
    cursor = conn.cursor()
    cursor.execute("SELECT company_id FROM companies")
    known = {row["company_id"] for row in cursor.fetchall()}
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
