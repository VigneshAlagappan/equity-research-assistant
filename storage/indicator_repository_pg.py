"""Postgres (Neon) port of `storage/indicator_repository.py`.

Checkpoint port: every function in `indicator_repository.py`, same names/
signatures, targeting a `psycopg2` connection (from
`storage.database.init_postgres_db()`) instead of `sqlite3.Connection`.
Both tables it touches (`indicator_rule_config`, `indicator_evaluations`)
are part of `schemas/postgres_schema.sql`, so all 7 functions are ported --
none skipped.

Translation notes (see `storage/company_repository_pg.py` /
`storage/repositories_pg.py` for the established patterns this reuses):
- `?` -> `%s`; every query goes through an explicit `conn.cursor()`.
- `ON CONFLICT(...) DO UPDATE SET col = excluded.col` -> Postgres's
  `ON CONFLICT (...) DO UPDATE SET col = EXCLUDED.col`.
- `cursor.lastrowid` -> `INSERT ... RETURNING evaluation_id` (Postgres
  `GENERATED ALWAYS AS IDENTITY` PK).
- `cursor.rowcount` for a DELETE's affected-row count works identically
  against psycopg2 -- no translation needed there.
- **`user_id IS ?` in `select_latest_indicator_result_hashes()` is exactly
  the NULL-safe-equality trap flagged in the porting instructions**: SQLite's
  `IS ?` binds fine whether the parameter is NULL or not, but Postgres's `IS`
  predicate only accepts NULL/TRUE/FALSE/UNKNOWN literally -- `col IS %s`
  bound to a non-NULL int (a signed-in user_id) is a syntax error on Neon.
  Both occurrences (the outer WHERE and the correlated subquery's WHERE) are
  ported to `user_id IS NOT DISTINCT FROM %s`, which is NULL-safe in both
  directions and accepts a bound parameter of either NULL or a real value.
"""

from __future__ import annotations

from storage.db_types import DBConnection, Row


# ------------------------------------------------------------------
# User configuration / overrides
# ------------------------------------------------------------------


def select_indicator_configs_for_user(conn: DBConnection, user_id: int) -> list[Row]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at
        FROM indicator_rule_config
        WHERE user_id = %s
        ORDER BY rule_id, scope_type, scope_value
        """,
        (user_id,),
    )
    return cursor.fetchall()


def select_indicator_configs_for_rule(conn: DBConnection, user_id: int, rule_id: str) -> list[Row]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at
        FROM indicator_rule_config
        WHERE user_id = %s AND rule_id = %s
        ORDER BY scope_type, scope_value
        """,
        (user_id, rule_id),
    )
    return cursor.fetchall()


def upsert_indicator_config(
    conn: DBConnection, *, user_id: int, rule_id: str, scope_type: str, scope_value: str,
    enabled: int | None, classification: str | None, thresholds_json: str | None, now: str,
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO indicator_rule_config
            (user_id, rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, rule_id, scope_type, scope_value) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            classification = EXCLUDED.classification,
            thresholds_json = EXCLUDED.thresholds_json,
            updated_at = EXCLUDED.updated_at
        """,
        (user_id, rule_id, scope_type, scope_value, enabled, classification, thresholds_json, now),
    )
    conn.commit()


def delete_indicator_config(
    conn: DBConnection, *, user_id: int, rule_id: str, scope_type: str, scope_value: str
) -> int:
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM indicator_rule_config
        WHERE user_id = %s AND rule_id = %s AND scope_type = %s AND scope_value = %s
        """,
        (user_id, rule_id, scope_type, scope_value),
    )
    conn.commit()
    return cursor.rowcount


# ------------------------------------------------------------------
# Evaluation audit trail (append-only)
# ------------------------------------------------------------------


def insert_indicator_evaluation(
    conn: DBConnection, *, company_id: str, user_id: int | None, rule_id: str, rule_version: str,
    classification: str, severity: str, explanation: str, facts_json: str, effective_config_json: str,
    scope_applied: str, period_label: str | None, provenance: str | None, result_hash: str, evaluated_at: str,
) -> int:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO indicator_evaluations (
            company_id, user_id, rule_id, rule_version, classification, severity, explanation,
            facts_json, effective_config_json, scope_applied, period_label, provenance,
            result_hash, evaluated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING evaluation_id
        """,
        (company_id, user_id, rule_id, rule_version, classification, severity, explanation,
         facts_json, effective_config_json, scope_applied, period_label, provenance,
         result_hash, evaluated_at),
    )
    row = cursor.fetchone()
    conn.commit()
    return row["evaluation_id"] if row else None


def select_latest_indicator_result_hashes(
    conn: DBConnection, company_id: str, user_id: int | None
) -> dict[str, str]:
    """rule_id -> the result_hash of that rule's most recent audit row for
    this (company, user). `user_id IS NOT DISTINCT FROM %s` (ported from
    SQLite's `user_id IS ?`) so the signed-out (NULL user_id) evaluations
    form their own comparison set instead of matching nothing.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT rule_id, result_hash FROM indicator_evaluations
        WHERE company_id = %s AND user_id IS NOT DISTINCT FROM %s
          AND evaluation_id IN (
              SELECT MAX(evaluation_id) FROM indicator_evaluations
              WHERE company_id = %s AND user_id IS NOT DISTINCT FROM %s
              GROUP BY rule_id
          )
        """,
        (company_id, user_id, company_id, user_id),
    )
    rows = cursor.fetchall()
    return {row["rule_id"]: row["result_hash"] for row in rows}


def select_indicator_evaluations(conn: DBConnection, company_id: str, *, limit: int = 200) -> list[Row]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM indicator_evaluations
        WHERE company_id = %s
        ORDER BY evaluation_id DESC
        LIMIT %s
        """,
        (company_id, limit),
    )
    return cursor.fetchall()
