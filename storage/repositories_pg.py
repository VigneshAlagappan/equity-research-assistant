"""Postgres (Neon) port of `storage/repositories.py`.

Checkpoint-3 port, extended 2026-09-13: every function in `repositories.py`
is now ported here, targeting a `psycopg2` connection (from `storage.
database.init_postgres_db()`) instead of `sqlite3.Connection`. Financial_
observations/reconciliation_log and the 7 audit/observability tables
(batch_job_runs/items, dataset_events, worker_processing_log, retrieval_
diagnostics, llm_call_log, ingestion_queue_items) were added to `schemas/
postgres_schema.sql` on the same date (previously excluded citing Neon's
free-tier storage cap, which production had already grown past by then --
see this file's `reconcile()`/`insert_financial_observations()` and
`docs/ADR/021` for the fuller history) -- `storage.repositories` is now a
clean wholesale swap (`storage/backend_bootstrap.py`), no hybrid module.

A few functions used to touch a table/column that existed in `schemas/
sqlite_schema.sql` but hadn't been added to `schemas/postgres_schema.sql`
yet -- both gaps below are closed now (2026-09-13), verified directly
against real Neon (not just import-time/schema-file checks):
    - `replace_document_chunks()` -- FTS5->tsvector gap is closed
      (`document_chunks.search_vector`, a GIN-indexed tsvector column,
      exists on Neon -- see schemas/postgres_schema.sql). This function
      writes both `document_chunks` and its `search_vector` column on insert.
    - `search_document_chunks()` -- implemented in `storage/fact_store_
      pg.py` (not here, mirroring where the SQLite original's `default_
      fact_store()` sources it from `repositories.py`), querying `document_
      chunks.search_vector` via `ts_rank`.
    - `hide_investigation`/`unhide_investigation`/`soft_delete_investigation`/
      `list_investigations`, and the equivalent `generated_reports` quartet
      -- `hidden_at`/`deleted_at` exist on both tables on Neon (schemas/
      postgres_schema.sql), and every one of these functions has been run
      directly against real production data (hide + unhide round-tripped
      on a real investigation and a real generated_reports row) with no
      error.

Translation notes (see also storage/company_repository_pg.py's own header,
and each function's own comments where relevant):
- `?` -> `%s`; every query goes through an explicit `conn.cursor()`.
- `INSERT OR IGNORE` -> `INSERT ... ON CONFLICT (...) DO NOTHING`.
- `ON CONFLICT(...) DO UPDATE SET col = excluded.col` ports to Postgres's
  `ON CONFLICT (...) DO UPDATE SET col = EXCLUDED.col` nearly verbatim.
- No `cursor.lastrowid` -- `INSERT ... RETURNING <pk or *>` instead of the
  SQLite insert-then-reselect two-step.
- **New pattern this file introduces, not exercised by company_repository.py**:
  SQLite's `col IS ?` (a NULL-safe equality this codebase relies on
  throughout for nullable `quarter`/`statement_type`/`region`/`company_id`
  scoping columns -- a bound parameter of `None` naturally matches `NULL`, a
  bound non-NULL value matches equality) has NO Postgres equivalent using
  the literal `IS` keyword: Postgres's `IS` predicate only accepts
  NULL/TRUE/FALSE/UNKNOWN on its right-hand side, so `col IS %s` bound to a
  non-NULL string is a syntax error in Postgres (verified against real
  Neon: `SELECT 1 WHERE %s IS %s` with two non-NULL params raises
  `SyntaxError`). The fix, also verified against real Neon for both NULL
  and non-NULL bindings, is Postgres's `IS NOT DISTINCT FROM` operator --
  a true NULL-safe equality regardless of which side is NULL. Every
  `col IS ?` in the original file becomes `col IS NOT DISTINCT FROM %s`
  here.
- No `strftime()`/date-string-manipulation SQL was found anywhere in
  `repositories.py` to port -- every date/period comparison in this module
  is either a plain ISO-text equality/range comparison or (for
  fiscal-year-quarter ordering) a plain string comparison/concatenation
  (e.g. `fiscal_year || quarter`), which ports to Postgres's `||` text
  concatenation operator unchanged.
- Window functions (`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`)
  port to Postgres unchanged -- verified against real Neon.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from psycopg2.extras import execute_values

from storage.db_types import DBConnection, Row

NORMALIZATION_VERSION = "v1"
XBRL_SOURCE_ID = "nse"


def _utcnow_iso() -> str:
    """Same shape as storage.database.utcnow_iso() -- not imported from
    there to keep this module's only storage.* dependency being db_types,
    same discipline company_repository_pg.py already follows."""
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Financial observations / reconciliation
# ------------------------------------------------------------------


def insert_financial_observations(conn: DBConnection, observations: Iterable) -> list[int]:
    """Insert each observation as a new row. Returns the assigned
    observation_ids. `RETURNING observation_id` replaces SQLite's
    `cursor.lastrowid` (psycopg2 cursors have none)."""
    now = _utcnow_iso()
    ids: list[int] = []
    with conn.cursor() as cur:
        for obs in observations:
            cur.execute(
                """
                INSERT INTO financial_observations (
                    company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                    value, unit, currency, source, source_document_id, source_file, source_url,
                    retrieved_at, parser_version, normalization_version, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
                RETURNING observation_id
                """,
                (
                    obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter,
                    obs.statement_type, obs.value, obs.unit, obs.currency, obs.source, obs.source_file,
                    obs.source_url, obs.retrieved_at or now, obs.parser_version, NORMALIZATION_VERSION, now,
                ),
            )
            ids.append(cur.fetchone()["observation_id"])
    conn.commit()
    return ids


def _period_is_xbrl_migrated(
    conn: DBConnection,
    company_id: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None,
    statement_type: str | None,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM financial_observations
            WHERE company_id = %s AND source = %s AND period_type = %s
              AND fiscal_year = %s AND quarter IS NOT DISTINCT FROM %s AND statement_type IS NOT DISTINCT FROM %s
            LIMIT 1
            """,
            (company_id, XBRL_SOURCE_ID, period_type, fiscal_year, quarter, statement_type),
        )
        return cur.fetchone() is not None


def reconcile(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None,
    statement_type: str | None,
) -> int | None:
    """Postgres port of repositories.reconcile() -- see that function's own
    docstring for the full behavioral contract (XBRL migration carve-out,
    trust_rank tiebreak, stale-canonical-row deletion). reconciliation_log
    writes (the audit trail of considered/chosen observations) are ported
    too now that table exists in Postgres (schemas/postgres_schema.sql,
    2026-09-13) -- see this file's own module docstring / docs/ADR/021 for
    the history of why it didn't before."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fo.observation_id, fo.value, fo.unit, fo.source, fo.retrieved_at, s.trust_rank
            FROM financial_observations fo
            JOIN sources s ON s.source_id = fo.source
            WHERE fo.company_id = %s AND fo.metric_key = %s AND fo.period_type = %s
              AND fo.fiscal_year = %s AND fo.quarter IS NOT DISTINCT FROM %s
              AND fo.statement_type IS NOT DISTINCT FROM %s
            ORDER BY fo.retrieved_at ASC, fo.observation_id ASC
            """,
            (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
        )
        rows = cur.fetchall()

    def _delete_stale_canonical_row() -> None:
        with conn.cursor() as cur2:
            cur2.execute(
                """
                DELETE FROM canonical_financials
                WHERE company_id = %s AND metric_key = %s AND period_type = %s
                  AND fiscal_year = %s AND quarter IS NOT DISTINCT FROM %s
                  AND statement_type IS NOT DISTINCT FROM %s
                """,
                (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
            )
        conn.commit()

    if not rows:
        return None

    latest_per_source: dict[str, Row] = {}
    for row in rows:
        latest_per_source[row["source"]] = row
    candidates = list(latest_per_source.values())
    all_candidates = candidates  # kept for the audit-log loop below even once `candidates` is narrowed

    migrated = _period_is_xbrl_migrated(conn, company_id, period_type, fiscal_year, quarter, statement_type)
    if migrated:
        xbrl_candidates = [row for row in candidates if row["source"] == XBRL_SOURCE_ID]
        if not xbrl_candidates:
            # Period is on XBRL now, but this metric wasn't in the filing —
            # blank, not backfilled from whatever legacy candidates exist.
            _delete_stale_canonical_row()
            now = _utcnow_iso()
            with conn.cursor() as cur3:
                for row in candidates:
                    cur3.execute(
                        """
                        INSERT INTO reconciliation_log (canonical_id, observation_id, considered_at, was_chosen, note)
                        VALUES (NULL, %s, %s, 0, %s)
                        """,
                        (
                            row["observation_id"], now,
                            f"not chosen (source={row['source']}): period migrated to validated "
                            f"{XBRL_SOURCE_ID!r} XBRL and this metric wasn't in the filing — left blank, not legacy-filled",
                        ),
                    )
            conn.commit()
            return None
        candidates = xbrl_candidates

    def sort_key(row: Row) -> tuple[int, str, int]:
        rank = row["trust_rank"] if row["trust_rank"] is not None else 999
        return (rank, row["retrieved_at"], row["observation_id"])

    chosen = min(candidates, key=sort_key)
    if migrated:
        reason = f"source '{chosen['source']}' — period validated on NSE XBRL"
    else:
        reason = (
            "only source available"
            if len(candidates) == 1
            else f"source '{chosen['source']}' preferred by trust_rank"
        )

    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_id FROM canonical_financials
            WHERE company_id = %s AND metric_key = %s AND period_type = %s
              AND fiscal_year = %s AND quarter IS NOT DISTINCT FROM %s
              AND statement_type IS NOT DISTINCT FROM %s
            """,
            (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
        )
        existing = cur.fetchone()

        if existing is None:
            cur.execute(
                """
                INSERT INTO canonical_financials (
                    company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                    canonical_value, unit, chosen_observation_id, reconciliation_reason,
                    normalization_version, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING canonical_id
                """,
                (
                    company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                    chosen["value"], chosen["unit"], chosen["observation_id"], reason,
                    NORMALIZATION_VERSION, now,
                ),
            )
            canonical_id = cur.fetchone()["canonical_id"]
        else:
            canonical_id = existing["canonical_id"]
            cur.execute(
                """
                UPDATE canonical_financials SET
                    canonical_value = %s, unit = %s, chosen_observation_id = %s,
                    reconciliation_reason = %s, normalization_version = %s, decided_at = %s
                WHERE canonical_id = %s
                """,
                (chosen["value"], chosen["unit"], chosen["observation_id"], reason,
                 NORMALIZATION_VERSION, now, canonical_id),
            )

        for row in all_candidates:
            was_chosen = row["observation_id"] == chosen["observation_id"]
            note_for_rejected = (
                f"not chosen (source={row['source']}, trust_rank={row['trust_rank']}): "
                f"period validated on NSE XBRL — legacy sources aren't eligible for this period"
                if migrated and row["source"] != XBRL_SOURCE_ID
                else None
            )
            cur.execute(
                """
                INSERT INTO reconciliation_log (canonical_id, observation_id, considered_at, was_chosen, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    canonical_id, row["observation_id"], now, int(was_chosen),
                    reason if was_chosen else (
                        note_for_rejected or f"not chosen (source={row['source']}, trust_rank={row['trust_rank']})"
                    ),
                ),
            )
    conn.commit()
    return canonical_id


def list_reconciliation_log(
    conn: DBConnection,
    *,
    company_id: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> list[Row]:
    """Postgres port of repositories.list_reconciliation_log() -- see that
    function's own docstring for the full behavioral contract (why the
    join is through financial_observations, not canonical_financials)."""
    query = """
        SELECT rl.log_id, rl.considered_at, rl.was_chosen, rl.note,
               fo.company_id, fo.metric_key, fo.period_type, fo.fiscal_year,
               fo.quarter, fo.statement_type, fo.source
        FROM reconciliation_log rl
        JOIN financial_observations fo ON fo.observation_id = rl.observation_id
        WHERE 1=1
    """
    params: list[object] = []
    if company_id:
        query += " AND fo.company_id = %s"
        params.append(company_id)
    if source:
        query += " AND fo.source = %s"
        params.append(source)
    query += " ORDER BY rl.considered_at DESC, rl.log_id DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def list_reconciliation_log_by_company(
    conn: DBConnection, company_ids: Iterable[str], *, limit_per_company: int = 20
) -> dict[str, list[Row]]:
    """Postgres port of repositories.list_reconciliation_log_by_company() --
    see that function's own docstring for the full behavioral contract."""
    ids = list(company_ids)
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT rl.log_id, rl.considered_at, rl.was_chosen, rl.note,
                       fo.company_id, fo.metric_key, fo.period_type, fo.fiscal_year,
                       fo.quarter, fo.statement_type, fo.source,
                       ROW_NUMBER() OVER (
                           PARTITION BY fo.company_id ORDER BY rl.considered_at DESC, rl.log_id DESC
                       ) AS rn
                FROM reconciliation_log rl
                JOIN financial_observations fo ON fo.observation_id = rl.observation_id
                WHERE fo.company_id IN ({placeholders})
            ) sub
            WHERE rn <= %s
            ORDER BY company_id, considered_at DESC
            """,
            (*ids, limit_per_company),
        )
        rows = cur.fetchall()
    by_company: dict[str, list[Row]] = {}
    for row in rows:
        by_company.setdefault(row["company_id"], []).append(row)
    return by_company


def get_canonical_value(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None = None,
    statement_type: str | None = "consolidated",
) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM canonical_financials
            WHERE company_id = %s AND metric_key = %s AND period_type = %s
              AND fiscal_year = %s AND quarter IS NOT DISTINCT FROM %s
              AND statement_type IS NOT DISTINCT FROM %s
            """,
            (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
        )
        return cur.fetchone()


def get_canonical_series(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM canonical_financials
            WHERE company_id = %s AND metric_key = %s AND period_type = %s
              AND statement_type IS NOT DISTINCT FROM %s
            ORDER BY fiscal_year ASC, quarter ASC
            """,
            (company_id, metric_key, period_type, statement_type),
        )
        return cur.fetchall()


def get_canonical_series_provenance(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> list[Row]:
    """Postgres port of storage.repositories.get_canonical_series_provenance
    -- see that docstring for the full rationale (web/charts_feed.py's
    XBRL-vs-NSE-PDF provenance tag)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cf.fiscal_year, cf.quarter, fo.source AS source, d.parser_version AS parser_version
            FROM canonical_financials cf
            LEFT JOIN financial_observations fo ON fo.observation_id = cf.chosen_observation_id
            LEFT JOIN documents d ON d.document_id = fo.source_document_id
            WHERE cf.company_id = %s AND cf.metric_key = %s AND cf.period_type = %s
              AND cf.statement_type IS NOT DISTINCT FROM %s
            ORDER BY cf.fiscal_year ASC, cf.quarter ASC
            """,
            (company_id, metric_key, period_type, statement_type),
        )
        return cur.fetchall()


def company_has_canonical_financials(conn: DBConnection, company_id: str) -> bool:
    """Postgres port of storage.repositories.company_has_canonical_financials
    -- see that docstring."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM canonical_financials WHERE company_id = %s LIMIT 1", (company_id,))
        return cur.fetchone() is not None


def get_available_statement_types(conn: DBConnection, company_id: str) -> set[str]:
    """Postgres port of storage.repositories.get_available_statement_types
    -- see that docstring."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT statement_type FROM canonical_financials WHERE company_id = %s", (company_id,))
        return {row["statement_type"] for row in cur.fetchall() if row["statement_type"]}


def list_canonical_financials_for_companies(conn: DBConnection, company_ids: list[str]) -> list[Row]:
    """LEFT JOIN pattern, verified against real Neon."""
    if not company_ids:
        return []
    placeholders = ",".join(["%s"] * len(company_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT cf.company_id, cf.metric_key, cf.period_type, cf.fiscal_year, cf.quarter,
                   cf.statement_type, cf.canonical_value, cf.unit, cf.decided_at,
                   md.display_name, md.category
            FROM canonical_financials cf
            LEFT JOIN metrics_dictionary md ON md.metric_key = cf.metric_key
            WHERE cf.company_id IN ({placeholders})
            """,
            company_ids,
        )
        return cur.fetchall()


def list_latest_shares_outstanding(conn: DBConnection) -> dict[str, tuple[float, str]]:
    """ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ... DESC) -- verified
    against real Neon, including that Postgres, like SQLite, sorts NULL
    last in a DESC ordering (quarter IS NULL for an annual row) so this
    ports with no behavioral change."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, canonical_value, fiscal_year FROM (
                SELECT company_id, canonical_value, fiscal_year,
                       ROW_NUMBER() OVER (
                           PARTITION BY company_id ORDER BY fiscal_year DESC, quarter DESC
                       ) AS rn
                FROM canonical_financials
                WHERE metric_key = 'shares_outstanding' AND statement_type = 'consolidated'
            ) sub
            WHERE rn = 1
            """
        )
        rows = cur.fetchall()
    return {row["company_id"]: (row["canonical_value"], row["fiscal_year"]) for row in rows}


def seed_metric_vocabulary(conn: DBConnection, metrics: Iterable[tuple], aliases: Iterable[tuple]) -> None:
    metrics = list(metrics)
    aliases = list(aliases)
    with conn.cursor() as cur:
        if metrics:
            execute_values(
                cur,
                "INSERT INTO metrics_dictionary (metric_key, display_name, category, applicable_sectors, default_unit) "
                "VALUES %s ON CONFLICT (metric_key) DO NOTHING",
                metrics,
            )
        if aliases:
            execute_values(
                cur,
                "INSERT INTO metric_aliases (source, raw_label, metric_key) VALUES %s "
                "ON CONFLICT (source, raw_label) DO NOTHING",
                aliases,
            )
    conn.commit()


def get_metric_key_for_alias(conn: DBConnection, source: str, raw_label: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT metric_key FROM metric_aliases WHERE source = %s AND raw_label = %s", (source, raw_label))
        return cur.fetchone()


def get_metric_dictionary_entry(conn: DBConnection, metric_key: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM metrics_dictionary WHERE metric_key = %s", (metric_key,))
        return cur.fetchone()


def compute_reconciliation_keys(conn: DBConnection, observations: Iterable) -> list[tuple]:
    observations = list(observations)
    keys = {
        (obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter, obs.statement_type)
        for obs in observations
    }

    period_scopes = {
        (obs.company_id, obs.period_type, obs.fiscal_year, obs.quarter, obs.statement_type)
        for obs in observations if obs.source == XBRL_SOURCE_ID
    }
    with conn.cursor() as cur:
        for company_id, period_type, fiscal_year, quarter, statement_type in period_scopes:
            cur.execute(
                """
                SELECT DISTINCT metric_key FROM financial_observations
                WHERE company_id = %s AND period_type = %s AND fiscal_year = %s
                  AND quarter IS NOT DISTINCT FROM %s AND statement_type IS NOT DISTINCT FROM %s
                """,
                (company_id, period_type, fiscal_year, quarter, statement_type),
            )
            for row in cur.fetchall():
                keys.add((company_id, row["metric_key"], period_type, fiscal_year, quarter, statement_type))

    return list(keys)


def reconcile_batch(conn: DBConnection, observations: Iterable) -> int:
    keys = compute_reconciliation_keys(conn, observations)
    return sum(1 for key in keys if reconcile(conn, *key) is not None)


def reconcile_company(conn: DBConnection, company_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT metric_key, period_type, fiscal_year, quarter, statement_type
            FROM financial_observations WHERE company_id = %s
            """,
            (company_id,),
        )
        keys = cur.fetchall()
    return sum(
        1
        for row in keys
        if reconcile(
            conn, company_id, row["metric_key"], row["period_type"],
            row["fiscal_year"], row["quarter"], row["statement_type"],
        )
        is not None
    )


def list_xbrl_migration_status(main_conn: DBConnection, financial_obs_conn: DBConnection) -> list[dict]:
    """Postgres port of repositories.list_xbrl_migration_status() -- see
    that function's own docstring for the full behavioral contract. Still
    takes two connection arguments for call-site compatibility with the
    SQLite version (web/app.py passes (db, logs_db) either way), but under
    Postgres both `companies` and `financial_observations` now live in the
    same database (schemas/postgres_schema.sql, 2026-09-13) -- `financial_
    obs_conn` is typically the exact same connection as `main_conn` here,
    not a second SQLite one."""
    with financial_obs_conn.cursor() as fo_cur:
        fo_cur.execute(
            """
            SELECT company_id,
                   MAX(CASE WHEN source = 'nse' THEN fiscal_year || quarter END) AS latest_xbrl_period,
                   MAX(fiscal_year || quarter) AS latest_any_period
            FROM financial_observations
            WHERE period_type = 'quarterly'
            GROUP BY company_id
            """
        )
        coverage_rows = fo_cur.fetchall()
    coverage_by_company = {row["company_id"]: row for row in coverage_rows}

    with main_conn.cursor() as main_cur:
        main_cur.execute(
            """
            SELECT company_id, display_name, nse_symbol FROM companies
            WHERE nse_symbol IS NOT NULL AND nse_symbol != '' AND status = 'active'
            """
        )
        companies = main_cur.fetchall()

    _STATUS_ORDER = {"pending": 0, "not_started": 1, "no_data": 2, "up_to_date": 3}
    results: list[dict] = []
    for company in companies:
        coverage = coverage_by_company.get(company["company_id"])
        latest_xbrl = coverage["latest_xbrl_period"] if coverage else None
        latest_any = coverage["latest_any_period"] if coverage else None
        if latest_any is None:
            migration_status = "no_data"
        elif latest_xbrl is None:
            migration_status = "not_started"
        elif latest_xbrl < latest_any:
            migration_status = "pending"
        else:
            migration_status = "up_to_date"
        results.append(
            {
                "company_id": company["company_id"],
                "display_name": company["display_name"],
                "nse_symbol": company["nse_symbol"],
                "latest_xbrl_period": latest_xbrl,
                "latest_legacy_period": latest_any,
                "migration_status": migration_status,
            }
        )
    results.sort(key=lambda r: (_STATUS_ORDER[r["migration_status"]], r["display_name"] or ""))
    return results


def list_sec_edgar_migration_status(main_conn: DBConnection, financial_obs_conn: DBConnection) -> list[dict]:
    """Postgres port of repositories.list_sec_edgar_migration_status() --
    see list_xbrl_migration_status() above and that function's own
    docstring for the full behavioral contract; same two-connection-for-
    call-site-compatibility shape."""
    with financial_obs_conn.cursor() as fo_cur:
        fo_cur.execute(
            """
            SELECT company_id,
                   MAX(CASE WHEN source = 'sec_edgar' THEN fiscal_year || quarter END) AS latest_edgar_period,
                   MAX(fiscal_year || quarter) AS latest_any_period
            FROM financial_observations
            WHERE period_type = 'quarterly'
            GROUP BY company_id
            """
        )
        coverage_rows = fo_cur.fetchall()
    coverage_by_company = {row["company_id"]: row for row in coverage_rows}

    with main_conn.cursor() as main_cur:
        main_cur.execute(
            "SELECT company_id, display_name FROM companies WHERE country = 'US' AND status = 'active'"
        )
        companies = main_cur.fetchall()

    _STATUS_ORDER = {"pending": 0, "not_started": 1, "no_data": 2, "up_to_date": 3}
    results: list[dict] = []
    for company in companies:
        coverage = coverage_by_company.get(company["company_id"])
        latest_edgar = coverage["latest_edgar_period"] if coverage else None
        latest_any = coverage["latest_any_period"] if coverage else None
        if latest_any is None:
            migration_status = "no_data"
        elif latest_edgar is None:
            migration_status = "not_started"
        elif latest_edgar < latest_any:
            migration_status = "pending"
        else:
            migration_status = "up_to_date"
        results.append(
            {
                "company_id": company["company_id"],
                "display_name": company["display_name"],
                "latest_edgar_period": latest_edgar,
                "latest_legacy_period": latest_any,
                "migration_status": migration_status,
            }
        )
    results.sort(key=lambda r: (_STATUS_ORDER[r["migration_status"]], r["display_name"] or ""))
    return results


# ------------------------------------------------------------------
# Watchlist
# ------------------------------------------------------------------

WATCHLIST_ITEM_TYPES = ("company", "thread")


def add_watchlist_item(conn: DBConnection, item_type: str, item_ref: str) -> int:
    if item_type not in WATCHLIST_ITEM_TYPES:
        raise ValueError(f"item_type must be one of {WATCHLIST_ITEM_TYPES}, got {item_type!r}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO watchlist_items (item_type, item_ref, pinned_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (item_type, item_ref) DO NOTHING RETURNING item_id",
            (item_type, item_ref, _utcnow_iso()),
        )
        row = cur.fetchone()
        if row is not None:
            conn.commit()
            return row["item_id"]
        cur.execute(
            "SELECT item_id FROM watchlist_items WHERE item_type = %s AND item_ref = %s", (item_type, item_ref)
        )
        item_id = cur.fetchone()["item_id"]
    conn.commit()
    return item_id


def remove_watchlist_item(conn: DBConnection, item_type: str, item_ref: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM watchlist_items WHERE item_type = %s AND item_ref = %s", (item_type, item_ref))
    conn.commit()


def list_watchlist_items(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM watchlist_items ORDER BY pinned_at DESC")
        return cur.fetchall()


def is_watchlisted(conn: DBConnection, item_type: str, item_ref: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM watchlist_items WHERE item_type = %s AND item_ref = %s", (item_type, item_ref))
        return cur.fetchone() is not None


# ------------------------------------------------------------------
# Company news
# ------------------------------------------------------------------

NEWS_RETENTION_DAYS = 49


def save_company_news(conn: DBConnection, company_id: str, items: list[dict]) -> None:
    now = _utcnow_iso()
    rows = [(company_id, i["title"], i["link"], i.get("source"), i.get("published_at"), now) for i in items]
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                "INSERT INTO company_news (company_id, title, link, source, published_at, fetched_at) VALUES %s "
                "ON CONFLICT (company_id, link) DO NOTHING",
                rows,
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWS_RETENTION_DAYS)).isoformat()
        cur.execute("DELETE FROM company_news WHERE COALESCE(published_at, fetched_at) < %s", (cutoff,))
    conn.commit()


def list_company_news(conn: DBConnection, company_ids: list[str] | None = None, limit: int = 200) -> list[Row]:
    if company_ids is not None and not company_ids:
        return []
    query = (
        "SELECT company_news.*, companies.display_name FROM company_news "
        "JOIN companies ON companies.company_id = company_news.company_id"
    )
    params: list = []
    if company_ids is not None:
        placeholders = ",".join(["%s"] * len(company_ids))
        query += f" WHERE company_news.company_id IN ({placeholders})"
        params.extend(company_ids)
    query += " ORDER BY COALESCE(company_news.published_at, company_news.fetched_at) DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


# ------------------------------------------------------------------
# Company insights / system insights
# ------------------------------------------------------------------


def get_company_insights(conn: DBConnection, company_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM company_insights WHERE company_id = %s ORDER BY generated_at DESC LIMIT 1", (company_id,)
        )
        return cur.fetchone()


def list_company_insights(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM company_insights WHERE company_id = %s ORDER BY generated_at DESC", (company_id,))
        return cur.fetchall()


def save_company_insights(conn: DBConnection, company_id: str, insight_text: str, statement_type: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO company_insights (company_id, insight_text, statement_type, generated_at) "
            "VALUES (%s, %s, %s, %s)",
            (company_id, insight_text, statement_type, _utcnow_iso()),
        )
    conn.commit()


def save_system_insight(
    conn: DBConnection, *, insight_id: str, company_ids: list[str], insight_text: str, source_claim_ids: list[int],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO system_insights (insight_id, company_ids, insight_text, source_claim_ids, status, generated_at) "
            "VALUES (%s, %s, %s, %s, 'new', %s)",
            (insight_id, json.dumps(company_ids), insight_text, json.dumps(source_claim_ids), _utcnow_iso()),
        )
    conn.commit()


def list_system_insights(conn: DBConnection, *, statuses: tuple[str, ...] = ("new", "retained")) -> list[dict]:
    placeholders = ",".join(["%s"] * len(statuses))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM system_insights WHERE status IN ({placeholders}) ORDER BY generated_at DESC", statuses
        )
        rows = cur.fetchall()
    return [
        {
            "insight_id": r["insight_id"], "company_ids": json.loads(r["company_ids"]),
            "insight_text": r["insight_text"],
            "source_claim_ids": json.loads(r["source_claim_ids"]) if r["source_claim_ids"] else [],
            "status": r["status"], "generated_at": r["generated_at"], "status_changed_at": r["status_changed_at"],
        }
        for r in rows
    ]


def update_system_insight_status(conn: DBConnection, insight_id: str, status: str) -> None:
    if status not in ("new", "retained", "archived"):
        raise ValueError(f"status must be one of new|retained|archived, got {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE system_insights SET status = %s, status_changed_at = %s WHERE insight_id = %s",
            (status, _utcnow_iso(), insight_id),
        )
    conn.commit()


def list_recent_high_confidence_claims(conn: DBConnection, *, claim_types: tuple[str, ...], limit: int = 10) -> list[Row]:
    placeholders = ",".join(["%s"] * len(claim_types))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM knowledge_claims WHERE claim_type IN ({placeholders}) "
            "ORDER BY extraction_confidence DESC, created_at DESC LIMIT %s",
            (*claim_types, limit),
        )
        return cur.fetchall()


def list_company_ids_with_financial_data(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT company_id FROM canonical_financials")
        return [r["company_id"] for r in cur.fetchall()]


# ------------------------------------------------------------------
# Company notes / attachments
# ------------------------------------------------------------------


def list_company_notes(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM company_notes WHERE company_id = %s ORDER BY created_at DESC", (company_id,))
        return cur.fetchall()


def save_company_note(conn: DBConnection, company_id: str, note_text: str) -> Row:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO company_notes (company_id, note_text, created_at) VALUES (%s, %s, %s) RETURNING *",
            (company_id, note_text, _utcnow_iso()),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def update_company_note(conn: DBConnection, company_id: str, note_id: int, note_text: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE company_notes SET note_text = %s, updated_at = %s WHERE note_id = %s AND company_id = %s "
            "RETURNING *",
            (note_text, _utcnow_iso(), note_id, company_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def delete_company_note(conn: DBConnection, company_id: str, note_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM company_notes WHERE note_id = %s AND company_id = %s", (note_id, company_id))
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def list_note_attachments(conn: DBConnection, note_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM company_note_attachments WHERE note_id = %s ORDER BY uploaded_at", (note_id,))
        return cur.fetchall()


def list_note_attachments_for_company(conn: DBConnection, company_id: str) -> dict[int, list[Row]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.* FROM company_note_attachments a
            JOIN company_notes n ON n.note_id = a.note_id
            WHERE n.company_id = %s
            ORDER BY a.uploaded_at
            """,
            (company_id,),
        )
        rows = cur.fetchall()
    by_note: dict[int, list[Row]] = {}
    for row in rows:
        by_note.setdefault(row["note_id"], []).append(row)
    return by_note


def get_note_attachment(conn: DBConnection, note_id: int, attachment_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM company_note_attachments WHERE attachment_id = %s AND note_id = %s",
            (attachment_id, note_id),
        )
        return cur.fetchone()


def save_note_attachment(conn: DBConnection, note_id: int, filename: str, raw_file_path: str, size_bytes: int) -> Row:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO company_note_attachments (note_id, filename, raw_file_path, size_bytes, uploaded_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (note_id, filename, raw_file_path, size_bytes, _utcnow_iso()),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def delete_note_attachment(conn: DBConnection, note_id: int, attachment_id: int) -> Row | None:
    row = get_note_attachment(conn, note_id, attachment_id)
    if row is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM company_note_attachments WHERE attachment_id = %s AND note_id = %s", (attachment_id, note_id)
        )
    conn.commit()
    return row


# ------------------------------------------------------------------
# Documents (documents.processing_status is the real ingestion gate --
# ingestion_queue_items, which is NOT ported, is discovery/status tracking
# only per schemas/postgres_schema.sql's own header comment)
# ------------------------------------------------------------------


def list_company_periods(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT fiscal_year, quarter FROM canonical_financials
            WHERE company_id = %s AND period_type = 'quarterly' AND quarter IS NOT NULL
            ORDER BY fiscal_year, quarter
            """,
            (company_id,),
        )
        return cur.fetchall()


def list_company_annual_years(conn: DBConnection, company_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT fiscal_year FROM canonical_financials WHERE company_id = %s AND period_type = 'annual'",
            (company_id,),
        )
        return [row["fiscal_year"] for row in cur.fetchall()]


def list_company_documents(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE company_id = %s ORDER BY fiscal_year, quarter", (company_id,))
        return cur.fetchall()


def get_document(conn: DBConnection, document_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE document_id = %s", (document_id,))
        return cur.fetchone()


def get_company_document(conn: DBConnection, company_id: str, document_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE document_id = %s AND company_id = %s", (document_id, company_id))
        return cur.fetchone()


def save_company_document(
    conn: DBConnection,
    company_id: str,
    *,
    document_type: str,
    fiscal_year: str,
    quarter: str | None,
    added_by_user: str,
    raw_file_path: str | None = None,
    source_url: str | None = None,
    storage_object_key: str | None = None,
    content_hash: str | None = None,
) -> Row:
    """Postgres port of repositories.save_company_document() -- see that
    function's own docstring. storage_object_key/content_hash were missing
    from this port (documents.storage_object_key/content_hash already
    exist in schemas/postgres_schema.sql -- only the function signature
    was out of sync), which crashed every Docs tab "Add a Document" upload
    in production with `TypeError: save_company_document() got an
    unexpected keyword argument 'storage_object_key'`."""
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (company_id, document_type, fiscal_year, quarter,
                                    raw_file_path, source_url, added_by_user, retrieved_at,
                                    storage_object_key, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                company_id, document_type, fiscal_year, quarter, raw_file_path, source_url, added_by_user, now,
                storage_object_key, content_hash,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def list_documents_by_status(conn: DBConnection, status: str | None = None) -> list[Row]:
    with conn.cursor() as cur:
        if status is None:
            cur.execute("SELECT * FROM documents ORDER BY retrieved_at DESC")
        else:
            cur.execute("SELECT * FROM documents WHERE processing_status = %s ORDER BY retrieved_at DESC", (status,))
        return cur.fetchall()


def mark_document_processing_status(
    conn: DBConnection,
    document_id: int,
    *,
    status: str,
    file_hash: str | None = None,
    processed_at: str | None = None,
    error_message: str | None = None,
) -> Row | None:
    with conn.cursor() as cur:
        if file_hash is not None:
            cur.execute(
                "UPDATE documents SET processing_status = %s, processed_at = %s, file_hash = %s, error_message = %s "
                "WHERE document_id = %s RETURNING *",
                (status, processed_at, file_hash, error_message, document_id),
            )
        else:
            cur.execute(
                "UPDATE documents SET processing_status = %s, processed_at = %s, error_message = %s "
                "WHERE document_id = %s RETURNING *",
                (status, processed_at, error_message, document_id),
            )
        row = cur.fetchone()
    conn.commit()
    return row


def set_document_processing_status(conn: DBConnection, document_id: int, status: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET processing_status = %s WHERE document_id = %s RETURNING *", (status, document_id)
        )
        row = cur.fetchone()
    conn.commit()
    return row


# ------------------------------------------------------------------
# Investigations (Steps 2E-2H)
#
# hide_investigation/unhide_investigation/soft_delete_investigation/
# list_investigations target `hidden_at`/`deleted_at` -- both columns exist
# directly on Neon's `investigations` table (schemas/postgres_schema.sql),
# and every one of these functions has been run directly against real
# production data with no error.
# ------------------------------------------------------------------


def _insert_investigation_companies_pg(conn: DBConnection, investigation_id: str, company_ids: list[str]) -> None:
    """Postgres-flavored inline equivalent of
    storage/investigation_repository.py::insert_investigation_companies()
    (which uses sqlite3-only `?`/`executemany` and is out of this port's
    scope -- only storage/repositories.py is being ported this checkpoint).
    Same dedup-preserving-order + INSERT-OR-IGNORE-by-composite-PK
    semantics, rewritten as one execute_values() bulk insert with
    `ON CONFLICT (investigation_id, company_id) DO NOTHING` (the table's
    real composite PRIMARY KEY, schemas/postgres_schema.sql). Does not
    commit -- caller owns the transaction, same contract the original
    gives."""
    if not company_ids:
        return
    seen: set[str] = set()
    rows = []
    for position, company_id in enumerate(company_ids):
        if company_id in seen:
            continue
        seen.add(company_id)
        rows.append((investigation_id, company_id, position))
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO investigation_companies (investigation_id, company_id, position) VALUES %s "
            "ON CONFLICT (investigation_id, company_id) DO NOTHING",
            rows,
        )


def create_research_case(
    conn: DBConnection, case_id: str, *, kind: str, question: str, company_ids: list[str],
    statement_type: str, owner_id: int | None,
) -> Row:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research_cases (case_id, kind, question, company_ids, statement_type, status, "
            "current_activity, owner_id, started_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'in_progress', 'Queued', %s, %s, %s)",
            (case_id, kind, question, json.dumps(company_ids), statement_type, owner_id, now, now),
        )
    conn.commit()
    return get_research_case(conn, case_id)


def get_research_case(conn: DBConnection, case_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM research_cases WHERE case_id = %s", (case_id,))
        return cur.fetchone()


def update_case_activity(conn: DBConnection, case_id: str, activity: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research_cases SET current_activity = %s, updated_at = %s "
            "WHERE case_id = %s AND status = 'in_progress'",
            (activity, _utcnow_iso(), case_id),
        )
    conn.commit()


def complete_research_case(
    conn: DBConnection, case_id: str, *, outcome: str, result_json: str,
    thread_id: str | None = None, investigation_id: str | None = None,
) -> None:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research_cases SET status = 'completed', outcome = %s, result_json = %s, thread_id = %s, "
            "investigation_id = %s, current_activity = NULL, updated_at = %s, completed_at = %s WHERE case_id = %s",
            (outcome, result_json, thread_id, investigation_id, now, now, case_id),
        )
    conn.commit()


def fail_research_case(conn: DBConnection, case_id: str, error_message: str) -> None:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research_cases SET status = 'failed', error_message = %s, current_activity = NULL, "
            "updated_at = %s, completed_at = %s WHERE case_id = %s",
            (error_message, now, now, case_id),
        )
    conn.commit()


def request_case_cancellation(conn: DBConnection, case_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research_cases SET cancel_requested = 1 WHERE case_id = %s AND status = 'in_progress'",
            (case_id,),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def is_case_cancel_requested(conn: DBConnection, case_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM research_cases WHERE case_id = %s", (case_id,))
        row = cur.fetchone()
    return bool(row and row["cancel_requested"])


def cancel_research_case(conn: DBConnection, case_id: str) -> None:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research_cases SET status = 'cancelled', current_activity = NULL, updated_at = %s, "
            "completed_at = %s WHERE case_id = %s",
            (now, now, case_id),
        )
    conn.commit()


def list_research_cases_for_feed(conn: DBConnection, *, owner_id: int | None = None) -> list[Row]:
    sql = "SELECT * FROM research_cases WHERE NOT (status = 'completed' AND outcome = 'answered')"
    params: list = []
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    sql += " ORDER BY started_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def list_stale_in_progress_cases(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM research_cases WHERE status = 'in_progress'")
        return cur.fetchall()


def list_research_cases_for_audit(
    conn: DBConnection, *, status: str | None = None, kind: str | None = None,
    since_iso: str | None = None, limit: int = 200,
) -> list[Row]:
    """Postgres sibling of storage.repositories' own -- see that docstring
    for why the Audit Log intentionally excludes nothing, unlike the
    user-facing Cases feed."""
    sql = "SELECT * FROM research_cases WHERE 1 = 1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if kind:
        sql += " AND kind = %s"
        params.append(kind)
    if since_iso:
        sql += " AND started_at >= %s"
        params.append(since_iso)
    sql += " ORDER BY started_at DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def save_investigation(
    conn: DBConnection,
    *,
    investigation_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    strongest_explanation: str | None,
    unanswered_questions: list[str],
    additional_evidence_needed: list[str],
    as_of: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO investigations (investigation_id, question, company_ids, statement_type, "
            "strongest_explanation, unanswered_questions, additional_evidence_needed, generated_at, as_of) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (investigation_id, question, json.dumps(company_ids), statement_type, strongest_explanation,
             json.dumps(unanswered_questions), json.dumps(additional_evidence_needed), _utcnow_iso(), as_of),
        )
    _insert_investigation_companies_pg(conn, investigation_id, company_ids)
    conn.commit()


def save_investigation_hypothesis(
    conn: DBConnection,
    *,
    hypothesis_id: str,
    investigation_id: str,
    statement: str,
    mechanism: str | None,
    category: str,
    rationale: str | None,
    unknowns: list[str],
    generation_order: int,
    chain_steps: list[str] | None = None,
    verdict: str | None = None,
    confidence_basis: str | None = None,
    confidence_score: int | None = None,
    synthesis_rank: int | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO investigation_hypotheses (hypothesis_id, investigation_id, statement, mechanism, "
            "chain_steps, category, rationale, unknowns, generation_order, verdict, confidence_basis, "
            "confidence_score, synthesis_rank, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (hypothesis_id, investigation_id, statement, mechanism, json.dumps(chain_steps or []), category,
             rationale, json.dumps(unknowns), generation_order, verdict, confidence_basis, confidence_score,
             synthesis_rank, _utcnow_iso()),
        )
    conn.commit()


def save_investigation_hypothesis_evidence(conn: DBConnection, hypothesis_id: str, evidence: list[dict]) -> None:
    if not evidence:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO investigation_hypothesis_evidence (hypothesis_id, stance, kind, label, value, citation) "
            "VALUES %s",
            [(hypothesis_id, e["stance"], e["kind"], e["label"], e.get("value"), e.get("citation")) for e in evidence],
        )
    conn.commit()


def get_investigation(conn: DBConnection, investigation_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM investigations WHERE investigation_id = %s", (investigation_id,))
        return cur.fetchone()


def list_investigations(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM investigations WHERE deleted_at IS NULL ORDER BY generated_at DESC")
        return cur.fetchall()


def hide_investigation(conn: DBConnection, investigation_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE investigations SET hidden_at = %s WHERE investigation_id = %s AND deleted_at IS NULL",
            (_utcnow_iso(), investigation_id),
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def unhide_investigation(conn: DBConnection, investigation_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE investigations SET hidden_at = NULL WHERE investigation_id = %s", (investigation_id,))
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def soft_delete_investigation(conn: DBConnection, investigation_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE investigations SET deleted_at = %s WHERE investigation_id = %s",
            (_utcnow_iso(), investigation_id),
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def get_strongest_verdict_by_investigation(conn: DBConnection, investigation_ids: list[str]) -> dict[str, str | None]:
    if not investigation_ids:
        return {}
    placeholders = ",".join(["%s"] * len(investigation_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT investigation_id, verdict FROM (
                SELECT investigation_id, verdict,
                       ROW_NUMBER() OVER (
                           PARTITION BY investigation_id
                           ORDER BY CASE WHEN synthesis_rank IS NULL THEN 1 ELSE 0 END, synthesis_rank, generation_order
                       ) AS rn
                FROM investigation_hypotheses
                WHERE investigation_id IN ({placeholders})
            ) sub
            WHERE rn = 1
            """,
            investigation_ids,
        )
        rows = cur.fetchall()
    return {row["investigation_id"]: row["verdict"] for row in rows}


def list_investigation_hypotheses(conn: DBConnection, investigation_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM investigation_hypotheses WHERE investigation_id = %s ORDER BY "
            "CASE WHEN synthesis_rank IS NULL THEN 1 ELSE 0 END, synthesis_rank, generation_order",
            (investigation_id,),
        )
        return cur.fetchall()


def list_investigation_hypothesis_evidence(conn: DBConnection, hypothesis_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM investigation_hypothesis_evidence WHERE hypothesis_id = %s ORDER BY id", (hypothesis_id,)
        )
        return cur.fetchall()


def update_investigation_s3_metadata(
    conn: DBConnection, investigation_id: str, *, s3_key: str, abstract: str | None,
    version: int, strongest_verdict: str | None,
) -> None:
    """Postgres counterpart of storage/repositories.py's function of the
    same name -- see its docstring for the full reasoning."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE investigations SET s3_key = %s, abstract = %s, version = %s, strongest_verdict = %s "
            "WHERE investigation_id = %s",
            (s3_key, abstract, version, strongest_verdict, investigation_id),
        )
    conn.commit()


# ------------------------------------------------------------------
# Knowledge Builder (Step 2A) / knowledge graph
# ------------------------------------------------------------------


def get_or_create_knowledge_entity(
    conn: DBConnection, entity_type: str, name: str, company_id: str | None = None
) -> Row:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM knowledge_entities WHERE entity_type = %s AND name = %s AND company_id IS NOT DISTINCT FROM %s",
            (entity_type, name, company_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            return existing
        cur.execute(
            "INSERT INTO knowledge_entities (entity_type, name, company_id, created_at) VALUES (%s, %s, %s, %s) "
            "RETURNING *",
            (entity_type, name, company_id, _utcnow_iso()),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def insert_knowledge_claim(
    conn: DBConnection,
    *,
    document_id: int,
    company_id: str | None,
    claim_type: str,
    category: str | None,
    claim_text: str,
    speaker: str | None,
    fiscal_year: str | None,
    quarter: str | None,
    extraction_confidence: float | None,
) -> Row:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge_claims (
                document_id, company_id, claim_type, category, claim_text, speaker,
                fiscal_year, quarter, extraction_confidence, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (document_id, company_id, claim_type, category, claim_text, speaker,
             fiscal_year, quarter, extraction_confidence, _utcnow_iso()),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def insert_knowledge_relationship(
    conn: DBConnection, *, claim_id: int | None, source_entity_id: int, relationship_type: str, target_entity_id: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge_relationships (claim_id, source_entity_id, relationship_type, target_entity_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (claim_id, source_entity_id, relationship_type, target_entity_id, _utcnow_iso()),
        )
    conn.commit()


def insert_knowledge_evidence(conn: DBConnection, *, claim_id: int, document_id: int, quote: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge_evidence (claim_id, document_id, quote, created_at) VALUES (%s, %s, %s, %s)",
            (claim_id, document_id, quote, _utcnow_iso()),
        )
    conn.commit()


def list_knowledge_claims_for_document(conn: DBConnection, document_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM knowledge_claims WHERE document_id = %s ORDER BY claim_id", (document_id,))
        return cur.fetchall()


def list_knowledge_claims_for_company(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM knowledge_claims WHERE company_id = %s ORDER BY fiscal_year, quarter, claim_id",
            (company_id,),
        )
        return cur.fetchall()


def list_knowledge_entities_for_companies(
    conn: DBConnection, company_ids: list[str], *, entity_types: tuple[str, ...] | None = None, limit: int | None = None,
) -> list[Row]:
    if not company_ids:
        return []
    placeholders = ",".join(["%s"] * len(company_ids))
    sql = f"SELECT DISTINCT entity_type, name FROM knowledge_entities WHERE company_id IN ({placeholders})"
    params: list[object] = list(company_ids)
    if entity_types:
        type_placeholders = ",".join(["%s"] * len(entity_types))
        sql += f" AND entity_type IN ({type_placeholders})"
        params.extend(entity_types)
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def list_all_knowledge_entities(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, entity_type, name, company_id FROM knowledge_entities")
        return cur.fetchall()


def list_all_knowledge_claims(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claim_id, document_id, company_id, claim_type, category, claim_text, speaker, "
            "fiscal_year, quarter, extraction_confidence FROM knowledge_claims"
        )
        return cur.fetchall()


def list_all_knowledge_relationships(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relationship_id, claim_id, source_entity_id, relationship_type, target_entity_id "
            "FROM knowledge_relationships"
        )
        return cur.fetchall()


def list_all_knowledge_evidence(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT evidence_id, claim_id, document_id, quote FROM knowledge_evidence")
        return cur.fetchall()


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _sanitize_fts_query(query: str) -> str:
    """Kept verbatim (pure Python, no SQL) -- still used by nothing in this
    file since search_document_chunks() itself is not portable this
    checkpoint (see module header), but left here in case a future
    tsvector-based search wants the same OR-joined tokenization."""
    tokens = _FTS_TOKEN_RE.findall(query)
    return " OR ".join(f'"{t}"' for t in tokens)


def replace_document_chunks(conn: DBConnection, document_id: int, chunks: list[dict]) -> None:
    """Ports the `document_chunks` half, now WITH its FTS write half too:
    the SQLite version also deletes/inserts matching rows in
    `document_chunks_fts` (an FTS5 virtual table); this version computes and
    sets `search_vector = to_tsvector('english', text)` on insert instead,
    so a newly-(re)chunked document stays searchable via
    `search_document_chunks()` without a separate backfill step. (Earlier
    checkpoint skipped this half because no `search_vector` column existed
    yet -- it now does, see schemas/postgres_schema.sql.)"""
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM document_chunks WHERE document_id = %s", (document_id,))
        for chunk in chunks:
            cur.execute(
                "INSERT INTO document_chunks "
                "(document_id, company_id, page_number, chunk_index, text, section_heading, created_at, "
                " search_vector) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, to_tsvector('english', %s))",
                (
                    chunk["document_id"], chunk["company_id"], chunk["page_number"], chunk["chunk_index"],
                    chunk["text"], chunk.get("section_heading"), now,
                    chunk["text"],
                ),
            )
    conn.commit()


# search_document_chunks() itself stays defined in storage/fact_store_pg.py
# (mirroring where storage/fact_store.py's own default_fact_store() sources
# its search_document_chunks from storage/repositories.py) -- it now has a
# real Postgres tsvector/GIN-backed implementation there.


def list_document_chunks(conn: DBConnection, document_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text,
                   dc.embedding_status, dc.embedding_model,
                   d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at
            FROM document_chunks dc
            JOIN documents d ON d.document_id = dc.document_id
            WHERE dc.document_id = %s
            ORDER BY dc.chunk_index
            """,
            (document_id,),
        )
        return cur.fetchall()


def get_document_chunks_by_ids(conn: DBConnection, chunk_ids: list[int]) -> list[Row]:
    if not chunk_ids:
        return []
    placeholders = ",".join(["%s"] * len(chunk_ids))
    sql = (
        "SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text, "
        "       d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at "
        "FROM document_chunks dc "
        "JOIN documents d ON d.document_id = dc.document_id "
        f"WHERE dc.chunk_id IN ({placeholders})"
    )
    with conn.cursor() as cur:
        cur.execute(sql, chunk_ids)
        return cur.fetchall()


def set_document_chunks_embedding_status(
    conn: DBConnection, chunk_ids: list[int], *, status: str, model: str | None, embedded_at: str | None
) -> None:
    if not chunk_ids:
        return
    placeholders = ",".join(["%s"] * len(chunk_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE document_chunks SET embedding_status = %s, embedding_model = %s, embedded_at = %s "
            f"WHERE chunk_id IN ({placeholders})",
            (status, model, embedded_at, *chunk_ids),
        )
    conn.commit()


def list_knowledge_evidence_for_claim(conn: DBConnection, claim_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM knowledge_evidence WHERE claim_id = %s ORDER BY evidence_id", (claim_id,))
        return cur.fetchall()


def find_knowledge_claims_about_entity(conn: DBConnection, entity_type: str, entity_name: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT c.*
            FROM knowledge_entities e
            JOIN knowledge_relationships r ON r.source_entity_id = e.entity_id OR r.target_entity_id = e.entity_id
            JOIN knowledge_claims c ON c.claim_id = r.claim_id
            WHERE e.entity_type = %s AND e.name = %s
            ORDER BY c.fiscal_year, c.quarter, c.claim_id
            """,
            (entity_type, entity_name),
        )
        return cur.fetchall()


def list_knowledge_relationships_for_claim(conn: DBConnection, claim_id: int) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*, se.entity_type AS source_type, se.name AS source_name,
                   te.entity_type AS target_type, te.name AS target_name
            FROM knowledge_relationships r
            JOIN knowledge_entities se ON se.entity_id = r.source_entity_id
            JOIN knowledge_entities te ON te.entity_id = r.target_entity_id
            WHERE r.claim_id = %s
            """,
            (claim_id,),
        )
        return cur.fetchall()


def list_company_type_knowledge_entities(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM knowledge_entities WHERE entity_type = 'Company' AND company_id = %s ORDER BY entity_id",
            (company_id,),
        )
        return cur.fetchall()


def merge_knowledge_entities(conn: DBConnection, *, from_entity_id: int, into_entity_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE knowledge_relationships SET source_entity_id = %s WHERE source_entity_id = %s",
            (into_entity_id, from_entity_id),
        )
        cur.execute(
            "UPDATE knowledge_relationships SET target_entity_id = %s WHERE target_entity_id = %s",
            (into_entity_id, from_entity_id),
        )
        cur.execute("DELETE FROM knowledge_entities WHERE entity_id = %s", (from_entity_id,))
    conn.commit()


def list_knowledge_entity_ids_by_type_and_name(conn: DBConnection, entity_type: str, name: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM knowledge_entities WHERE entity_type = %s AND name = %s", (entity_type, name))
        return [row["entity_id"] for row in cur.fetchall()]


def list_entity_neighbors(conn: DBConnection, entity_ids: list[int]) -> list[Row]:
    if not entity_ids:
        return []
    placeholders = ",".join(["%s"] * len(entity_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.relationship_id, r.claim_id, r.source_entity_id, r.relationship_type, r.target_entity_id,
                   se.entity_type AS source_type, se.name AS source_name,
                   te.entity_type AS target_type, te.name AS target_name
            FROM knowledge_relationships r
            JOIN knowledge_entities se ON se.entity_id = r.source_entity_id
            JOIN knowledge_entities te ON te.entity_id = r.target_entity_id
            WHERE r.source_entity_id IN ({placeholders}) OR r.target_entity_id IN ({placeholders})
            """,
            [*entity_ids, *entity_ids],
        )
        return cur.fetchall()


def find_knowledge_claims_for_entity_ids(conn: DBConnection, entity_ids: list[int]) -> list[Row]:
    """UNION inside a subquery, then JOIN -- verified against real Neon."""
    if not entity_ids:
        return []
    placeholders = ",".join(["%s"] * len(entity_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT c.*, x.entity_id AS matched_entity_id
            FROM (
                SELECT claim_id, source_entity_id AS entity_id FROM knowledge_relationships
                WHERE source_entity_id IN ({placeholders})
                UNION
                SELECT claim_id, target_entity_id AS entity_id FROM knowledge_relationships
                WHERE target_entity_id IN ({placeholders})
            ) x
            JOIN knowledge_claims c ON c.claim_id = x.claim_id
            ORDER BY c.fiscal_year, c.quarter, c.claim_id
            """,
            [*entity_ids, *entity_ids],
        )
        return cur.fetchall()


# ------------------------------------------------------------------
# Generated Signals reports (research/signals_report.py)
#
# hide_generated_report/unhide_generated_report/soft_delete_generated_
# report/list_generated_reports (via _row_to_generated_report) target
# `hidden_at`/`deleted_at` -- both columns exist directly on Neon's
# `generated_reports` table (schemas/postgres_schema.sql), and every one
# of these functions has been run directly against real production data
# with no error.
# ------------------------------------------------------------------


def _row_to_generated_report(row: Row) -> dict:
    return {
        "thread_id": row["thread_id"],
        "question": row["question"],
        "company_ids": json.loads(row["company_ids"]),
        "statement_type": row["statement_type"],
        "report_markdown": row["report_markdown"],
        "generated_at": row["generated_at"],
        "question_embedding": json.loads(row["question_embedding"]) if row["question_embedding"] else None,
        "question_embedding_model": row["question_embedding_model"],
        "hidden_at": row["hidden_at"],
        # ADR-021 persistence-split columns -- live on Neon (see this
        # session's ALTER TABLE), same reasoning as storage/repositories.py's
        # sibling function.
        "s3_key": row.get("s3_key"),
        "abstract": row.get("abstract"),
        "version": row.get("version"),
        "visibility": row.get("visibility", "private"),
        "owner_id": row.get("owner_id"),
    }


def save_generated_report(
    conn: DBConnection,
    thread_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    report_markdown: str,
    *,
    question_embedding: list[float] | None = None,
    question_embedding_model: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO generated_reports "
            "(thread_id, question, company_ids, statement_type, report_markdown, generated_at, "
            " question_embedding, question_embedding_model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                thread_id, question, json.dumps(company_ids), statement_type, report_markdown, _utcnow_iso(),
                json.dumps(question_embedding) if question_embedding is not None else None,
                question_embedding_model,
            ),
        )
    conn.commit()


def get_generated_report(conn: DBConnection, thread_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM generated_reports WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
    return _row_to_generated_report(row) if row is not None else None


def list_generated_reports(conn: DBConnection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM generated_reports WHERE deleted_at IS NULL ORDER BY generated_at DESC")
        rows = cur.fetchall()
    return [_row_to_generated_report(row) for row in rows]


def hide_generated_report(conn: DBConnection, thread_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_reports SET hidden_at = %s WHERE thread_id = %s AND deleted_at IS NULL",
            (_utcnow_iso(), thread_id),
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def unhide_generated_report(conn: DBConnection, thread_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE generated_reports SET hidden_at = NULL WHERE thread_id = %s", (thread_id,))
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def soft_delete_generated_report(conn: DBConnection, thread_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_reports SET deleted_at = %s WHERE thread_id = %s", (_utcnow_iso(), thread_id)
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


def save_report_evidence(conn: DBConnection, thread_id: str, evidence: list[dict]) -> None:
    if not evidence:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO research_thread_evidence "
            "(thread_id, sort_order, kind, company_id, label, value, citation) VALUES %s",
            [
                (thread_id, i, ev["kind"], ev["company_id"], ev["label"], ev["value"], ev["citation"])
                for i, ev in enumerate(evidence)
            ],
        )
    conn.commit()


def list_report_evidence(conn: DBConnection, thread_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM research_thread_evidence WHERE thread_id = %s ORDER BY sort_order", (thread_id,)
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def save_report_followups(conn: DBConnection, thread_id: str, followups: list[str]) -> None:
    if not followups:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO research_thread_followups (thread_id, sort_order, followup_text) VALUES %s",
            [(thread_id, i, text) for i, text in enumerate(followups)],
        )
    conn.commit()


def list_report_followups(conn: DBConnection, thread_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT followup_text FROM research_thread_followups WHERE thread_id = %s ORDER BY sort_order",
            (thread_id,),
        )
        return [row["followup_text"] for row in cur.fetchall()]


def update_generated_report_s3_metadata(
    conn: DBConnection, thread_id: str, *, s3_key: str, abstract: str | None,
    version: int, owner_id: int | None = None,
) -> None:
    """Postgres counterpart of storage/repositories.py's function of the
    same name -- see its docstring for the full reasoning."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_reports SET s3_key = %s, abstract = %s, version = %s, owner_id = %s WHERE thread_id = %s",
            (s3_key, abstract, version, owner_id, thread_id),
        )
    conn.commit()


def get_latest_data_timestamp(conn: DBConnection, company_ids: list[str]) -> str | None:
    """UNION ALL inside a subquery -- verified against real Neon.

    storage/repositories.py's SQLite counterpart sources the "financial
    data" half of this freshness check from financial_observations.
    created_at -- but that table was deliberately excluded from the
    Postgres migration (see this module's own comment just above
    list_xbrl_migration_status/list_sec_edgar_migration_status), so
    querying it here raised `UndefinedTable: relation "financial_
    observations" does not exist` on every single /research/ask call for
    any company (context/reuse.py's find_reusable_report -> this function,
    on the hot path of every question). canonical_financials.decided_at is
    the closest Postgres-side equivalent -- the reconciled value's own
    timestamp, which only advances when new financial data has actually
    been decided/written, same freshness contract the excluded table's
    created_at gave the SQLite version.

    company_ids=[] (a macro-only question, research/macro_evidence.py --
    no company to check freshness against) short-circuits before building
    any SQL: `WHERE company_id IN ()` is a Postgres SYNTAX error, not just
    an always-false condition the way SQLite tolerates it -- found live,
    every macro-only question through the reuse-before-recompute check
    (context/reuse.py's find_reusable_report, called on every answer_
    question() -- the hot path of every /research/ask-shaped request) was
    crashing outright under DATABASE_BACKEND=postgres."""
    if not company_ids:
        return None
    placeholders = ",".join(["%s"] * len(company_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MAX(ts) AS latest FROM (
                SELECT MAX(decided_at) AS ts FROM canonical_financials WHERE company_id IN ({placeholders})
                UNION ALL
                SELECT MAX(retrieved_at) AS ts FROM documents WHERE company_id IN ({placeholders})
            ) sub
            """,
            (*company_ids, *company_ids),
        )
        row = cur.fetchone()
    return row["latest"] if row is not None else None


def delete_generated_report(conn: DBConnection, thread_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM research_thread_evidence WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM research_thread_followups WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM generated_reports WHERE thread_id = %s", (thread_id,))
        rowcount = cur.rowcount
    conn.commit()
    return rowcount > 0


# ------------------------------------------------------------------
# Index membership / sector / industry / index-tag vocabularies
# ------------------------------------------------------------------


def get_company_index_tags(conn: DBConnection, company_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT index_name FROM company_index_membership WHERE company_id = %s ORDER BY index_name",
            (company_id,),
        )
        return [row["index_name"] for row in cur.fetchall()]


def get_all_company_index_tags(conn: DBConnection) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT company_id, index_name FROM company_index_membership ORDER BY company_id, index_name")
        rows = cur.fetchall()
    tags_by_company: dict[str, list[str]] = {}
    for row in rows:
        tags_by_company.setdefault(row["company_id"], []).append(row["index_name"])
    return tags_by_company


def set_company_index_tags(conn: DBConnection, company_id: str, index_names: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM index_definitions")
        known = {row["name"] for row in cur.fetchall()}
        unknown = set(index_names) - known
        if unknown:
            raise ValueError(f"Unknown index name(s): {sorted(unknown)}; must be one of {sorted(known)}")
        cur.execute("DELETE FROM company_index_membership WHERE company_id = %s", (company_id,))
        if index_names:
            execute_values(
                cur,
                "INSERT INTO company_index_membership (company_id, index_name) VALUES %s",
                [(company_id, name) for name in index_names],
            )
    conn.commit()


def list_all_metrics(conn: DBConnection) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT metric_key, display_name FROM metrics_dictionary ORDER BY metric_key")
        rows = cur.fetchall()
    return [(r["metric_key"], r["display_name"] or r["metric_key"]) for r in rows]


def list_sectors(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM sectors ORDER BY name")
        return [row["name"] for row in cur.fetchall()]


def list_macro_economic_sectors(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT macro_economic_sector FROM companies WHERE macro_economic_sector IS NOT NULL")
        return [row["macro_economic_sector"] for row in cur.fetchall()]


def count_companies_by_sector(conn: DBConnection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT sector, COUNT(*) AS n FROM companies WHERE sector IS NOT NULL GROUP BY sector")
        rows = cur.fetchall()
    return {row["sector"]: row["n"] for row in rows}


def add_sector(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sectors (name, created_at) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, _utcnow_iso()),
        )
    conn.commit()


def rename_sector(conn: DBConnection, old_name: str, new_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sectors SET name = %s WHERE name = %s", (new_name, old_name))
        cur.execute("UPDATE companies SET sector = %s WHERE sector = %s", (new_name, old_name))
    conn.commit()


def delete_sector(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE companies SET sector = NULL WHERE sector = %s", (name,))
        cur.execute("DELETE FROM sectors WHERE name = %s", (name,))
    conn.commit()


def list_industries(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM industries ORDER BY name")
        return [row["name"] for row in cur.fetchall()]


def count_companies_by_industry(conn: DBConnection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT industry, COUNT(*) AS n FROM companies WHERE industry IS NOT NULL GROUP BY industry")
        rows = cur.fetchall()
    return {row["industry"]: row["n"] for row in rows}


def add_industry(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO industries (name, created_at) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, _utcnow_iso()),
        )
    conn.commit()


def rename_industry(conn: DBConnection, old_name: str, new_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE industries SET name = %s WHERE name = %s", (new_name, old_name))
        cur.execute("UPDATE companies SET industry = %s WHERE industry = %s", (new_name, old_name))
    conn.commit()


def delete_industry(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE companies SET industry = NULL WHERE industry = %s", (name,))
        cur.execute("DELETE FROM industries WHERE name = %s", (name,))
    conn.commit()


def list_index_definitions(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM index_definitions ORDER BY name")
        return [row["name"] for row in cur.fetchall()]


def count_companies_by_index_tag(conn: DBConnection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT index_name, COUNT(*) AS n FROM company_index_membership GROUP BY index_name")
        rows = cur.fetchall()
    return {row["index_name"]: row["n"] for row in rows}


def add_index_definition(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO index_definitions (name, created_at) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, _utcnow_iso()),
        )
    conn.commit()


def rename_index_definition(conn: DBConnection, old_name: str, new_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE index_definitions SET name = %s WHERE name = %s", (new_name, old_name))
        cur.execute(
            "UPDATE company_index_membership SET index_name = %s WHERE index_name = %s", (new_name, old_name)
        )
    conn.commit()


def delete_index_definition(conn: DBConnection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM company_index_membership WHERE index_name = %s", (name,))
        cur.execute("DELETE FROM index_definitions WHERE name = %s", (name,))
    conn.commit()


# ------------------------------------------------------------------
# Company List Columns / Overview Ratio settings
# ------------------------------------------------------------------

COMPANY_LIST_COLUMNS = [
    {"key": "sector", "label": "Sector"},
    {"key": "industry", "label": "Industry"},
    {"key": "price", "label": "Price"},
    {"key": "market_cap", "label": "Mkt Cap"},
    {"key": "week52", "label": "52W Range"},
    {"key": "all_time", "label": "All-Time Range"},
    {"key": "status", "label": "Status"},
    {"key": "tags", "label": "Index tags & IDs"},
]
_COMPANY_LIST_COLUMN_KEYS = {c["key"] for c in COMPANY_LIST_COLUMNS}


def get_company_list_column_settings(conn: DBConnection) -> dict[str, bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT column_key, enabled FROM company_list_column_settings")
        rows = cur.fetchall()
    overrides = {row["column_key"]: bool(row["enabled"]) for row in rows}
    return {key: overrides.get(key, True) for key in _COMPANY_LIST_COLUMN_KEYS}


def set_company_list_column_settings(conn: DBConnection, enabled_keys: Iterable[str]) -> None:
    enabled_keys = set(enabled_keys)
    unknown = enabled_keys - _COMPANY_LIST_COLUMN_KEYS
    if unknown:
        raise ValueError(f"Unknown column key(s): {sorted(unknown)}; must be one of {sorted(_COMPANY_LIST_COLUMN_KEYS)}")
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO company_list_column_settings (column_key, enabled) VALUES %s "
            "ON CONFLICT (column_key) DO UPDATE SET enabled = EXCLUDED.enabled",
            [(key, 1 if key in enabled_keys else 0) for key in _COMPANY_LIST_COLUMN_KEYS],
        )
    conn.commit()


OVERVIEW_RATIO_CATALOG = [
    {"key": "marketCap", "label": "Market Cap", "default_enabled": True},
    {"key": "price", "label": "Current Price", "default_enabled": True},
    {"key": "stockPE", "label": "Stock P/E", "default_enabled": True},
    {"key": "bookValue", "label": "Book Value", "default_enabled": True},
    {"key": "dividendYield", "label": "Dividend Yield", "default_enabled": True},
    {"key": "roe", "label": "ROE", "default_enabled": True},
    {"key": "eps", "label": "EPS", "default_enabled": True},
    {"key": "priceToBook", "label": "Price to Book Value", "default_enabled": True},
    {"key": "debtToEquity", "label": "Debt to Equity", "default_enabled": True},
    {"key": "payout", "label": "Dividend Payout", "default_enabled": True},
    {"key": "shares", "label": "No. Equity Shares", "default_enabled": True},
    {"key": "netProfit", "label": "Net Profit (latest FY)", "default_enabled": True},
    {"key": "revenue", "label": "Revenue (latest FY)", "default_enabled": True},
    {"key": "salesCagr", "label": "Sales Growth (full recorded range)", "default_enabled": True},
    {"key": "profitCagr", "label": "Profit Growth (full recorded range)", "default_enabled": True},
    {"key": "netMargin", "label": "Net Profit Margin", "default_enabled": False},
    {"key": "taxRate", "label": "Tax Rate", "default_enabled": False},
    {"key": "retention", "label": "Retention Ratio", "default_enabled": False},
    {"key": "roa", "label": "Return on Assets (bank/NBFC)", "default_enabled": False},
    {"key": "cdRatio", "label": "Credit-Deposit Ratio (bank)", "default_enabled": False},
    {"key": "intCoverage", "label": "Interest Coverage", "default_enabled": False},
    {"key": "networth", "label": "Net Worth", "default_enabled": False},
    {"key": "totalAssets", "label": "Total Assets", "default_enabled": False},
    {"key": "salesPerShare", "label": "Sales per Share", "default_enabled": False},
]
_OVERVIEW_RATIO_KEYS = {r["key"] for r in OVERVIEW_RATIO_CATALOG}
_OVERVIEW_RATIO_DEFAULT_ENABLED = {r["key"] for r in OVERVIEW_RATIO_CATALOG if r["default_enabled"]}


def get_overview_ratio_settings(conn: DBConnection) -> dict[str, bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT ratio_key, enabled FROM overview_ratio_settings")
        rows = cur.fetchall()
    overrides = {row["ratio_key"]: bool(row["enabled"]) for row in rows}
    return {key: overrides.get(key, key in _OVERVIEW_RATIO_DEFAULT_ENABLED) for key in _OVERVIEW_RATIO_KEYS}


def set_overview_ratio_settings(conn: DBConnection, enabled_keys: Iterable[str]) -> None:
    enabled_keys = set(enabled_keys)
    unknown = enabled_keys - _OVERVIEW_RATIO_KEYS
    if unknown:
        raise ValueError(f"Unknown ratio key(s): {sorted(unknown)}; must be one of {sorted(_OVERVIEW_RATIO_KEYS)}")
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO overview_ratio_settings (ratio_key, enabled) VALUES %s "
            "ON CONFLICT (ratio_key) DO UPDATE SET enabled = EXCLUDED.enabled",
            [(key, 1 if key in enabled_keys else 0) for key in _OVERVIEW_RATIO_KEYS],
        )
    conn.commit()


# ------------------------------------------------------------------
# Macro observations / bank infrastructure observations
# ------------------------------------------------------------------


def insert_macro_observations(conn: DBConnection, observations: Iterable) -> list[int]:
    now = _utcnow_iso()
    ids: list[int] = []
    with conn.cursor() as cur:
        for obs in observations:
            cur.execute(
                """
                INSERT INTO macro_observations (
                    series_key, region, period_type, period, value, unit,
                    source, source_file, source_url, retrieved_at, parser_version, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING observation_id
                """,
                (
                    obs.series_key, obs.region, obs.period_type, obs.period, obs.value, obs.unit,
                    obs.source, obs.source_file, obs.source_url, obs.retrieved_at or now, obs.parser_version, now,
                ),
            )
            ids.append(cur.fetchone()["observation_id"])
    conn.commit()
    return ids


def insert_bank_infrastructure_observations(conn: DBConnection, observations: Iterable) -> list[int]:
    now = _utcnow_iso()
    ids: list[int] = []
    with conn.cursor() as cur:
        for obs in observations:
            cur.execute(
                """
                INSERT INTO bank_infrastructure_observations (
                    bank_name, metric, period_type, period, value, unit,
                    source, source_file, parser_version, retrieved_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING observation_id
                """,
                (
                    obs.bank_name, obs.metric, obs.period_type, obs.period, obs.value, obs.unit,
                    obs.source, obs.source_file, obs.parser_version, now, now,
                ),
            )
            ids.append(cur.fetchone()["observation_id"])
    conn.commit()
    return ids


def get_bank_infrastructure_series(conn: DBConnection, bank_name: str, metric: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM bank_infrastructure_observations WHERE bank_name = %s AND metric = %s ORDER BY period",
            (bank_name, metric),
        )
        return cur.fetchall()


def get_existing_macro_periods(conn: DBConnection, series_key: str, region: str | None, source: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT period FROM macro_observations WHERE series_key = %s AND region IS NOT DISTINCT FROM %s AND source = %s",
            (series_key, region, source),
        )
        return {r["period"] for r in cur.fetchall()}


def get_macro_series(conn: DBConnection, series_key: str, region: str | None = None) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM macro_observations WHERE series_key = %s AND region IS NOT DISTINCT FROM %s ORDER BY period ASC",
            (series_key, region),
        )
        return cur.fetchall()


def list_macro_series_summary(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT series_key, source, MIN(period) AS earliest, MAX(period) AS latest "
            "FROM macro_observations WHERE region IS NULL GROUP BY series_key, source"
        )
        return cur.fetchall()


# ------------------------------------------------------------------
# Users
# ------------------------------------------------------------------

VALID_THEMES = {"light", "white", "green", "dark", "schwab", "signals", "signals-light"}
DEFAULT_THEME = "signals"


def create_user(conn: DBConnection, email: str, password_hash: str) -> int:
    """Raises psycopg2.errors.UniqueViolation if the email is already taken
    -- same "last-word uniqueness guard" role the SQLite IntegrityError
    plays."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, is_admin, theme, created_at) VALUES (%s, %s, 0, %s, %s) "
            "RETURNING user_id",
            (email, password_hash, DEFAULT_THEME, _utcnow_iso()),
        )
        user_id = cur.fetchone()["user_id"]
    conn.commit()
    return user_id


def get_user_by_email(conn: DBConnection, email: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


def get_user_by_login(conn: DBConnection, identifier: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s OR username = %s", (identifier, identifier))
        return cur.fetchone()


def get_user_by_id(conn: DBConnection, user_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone()


def update_user_theme(conn: DBConnection, user_id: int, theme: str) -> None:
    if theme not in VALID_THEMES:
        raise ValueError(f"theme must be one of {sorted(VALID_THEMES)}, got {theme!r}")
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET theme = %s WHERE user_id = %s", (theme, user_id))
    conn.commit()


# ------------------------------------------------------------------
# Shareholding pattern (SEBI LODR Reg 31)
# ------------------------------------------------------------------


def insert_shareholding_observations(conn: DBConnection, company_id: str, summaries: Iterable) -> int:
    now = _utcnow_iso()
    count = 0
    with conn.cursor() as cur:
        for s in summaries:
            cur.execute(
                """
                INSERT INTO shareholding_observations
                    (company_id, fiscal_year, quarter, promoter_holding_percent,
                     public_holding_percent, employee_trust_percent, source,
                     source_url, submission_date, retrieved_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'nse', %s, %s, %s, %s)
                ON CONFLICT (company_id, fiscal_year, quarter) DO UPDATE SET
                    promoter_holding_percent = EXCLUDED.promoter_holding_percent,
                    public_holding_percent = EXCLUDED.public_holding_percent,
                    employee_trust_percent = EXCLUDED.employee_trust_percent,
                    source_url = EXCLUDED.source_url,
                    submission_date = EXCLUDED.submission_date,
                    retrieved_at = EXCLUDED.retrieved_at
                """,
                (
                    company_id, s.fiscal_year, s.quarter, s.promoter_percent,
                    s.public_percent, s.employee_trust_percent, s.source_url,
                    s.submission_date, now, now,
                ),
            )
            count += 1
    conn.commit()
    return count


def update_shareholding_category_breakdown(
    conn: DBConnection, company_id: str, fiscal_year: str, quarter: str, breakdown
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE shareholding_observations
            SET fii_percent = %s, dii_percent = %s, government_percent = %s,
                public_non_institutional_percent = %s, num_shareholders = %s
            WHERE company_id = %s AND fiscal_year = %s AND quarter = %s
            """,
            (
                breakdown.fii_percent, breakdown.dii_percent, breakdown.government_percent,
                breakdown.public_non_institutional_percent, breakdown.num_shareholders,
                company_id, fiscal_year, quarter,
            ),
        )
    conn.commit()


def mark_shareholding_detail_fetched(conn: DBConnection, company_id: str, fiscal_year: str, quarter: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shareholding_observations SET detail_fetched_at = %s WHERE company_id = %s AND fiscal_year = %s AND quarter = %s",
            (_utcnow_iso(), company_id, fiscal_year, quarter),
        )
    conn.commit()


def get_shareholding_detail_fetched_periods(conn: DBConnection, company_id: str) -> set[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fiscal_year, quarter FROM shareholding_observations "
            "WHERE company_id = %s AND detail_fetched_at IS NOT NULL",
            (company_id,),
        )
        return {(row["fiscal_year"], row["quarter"]) for row in cur.fetchall()}


def insert_shareholding_holders(
    conn: DBConnection,
    company_id: str,
    fiscal_year: str,
    quarter: str,
    holdings: Iterable,
    *,
    source_url: str | None,
    submission_date: str | None,
) -> int:
    now = _utcnow_iso()
    count = 0
    with conn.cursor() as cur:
        for h in holdings:
            cur.execute(
                """
                INSERT INTO shareholding_holders
                    (company_id, fiscal_year, quarter, side, category, holder_name,
                     num_shares, percent_of_shares, source, source_url,
                     submission_date, retrieved_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'nse', %s, %s, %s, %s)
                ON CONFLICT (company_id, fiscal_year, quarter, side, holder_name) DO UPDATE SET
                    category = EXCLUDED.category,
                    num_shares = EXCLUDED.num_shares,
                    percent_of_shares = EXCLUDED.percent_of_shares,
                    source_url = EXCLUDED.source_url,
                    submission_date = EXCLUDED.submission_date,
                    retrieved_at = EXCLUDED.retrieved_at
                """,
                (
                    company_id, fiscal_year, quarter, h.side, h.category, h.holder_name,
                    h.num_shares, h.percent_of_shares, source_url, submission_date, now, now,
                ),
            )
            count += 1
    conn.commit()
    return count


_SHAREHOLDING_HISTORY_QUARTERS = 40


def list_shareholding_history(conn: DBConnection, company_id: str, limit: int = _SHAREHOLDING_HISTORY_QUARTERS) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM shareholding_observations
            WHERE company_id = %s
            ORDER BY fiscal_year DESC, quarter DESC
            LIMIT %s
            """,
            (company_id, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]


def list_shareholding_holders_all(conn: DBConnection, company_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fiscal_year, quarter, side, category, holder_name, num_shares, percent_of_shares
            FROM shareholding_holders
            WHERE company_id = %s
            """,
            (company_id,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# batch_job_runs / batch_job_items
# ------------------------------------------------------------------


def start_batch_job_run(conn: DBConnection, job_name: str, scope_label: str | None = None) -> int:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO batch_job_runs (job_name, scope_label, started_at, status) "
            "VALUES (%s, %s, %s, 'running') RETURNING run_id",
            (job_name, scope_label, now),
        )
        run_id = cur.fetchone()["run_id"]
    conn.commit()
    return run_id


def finish_batch_job_run(conn: DBConnection, run_id: int, *, status: str, notes: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS succeeded, "
            "  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed "
            "FROM batch_job_items WHERE run_id = %s",
            (run_id,),
        )
        counts = cur.fetchone()
        cur.execute(
            "UPDATE batch_job_runs SET finished_at = %s, status = %s, notes = %s, "
            "  items_total = %s, items_succeeded = %s, items_failed = %s "
            "WHERE run_id = %s",
            (
                _utcnow_iso(), status, notes,
                counts["total"] or 0, counts["succeeded"] or 0, counts["failed"] or 0,
                run_id,
            ),
        )
    conn.commit()


def start_batch_job_item(conn: DBConnection, run_id: int, company_id: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO batch_job_items (run_id, company_id, started_at, status) "
            "VALUES (%s, %s, %s, 'running') RETURNING item_id",
            (run_id, company_id, _utcnow_iso()),
        )
        item_id = cur.fetchone()["item_id"]
    conn.commit()
    return item_id


def finish_batch_job_item(conn: DBConnection, item_id: int, *, status: str, detail: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE batch_job_items SET finished_at = %s, status = %s, detail = %s WHERE item_id = %s",
            (_utcnow_iso(), status, detail, item_id),
        )
    conn.commit()


def get_last_successful_batch_item_times(
    conn: DBConnection, job_name: str, company_ids: list[str]
) -> dict[str, str]:
    if not company_ids:
        return {}
    placeholders = ",".join(["%s"] * len(company_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT bi.company_id, MAX(bi.finished_at) AS last_success
            FROM batch_job_items bi
            JOIN batch_job_runs br ON br.run_id = bi.run_id
            WHERE br.job_name = %s AND bi.status = 'ok' AND bi.company_id IN ({placeholders})
            GROUP BY bi.company_id
            """,
            (job_name, *company_ids),
        )
        rows = cur.fetchall()
    return {row["company_id"]: row["last_success"] for row in rows}


def get_latest_batch_item_for_company(conn: DBConnection, job_name: str, company_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bi.*
            FROM batch_job_items bi
            JOIN batch_job_runs br ON br.run_id = bi.run_id
            WHERE br.job_name = %s AND bi.company_id = %s
            ORDER BY bi.item_id DESC
            LIMIT 1
            """,
            (job_name, company_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_running_batch_job_runs(conn: DBConnection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM batch_job_runs WHERE status = 'running' ORDER BY started_at ASC")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_batch_job_runs(
    conn: DBConnection, job_name: str | None = None, limit: int = 20, since_iso: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if job_name is not None:
        clauses.append("job_name = %s")
        params.append(job_name)
    if since_iso is not None:
        clauses.append("started_at >= %s")
        params.append(since_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM batch_job_runs {where} ORDER BY started_at DESC LIMIT %s",
            (*params, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_distinct_batch_job_names(conn: DBConnection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT job_name FROM batch_job_runs ORDER BY job_name")
        rows = cur.fetchall()
    return [r["job_name"] for r in rows]


def get_latest_batch_job_run(conn: DBConnection, job_name: str) -> dict | None:
    rows = list_batch_job_runs(conn, job_name=job_name, limit=1)
    return rows[0] if rows else None


def list_batch_job_items(conn: DBConnection, run_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM batch_job_items WHERE run_id = %s ORDER BY item_id ASC", (run_id,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_batch_job_run_live_progress(conn: DBConnection, run_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS items_started,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS items_succeeded,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS items_failed
            FROM batch_job_items WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
    return {
        "items_started": row["items_started"] or 0,
        "items_succeeded": row["items_succeeded"] or 0,
        "items_failed": row["items_failed"] or 0,
    }


# ------------------------------------------------------------------
# dataset_events / worker_processing_log
# ------------------------------------------------------------------


def insert_dataset_event(conn: DBConnection, event) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dataset_events (
                event_id, event_type, dataset_id, dataset_type, source, scope_json,
                period, storage_reference_json, ingestion_id, ingested_at, metadata_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id, event.event_type, event.dataset_id, event.dataset_type, event.source,
                json.dumps(event.scope), event.period, json.dumps(event.storage_reference),
                event.ingestion_id, event.ingested_at, json.dumps(event.metadata), _utcnow_iso(),
            ),
        )
    conn.commit()


def get_dataset_event(conn: DBConnection, event_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM dataset_events WHERE event_id = %s", (event_id,))
        return cur.fetchone()


def list_dataset_events(
    conn: DBConnection,
    *,
    event_id: str | None = None,
    dataset_type: str | None = None,
    source: str | None = None,
    ingestion_id: str | None = None,
    since: str | None = None,
) -> list[Row]:
    clauses, params = [], []
    if event_id is not None:
        clauses.append("event_id = %s")
        params.append(event_id)
    if dataset_type is not None:
        clauses.append("dataset_type = %s")
        params.append(dataset_type)
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if ingestion_id is not None:
        clauses.append("ingestion_id = %s")
        params.append(ingestion_id)
    if since is not None:
        clauses.append("ingested_at >= %s")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM dataset_events {where} ORDER BY ingested_at ASC", params)
        return cur.fetchall()


def start_worker_log(
    conn: DBConnection, *, event_id: str, ingestion_id: str, worker_name: str, worker_version: str
) -> int:
    now = _utcnow_iso()
    existing = get_worker_log(conn, event_id, worker_name, worker_version)
    with conn.cursor() as cur:
        if existing is not None:
            cur.execute(
                "UPDATE worker_processing_log SET status = 'running', started_at = %s, completed_at = NULL, "
                "  retry_count = retry_count + 1 WHERE log_id = %s",
                (now, existing["log_id"]),
            )
            conn.commit()
            return existing["log_id"]
        cur.execute(
            "INSERT INTO worker_processing_log (event_id, ingestion_id, worker_name, worker_version, status, started_at) "
            "VALUES (%s, %s, %s, %s, 'running', %s) RETURNING log_id",
            (event_id, ingestion_id, worker_name, worker_version, now),
        )
        log_id = cur.fetchone()["log_id"]
    conn.commit()
    return log_id


def finish_worker_log(
    conn: DBConnection,
    log_id: int,
    *,
    status: str,
    output_reference: str | None = None,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_processing_log SET status = %s, completed_at = %s, output_reference = %s, error_message = %s "
            "WHERE log_id = %s",
            (status, _utcnow_iso(), output_reference, error_message, log_id),
        )
    conn.commit()


def get_worker_log(conn: DBConnection, event_id: str, worker_name: str, worker_version: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM worker_processing_log WHERE event_id = %s AND worker_name = %s AND worker_version = %s",
            (event_id, worker_name, worker_version),
        )
        return cur.fetchone()


def list_worker_processing_log(
    conn: DBConnection,
    *,
    event_id: str | None = None,
    worker_name: str | None = None,
    status: str | None = None,
) -> list[Row]:
    clauses, params = [], []
    if event_id is not None:
        clauses.append("event_id = %s")
        params.append(event_id)
    if worker_name is not None:
        clauses.append("worker_name = %s")
        params.append(worker_name)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM worker_processing_log {where} ORDER BY log_id ASC", params)
        return cur.fetchall()


# ------------------------------------------------------------------
# retrieval_diagnostics
# ------------------------------------------------------------------


def insert_retrieval_diagnostic(
    conn: DBConnection,
    *,
    created_at: str,
    query_excerpt: str | None,
    company_id: str | None,
    as_of: str | None,
    keyword_candidate_count: int,
    semantic_candidate_count: int,
    returned_count: int,
    embedding_latency_ms: float | None,
    vector_store_latency_ms: float | None,
    keyword_latency_ms: float | None,
    degraded: bool,
    degradation_reason: str | None,
    passages_json: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO retrieval_diagnostics (
                created_at, query_excerpt, company_id, as_of, keyword_candidate_count,
                semantic_candidate_count, returned_count, embedding_latency_ms, vector_store_latency_ms,
                keyword_latency_ms, degraded, degradation_reason, passages_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                created_at, query_excerpt, company_id, as_of, keyword_candidate_count,
                semantic_candidate_count, returned_count, embedding_latency_ms, vector_store_latency_ms,
                keyword_latency_ms, int(degraded), degradation_reason, passages_json,
            ),
        )
    conn.commit()


def list_retrieval_diagnostics(conn: DBConnection, limit: int = 50) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM retrieval_diagnostics ORDER BY retrieval_id DESC LIMIT %s", (limit,))
        return cur.fetchall()


# ------------------------------------------------------------------
# llm_call_log
# ------------------------------------------------------------------


def insert_llm_call_log(
    conn: DBConnection,
    *,
    task_name: str,
    company_ids: str,
    question: str | None,
    thread_id: str | None,
    complexity_tier: str,
    complexity_level: int,
    complexity_reason: str,
    model_used: str,
    provider_used: str,
    fallback_used: bool,
    attempts_json: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    stop_reason: str,
    context_tokens_before: int | None = None,
    context_tokens_after: int | None = None,
    context_items_dropped: int | None = None,
    reuse_hit: bool = False,
    reused_thread_id: str | None = None,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    graph_hit: bool = False,
    graph_hit_thread_id: str | None = None,
    graph_hit_score: float | None = None,
    investigation_id: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_call_log "
            "(created_at, task_name, company_ids, question, thread_id, complexity_tier, complexity_level, "
            "complexity_reason, model_used, provider_used, fallback_used, attempts_json, input_tokens, "
            "output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before, "
            "context_tokens_after, context_items_dropped, reuse_hit, reused_thread_id, "
            "cache_creation_input_tokens, cache_read_input_tokens, graph_hit, graph_hit_thread_id, "
            "graph_hit_score, investigation_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                _utcnow_iso(), task_name, company_ids, question, thread_id, complexity_tier, complexity_level,
                complexity_reason, model_used, provider_used, int(fallback_used), attempts_json, input_tokens,
                output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before,
                context_tokens_after, context_items_dropped, int(reuse_hit), reused_thread_id,
                cache_creation_input_tokens, cache_read_input_tokens, int(graph_hit), graph_hit_thread_id,
                graph_hit_score, investigation_id,
            ),
        )
    conn.commit()


def list_llm_call_log(conn: DBConnection, limit: int = 200) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM llm_call_log ORDER BY call_id DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def get_llm_usage_summary(conn: DBConnection) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd, "
            "COALESCE(SUM(reuse_hit), 0) AS reused_calls "
            "FROM llm_call_log"
        )
        totals = cur.fetchone()

        cur.execute(
            "SELECT task_name, COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
            "FROM llm_call_log GROUP BY task_name ORDER BY cost_usd DESC"
        )
        by_task = cur.fetchall()

        cur.execute(
            "SELECT model_used, COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
            "FROM llm_call_log WHERE reuse_hit = 0 GROUP BY model_used ORDER BY cost_usd DESC"
        )
        by_model = cur.fetchall()

    return {
        "calls": totals["calls"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cost_usd": totals["cost_usd"],
        "reused_calls": totals["reused_calls"],
        "by_task": [dict(row) for row in by_task],
        "by_model": [dict(row) for row in by_model],
    }


def get_investigation_cost_summary(conn: DBConnection, investigation_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
            "FROM llm_call_log WHERE investigation_id = %s",
            (investigation_id,),
        )
        row = cur.fetchone()
    return dict(row)


# ------------------------------------------------------------------
# ingestion_queue_items
# ------------------------------------------------------------------


def list_ingestion_queue_items(
    conn: DBConnection, *, status: str | None = None, item_kind: str | None = None
) -> list[Row]:
    query = "SELECT * FROM ingestion_queue_items WHERE 1=1"
    params: list[object] = []
    if status is not None:
        query += " AND status = %s"
        params.append(status)
    if item_kind is not None:
        query += " AND item_kind = %s"
        params.append(item_kind)
    query += " ORDER BY discovered_at DESC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_ingestion_queue_item(conn: DBConnection, item_id: int) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ingestion_queue_items WHERE item_id = %s", (item_id,))
        return cur.fetchone()


def get_ingestion_queue_item_by_path(conn: DBConnection, file_path: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ingestion_queue_items WHERE file_path = %s", (file_path,))
        return cur.fetchone()


def upsert_ingestion_queue_item(
    conn: DBConnection,
    *,
    item_kind: str,
    file_path: str,
    content_hash: str | None,
    company_id: str | None,
    source_id: str | None,
    status: str,
    status_reason: str | None,
) -> Row:
    now = _utcnow_iso()
    existing = get_ingestion_queue_item_by_path(conn, file_path)
    with conn.cursor() as cur:
        if existing is None:
            cur.execute(
                """
                INSERT INTO ingestion_queue_items (
                    item_kind, file_path, content_hash, company_id, source_id,
                    status, status_reason, discovered_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING item_id
                """,
                (item_kind, file_path, content_hash, company_id, source_id, status, status_reason, now),
            )
            item_id = cur.fetchone()["item_id"]
        else:
            cur.execute(
                """
                UPDATE ingestion_queue_items SET
                    item_kind = %s, content_hash = %s, company_id = %s, source_id = %s,
                    status = %s, status_reason = %s
                WHERE item_id = %s
                """,
                (item_kind, content_hash, company_id, source_id, status, status_reason, existing["item_id"]),
            )
            item_id = existing["item_id"]
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def update_ingestion_queue_item_result(
    conn: DBConnection,
    item_id: int,
    *,
    status: str,
    error_message: str | None = None,
    processed_at: str | None = None,
    last_processed_content_hash: str | None = None,
) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_queue_items SET
                status = %s, error_message = %s, last_attempt_at = %s,
                processed_at = COALESCE(%s, processed_at),
                last_processed_content_hash = COALESCE(%s, last_processed_content_hash)
            WHERE item_id = %s
            """,
            (status, error_message, _utcnow_iso(), processed_at, last_processed_content_hash, item_id),
        )
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def set_ingestion_queue_item_status(conn: DBConnection, item_id: int, status: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ingestion_queue_items SET status = %s WHERE item_id = %s", (status, item_id))
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)
