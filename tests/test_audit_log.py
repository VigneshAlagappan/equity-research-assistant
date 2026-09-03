"""Admin Audit Log backing queries (storage/repositories.py):
list_reconciliation_log() and list_xbrl_migration_status() — the "what's
pending for NSE XBRL migration" view."""

from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from sources.base import NormalizedObservation
from storage.repositories import (
    insert_financial_observations,
    list_reconciliation_log,
    list_reconciliation_log_by_company,
    list_xbrl_migration_status,
    reconcile_batch,
)


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    return db_conn


def _obs(company_id: str, source: str, metric_key: str, fiscal_year: str, quarter: str, value: float) -> NormalizedObservation:
    return NormalizedObservation(
        company_id=company_id, metric_key=metric_key, period_type="quarterly",
        fiscal_year=fiscal_year, quarter=quarter, statement_type="consolidated",
        value=value, unit="INR_CRORE", source=source, source_file=f"{source}.file",
        parser_version="v1", retrieved_at="2026-06-01T00:00:00+00:00",
    )


def test_migration_status_pending_when_legacy_ahead_of_xbrl(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    legacy_q2 = _obs("ACME", "proprietary", "net_profit", "FY2025", "Q2", 100.0)
    xbrl_q1 = _obs("ACME", "nse", "net_profit", "FY2025", "Q1", 90.0)
    insert_financial_observations(conn, [legacy_q2, xbrl_q1])
    reconcile_batch(conn, [legacy_q2, xbrl_q1])

    status = {row["company_id"]: row for row in list_xbrl_migration_status(conn)}
    assert status["ACME"]["migration_status"] == "pending"
    assert status["ACME"]["latest_xbrl_period"] == "FY2025Q1"
    assert status["ACME"]["latest_legacy_period"] == "FY2025Q2"


def test_migration_status_up_to_date_when_xbrl_covers_latest(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    legacy_q1 = _obs("ACME", "proprietary", "net_profit", "FY2025", "Q1", 90.0)
    xbrl_q2 = _obs("ACME", "nse", "net_profit", "FY2025", "Q2", 100.0)
    insert_financial_observations(conn, [legacy_q1, xbrl_q2])
    reconcile_batch(conn, [legacy_q1, xbrl_q2])

    status = {row["company_id"]: row for row in list_xbrl_migration_status(conn)}
    assert status["ACME"]["migration_status"] == "up_to_date"


def test_migration_status_not_started_when_no_xbrl_at_all(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    legacy = _obs("ACME", "proprietary", "net_profit", "FY2025", "Q1", 90.0)
    insert_financial_observations(conn, [legacy])
    reconcile_batch(conn, [legacy])

    status = {row["company_id"]: row for row in list_xbrl_migration_status(conn)}
    assert status["ACME"]["migration_status"] == "not_started"
    assert status["ACME"]["latest_xbrl_period"] is None


def test_migration_status_no_data_when_nothing_on_file(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")

    status = {row["company_id"]: row for row in list_xbrl_migration_status(conn)}
    assert status["ACME"]["migration_status"] == "no_data"


def test_migration_status_excludes_companies_without_nse_symbol(conn: sqlite3.Connection) -> None:
    register_company(conn, "NOEXCH", "No Exchange Ltd", "No Exchange")  # no nse_symbol
    status = {row["company_id"]: row for row in list_xbrl_migration_status(conn)}
    assert "NOEXCH" not in status


def test_reconciliation_log_lists_newest_first_and_filters_by_company(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    register_company(conn, "OTHER", "Other Ltd", "Other", nse_symbol="OTHER")
    acme_obs = _obs("ACME", "proprietary", "net_profit", "FY2025", "Q1", 90.0)
    other_obs = _obs("OTHER", "proprietary", "net_profit", "FY2025", "Q1", 50.0)
    insert_financial_observations(conn, [acme_obs, other_obs])
    reconcile_batch(conn, [acme_obs, other_obs])

    all_rows = list_reconciliation_log(conn)
    assert len(all_rows) == 2

    acme_rows = list_reconciliation_log(conn, company_id="ACME")
    assert len(acme_rows) == 1
    assert acme_rows[0]["company_id"] == "ACME"
    assert acme_rows[0]["was_chosen"] == 1


def test_reconciliation_log_includes_rejected_candidates_with_null_canonical_id(conn: sqlite3.Connection) -> None:
    """A metric an XBRL filing didn't report gets its canonical row deleted
    (reconcile()'s migration branch) but the rejected legacy candidate must
    still show up in the audit trail, not silently vanish."""
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    legacy_eps = _obs("ACME", "proprietary", "eps", "FY2025", "Q1", 12.5)
    xbrl_net_profit = _obs("ACME", "nse", "net_profit", "FY2025", "Q1", 999.0)
    insert_financial_observations(conn, [legacy_eps, xbrl_net_profit])
    reconcile_batch(conn, [legacy_eps, xbrl_net_profit])

    rows = list_reconciliation_log(conn, company_id="ACME")
    eps_rows = [r for r in rows if r["metric_key"] == "eps"]
    assert len(eps_rows) == 1
    assert eps_rows[0]["was_chosen"] == 0
    assert "migrated" in eps_rows[0]["note"]


def test_reconciliation_log_by_company_batches_and_caps_per_company(conn: sqlite3.Connection) -> None:
    register_company(conn, "ACME", "Acme Ltd", "Acme", nse_symbol="ACME")
    register_company(conn, "OTHER", "Other Ltd", "Other", nse_symbol="OTHER")
    register_company(conn, "QUIET", "Quiet Ltd", "Quiet", nse_symbol="QUIET")  # no observations at all
    acme_obs = [_obs("ACME", "proprietary", "net_profit", "FY2025", q, 10.0) for q in ("Q1", "Q2", "Q3")]
    other_obs = [_obs("OTHER", "proprietary", "net_profit", "FY2025", "Q1", 50.0)]
    insert_financial_observations(conn, acme_obs + other_obs)
    reconcile_batch(conn, acme_obs + other_obs)

    by_company = list_reconciliation_log_by_company(conn, ["ACME", "OTHER", "QUIET"], limit_per_company=2)

    assert len(by_company["ACME"]) == 2  # capped, not all 3
    assert len(by_company["OTHER"]) == 1
    assert "QUIET" not in by_company  # no rows at all -> absent, not an empty list
    # newest first
    assert by_company["ACME"][0]["considered_at"] >= by_company["ACME"][1]["considered_at"]


def test_reconciliation_log_by_company_empty_input(conn: sqlite3.Connection) -> None:
    assert list_reconciliation_log_by_company(conn, []) == {}
