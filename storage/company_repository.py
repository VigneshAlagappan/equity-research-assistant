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

import json

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


def insert_corporate_actions_raw(conn: DBConnection, company_id: str, actions: list, *, now: str) -> int:
    """Bulk-insert NSE's raw corporate-actions rows for one company --
    `INSERT OR IGNORE` on (company_id, ex_date, subject) makes a re-fetch
    idempotent (same convention as shareholding's "already fetched"
    tracking), so calling this again with an overlapping/full history is
    always safe. `actions` is a list of
    sources.nse_corporate_actions.CorporateActionRef. Returns how many rows
    were newly inserted (0 if every row was already on file)."""
    changes_before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO corporate_actions_raw (
            company_id, subject, ex_date, record_date, face_value,
            bc_start_date, bc_end_date, raw_json, source, retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nse', ?)
        """,
        [
            (
                company_id, a.subject, a.ex_date.isoformat(),
                a.record_date.isoformat() if a.record_date else None,
                a.face_value,
                a.bc_start_date.isoformat() if a.bc_start_date else None,
                a.bc_end_date.isoformat() if a.bc_end_date else None,
                json.dumps(a.raw), now,
            )
            for a in actions
        ],
    )
    conn.commit()
    # executemany()'s own cursor.rowcount isn't reliable for INSERT OR
    # IGNORE (ignored conflicts aren't consistently excluded across sqlite3
    # versions) -- conn.total_changes only increments for rows actually
    # written, so a before/after diff is the correct "how many new" count.
    return conn.total_changes - changes_before


def select_unprocessed_corporate_actions_raw(conn: DBConnection, company_id: str) -> list[Row]:
    return conn.execute(
        "SELECT * FROM corporate_actions_raw WHERE company_id = ? AND processed_at IS NULL ORDER BY ex_date",
        (company_id,),
    ).fetchall()


def select_all_corporate_actions_raw(conn: DBConnection, company_id: str) -> list[Row]:
    """Every raw row for this company regardless of processed_at -- for
    re-classifying history after a classify_action_type() rule change
    (ingestion/corporate_actions.py's reclassify_company_corporate_actions),
    as opposed to select_unprocessed_corporate_actions_raw's "only what's
    new" scope used by the normal ingest pass."""
    return conn.execute(
        "SELECT * FROM corporate_actions_raw WHERE company_id = ? ORDER BY ex_date",
        (company_id,),
    ).fetchall()


def mark_corporate_actions_raw_processed(conn: DBConnection, raw_id: int, *, now: str) -> None:
    conn.execute("UPDATE corporate_actions_raw SET processed_at = ? WHERE raw_id = ?", (now, raw_id))
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
    classifier's last-run time."""
    conn.execute(
        """
        INSERT INTO corporate_actions (
            raw_id, company_id, action_type, subject, ex_date, record_date,
            face_value, classifier_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(raw_id) DO UPDATE SET
            action_type = excluded.action_type,
            subject = excluded.subject,
            ex_date = excluded.ex_date,
            record_date = excluded.record_date,
            face_value = excluded.face_value,
            classifier_version = excluded.classifier_version
        """,
        (raw_id, company_id, action_type, subject, ex_date, record_date, face_value, classifier_version, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM corporate_actions WHERE raw_id = ?", (raw_id,)).fetchone()


def select_corporate_actions(conn: DBConnection, company_id: str) -> list[Row]:
    return conn.execute(
        "SELECT * FROM corporate_actions WHERE company_id = ? ORDER BY ex_date DESC",
        (company_id,),
    ).fetchall()


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


_TAG_GROUP_COLUMNS = {"sector": "sector", "industry": "industry"}


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
    sql = f"SELECT company_id FROM companies WHERE {_TAG_GROUP_COLUMNS[column]} = ? AND status = 'active' ORDER BY company_id"
    return conn.execute(sql, (value,)).fetchall()


def select_company_ids_by_status(conn: DBConnection, status: str) -> list[Row]:
    """company_id for every company at this lifecycle status ("active" |
    "archived", companies.status's own CHECK constraint) -- retrieval/
    tag_resolver.py's "archived companies" tag, the one dimension of that
    resolver with no country/index/sector precedent to reuse."""
    return conn.execute(
        "SELECT company_id FROM companies WHERE status = ? ORDER BY company_id", (status,)
    ).fetchall()


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
    return conn.execute(
        "SELECT company_id FROM companies WHERE country = ? AND status = 'active' ORDER BY company_id",
        (country,),
    ).fetchall()


def select_india_companies_not_in_index(conn: DBConnection, exclude_index_name: str) -> list[Row]:
    """company_id for every Indian company NOT tagged `exclude_index_name` in
    company_index_membership -- scripts/tag_nifty_microcap.py's source set
    (everything country='IN' outside Nifty 500, the tier NSE's own index
    universe stops covering)."""
    return conn.execute(
        """
        SELECT company_id FROM companies
        WHERE country = 'IN' AND company_id NOT IN (
            SELECT company_id FROM company_index_membership WHERE index_name = ?
        )
        ORDER BY company_id
        """,
        (exclude_index_name,),
    ).fetchall()


def tag_companies_index(conn: DBConnection, company_ids: list[str], index_name: str) -> int:
    """Additive tag, not set_company_index_tags()'s "replace this company's
    whole tag set" -- that function DELETEs a company's existing
    company_index_membership rows first, which would silently drop any
    other index tags (BSE indices, Nifty sub-variants) these companies
    already carry. INSERT OR IGNORE keyed on the table's own (company_id,
    index_name) uniqueness makes re-running this against an overlapping
    company_ids list free. Returns how many rows were newly tagged (0 if
    every company was already tagged)."""
    changes_before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO company_index_membership (company_id, index_name) VALUES (?, ?)",
        [(company_id, index_name) for company_id in company_ids],
    )
    conn.commit()
    return conn.total_changes - changes_before


def update_company_valuation_model_file(conn: DBConnection, company_id: str, valuation_model_file: str) -> None:
    conn.execute(
        "UPDATE companies SET valuation_model_file = ? WHERE company_id = ?",
        (valuation_model_file, company_id),
    )
    conn.commit()
