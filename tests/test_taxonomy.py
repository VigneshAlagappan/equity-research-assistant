"""storage/repositories.py's Sector/Industry/Index-tag lookup tables — the
Admin "Sectors, Industries & Tags" panel's backing functions. All three
follow the same shape (list/add/rename/delete + a company-usage count), so
these tests exercise sectors directly and only re-check the industry/
index-tag variants where their cascade target differs (companies.industry,
company_index_membership)."""

from __future__ import annotations

import sqlite3

from companies.registry import register_company
from storage.database import init_db
from storage.repositories import (
    add_industry,
    add_index_definition,
    add_sector,
    count_companies_by_index_tag,
    count_companies_by_industry,
    count_companies_by_sector,
    delete_index_definition,
    delete_industry,
    delete_sector,
    list_index_definitions,
    list_industries,
    list_sectors,
    rename_index_definition,
    rename_industry,
    rename_sector,
    set_company_index_tags,
)


def test_seeding_backfills_from_existing_company_usage(tmp_path) -> None:
    """A database that already had companies with sector/industry values
    before these tables existed must not lose those values as dropdown
    options — init_db()'s seeding step backfills from actual usage."""
    conn = init_db(db_path=tmp_path / "seed.db")
    register_company(conn, "ACME", "Acme Ltd", "Acme", sector="Energy", industry="Oil & Gas")

    # Re-running init_db() (e.g. the next process start) must not wipe or
    # duplicate what's already there — INSERT OR IGNORE, not INSERT.
    conn2 = init_db(db_path=tmp_path / "seed.db")
    assert "Energy" in list_sectors(conn2)
    assert "Oil & Gas" in list_industries(conn2)
    assert list_sectors(conn2).count("Energy") == 1


def test_add_sector_is_idempotent(db_conn: sqlite3.Connection) -> None:
    add_sector(db_conn, "Energy")
    add_sector(db_conn, "Energy")  # INSERT OR IGNORE — no error, no duplicate
    assert list_sectors(db_conn).count("Energy") == 1


def test_rename_sector_cascades_to_companies(db_conn: sqlite3.Connection) -> None:
    add_sector(db_conn, "Energy")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme", sector="Energy")
    register_company(db_conn, "OILCO", "Oil Co Ltd", "Oil Co", sector="Energy")

    rename_sector(db_conn, "Energy", "Energy & Utilities")

    assert "Energy" not in list_sectors(db_conn)
    assert "Energy & Utilities" in list_sectors(db_conn)
    rows = db_conn.execute("SELECT company_id, sector FROM companies ORDER BY company_id").fetchall()
    assert {r["company_id"]: r["sector"] for r in rows} == {
        "ACME": "Energy & Utilities", "OILCO": "Energy & Utilities",
    }


def test_delete_sector_clears_companies_to_null_not_reassign(db_conn: sqlite3.Connection) -> None:
    add_sector(db_conn, "Energy")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme", sector="Energy")

    delete_sector(db_conn, "Energy")

    assert "Energy" not in list_sectors(db_conn)
    row = db_conn.execute("SELECT sector FROM companies WHERE company_id = 'ACME'").fetchone()
    assert row["sector"] is None


def test_delete_sector_with_zero_companies(db_conn: sqlite3.Connection) -> None:
    add_sector(db_conn, "Unused Sector")
    delete_sector(db_conn, "Unused Sector")
    assert "Unused Sector" not in list_sectors(db_conn)


def test_count_companies_by_sector(db_conn: sqlite3.Connection) -> None:
    add_sector(db_conn, "Energy")
    add_sector(db_conn, "Financial Services")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme", sector="Energy")
    register_company(db_conn, "OILCO", "Oil Co Ltd", "Oil Co", sector="Energy")
    register_company(db_conn, "NOSECTOR", "No Sector Ltd", "No Sector")  # sector=None

    counts = count_companies_by_sector(db_conn)
    assert counts == {"Energy": 2}  # Financial Services (0) and None omitted, not zero-valued


def test_add_sector_can_precede_any_company_using_it(db_conn: sqlite3.Connection) -> None:
    """A sector can exist in the lookup table with zero companies attached
    — "add before assigning" is the whole point of a real lookup table
    over deriving options purely from current company usage."""
    add_sector(db_conn, "Quantum Computing")
    assert "Quantum Computing" in list_sectors(db_conn)
    assert count_companies_by_sector(db_conn).get("Quantum Computing", 0) == 0


# ------------------------------------------------------------------
# Industry — same shape as sector, spot-checked for its own cascade target.
# ------------------------------------------------------------------


def test_rename_industry_cascades_to_companies(db_conn: sqlite3.Connection) -> None:
    add_industry(db_conn, "Oil & Gas")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme", industry="Oil & Gas")

    rename_industry(db_conn, "Oil & Gas", "Oil, Gas & Renewables")

    row = db_conn.execute("SELECT industry FROM companies WHERE company_id = 'ACME'").fetchone()
    assert row["industry"] == "Oil, Gas & Renewables"


def test_delete_industry_clears_companies(db_conn: sqlite3.Connection) -> None:
    add_industry(db_conn, "Oil & Gas")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme", industry="Oil & Gas")
    delete_industry(db_conn, "Oil & Gas")
    row = db_conn.execute("SELECT industry FROM companies WHERE company_id = 'ACME'").fetchone()
    assert row["industry"] is None


# ------------------------------------------------------------------
# Index tags — cascades to company_index_membership, not a companies column.
# ------------------------------------------------------------------


def test_rename_index_definition_cascades_to_memberships(db_conn: sqlite3.Connection) -> None:
    add_index_definition(db_conn, "Nifty 50")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme")
    set_company_index_tags(db_conn, "ACME", ["Nifty 50"])

    rename_index_definition(db_conn, "Nifty 50", "Nifty Fifty")

    assert "Nifty 50" not in list_index_definitions(db_conn)
    assert "Nifty Fifty" in list_index_definitions(db_conn)
    row = db_conn.execute(
        "SELECT index_name FROM company_index_membership WHERE company_id = 'ACME'"
    ).fetchone()
    assert row["index_name"] == "Nifty Fifty"


def test_delete_index_definition_removes_memberships(db_conn: sqlite3.Connection) -> None:
    add_index_definition(db_conn, "Nifty 50")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme")
    set_company_index_tags(db_conn, "ACME", ["Nifty 50"])

    delete_index_definition(db_conn, "Nifty 50")

    assert "Nifty 50" not in list_index_definitions(db_conn)
    rows = db_conn.execute("SELECT * FROM company_index_membership WHERE company_id = 'ACME'").fetchall()
    assert rows == []


def test_set_company_index_tags_rejects_a_tag_not_in_index_definitions(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "ACME", "Acme Ltd", "Acme")
    import pytest

    with pytest.raises(ValueError, match="Unknown index name"):
        set_company_index_tags(db_conn, "ACME", ["Not A Real Index"])


def test_count_companies_by_index_tag(db_conn: sqlite3.Connection) -> None:
    add_index_definition(db_conn, "Nifty 50")
    register_company(db_conn, "ACME", "Acme Ltd", "Acme")
    set_company_index_tags(db_conn, "ACME", ["Nifty 50"])

    assert count_companies_by_index_tag(db_conn) == {"Nifty 50": 1}
