"""Repository layer for the Configurable Indicator Framework — every raw SQL
statement `indicators/*.py` and the Settings routes need, so the rule
engine, the config resolver and the evaluation engine hold rules/business
logic only and never call `conn.execute(...)` themselves (same contract
`storage/company_repository.py` establishes for companies/stock_actions).

Two tables, two very different lifecycles:

* `indicator_rule_config` — mutable, user-owned. Upserted/deleted by the
  Settings UI. Keyed by (user_id, rule_id, scope_type, scope_value); NULL
  columns mean "inherit", never "off".
* `indicator_evaluations` — append-only audit trail. Never UPDATEd, never
  DELETEd (same rule as `reconciliation_log`). `result_hash` exists so a
  page refresh that reproduces an identical result doesn't append a
  duplicate row — see `select_latest_indicator_result_hashes()`.

Functions here take already-validated arguments (a `scope_type` the caller
has already checked against indicators/framework.py's `SCOPE_TYPES`, a
`classification` already checked against `CLASSIFICATIONS`); this module
runs the query, it does not re-derive business rules.
"""

from __future__ import annotations

from storage.db_types import DBConnection, Row


# ------------------------------------------------------------------
# User configuration / overrides
# ------------------------------------------------------------------


def select_indicator_configs_for_user(conn: DBConnection, user_id: int) -> list[Row]:
    """Every override this user has, any rule, any scope — the evaluation
    engine resolves the whole set in Python (one query per page render
    rather than one per rule; this table is tiny by construction, at most a
    handful of rows per rule a user has actually touched)."""
    return conn.execute(
        """
        SELECT rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at
        FROM indicator_rule_config
        WHERE user_id = ?
        ORDER BY rule_id, scope_type, scope_value
        """,
        (user_id,),
    ).fetchall()


def select_indicator_configs_for_rule(conn: DBConnection, user_id: int, rule_id: str) -> list[Row]:
    return conn.execute(
        """
        SELECT rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at
        FROM indicator_rule_config
        WHERE user_id = ? AND rule_id = ?
        ORDER BY scope_type, scope_value
        """,
        (user_id, rule_id),
    ).fetchall()


def upsert_indicator_config(
    conn: DBConnection, *, user_id: int, rule_id: str, scope_type: str, scope_value: str,
    enabled: int | None, classification: str | None, thresholds_json: str | None, now: str,
) -> None:
    """One override row per (user, rule, scope). Every field is written as
    given — a caller passing None for `classification` is explicitly saying
    "inherit that field", which is exactly what the UI's "(inherit)" option
    means, so this is a full replace of the row rather than a merge."""
    conn.execute(
        """
        INSERT INTO indicator_rule_config
            (user_id, rule_id, scope_type, scope_value, enabled, classification, thresholds_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, rule_id, scope_type, scope_value) DO UPDATE SET
            enabled = excluded.enabled,
            classification = excluded.classification,
            thresholds_json = excluded.thresholds_json,
            updated_at = excluded.updated_at
        """,
        (user_id, rule_id, scope_type, scope_value, enabled, classification, thresholds_json, now),
    )
    conn.commit()


def delete_indicator_config(
    conn: DBConnection, *, user_id: int, rule_id: str, scope_type: str, scope_value: str
) -> int:
    """"Reset to inherited/default" is a DELETE, not a write of the default
    values — an override row that stored today's system default would
    silently pin the rule if that default ever changed. Returns rowcount so
    the caller can tell "there was nothing to reset" from a real reset."""
    cursor = conn.execute(
        """
        DELETE FROM indicator_rule_config
        WHERE user_id = ? AND rule_id = ? AND scope_type = ? AND scope_value = ?
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
    cursor = conn.execute(
        """
        INSERT INTO indicator_evaluations (
            company_id, user_id, rule_id, rule_version, classification, severity, explanation,
            facts_json, effective_config_json, scope_applied, period_label, provenance,
            result_hash, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, user_id, rule_id, rule_version, classification, severity, explanation,
         facts_json, effective_config_json, scope_applied, period_label, provenance,
         result_hash, evaluated_at),
    )
    conn.commit()
    return cursor.lastrowid


def select_latest_indicator_result_hashes(
    conn: DBConnection, company_id: str, user_id: int | None
) -> dict[str, str]:
    """rule_id -> the result_hash of that rule's most recent audit row for
    this (company, user). The evaluation engine appends only where the new
    hash differs, so viewing a company page repeatedly doesn't inflate the
    trail with identical rows — a *changed* indicator is the auditable
    event, a re-render isn't.

    `user_id IS ?` rather than `= ?` so the signed-out (NULL user_id)
    evaluations form their own comparison set instead of matching nothing.
    """
    rows = conn.execute(
        """
        SELECT rule_id, result_hash FROM indicator_evaluations
        WHERE company_id = ? AND user_id IS ?
          AND evaluation_id IN (
              SELECT MAX(evaluation_id) FROM indicator_evaluations
              WHERE company_id = ? AND user_id IS ?
              GROUP BY rule_id
          )
        """,
        (company_id, user_id, company_id, user_id),
    ).fetchall()
    return {row["rule_id"]: row["result_hash"] for row in rows}


def select_indicator_evaluations(conn: DBConnection, company_id: str, *, limit: int = 200) -> list[Row]:
    """Most recent audit rows for a company, newest first — the reproducibility
    record behind what the company page currently shows."""
    return conn.execute(
        """
        SELECT * FROM indicator_evaluations
        WHERE company_id = ?
        ORDER BY evaluation_id DESC
        LIMIT ?
        """,
        (company_id, limit),
    ).fetchall()
