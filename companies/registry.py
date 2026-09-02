"""CompanyRegistry — create/read/list company records.

company_id is the stable internal identifier; ticker symbols may change
around it (README: Company Master). Archiving/restoring lives in
companies/lifecycle.py, not here.
"""

from __future__ import annotations

from normalization.companies import normalize_company_id
from storage import company_repository as repo
from storage.database import utcnow_iso
from storage.db_types import DBConnection, Row

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
    conn: DBConnection,
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

    fields = dict(
        company_id=company_id, legal_name=legal_name, display_name=display_name, nse_symbol=nse_symbol,
        bse_code=bse_code, isin=isin, country=country, currency=currency,
        fiscal_year_end_month=fiscal_year_end_month, website=website,
        macro_economic_sector=macro_economic_sector, sector=sector, industry=industry,
        basic_industry=basic_industry, listed_date=listed_date, now=now,
    )
    if repo.select_company_id(conn, company_id) is None:
        repo.insert_company(conn, **fields)
    else:
        repo.update_company(conn, **fields)
    return company_id


def get_company(conn: DBConnection, company_id: str) -> Row | None:
    return repo.select_company(conn, normalize_company_id(company_id))


def update_company_website(conn: DBConnection, company_id: str, website: str) -> None:
    """Set just the website column — unlike register_company(), doesn't
    require every other mutable field passed back through to avoid stomping
    it. For scripts/backfill_company_websites.py's one-time yfinance
    backfill of the handful of US companies registered without one (unlike
    the Indian ones, which already carry it)."""
    repo.update_company_website_row(conn, normalize_company_id(company_id), website, utcnow_iso())


_SECTOR_PEER_FIELDS = ("basic_industry", "macro_economic_sector")


def list_companies_by_sector_field(
    conn: DBConnection, field: str, value: str, exclude_company_id: str
) -> list[Row]:
    """Companies sharing one sector-classification column's value, used by
    context/graph.py's sector-peer traversal. `field` is restricted to the
    two known sector columns and validated here (never
    string-formatted from a caller-supplied value) — no injection surface
    even though today's only caller already only passes a fixed literal."""
    if field not in _SECTOR_PEER_FIELDS:
        raise ValueError(f"field must be one of {_SECTOR_PEER_FIELDS}, got {field!r}")
    return repo.select_companies_by_sector_column(conn, field, value, exclude_company_id)


def list_companies_with_sector(conn: DBConnection) -> list[Row]:
    """Every company with one resolved sector value (basic_industry
    preferred over macro_economic_sector, same preference _sector_peers()
    above/context/graph.py's own logic uses) — used for a full graph
    resync (context/graph_neo4j.py's sync_graph()), not a per-request path."""
    return repo.select_companies_with_sector_column(conn)


def search_companies(
    conn: DBConnection, query: str, *, limit: int = 8, index_name: str | None = None
) -> list[Row]:
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
    stripped = query.strip()
    if not stripped:
        return []
    return repo.search_companies_rows(
        conn, f"%{stripped}%", f"{stripped}%", limit, index_name=index_name
    )


def list_companies(conn: DBConnection, *, include_archived: bool = False) -> list[Row]:
    return repo.select_companies(conn, include_archived=include_archived)


def seed_companies(conn: DBConnection) -> list[str]:
    """Register the POC seed companies (HDFCBANK, ICICIBANK). Returns their company_ids."""
    return [register_company(conn, **company) for company in SEED_COMPANIES]
