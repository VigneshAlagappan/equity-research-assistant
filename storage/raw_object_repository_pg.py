"""Postgres (Neon) port of `storage/raw_object_repository.py`. Same names/
signatures, targeting a `psycopg2` connection instead of `sqlite3.Connection`.
`raw_objects`/`raw_object_lineage` are first-class Postgres tables (see
`schemas/postgres_schema.sql`'s own comment on why, unlike
`financial_observations`) -- this is a real, permanent Postgres backend,
not a checkpoint/proof-of-concept port.

Translation notes (see `storage/company_repository_pg.py`/
`storage/indicator_repository_pg.py` for the established patterns this
reuses):
- `?` -> `%s`; every query goes through an explicit `conn.cursor()`.
- `cursor.lastrowid` -> `INSERT ... RETURNING object_id`/`RETURNING
  lineage_id` (Postgres `GENERATED ALWAYS AS IDENTITY` PK).
- `entity IS ?` / `period IS ?` -> `entity IS NOT DISTINCT FROM %s` /
  `period IS NOT DISTINCT FROM %s` -- the exact NULL-safe-equality trap
  storage/indicator_repository_pg.py's own docstring already flags:
  SQLite's `IS ?` binds fine whether the parameter is NULL or not,
  Postgres's `IS` predicate only accepts a NULL/TRUE/FALSE/UNKNOWN
  literal, not a bound parameter.
"""

from __future__ import annotations

from datetime import datetime, timezone

from storage.db_types import DBConnection, Row

RAW_PREFIXES = ("companies", "market-data", "macro", "regulatory")
STATES = ("fetched", "stored", "validated", "parsed", "ingested", "reconciled", "failed", "quarantined")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_duplicate(
    conn: DBConnection, *, source: str, entity: str | None, object_type: str,
    period: str | None, content_hash: str,
) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM raw_objects
            WHERE source = %s AND entity IS NOT DISTINCT FROM %s AND object_type = %s
              AND period IS NOT DISTINCT FROM %s AND content_hash = %s
            """,
            (source, entity, object_type, period, content_hash),
        )
        return cur.fetchone()


def insert_raw_object(
    conn: DBConnection, *, source: str, entity: str | None, object_type: str, period: str | None,
    source_url: str | None, raw_prefix: str, s3_key: str, content_hash: str, parser_version: str | None = None,
    state: str = "fetched", fetched_at: str | None = None,
) -> int:
    if raw_prefix not in RAW_PREFIXES:
        raise ValueError(f"insert_raw_object: raw_prefix must be one of {RAW_PREFIXES}, got {raw_prefix!r}")
    if state not in STATES:
        raise ValueError(f"insert_raw_object: state must be one of {STATES}, got {state!r}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_objects (
                source, entity, object_type, period, source_url, raw_prefix, s3_key,
                content_hash, fetched_at, parser_version, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING object_id
            """,
            (
                source, entity, object_type, period, source_url, raw_prefix, s3_key,
                content_hash, fetched_at or _utcnow_iso(), parser_version, state,
            ),
        )
        object_id = cur.fetchone()["object_id"]
    conn.commit()
    return object_id


def update_raw_object_state(
    conn: DBConnection, object_id: int, *, state: str, last_error: str | None = None,
    increment_retry: bool = False, mark_processed: bool = False,
) -> None:
    if state not in STATES:
        raise ValueError(f"update_raw_object_state: state must be one of {STATES}, got {state!r}")
    set_clauses = ["state = %s", "last_error = %s"]
    params: list = [state, last_error]
    if increment_retry:
        set_clauses.append("retry_count = retry_count + 1")
    if mark_processed:
        set_clauses.append("processed_at = %s")
        params.append(_utcnow_iso())
    params.append(object_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE raw_objects SET {', '.join(set_clauses)} WHERE object_id = %s", params)
    conn.commit()


def get_raw_object(conn: DBConnection, object_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_objects WHERE object_id = %s", (object_id,))
        return cur.fetchone()


def list_raw_objects(
    conn: DBConnection, *, source: str | None = None, entity: str | None = None,
    object_type: str | None = None, state: str | None = None,
    period_start: str | None = None, period_end: str | None = None, limit: int = 500,
) -> list[Row]:
    clauses: list[str] = []
    params: list = []
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if entity is not None:
        clauses.append("entity = %s")
        params.append(entity)
    if object_type is not None:
        clauses.append("object_type = %s")
        params.append(object_type)
    if state is not None:
        clauses.append("state = %s")
        params.append(state)
    if period_start is not None:
        clauses.append("period >= %s")
        params.append(period_start)
    if period_end is not None:
        clauses.append("period <= %s")
        params.append(period_end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM raw_objects {where} ORDER BY fetched_at DESC LIMIT %s", params)
        return cur.fetchall()


def list_all_raw_objects(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_objects")
        return cur.fetchall()


def insert_lineage(
    conn: DBConnection, *, object_id: int, derived_store: str, derived_table: str, derived_record_id: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_object_lineage (object_id, derived_store, derived_table, derived_record_id, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING lineage_id
            """,
            (object_id, derived_store, derived_table, derived_record_id, _utcnow_iso()),
        )
        lineage_id = cur.fetchone()["lineage_id"]
    conn.commit()
    return lineage_id


def get_lineage_for_object(conn: DBConnection, object_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_object_lineage WHERE object_id = %s ORDER BY lineage_id", (object_id,))
        return cur.fetchall()


def get_lineage_for_record(
    conn: DBConnection, *, derived_store: str, derived_table: str, derived_record_id: str,
) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ro.* FROM raw_object_lineage l
            JOIN raw_objects ro ON ro.object_id = l.object_id
            WHERE l.derived_store = %s AND l.derived_table = %s AND l.derived_record_id = %s
            ORDER BY l.lineage_id
            """,
            (derived_store, derived_table, derived_record_id),
        )
        return cur.fetchall()
