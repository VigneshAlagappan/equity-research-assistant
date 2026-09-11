"""Postgres (Neon) port of `storage/company_repository.py`.

Checkpoint-2 proof-of-concept: every function in `company_repository.py` is
ported here, same names/signatures, targeting a `psycopg2` connection (from
`storage.database.init_postgres_db()`) instead of `sqlite3.Connection`.

This file is purely additive and NOT wired into any caller
(`companies/registry.py`, `companies/stock_actions.py`,
`scripts/tag_nifty_microcap.py`, `ingestion/corporate_actions.py`, ...) --
those all keep importing the original SQLite-backed module exactly as today.

Translation notes (see also each function's own comments where relevant):
- `?` positional placeholders become `%s` (psycopg2's paramstyle).
- Unlike `sqlite3.Connection`, a `psycopg2` connection has no `.execute()`
  of its own -- every query here goes through an explicit `conn.cursor()`.
- `INSERT OR IGNORE` becomes `INSERT ... ON CONFLICT (<real unique/PK
  columns, from schemas/postgres_schema.sql>) DO NOTHING`.
- SQLite's `ON CONFLICT(...) DO UPDATE SET col = excluded.col` upsert syntax
  ports to Postgres nearly verbatim -- `EXCLUDED` is the same pseudo-table
  name (case-insensitive), verified against real Neon in this checkpoint.
- The two "how many rows were newly inserted" bulk functions
  (`insert_corporate_actions_raw`, `tag_companies_index`) used SQLite's
  `conn.total_changes` before/after diff -- psycopg2 has no connection-level
  change counter, and `executemany()`'s `cursor.rowcount` is unreliable for
  this per psycopg2's own docs. Both are rewritten as a single multi-row
  `INSERT ... VALUES %s ON CONFLICT (...) DO NOTHING RETURNING <pk>` built
  with `psycopg2.extras.execute_values()`, counting the returned rows.
- SQLite's `LIKE ... COLLATE NOCASE` (search_companies_rows) becomes
  Postgres's native case-insensitive `ILIKE`.
- `INSERT ... RETURNING *` replaces the SQLite "insert, then re-SELECT by
  lastrowid" two-step (psycopg2 cursors have no `.lastrowid`) for
  `insert_stock_action` / `insert_corporate_action`.
"""

from __future__ import annotations

import json

from psycopg2.extras import execute_values

from storage.db_types import DBConnection, Row

_SECTOR_PEER_COLUMNS = {"basic_industry": "basic_industry", "macro_economic_sector": "macro_economic_sector"}
_TAG_GROUP_COLUMNS = {"sector": "sector", "industry": "industry"}


def select_company_id(conn: DBConnection, company_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT company_id FROM companies WHERE company_id = %s", (company_id,))
        return cur.fetchone()


def insert_company(
    conn: DBConnection, *, company_id: str, legal_name: str, display_name: str, nse_symbol: str | None,
    bse_code: str | None, isin: str | None, country: str, currency: str, fiscal_year_end_month: int,
    website: str | None, macro_economic_sector: str | None, sector: str | None, industry: str | None,
    basic_industry: str | None, listed_date: str | None, now: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (
                company_id, legal_name, display_name, nse_symbol, bse_code, isin, country, currency,
                fiscal_year_end_month, website,
                macro_economic_sector, sector, industry, basic_industry,
                status, listed_date, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
            """,
            (company_id, legal_name, display_name, nse_symbol, bse_code, isin, country, currency,
             fiscal_year_end_month, website,
             macro_economic_sector, sector, industry, basic_industry, listed_date, now, now),
        )
    conn.commit()


def update_company(
    conn: DBConnection, *, company_id: str, legal_name: str, display_name: str, nse_symbol: str | None,
    bse_code: str | None, isin: str | None, country: str, currency: str, fiscal_year_end_month: int,
    website: str | None, macro_economic_sector: str | None, sector: str | None, industry: str | None,
    basic_industry: str | None, listed_date: str | None, now: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET
                legal_name = %s, display_name = %s, nse_symbol = %s, bse_code = %s, isin = %s,
                country = %s, currency = %s, fiscal_year_end_month = %s, website = %s,
                macro_economic_sector = %s, sector = %s, industry = %s, basic_industry = %s,
                listed_date = %s, updated_at = %s
            WHERE company_id = %s
            """,
            (legal_name, display_name, nse_symbol, bse_code, isin, country, currency, fiscal_year_end_month,
             website, macro_economic_sector, sector, industry, basic_industry, listed_date, now, company_id),
        )
    conn.commit()


def select_company(conn: DBConnection, company_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE company_id = %s", (company_id,))
        return cur.fetchone()


def update_company_website_row(conn: DBConnection, company_id: str, website: str, now: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET website = %s, updated_at = %s WHERE company_id = %s",
            (website, now, company_id),
        )
    conn.commit()


def select_companies_by_sector_column(conn: DBConnection, column: str, value: str, exclude_company_id: str) -> list[Row]:
    """`column` must already be validated by the caller against a fixed
    allowlist (companies/registry.py's `_SECTOR_PEER_FIELDS`) — it's never
    accepted as free text here, just re-checked against this module's own
    allowlist as a second gate before being interpolated into the query
    (a column name can't be a bind parameter)."""
    if column not in _SECTOR_PEER_COLUMNS:
        raise ValueError(f"column must be one of {sorted(_SECTOR_PEER_COLUMNS)}, got {column!r}")
    sql = f"SELECT company_id FROM companies WHERE {_SECTOR_PEER_COLUMNS[column]} = %s AND company_id != %s"
    with conn.cursor() as cur:
        cur.execute(sql, (value, exclude_company_id))
        return cur.fetchall()


def select_companies_with_sector_column(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id, COALESCE(NULLIF(basic_industry, ''), NULLIF(macro_economic_sector, '')) AS sector "
            "FROM companies"
        )
        return cur.fetchall()


def search_companies_rows(
    conn: DBConnection, like: str, prefix_like: str, limit: int, *, index_name: str | None,
) -> list[Row]:
    """SQLite's `LIKE ... COLLATE NOCASE` ports to Postgres's native
    case-insensitive `ILIKE` -- Postgres has no COLLATE NOCASE collation."""
    index_clause = (
        "AND EXISTS (SELECT 1 FROM company_index_membership m WHERE m.company_id = companies.company_id AND m.index_name = %s)"
        if index_name is not None
        else ""
    )
    params: tuple = (like, like, like, like)
    if index_name is not None:
        params += (index_name,)
    params += (prefix_like, limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM companies
            WHERE status = 'active' AND (
                company_id ILIKE %s
                OR display_name ILIKE %s
                OR legal_name ILIKE %s
                OR nse_symbol ILIKE %s
            )
            {index_clause}
            ORDER BY
                CASE WHEN company_id ILIKE %s THEN 0 ELSE 1 END,
                display_name
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def select_companies(conn: DBConnection, *, include_archived: bool) -> list[Row]:
    with conn.cursor() as cur:
        if include_archived:
            cur.execute("SELECT * FROM companies ORDER BY company_id")
        else:
            cur.execute("SELECT * FROM companies WHERE status = 'active' ORDER BY company_id")
        return cur.fetchall()


def update_company_lifecycle_status(
    conn: DBConnection, company_id: str, *, status: str, archived_at: str | None, archive_reason: str | None, now: str,
) -> int:
    """Used by both archive_company() (status='archived', reason set) and
    restore_company() (status='active', archived_at/reason cleared to
    None). Returns rowcount so the caller can tell "no such company" apart
    from a successful flip without a second SELECT. psycopg2's
    cursor.rowcount is reliable for a plain single-statement UPDATE like
    this (the "unreliable" caveat only applies to executemany())."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET status = %s, archived_at = %s, archive_reason = %s, updated_at = %s
            WHERE company_id = %s
            """,
            (status, archived_at, archive_reason, now, company_id),
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def select_company_status(conn: DBConnection, company_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM companies WHERE company_id = %s", (company_id,))
        return cur.fetchone()


def insert_stock_action(
    conn: DBConnection, *, company_id: str, action_type: str, action_date: str, ratio_from: float, ratio_to: float,
    subscription_price: float | None, source: str | None, source_url: str | None, notes: str | None, now: str,
) -> Row:
    """psycopg2 cursors have no `.lastrowid` -- `RETURNING *` gets the
    inserted row back in the same round trip instead of a second SELECT."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stock_actions (
                company_id, action_type, action_date, ratio_from, ratio_to,
                subscription_price, source, source_url, notes, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (company_id, action_type, action_date, ratio_from, ratio_to,
             subscription_price, source, source_url, notes, now, now),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def select_stock_actions(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM stock_actions WHERE company_id = %s ORDER BY action_date DESC, action_id DESC",
            (company_id,),
        )
        return cur.fetchall()


def delete_stock_action_row(conn: DBConnection, company_id: str, action_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stock_actions WHERE action_id = %s AND company_id = %s", (action_id, company_id)
        )
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def insert_corporate_actions_raw(conn: DBConnection, company_id: str, actions: list, *, now: str) -> int:
    """Bulk-insert NSE's raw corporate-actions rows for one company --
    `ON CONFLICT (company_id, ex_date, subject) DO NOTHING` (the table's own
    UNIQUE constraint, schemas/postgres_schema.sql) makes a re-fetch
    idempotent, same as SQLite's `INSERT OR IGNORE` on the same columns, so
    calling this again with an overlapping/full history is always safe.
    `actions` is a list of sources.nse_corporate_actions.CorporateActionRef.
    Returns how many rows were newly inserted (0 if every row was already
    on file).

    Rewritten from SQLite's `conn.total_changes` before/after diff (no
    Postgres equivalent) into a single multi-row INSERT built with
    `psycopg2.extras.execute_values()`, using `RETURNING raw_id` and
    counting the rows actually returned -- the precise Postgres-native way
    to know how many were genuinely new (executemany()'s cursor.rowcount is
    unreliable for ON CONFLICT DO NOTHING per psycopg2's own docs)."""
    if not actions:
        return 0
    rows = [
        (
            company_id, a.subject, a.ex_date.isoformat(),
            a.record_date.isoformat() if a.record_date else None,
            a.face_value,
            a.bc_start_date.isoformat() if a.bc_start_date else None,
            a.bc_end_date.isoformat() if a.bc_end_date else None,
            json.dumps(a.raw), "nse", now,
        )
        for a in actions
    ]
    with conn.cursor() as cur:
        inserted = execute_values(
            cur,
            """
            INSERT INTO corporate_actions_raw (
                company_id, subject, ex_date, record_date, face_value,
                bc_start_date, bc_end_date, raw_json, source, retrieved_at
            ) VALUES %s
            ON CONFLICT (company_id, ex_date, subject) DO NOTHING
            RETURNING raw_id
            """,
            rows,
            fetch=True,
        )
    conn.commit()
    return len(inserted)


def select_unprocessed_corporate_actions_raw(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM corporate_actions_raw WHERE company_id = %s AND processed_at IS NULL ORDER BY ex_date",
            (company_id,),
        )
        return cur.fetchall()


def select_all_corporate_actions_raw(conn: DBConnection, company_id: str) -> list[Row]:
    """Every raw row for this company regardless of processed_at -- for
    re-classifying history after a classify_action_type() rule change
    (ingestion/corporate_actions.py's reclassify_company_corporate_actions),
    as opposed to select_unprocessed_corporate_actions_raw's "only what's
    new" scope used by the normal ingest pass."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM corporate_actions_raw WHERE company_id = %s ORDER BY ex_date",
            (company_id,),
        )
        return cur.fetchall()


def mark_corporate_actions_raw_processed(conn: DBConnection, raw_id: int, *, now: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE corporate_actions_raw SET processed_at = %s WHERE raw_id = %s", (now, raw_id))
    conn.commit()


def insert_corporate_action(
    conn: DBConnection, *, raw_id: int, company_id: str, action_type: str, subject: str,
    ex_date: str, record_date: str | None, face_value: float | None, classifier_version: str, now: str,
) -> Row:
    """Upsert on raw_id (UNIQUE) -- doubles as both "classify this new raw
    row" (the normal ingest pass) and "re-classify this already-processed
    row under a newer classifier_version" (reclassify_company_corporate_
    actions, after a classify_action_type() rule change), without needing
    two separate SQL statements. created_at is intentionally left alone on
    a re-classify -- it's this action's original discovery time, not the
    classifier's last-run time.

    Postgres's `ON CONFLICT (...) DO UPDATE SET col = EXCLUDED.col` is the
    same convention SQLite's `ON CONFLICT(...) DO UPDATE SET col =
    excluded.col` uses (confirmed against real Neon) -- only the
    placeholder style changes. `RETURNING *` replaces the follow-up SELECT
    the SQLite version needs (no lastrowid to key it by here anyway --
    raw_id is already known)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corporate_actions (
                raw_id, company_id, action_type, subject, ex_date, record_date,
                face_value, classifier_version, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (raw_id) DO UPDATE SET
                action_type = EXCLUDED.action_type,
                subject = EXCLUDED.subject,
                ex_date = EXCLUDED.ex_date,
                record_date = EXCLUDED.record_date,
                face_value = EXCLUDED.face_value,
                classifier_version = EXCLUDED.classifier_version
            RETURNING *
            """,
            (raw_id, company_id, action_type, subject, ex_date, record_date, face_value, classifier_version, now),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def select_corporate_actions(conn: DBConnection, company_id: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM corporate_actions WHERE company_id = %s ORDER BY ex_date DESC",
            (company_id,),
        )
        return cur.fetchall()


# ------------------------------------------------------------------
# One-off/scheduled backfill scripts (scripts/backfill_company_websites.py,
# scripts/backfill_sector_industry.py, scripts/fetch_daily_prices.py,
# scripts/backfill_price_history.py) -- moved here so those scripts issue no
# SQL of their own, same "business logic doesn't touch the DB directly"
# contract as the rest of this module.
# ------------------------------------------------------------------


def select_companies_missing_website(conn: DBConnection, *, company_id: str | None = None) -> list[Row]:
    query = "SELECT company_id FROM companies WHERE country != 'IN' AND website IS NULL"
    params: tuple = ()
    if company_id is not None:
        query += " AND company_id = %s"
        params = (company_id,)
    query += " ORDER BY company_id"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def select_companies_missing_sector_or_industry(conn: DBConnection) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id, nse_symbol, sector, industry FROM companies "
            "WHERE (sector IS NULL OR industry IS NULL) AND nse_symbol IS NOT NULL "
            "ORDER BY company_id"
        )
        return cur.fetchall()


def update_company_sector_industry(conn: DBConnection, company_id: str, *, sector: str | None, industry: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET sector = %s, industry = %s WHERE company_id = %s",
            (sector, industry, company_id),
        )
    conn.commit()


def select_index_members_with_nse_symbol(
    conn: DBConnection, index_name: str, *, company_id: str | None = None
) -> list[Row]:
    """company_id/nse_symbol for every company tagged with `index_name` in
    company_index_membership that has an NSE symbol on file — the ticker
    universe scripts/fetch_daily_prices.py and
    scripts/backfill_price_history.py both fetch price history for."""
    query = """
        SELECT c.company_id, c.nse_symbol
        FROM companies c
        JOIN company_index_membership m ON m.company_id = c.company_id
        WHERE m.index_name = %s AND c.nse_symbol IS NOT NULL
    """
    params: tuple = (index_name,)
    if company_id is not None:
        query += " AND c.company_id = %s"
        params += (company_id,)
    query += " ORDER BY c.company_id"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def select_company_ids_by_index(conn: DBConnection, index_name: str) -> list[Row]:
    """company_id for every company tagged with `index_name` in
    company_index_membership -- scripts/batch_fetch_nse.py's `--index`
    company-list source."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id FROM company_index_membership WHERE index_name = %s ORDER BY company_id",
            (index_name,),
        )
        return cur.fetchall()


def select_company_ids_by_tag_column(conn: DBConnection, column: str, value: str) -> list[Row]:
    """company_id for every active company whose `sector`/`industry` column
    equals `value`, across BOTH countries -- unlike select_companies_by_
    sector_column() above (India-only basic_industry/macro_economic_sector,
    NSE's own 4-level classification), the plain sector/industry columns
    this queries are populated for US companies too (e.g. sector=
    "Technology" for AAPL/MSFT/GOOGL/NVDA, verified against real data), so
    this is the one a cross-country tag lookup (retrieval/tag_resolver.py:
    "Technology companies") needs. `column` is re-checked against this
    module's own fixed allowlist before being interpolated into the query
    (a column name can't be a bind parameter), same defense
    select_companies_by_sector_column() already applies for its own
    allowlist."""
    if column not in _TAG_GROUP_COLUMNS:
        raise ValueError(f"column must be one of {sorted(_TAG_GROUP_COLUMNS)}, got {column!r}")
    sql = f"SELECT company_id FROM companies WHERE {_TAG_GROUP_COLUMNS[column]} = %s AND status = 'active' ORDER BY company_id"
    with conn.cursor() as cur:
        cur.execute(sql, (value,))
        return cur.fetchall()


def select_company_ids_by_status(conn: DBConnection, status: str) -> list[Row]:
    """company_id for every company at this lifecycle status ("active" |
    "archived", companies.status's own CHECK constraint) -- retrieval/
    tag_resolver.py's "archived companies" tag, the one dimension of that
    resolver with no country/index/sector precedent to reuse."""
    with conn.cursor() as cur:
        cur.execute("SELECT company_id FROM companies WHERE status = %s ORDER BY company_id", (status,))
        return cur.fetchall()


def select_active_companies_by_country(conn: DBConnection, country: str) -> list[Row]:
    """company_id for every active company registered under `country`
    (companies.country -- "IN"/"US" today) -- scripts/
    fetch_daily_prices_usa.py's ticker universe. Unlike the NSE-side
    queries above, there's no index-membership filter here: the US
    universe (a dozen companies today) is small enough that "every US
    company on file" is the whole ticker list, not just an index subset,
    and a couple of them (e.g. Lyft) aren't in any of the US indices
    already tagged in company_index_membership (S&P 500/Nasdaq 100/Dow)
    anyway -- filtering by one of those would silently drop them."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id FROM companies WHERE country = %s AND status = 'active' ORDER BY company_id",
            (country,),
        )
        return cur.fetchall()


def select_india_companies_not_in_index(conn: DBConnection, exclude_index_name: str) -> list[Row]:
    """company_id for every Indian company NOT tagged `exclude_index_name` in
    company_index_membership -- scripts/tag_nifty_microcap.py's source set
    (everything country='IN' outside Nifty 500, the tier NSE's own index
    universe stops covering)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id FROM companies
            WHERE country = 'IN' AND company_id NOT IN (
                SELECT company_id FROM company_index_membership WHERE index_name = %s
            )
            ORDER BY company_id
            """,
            (exclude_index_name,),
        )
        return cur.fetchall()


def tag_companies_index(conn: DBConnection, company_ids: list[str], index_name: str) -> int:
    """Additive tag, not set_company_index_tags()'s "replace this company's
    whole tag set" -- that function DELETEs a company's existing
    company_index_membership rows first, which would silently drop any
    other index tags (BSE indices, Nifty sub-variants) these companies
    already carry. `ON CONFLICT (company_id, index_name) DO NOTHING` (the
    table's own composite PRIMARY KEY, schemas/postgres_schema.sql) makes
    re-running this against an overlapping company_ids list free, same as
    SQLite's `INSERT OR IGNORE`. Returns how many rows were newly tagged (0
    if every company was already tagged).

    Same `execute_values()` + `RETURNING ... ` + count-the-returned-rows
    rewrite as insert_corporate_actions_raw() above, for the same reason
    (no Postgres equivalent of SQLite's conn.total_changes, and
    executemany()'s rowcount is unreliable for ON CONFLICT DO NOTHING)."""
    if not company_ids:
        return 0
    with conn.cursor() as cur:
        inserted = execute_values(
            cur,
            "INSERT INTO company_index_membership (company_id, index_name) VALUES %s "
            "ON CONFLICT (company_id, index_name) DO NOTHING RETURNING company_id",
            [(company_id, index_name) for company_id in company_ids],
            fetch=True,
        )
    conn.commit()
    return len(inserted)


def update_company_valuation_model_file(conn: DBConnection, company_id: str, valuation_model_file: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET valuation_model_file = %s WHERE company_id = %s",
            (valuation_model_file, company_id),
        )
    conn.commit()
