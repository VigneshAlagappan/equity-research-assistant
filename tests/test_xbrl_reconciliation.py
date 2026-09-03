"""NSE XBRL source-priority rule (config/settings.py's DEFAULT_SOURCES comment,
README: Source / Provenance & Reconciliation): once a reporting period has a
validated NSE observation on file, NSE XBRL is the sole source of truth for
that period — legacy sources are outranked for metrics XBRL reported, and
metrics XBRL didn't report go blank rather than falling back to legacy
("do not mix missing metrics from legacy data into that same period").
"""

from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from sources.base import NormalizedObservation
from storage.repositories import get_canonical_value, insert_financial_observations, reconcile_batch


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "ACME", "Acme Ltd", "Acme")
    return db_conn


def _obs(source: str, metric_key: str, value: float, **overrides) -> NormalizedObservation:
    defaults = dict(
        company_id="ACME",
        metric_key=metric_key,
        period_type="quarterly",
        fiscal_year="FY2026",
        quarter="Q1",
        statement_type="consolidated",
        value=value,
        unit="INR_CRORE",
        source=source,
        source_file=f"{source}.file",
        parser_version="v1",
        retrieved_at="2026-06-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return NormalizedObservation(**defaults)


def test_nse_outranks_proprietary_on_the_same_metric(conn: sqlite3.Connection) -> None:
    legacy = _obs("proprietary", "net_profit", 100.0, retrieved_at="2026-01-01T00:00:00+00:00")
    xbrl = _obs("nse", "net_profit", 999.0, retrieved_at="2026-06-01T00:00:00+00:00")

    insert_financial_observations(conn, [legacy])
    reconcile_batch(conn, [legacy])
    insert_financial_observations(conn, [xbrl])
    reconcile_batch(conn, [xbrl])

    row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "consolidated")
    assert row["canonical_value"] == 999.0
    assert "nse" in row["reconciliation_reason"]


def test_xbrl_beats_even_a_more_recently_reingested_legacy_row(conn: sqlite3.Connection) -> None:
    """trust_rank, not recency, decides once a period is migrated — a legacy
    file re-ingested after the XBRL filing must not win back the metric."""
    xbrl = _obs("nse", "net_profit", 999.0, retrieved_at="2026-01-01T00:00:00+00:00")
    legacy_reingest = _obs("proprietary", "net_profit", 100.0, retrieved_at="2026-12-01T00:00:00+00:00")

    insert_financial_observations(conn, [xbrl])
    reconcile_batch(conn, [xbrl])
    insert_financial_observations(conn, [legacy_reingest])
    reconcile_batch(conn, [legacy_reingest])

    row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "consolidated")
    assert row["canonical_value"] == 999.0


def test_migrated_period_blanks_a_metric_xbrl_did_not_report(conn: sqlite3.Connection) -> None:
    """Legacy had both net_profit and eps for this period. XBRL only reports
    net_profit. eps must go blank (canonical row deleted), not keep showing
    the legacy value — the "no mixing" rule."""
    legacy_net_profit = _obs("proprietary", "net_profit", 100.0)
    legacy_eps = _obs("proprietary", "eps", 12.5, unit="INR")
    insert_financial_observations(conn, [legacy_net_profit, legacy_eps])
    reconcile_batch(conn, [legacy_net_profit, legacy_eps])

    assert get_canonical_value(conn, "ACME", "eps", "quarterly", "FY2026", "Q1", "consolidated") is not None

    xbrl_net_profit_only = _obs("nse", "net_profit", 999.0)
    insert_financial_observations(conn, [xbrl_net_profit_only])
    reconciled = reconcile_batch(conn, [xbrl_net_profit_only])

    net_profit_row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "consolidated")
    assert net_profit_row["canonical_value"] == 999.0

    eps_row = get_canonical_value(conn, "ACME", "eps", "quarterly", "FY2026", "Q1", "consolidated")
    assert eps_row is None  # blank, not the stale legacy 12.5

    # reconcile_batch's return count only reflects keys with a real canonical
    # row afterward — net_profit counts, the blanked eps key doesn't.
    assert reconciled == 1


def test_unmigrated_period_is_unaffected(conn: sqlite3.Connection) -> None:
    """A company/period with no NSE observation at all must reconcile exactly
    as before — no regression for the vast majority of data with no XBRL yet."""
    legacy = _obs("proprietary", "net_profit", 100.0)
    insert_financial_observations(conn, [legacy])
    reconcile_batch(conn, [legacy])

    row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "consolidated")
    assert row["canonical_value"] == 100.0
    assert row["reconciliation_reason"] == "only source available"


def test_migration_is_scoped_to_statement_type(conn: sqlite3.Connection) -> None:
    """An XBRL consolidated filing must not blank standalone legacy data for
    the same period — standalone/consolidated are independently migrated."""
    legacy_standalone = _obs("proprietary", "net_profit", 50.0, statement_type="standalone")
    insert_financial_observations(conn, [legacy_standalone])
    reconcile_batch(conn, [legacy_standalone])

    xbrl_consolidated = _obs("nse", "net_profit", 999.0, statement_type="consolidated")
    insert_financial_observations(conn, [xbrl_consolidated])
    reconcile_batch(conn, [xbrl_consolidated])

    standalone_row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "standalone")
    assert standalone_row["canonical_value"] == 50.0  # untouched


def test_reconcile_batch_is_idempotent(conn: sqlite3.Connection) -> None:
    legacy_eps = _obs("proprietary", "eps", 12.5, unit="INR")
    xbrl_net_profit = _obs("nse", "net_profit", 999.0)
    insert_financial_observations(conn, [legacy_eps, xbrl_net_profit])

    reconcile_batch(conn, [legacy_eps, xbrl_net_profit])
    first_count = reconcile_batch(conn, [legacy_eps, xbrl_net_profit])

    assert first_count == 1  # only net_profit yields a canonical row; eps stays blank both times
    assert get_canonical_value(conn, "ACME", "eps", "quarterly", "FY2026", "Q1", "consolidated") is None
    row = get_canonical_value(conn, "ACME", "net_profit", "quarterly", "FY2026", "Q1", "consolidated")
    assert row["canonical_value"] == 999.0
