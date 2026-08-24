from __future__ import annotations

import sqlite3

import pytest

from companies.lifecycle import (
    CompanyNotActiveError,
    CompanyNotFoundError,
    InvalidArchiveReasonError,
    archive_company,
    assert_active,
    restore_company,
)
from companies.registry import get_company, list_companies, register_company, seed_companies


def test_register_and_get_company(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "hdfcbank", "HDFC Bank Limited", "HDFC Bank", nse_symbol="HDFCBANK")
    row = get_company(db_conn, "HDFCBANK")
    assert row is not None
    assert row["legal_name"] == "HDFC Bank Limited"
    assert row["status"] == "active"


def test_register_company_normalizes_id(db_conn: sqlite3.Connection) -> None:
    company_id = register_company(db_conn, "  hdfcbank ", "HDFC Bank Limited", "HDFC Bank")
    assert company_id == "HDFCBANK"


def test_register_company_is_upsert(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", sector="Old Sector")
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", sector="Financial Services")
    row = get_company(db_conn, "HDFCBANK")
    assert row["sector"] == "Financial Services"


def test_seed_companies(db_conn: sqlite3.Connection) -> None:
    company_ids = seed_companies(db_conn)
    assert set(company_ids) == {"HDFCBANK", "ICICIBANK"}
    assert len(list_companies(db_conn)) == 2


def test_list_companies_excludes_archived_by_default(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    archive_company(db_conn, "HDFCBANK", "manual")

    active = list_companies(db_conn)
    assert [c["company_id"] for c in active] == ["ICICIBANK"]

    everyone = list_companies(db_conn, include_archived=True)
    assert len(everyone) == 2


def test_archive_company_rejects_invalid_reason(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    with pytest.raises(InvalidArchiveReasonError):
        archive_company(db_conn, "HDFCBANK", "not-a-real-reason")


def test_archive_company_raises_for_unknown_company(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(CompanyNotFoundError):
        archive_company(db_conn, "NOPE", "manual")


def test_archive_and_restore_round_trip(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    archive_company(db_conn, "HDFCBANK", "delisted")

    row = get_company(db_conn, "HDFCBANK")
    assert row["status"] == "archived"
    assert row["archive_reason"] == "delisted"

    restore_company(db_conn, "HDFCBANK")
    row = get_company(db_conn, "HDFCBANK")
    assert row["status"] == "active"
    assert row["archive_reason"] is None


def test_assert_active_gate(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert_active(db_conn, "HDFCBANK")  # should not raise

    archive_company(db_conn, "HDFCBANK", "manual")
    with pytest.raises(CompanyNotActiveError):
        assert_active(db_conn, "HDFCBANK")


def test_assert_active_raises_for_unregistered_company(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(CompanyNotActiveError):
        assert_active(db_conn, "UNKNOWN")
