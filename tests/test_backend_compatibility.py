"""ADR-020 step 4 ("Run compatibility and migration tests"): verify the
SQLite and Postgres repository implementations behave identically for the
same inputs, on the four modules `storage/backend_bootstrap.py` swaps
wholesale (company_repository, indicator_repository, investigation_repository)
plus the investigation-shaped functions `storage/repositories.py` /
`storage/repositories_pg.py` both define directly.

This is deliberately NOT a test against the production Neon database (see
ADR-021: `DATABASE_BACKEND=postgres` there is live in production, and this
repo's memory of a prior incident is explicit that a monkeypatch/live
connection string is never a substitute for a real scratch target when
verifying a write path). Instead:

- SQLite side: a fresh `tmp_path` file via `storage.database.init_db()`,
  same as every other test in this suite (see `tests/conftest.py::db_conn`).
- Postgres side: a dedicated Neon branch, never the production database.
  Set `NEON_TEST_URL` to that branch's connection string to run these
  tests; the whole Postgres half is skipped (not failed) when it's unset,
  so this file is safe to run in any environment, including CI without
  Postgres access.

Every test seeds and tears down its own rows (unique IDs) so the branch
stays reusable across runs and other engineers' tests.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from storage import company_repository as company_repository_sqlite
from storage import company_repository_pg
from storage import indicator_repository as indicator_repository_sqlite
from storage import indicator_repository_pg
from storage import investigation_repository as investigation_repository_sqlite
from storage import investigation_repository_pg
from storage import repositories as repositories_sqlite
from storage import repositories_pg
from storage.database import init_db, init_postgres_db

NEON_TEST_URL = os.environ.get("NEON_TEST_URL")

pg_only = pytest.mark.skipif(
    not NEON_TEST_URL,
    reason="NEON_TEST_URL not set -- point it at a dedicated Neon branch (never production) to run these",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def sqlite_conn(tmp_path: Path):
    conn = init_db(db_path=tmp_path / "compat_test.db")
    yield conn
    conn.close()


@pytest.fixture
def pg_conn() -> Iterator:
    conn = init_postgres_db(connection_string=NEON_TEST_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _make_company_id() -> str:
    return f"COMPAT_{uuid.uuid4().hex[:12].upper()}"


def _insert_company(conn, repo, company_id: str) -> None:
    repo.insert_company(
        conn,
        company_id=company_id,
        legal_name=f"{company_id} Legal Name",
        display_name=f"{company_id} Display",
        nse_symbol=None,
        bse_code=None,
        isin=None,
        country="IN",
        currency="INR",
        fiscal_year_end_month=3,
        website=None,
        macro_economic_sector="Financial Services",
        sector="Financial Services",
        industry="Banks",
        basic_industry="Private Sector Bank",
        listed_date=None,
        now=_now(),
    )


def _cleanup_pg(conn, *, company_ids: list[str] = (), investigation_ids: list[str] = (), user_ids: list[int] = ()) -> None:
    """Best-effort teardown so a dedicated Neon branch never accumulates
    compat-test rows across runs -- mirrors the production S3 backfill
    script's idempotency philosophy (re-runnable, leaves no residue)."""
    with conn.cursor() as cur:
        for investigation_id in investigation_ids:
            cur.execute("DELETE FROM investigation_hypothesis_evidence WHERE hypothesis_id IN "
                        "(SELECT hypothesis_id FROM investigation_hypotheses WHERE investigation_id = %s)", (investigation_id,))
            cur.execute("DELETE FROM investigation_hypotheses WHERE investigation_id = %s", (investigation_id,))
            cur.execute("DELETE FROM investigation_companies WHERE investigation_id = %s", (investigation_id,))
            cur.execute("DELETE FROM investigations WHERE investigation_id = %s", (investigation_id,))
        for company_id in company_ids:
            cur.execute("DELETE FROM indicator_evaluations WHERE company_id = %s", (company_id,))
            cur.execute("DELETE FROM companies WHERE company_id = %s", (company_id,))
        for user_id in user_ids:
            cur.execute("DELETE FROM indicator_rule_config WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    conn.commit()


def _create_pg_test_user(conn, email: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (%s, %s, 0, %s) RETURNING user_id",
            (email, "unused", _now()),
        )
        user_id = cur.fetchone()["user_id"]
    conn.commit()
    return user_id


# ------------------------------------------------------------------
# company_repository
# ------------------------------------------------------------------


def test_company_repository_sqlite_insert_select_update_round_trip(sqlite_conn):
    company_id = _make_company_id()
    _insert_company(sqlite_conn, company_repository_sqlite, company_id)
    sqlite_conn.commit()

    row = company_repository_sqlite.select_company(sqlite_conn, company_id)
    assert dict(row)["legal_name"] == f"{company_id} Legal Name"
    assert dict(row)["sector"] == "Financial Services"

    company_repository_sqlite.update_company(
        sqlite_conn, company_id=company_id, legal_name=f"{company_id} Updated", display_name=f"{company_id} Display",
        nse_symbol=None, bse_code=None, isin=None, country="IN", currency="INR", fiscal_year_end_month=3,
        website="https://example.test", macro_economic_sector="Financial Services", sector="Financial Services",
        industry="Banks", basic_industry="Private Sector Bank", listed_date=None, now=_now(),
    )
    sqlite_conn.commit()
    row = company_repository_sqlite.select_company(sqlite_conn, company_id)
    assert dict(row)["legal_name"] == f"{company_id} Updated"
    assert dict(row)["website"] == "https://example.test"

    like = f"%{company_id}%"
    matches = company_repository_sqlite.search_companies_rows(
        sqlite_conn, like, f"{company_id}%", 10, index_name=None,
    )
    assert any(dict(m)["company_id"] == company_id for m in matches)


@pg_only
def test_company_repository_pg_insert_select_update_round_trip(pg_conn):
    company_id = _make_company_id()
    try:
        _insert_company(pg_conn, company_repository_pg, company_id)
        pg_conn.commit()

        row = company_repository_pg.select_company(pg_conn, company_id)
        assert dict(row)["legal_name"] == f"{company_id} Legal Name"
        assert dict(row)["sector"] == "Financial Services"

        company_repository_pg.update_company(
            pg_conn, company_id=company_id, legal_name=f"{company_id} Updated", display_name=f"{company_id} Display",
            nse_symbol=None, bse_code=None, isin=None, country="IN", currency="INR", fiscal_year_end_month=3,
            website="https://example.test", macro_economic_sector="Financial Services", sector="Financial Services",
            industry="Banks", basic_industry="Private Sector Bank", listed_date=None, now=_now(),
        )
        pg_conn.commit()
        row = company_repository_pg.select_company(pg_conn, company_id)
        assert dict(row)["legal_name"] == f"{company_id} Updated"
        assert dict(row)["website"] == "https://example.test"

        like = f"%{company_id}%"
        matches = company_repository_pg.search_companies_rows(
            pg_conn, like, f"{company_id}%", 10, index_name=None,
        )
        assert any(dict(m)["company_id"] == company_id for m in matches)
    finally:
        _cleanup_pg(pg_conn, company_ids=[company_id])


# ------------------------------------------------------------------
# investigation_repository / repositories.save_investigation
# (identical call sites across both backends -- see storage/backend_
# bootstrap.py's docstring for why these two modules must stay in lockstep)
# ------------------------------------------------------------------


def test_investigation_round_trip_sqlite(sqlite_conn):
    company_id = _make_company_id()
    _insert_company(sqlite_conn, company_repository_sqlite, company_id)
    sqlite_conn.commit()

    investigation_id = f"inv-{uuid.uuid4().hex[:12]}"
    repositories_sqlite.save_investigation(
        sqlite_conn, investigation_id=investigation_id, question="Why did margins compress?",
        company_ids=[company_id], statement_type="hypothesis", strongest_explanation=None,
        unanswered_questions=[], additional_evidence_needed=[], as_of=None,
    )

    row = repositories_sqlite.get_investigation(sqlite_conn, investigation_id)
    assert dict(row)["question"] == "Why did margins compress?"

    company_ids = investigation_repository_sqlite.select_company_ids_for_investigation(sqlite_conn, investigation_id)
    assert company_ids == [company_id]

    count = investigation_repository_sqlite.count_investigation_hypotheses(sqlite_conn, investigation_id)
    assert count == 0


@pg_only
def test_investigation_round_trip_pg(pg_conn):
    company_id = _make_company_id()
    investigation_id = f"inv-{uuid.uuid4().hex[:12]}"
    try:
        _insert_company(pg_conn, company_repository_pg, company_id)
        pg_conn.commit()

        repositories_pg.save_investigation(
            pg_conn, investigation_id=investigation_id, question="Why did margins compress?",
            company_ids=[company_id], statement_type="hypothesis", strongest_explanation=None,
            unanswered_questions=[], additional_evidence_needed=[], as_of=None,
        )

        row = repositories_pg.get_investigation(pg_conn, investigation_id)
        assert dict(row)["question"] == "Why did margins compress?"

        company_ids = investigation_repository_pg.select_company_ids_for_investigation(pg_conn, investigation_id)
        assert company_ids == [company_id]

        count = investigation_repository_pg.count_investigation_hypotheses(pg_conn, investigation_id)
        assert count == 0
    finally:
        _cleanup_pg(pg_conn, company_ids=[company_id], investigation_ids=[investigation_id])


# ------------------------------------------------------------------
# indicator_repository -- exercises the NULL-safe-equality translation
# documented in storage/indicator_repository_pg.py's own header (SQLite's
# `IS ?` vs Postgres's `IS NOT DISTINCT FROM %s`), plus the ON CONFLICT
# upsert and RETURNING-based lastrowid replacement.
# ------------------------------------------------------------------


def test_indicator_repository_upsert_and_evaluation_sqlite(sqlite_conn):
    # init_db() already seeds an admin user (storage/database.py::_seed_admin_user).
    user_row = sqlite_conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()
    user_id = dict(user_row)["user_id"]

    company_id = _make_company_id()
    _insert_company(sqlite_conn, company_repository_sqlite, company_id)
    sqlite_conn.commit()

    indicator_repository_sqlite.upsert_indicator_config(
        sqlite_conn, user_id=user_id, rule_id="promoter_holding_decline", scope_type="company",
        scope_value=company_id, enabled=1, classification="warning", thresholds_json=None, now=_now(),
    )
    sqlite_conn.commit()
    configs = indicator_repository_sqlite.select_indicator_configs_for_user(sqlite_conn, user_id)
    assert any(dict(c)["rule_id"] == "promoter_holding_decline" for c in configs)

    evaluation_id = indicator_repository_sqlite.insert_indicator_evaluation(
        sqlite_conn, company_id=company_id, user_id=None, rule_id="promoter_holding_decline",
        rule_version="v1", classification="warning", severity="medium", explanation="test",
        facts_json="{}", effective_config_json="{}", scope_applied=f"company:{company_id}",
        period_label=None, provenance=None, result_hash=uuid.uuid4().hex, evaluated_at=_now(),
    )
    assert evaluation_id is not None

    deleted = indicator_repository_sqlite.delete_indicator_config(
        sqlite_conn, user_id=user_id, rule_id="promoter_holding_decline", scope_type="company", scope_value=company_id,
    )
    assert deleted == 1


@pg_only
def test_indicator_repository_upsert_and_evaluation_pg(pg_conn):
    company_id = _make_company_id()
    user_id = _create_pg_test_user(pg_conn, f"{company_id.lower()}@compat-test.invalid")
    try:
        _insert_company(pg_conn, company_repository_pg, company_id)
        pg_conn.commit()

        indicator_repository_pg.upsert_indicator_config(
            pg_conn, user_id=user_id, rule_id="promoter_holding_decline", scope_type="company",
            scope_value=company_id, enabled=1, classification="warning", thresholds_json=None, now=_now(),
        )
        pg_conn.commit()
        configs = indicator_repository_pg.select_indicator_configs_for_user(pg_conn, user_id)
        assert any(dict(c)["rule_id"] == "promoter_holding_decline" for c in configs)

        evaluation_id = indicator_repository_pg.insert_indicator_evaluation(
            pg_conn, company_id=company_id, user_id=None, rule_id="promoter_holding_decline",
            rule_version="v1", classification="warning", severity="medium", explanation="test",
            facts_json="{}", effective_config_json="{}", scope_applied=f"company:{company_id}",
            period_label=None, provenance=None, result_hash=uuid.uuid4().hex, evaluated_at=_now(),
        )
        assert evaluation_id is not None

        # user_id IS NULL leg of the NULL-safe-equality translation.
        hashes = indicator_repository_pg.select_latest_indicator_result_hashes(pg_conn, company_id, user_id=None)
        assert "promoter_holding_decline" in hashes

        deleted = indicator_repository_pg.delete_indicator_config(
            pg_conn, user_id=user_id, rule_id="promoter_holding_decline", scope_type="company", scope_value=company_id,
        )
        assert deleted == 1
    finally:
        _cleanup_pg(pg_conn, company_ids=[company_id], user_ids=[user_id])
