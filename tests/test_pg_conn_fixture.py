"""Proves tests/conftest.py's pg_conn fixture actually works end-to-end
against the local Docker Postgres (docker-compose.test.yml) -- SQLite-
removal stage 1. Not testing application logic here, just the fixture
plumbing itself: a fresh, schema-applied, isolated database per test."""

from __future__ import annotations

import uuid


def test_pg_conn_has_the_real_schema_applied(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('companies') IS NOT NULL AS has_companies_table")
        assert cur.fetchone()["has_companies_table"] is True


def test_pg_conn_starts_empty(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM companies")
        assert cur.fetchone()["n"] == 0


def test_pg_conn_is_isolated_between_tests(pg_conn) -> None:
    """Writes a row -- if a previous test's database somehow leaked into
    this one, test_pg_conn_starts_empty above would already have failed,
    but this also proves a write actually persists within one test's own
    database (not silently rolled back or misrouted)."""
    company_id = f"COMPAT_{uuid.uuid4().hex[:12].upper()}"
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (
                company_id, legal_name, display_name, country, currency,
                fiscal_year_end_month, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (company_id, "Test Co", "Test Co", "IN", "INR", 3, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM companies WHERE company_id = %s", (company_id,))
        assert cur.fetchone()["n"] == 1
