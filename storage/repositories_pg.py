"""Postgres (Neon) port of `storage/repositories.py`.

Checkpoint-3 port: every function in `repositories.py` whose tables are part
of `schemas/postgres_schema.sql` is ported here, same names/signatures,
targeting a `psycopg2` connection (from `storage.database.init_postgres_db()`)
instead of `sqlite3.Connection`. This file is purely additive and NOT wired
into any caller (`research/`, `web/`, `financials/`, `context/`, `ingestion/`
all keep importing the original SQLite-backed module exactly as today).

Scope -- functions deliberately NOT ported here (see this file's bottom
section header comments for the exact list), because they touch ONLY one of
the 8 tables that stay SQLite-only forever (confirmed this session that
nothing reads them to gate a fetch/reprocessing decision):
    batch_job_runs, batch_job_items, dataset_events, llm_call_log,
    retrieval_diagnostics, reconciliation_log, worker_processing_log,
    ingestion_queue_items

A few functions touch BOTH a kept table and one of those excluded tables (or
a table/column that exists in `schemas/sqlite_schema.sql` but was never added
to `schemas/postgres_schema.sql` at all). Those are ported for their
kept-table half, with the excluded half clearly flagged in a comment at the
call site -- see:
    - `reconcile()` -- writes `canonical_financials` (ported) and
      `reconciliation_log` (skipped; accepted loss -- pure audit data, not
      blocking, not revisited).
    - `replace_document_chunks()` -- FTS5->tsvector gap is now closed
      (`document_chunks.search_vector`, a GIN-indexed tsvector column, exists
      on Neon -- see schemas/postgres_schema.sql). This function now writes
      both `document_chunks` and its `search_vector` column on insert.
    - `search_document_chunks()` -- now implemented in
      `storage/fact_store_pg.py` (not here, mirroring where the SQLite
      original's `default_fact_store()` sources it from `repositories.py`),
      querying `document_chunks.search_vector` via `ts_rank`.
    - `hide_investigation`/`unhide_investigation`/`soft_delete_investigation`/
      `list_investigations`, and the equivalent `generated_reports` quartet
      -- these target `hidden_at`/`deleted_at` columns that
      `storage/database.py`'s `_migrate_case_visibility_columns` adds to the
      SQLite `investigations`/`generated_reports` tables via `ALTER TABLE`,
      but that migration was never carried into `schemas/postgres_schema.sql`
      -- those two tables have no `hidden_at`/`deleted_at` columns on Neon
      today. Ported here targeting those columns anyway (so this file needs
      no further edits once the schema gap is closed), but they cannot be
      verified against real Neon in this checkpoint and WILL error
      (`UndefinedColumn`) until that schema gap is fixed -- flagged clearly
      in the checkpoint report.

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
    trust_rank tiebreak, stale-canonical-row deletion).

    NOT ported here: every `reconciliation_log` INSERT/UPDATE the SQLite
    version performs (the audit trail of considered/chosen observations) --
    `reconciliation_log` is one of the 8 tables staying SQLite-only forever.
    The `canonical_financials` decision itself (what this function exists to
    compute) is fully ported and behaves identically; only the side-channel
    audit write is missing. Flagged for the eventual real cutover: either
    (a) log to SQLite via a second connection from the same call, or (b)
    drop this audit trail for Postgres-backed installs -- a real decision
    needed before this file is wired in, not made here.
    """
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

    migrated = _period_is_xbrl_migrated(conn, company_id, period_type, fiscal_year, quarter, statement_type)
    if migrated:
        xbrl_candidates = [row for row in candidates if row["source"] == XBRL_SOURCE_ID]
        if not xbrl_candidates:
            _delete_stale_canonical_row()
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
    conn.commit()
    return canonical_id


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


def list_xbrl_migration_status(conn: DBConnection) -> list[dict]:
    """MAX(CASE WHEN ... THEN ... END) conditional aggregate + GROUP BY --
    verified against real Neon."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id,
                   MAX(CASE WHEN source = 'nse' THEN fiscal_year || quarter END) AS latest_xbrl_period,
                   MAX(fiscal_year || quarter) AS latest_any_period
            FROM financial_observations
            WHERE period_type = 'quarterly'
            GROUP BY company_id
            """
        )
        coverage_rows = cur.fetchall()
        coverage_by_company = {row["company_id"]: row for row in coverage_rows}

        cur.execute(
            """
            SELECT company_id, display_name, nse_symbol FROM companies
            WHERE nse_symbol IS NOT NULL AND nse_symbol != '' AND status = 'active'
            """
        )
        companies = cur.fetchall()

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


def list_sec_edgar_migration_status(conn: DBConnection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id,
                   MAX(CASE WHEN source = 'sec_edgar' THEN fiscal_year || quarter END) AS latest_edgar_period,
                   MAX(fiscal_year || quarter) AS latest_any_period
            FROM financial_observations
            WHERE period_type = 'quarterly'
            GROUP BY company_id
            """
        )
        coverage_rows = cur.fetchall()
        coverage_by_company = {row["company_id"]: row for row in coverage_rows}

        cur.execute("SELECT company_id, display_name FROM companies WHERE country = 'US' AND status = 'active'")
        companies = cur.fetchall()

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
) -> Row:
    now = _utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (company_id, document_type, fiscal_year, quarter,
                                    raw_file_path, source_url, added_by_user, retrieved_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (company_id, document_type, fiscal_year, quarter, raw_file_path, source_url, added_by_user, now),
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
# NOTE on hide_investigation/unhide_investigation/soft_delete_investigation/
# list_investigations: these target `hidden_at`/`deleted_at` columns that,
# on SQLite, storage/database.py's _migrate_case_visibility_columns() adds
# to `investigations` via ALTER TABLE -- that migration was never carried
# into schemas/postgres_schema.sql, so the real Neon `investigations` table
# has NO hidden_at/deleted_at columns today. Ported here targeting those
# columns anyway (matching names/signatures per the porting brief, and so
# this file needs no further edits once the schema gap is closed), but they
# WILL raise psycopg2.errors.UndefinedColumn against the current live Neon
# schema -- NOT verified against real Neon in this checkpoint. Flagged in
# the checkpoint report as a pre-existing schema gap, not something
# introduced by this port.
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
    """See this file's module-level NOTE -- targets `deleted_at`, which
    does not exist on the real Neon `investigations` table yet."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM investigations WHERE deleted_at IS NULL ORDER BY generated_at DESC")
        return cur.fetchall()


def hide_investigation(conn: DBConnection, investigation_id: str) -> bool:
    """See this file's module-level NOTE -- targets `hidden_at`, which does
    not exist on the real Neon `investigations` table yet."""
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
# NOTE on hide_generated_report/unhide_generated_report/
# soft_delete_generated_report/list_generated_reports (via
# _row_to_generated_report): same schema gap as the investigations quartet
# above -- `hidden_at`/`deleted_at` are added to SQLite's `generated_reports`
# by the same _migrate_case_visibility_columns() ALTER TABLE, and were never
# carried into schemas/postgres_schema.sql. Ported here targeting those
# columns anyway; NOT verified against real Neon in this checkpoint (will
# raise UndefinedColumn against the live schema).
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
    """See this file's module-level NOTE -- targets `deleted_at`, which
    does not exist on the real Neon `generated_reports` table yet."""
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


def get_latest_data_timestamp(conn: DBConnection, company_ids: list[str]) -> str | None:
    """UNION ALL inside a subquery -- verified against real Neon."""
    placeholders = ",".join(["%s"] * len(company_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MAX(ts) AS latest FROM (
                SELECT MAX(created_at) AS ts FROM financial_observations WHERE company_id IN ({placeholders})
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
# NOT PORTED -- pure audit/observability tables (SQLite-only forever, per
# schemas/postgres_schema.sql's own header comment and this checkpoint's
# scoping instructions):
#
#   batch_job_runs / batch_job_items:
#     start_batch_job_run, finish_batch_job_run, start_batch_job_item,
#     finish_batch_job_item, get_last_successful_batch_item_times,
#     get_latest_batch_item_for_company, list_running_batch_job_runs,
#     list_batch_job_runs, list_distinct_batch_job_names,
#     get_latest_batch_job_run, list_batch_job_items,
#     get_batch_job_run_live_progress
#
#   dataset_events:
#     insert_dataset_event, get_dataset_event, list_dataset_events
#
#   worker_processing_log:
#     start_worker_log, finish_worker_log, get_worker_log,
#     list_worker_processing_log
#
#   retrieval_diagnostics:
#     insert_retrieval_diagnostic, list_retrieval_diagnostics
#
#   llm_call_log:
#     insert_llm_call_log, list_llm_call_log, get_llm_usage_summary,
#     get_investigation_cost_summary
#
#   ingestion_queue_items:
#     list_ingestion_queue_items, get_ingestion_queue_item,
#     get_ingestion_queue_item_by_path, upsert_ingestion_queue_item,
#     update_ingestion_queue_item_result, set_ingestion_queue_item_status
#
#   reconciliation_log (read-only audit display -- the log table itself has
#   no Postgres home, so these joins have nothing to read):
#     list_reconciliation_log, list_reconciliation_log_by_company
#
# See this file's module-level docstring for the functions that touch BOTH
# a kept and an excluded/missing table (reconcile(), replace_document_chunks(),
# search_document_chunks(), and the hidden_at/deleted_at investigations and
# generated_reports quartets) -- those are handled with inline flags at the
# call site above, not silently dropped.
# ------------------------------------------------------------------
