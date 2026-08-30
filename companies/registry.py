"""CompanyRegistry — create/read/list company records.

company_id is the stable internal identifier; ticker symbols may change
around it (README: Company Master). Archiving/restoring lives in
companies/lifecycle.py, not here.
"""

from __future__ import annotations

import sqlite3

from normalization.companies import normalize_company_id
from storage.database import utcnow_iso

# The two POC companies named in the README's Implementation Sequence step 2.
# Convenience seed data only — register_company() works for any company.
SEED_COMPANIES: list[dict[str, str]] = [
    {
        "company_id": "HDFCBANK",
        "legal_name": "HDFC Bank Limited",
        "display_name": "HDFC Bank",
        "nse_symbol": "HDFCBANK",
        "bse_code": "500180",
        "macro_economic_sector": "Financial Services",
        "sector": "Financial Services",
        "industry": "Banks",
        "basic_industry": "Private Sector Bank",
    },
    {
        "company_id": "ICICIBANK",
        "legal_name": "ICICI Bank Limited",
        "display_name": "ICICI Bank",
        "nse_symbol": "ICICIBANK",
        "bse_code": "532174",
        "macro_economic_sector": "Financial Services",
        "sector": "Financial Services",
        "industry": "Banks",
        "basic_industry": "Private Sector Bank",
    },
]


def register_company(
    conn: sqlite3.Connection,
    company_id: str,
    legal_name: str,
    display_name: str,
    *,
    nse_symbol: str | None = None,
    bse_code: str | None = None,
    isin: str | None = None,
    country: str = "IN",
    currency: str = "INR",
    fiscal_year_end_month: int = 3,
    website: str | None = None,
    macro_economic_sector: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    basic_industry: str | None = None,
    listed_date: str | None = None,
) -> str:
    """Insert a new company, or update its mutable fields if it already exists.

    macro_economic_sector/sector/industry/basic_industry are NSE's own
    4-level classification (broadest to most granular — e.g. "Financial
    Services" / "Financial Services" / "Banks" / "Private Sector Bank" for
    HDFC Bank), not a looser in-house taxonomy — meaningless for a non-Indian
    company, left None there.

    country/currency/fiscal_year_end_month default to India/INR/March-close
    (every company here was, until multi-country support existed) — same
    "overwrite with exactly what's passed" contract as every other field: a
    caller re-registering an existing company without knowledge of these
    (e.g. companies/nse_import.py's periodic refresh) must pass the existing
    row's values through itself, or risk stomping a manually-registered
    non-Indian company back to India/INR/March-close.

    Returns the normalized company_id. company_id itself is immutable once
    created — re-registering the same id updates everything except status
    and the identifiers that would need lifecycle history (see
    companies/lifecycle.py) if they changed.
    """
    company_id = normalize_company_id(company_id)
    now = utcnow_iso()

    existing = conn.execute(
        "SELECT company_id FROM companies WHERE company_id = ?", (company_id,)
    ).fetchone()

    if existing is None:
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
    else:
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
    return company_id


def get_company(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM companies WHERE company_id = ?",
        (normalize_company_id(company_id),),
    ).fetchone()


def update_company_website(conn: sqlite3.Connection, company_id: str, website: str) -> None:
    """Set just the website column — unlike register_company(), doesn't
    require every other mutable field passed back through to avoid stomping
    it. For scripts/backfill_company_websites.py's one-time yfinance
    backfill of the handful of US companies registered without one (unlike
    the Indian ones, which already carry it)."""
    conn.execute(
        "UPDATE companies SET website = ?, updated_at = ? WHERE company_id = ?",
        (website, utcnow_iso(), normalize_company_id(company_id)),
    )
    conn.commit()


_SECTOR_PEER_FIELDS = ("basic_industry", "macro_economic_sector")


def list_companies_by_sector_field(
    conn: sqlite3.Connection, field: str, value: str, exclude_company_id: str
) -> list[sqlite3.Row]:
    """Companies sharing one sector-classification column's value, used by
    context/graph.py's sector-peer traversal. `field` is restricted to the
    two known sector columns and branched on internally (never
    string-formatted from a caller-supplied value) — no injection surface
    even though today's only caller already only passes a fixed literal."""
    if field not in _SECTOR_PEER_FIELDS:
        raise ValueError(f"field must be one of {_SECTOR_PEER_FIELDS}, got {field!r}")
    if field == "basic_industry":
        sql = "SELECT company_id FROM companies WHERE basic_industry = ? AND company_id != ?"
    else:
        sql = "SELECT company_id FROM companies WHERE macro_economic_sector = ? AND company_id != ?"
    return conn.execute(sql, (value, exclude_company_id)).fetchall()


def list_companies_with_sector(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every company with one resolved sector value (basic_industry
    preferred over macro_economic_sector, same preference _sector_peers()
    above/context/graph.py's own logic uses) — used for a full graph
    resync (context/graph_neo4j.py's sync_graph()), not a per-request path."""
    return conn.execute(
        "SELECT company_id, COALESCE(NULLIF(basic_industry, ''), NULLIF(macro_economic_sector, '')) AS sector "
        "FROM companies"
    ).fetchall()


def search_companies(
    conn: sqlite3.Connection, query: str, *, limit: int = 8, index_name: str | None = None
) -> list[sqlite3.Row]:
    """Active companies whose id/display name/legal name/NSE symbol contains
    `query` (case-insensitive substring, not a fuzzy match) — the header
    search box's typeahead. company_id first (exact-prefix tickers like
    "HDFC" surface before a display-name substring match buried elsewhere),
    then display_name.

    index_name optionally restricts results to companies tagged with that
    index in company_index_membership (e.g. "Nifty 500") — the Charts tab's
    "Compare With" search (web/static/js/charts_overlay.js) only offers
    companies the price-history db actually covers. None (the default)
    keeps today's unfiltered behavior, so the header search is unaffected."""
    like = f"%{query.strip()}%"
    if not query.strip():
        return []
    index_clause = (
        "AND EXISTS (SELECT 1 FROM company_index_membership m WHERE m.company_id = companies.company_id AND m.index_name = ?)"
        if index_name is not None
        else ""
    )
    params: tuple = (like, like, like, like)
    if index_name is not None:
        params += (index_name,)
    params += (f"{query.strip()}%", limit)
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


def list_companies(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[sqlite3.Row]:
    if include_archived:
        return conn.execute("SELECT * FROM companies ORDER BY company_id").fetchall()
    return conn.execute(
        "SELECT * FROM companies WHERE status = 'active' ORDER BY company_id"
    ).fetchall()


def seed_companies(conn: sqlite3.Connection) -> list[str]:
    """Register the POC seed companies (HDFCBANK, ICICIBANK). Returns their company_ids."""
    return [register_company(conn, **company) for company in SEED_COMPANIES]
