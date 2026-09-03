"""Repository layer for `companies` and `stock_actions` — every raw SQL
statement `companies/registry.py`, `companies/lifecycle.py`, and
`companies/stock_actions.py` need, so those modules hold validation/business
rules only and never call `conn.execute(...)` themselves (same "business
logic never depends on SQLite-specific behavior" contract
`storage/repositories.py` and `storage/fact_store.py` already establish for
the rest of the app).

Functions here take already-validated, already-normalized arguments (e.g. a
`company_id` the caller has already run through `normalize_company_id()`,
an `action_type` the caller has already checked against `ACTION_TYPES`) —
this module's job is only "run this exact query," not re-validate business
rules a caller already enforced.
"""

from __future__ import annotations

from storage.db_types import DBConnection, Row

_SECTOR_PEER_COLUMNS = {"basic_industry": "basic_industry", "macro_economic_sector": "macro_economic_sector"}


def select_company_id(conn: DBConnection, company_id: str) -> Row | None:
    return conn.execute("SELECT company_id FROM companies WHERE company_id = ?", (company_id,)).fetchone()


def insert_company(
    conn: DBConnection, *, company_id: str, legal_name: str, display_name: str, nse_symbol: str | None,
    bse_code: str | None, isin: str | None, country: str, currency: str, fiscal_year_end_month: int,
    website: str | None, macro_economic_sector: str | None, sector: str | None, industry: str | None,
    basic_industry: str | None, listed_date: str | None, now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO companies (
            company_id, legal_name, display_name, nse_symbol, bse_code, isin, country, currency,
            fiscal_year_end_month, website,
            macro_economic_sector, sector, industry, basic_industry,
            status, listed_date, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
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
    conn.execute(
        """
        UPDATE companies SET
            legal_name = ?, display_name = ?, nse_symbol = ?, bse_code = ?, isin = ?,
            country = ?, currency = ?, fiscal_year_end_month = ?, website = ?,
            macro_economic_sector = ?, sector = ?, industry = ?, basic_industry = ?,
            listed_date = ?, updated_at = ?
        WHERE company_id = ?
        """,
        (legal_name, display_name, nse_symbol, bse_code, isin, country, currency, fiscal_year_end_month,
         website, macro_economic_sector, sector, industry, basic_industry, listed_date, now, company_id),
    )
    conn.commit()


def select_company(conn: DBConnection, company_id: str) -> Row | None:
    return conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()


def update_company_website_row(conn: DBConnection, company_id: str, website: str, now: str) -> None:
    conn.execute(
        "UPDATE companies SET website = ?, updated_at = ? WHERE company_id = ?",
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
    sql = f"SELECT company_id FROM companies WHERE {_SECTOR_PEER_COLUMNS[column]} = ? AND company_id != ?"
    return conn.execute(sql, (value, exclude_company_id)).fetchall()


def select_companies_with_sector_column(conn: DBConnection) -> list[Row]:
    return conn.execute(
        "SELECT company_id, COALESCE(NULLIF(basic_industry, ''), NULLIF(macro_economic_sector, '')) AS sector "
        "FROM companies"
    ).fetchall()


def search_companies_rows(
    conn: DBConnection, like: str, prefix_like: str, limit: int, *, index_name: str | None,
) -> list[Row]:
    index_clause = (
        "AND EXISTS (SELECT 1 FROM company_index_membership m WHERE m.company_id = companies.company_id AND m.index_name = ?)"
        if index_name is not None
        else ""
    )
    params: tuple = (like, like, like, like)
    if index_name is not None:
        params += (index_name,)
    params += (prefix_like, limit)
    return conn.execute(
        f"""
        SELECT * FROM companies
        WHERE status = 'active' AND (
            company_id LIKE ? COLLATE NOCASE
            OR display_name LIKE ? COLLATE NOCASE
            OR legal_name LIKE ? COLLATE NOCASE
            OR nse_symbol LIKE ? COLLATE NOCASE
        )
        {index_clause}
        ORDER BY
            CASE WHEN company_id LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
            display_name
        LIMIT ?
        """,
        params,
    ).fetchall()


def select_companies(conn: DBConnection, *, include_archived: bool) -> list[Row]:
    if include_archived:
        return conn.execute("SELECT * FROM companies ORDER BY company_id").fetchall()
    return conn.execute("SELECT * FROM companies WHERE status = 'active' ORDER BY company_id").fetchall()


def update_company_lifecycle_status(
    conn: DBConnection, company_id: str, *, status: str, archived_at: str | None, archive_reason: str | None, now: str,
) -> int:
    """Used by both archive_company() (status='archived', reason set) and
    restore_company() (status='active', archived_at/reason cleared to
    None). Returns rowcount so the caller can tell "no such company" apart
    from a successful flip without a second SELECT."""
    cursor = conn.execute(
        """
        UPDATE companies SET status = ?, archived_at = ?, archive_reason = ?, updated_at = ?
        WHERE company_id = ?
        """,
        (status, archived_at, archive_reason, now, company_id),
    )
    conn.commit()
    return cursor.rowcount


def select_company_status(conn: DBConnection, company_id: str) -> Row | None:
    return conn.execute("SELECT status FROM companies WHERE company_id = ?", (company_id,)).fetchone()


def insert_stock_action(
    conn: DBConnection, *, company_id: str, action_type: str, action_date: str, ratio_from: float, ratio_to: float,
    subscription_price: float | None, source: str | None, source_url: str | None, notes: str | None, now: str,
) -> Row:
    cursor = conn.execute(
        """
        INSERT INTO stock_actions (
            company_id, action_type, action_date, ratio_from, ratio_to,
            subscription_price, source, source_url, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, action_type, action_date, ratio_from, ratio_to,
         subscription_price, source, source_url, notes, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM stock_actions WHERE action_id = ?", (cursor.lastrowid,)).fetchone()


def select_stock_actions(conn: DBConnection, company_id: str) -> list[Row]:
    return conn.execute(
        "SELECT * FROM stock_actions WHERE company_id = ? ORDER BY action_date DESC, action_id DESC",
        (company_id,),
    ).fetchall()


def delete_stock_action_row(conn: DBConnection, company_id: str, action_id: int) -> int:
    cursor = conn.execute(
        "DELETE FROM stock_actions WHERE action_id = ? AND company_id = ?", (action_id, company_id)
    )
    conn.commit()
    return cursor.rowcount


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
        query += " AND company_id = ?"
        params = (company_id,)
    query += " ORDER BY company_id"
    return conn.execute(query, params).fetchall()


def select_companies_missing_sector_or_industry(conn: DBConnection) -> list[Row]:
    return conn.execute(
        "SELECT company_id, nse_symbol, sector, industry FROM companies "
        "WHERE (sector IS NULL OR industry IS NULL) AND nse_symbol IS NOT NULL "
        "ORDER BY company_id"
    ).fetchall()


def update_company_sector_industry(conn: DBConnection, company_id: str, *, sector: str | None, industry: str | None) -> None:
    conn.execute(
        "UPDATE companies SET sector = ?, industry = ? WHERE company_id = ?",
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
        WHERE m.index_name = ? AND c.nse_symbol IS NOT NULL
    """
    params: tuple = (index_name,)
    if company_id is not None:
        query += " AND c.company_id = ?"
        params += (company_id,)
    query += " ORDER BY c.company_id"
    return conn.execute(query, params).fetchall()


def select_company_ids_by_index(conn: DBConnection, index_name: str) -> list[Row]:
    """company_id for every company tagged with `index_name` in
    company_index_membership -- scripts/batch_fetch_nse.py's `--index`
    company-list source."""
    return conn.execute(
        "SELECT company_id FROM company_index_membership WHERE index_name = ? ORDER BY company_id",
        (index_name,),
    ).fetchall()


def update_company_valuation_model_file(conn: DBConnection, company_id: str, valuation_model_file: str) -> None:
    conn.execute(
        "UPDATE companies SET valuation_model_file = ? WHERE company_id = ?",
        (valuation_model_file, company_id),
    )
    conn.commit()
