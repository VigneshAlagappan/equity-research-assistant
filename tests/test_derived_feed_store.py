from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from web import derived_feed_store as store
from web.charts_feed import build_charts_feed


def _canonical(conn: sqlite3.Connection, metric: str, fy: str, value: float) -> None:
    conn.execute(
        "INSERT INTO canonical_financials (company_id, metric_key, period_type, fiscal_year, quarter, statement_type, "
        "canonical_value, unit, reconciliation_reason, normalization_version, decided_at) "
        "VALUES ('DCO', ?, 'annual', ?, NULL, 'consolidated', ?, 'INR_CRORE', 't', 'v1', ?)",
        (metric, fy, value, f"2026-01-01T00:00:0{len(metric) % 9}"),
    )
    conn.commit()


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "DCO", legal_name="D Co", display_name="D Co")
    _canonical(db_conn, "net_profit", "FY2024", 50.0)
    _canonical(db_conn, "shares_outstanding", "FY2024", 100.0)
    return db_conn


def _get(conn):
    calls = []

    def build():
        calls.append(1)
        return build_charts_feed(conn, "DCO")

    feed = store.get_or_build(conn, "DCO", "charts", "consolidated", "annual", build)
    return feed, len(calls)


def test_second_read_is_served_from_the_stored_row(conn) -> None:
    first, n1 = _get(conn)
    second, n2 = _get(conn)
    assert (n1, n2) == (1, 0)
    assert second == first


def test_changed_shares_outstanding_triggers_a_rebuild(conn) -> None:
    _get(conn)
    _canonical(conn, "shares_outstanding", "FY2025", 200.0)
    _, rebuilt = _get(conn)
    assert rebuilt == 1


def test_new_corporate_action_triggers_a_rebuild(conn) -> None:
    _get(conn)
    conn.execute(
        "INSERT INTO corporate_actions_raw (company_id, subject, ex_date, raw_json, retrieved_at) VALUES ('DCO','Bonus 1:1','2025-01-01','{}','x')"
    )
    conn.execute(
        "INSERT INTO corporate_actions (raw_id, company_id, action_type, subject, ex_date, classifier_version, created_at) "
        "SELECT raw_id, company_id, 'bonus', subject, ex_date, 'v3', 'x' FROM corporate_actions_raw"
    )
    conn.commit()
    _, rebuilt = _get(conn)
    assert rebuilt == 1


def test_storage_failure_falls_back_to_computing(conn, monkeypatch) -> None:
    monkeypatch.setattr(store.repo, "get_derived_feed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(store.repo, "ensure_derived_feeds_table", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    feed, n = _get(conn)
    assert n == 1 and feed["PERIODS"] == ["FY2024"]


def test_decimal_values_from_postgres_are_stored_as_numbers(conn, monkeypatch) -> None:
    from decimal import Decimal

    feed = {"METRICS": {"x": [{"values": [Decimal("12.50"), None]}]}}
    store.get_or_build(conn, "DCO", "charts", "consolidated", "annual", lambda: feed)
    stored = store.repo.get_derived_feed(conn, "DCO", "charts", "consolidated", "annual")
    assert stored is not None
    assert __import__("json").loads(stored["payload"])["METRICS"]["x"][0]["values"] == [12.5, None]
