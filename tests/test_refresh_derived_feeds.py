from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import scripts.refresh_derived_feeds as job
from companies.registry import register_company
from storage.company_repository import tag_companies_index
from storage.price_database import init_price_db


@pytest.fixture
def setup(db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch):
    for cid in ("A1", "B2", "C3"):
        register_company(db_conn, cid, legal_name=cid, display_name=cid)
    tag_companies_index(db_conn, ["A1"], "Nifty 50")
    tag_companies_index(db_conn, ["B2", "C3"], "Nifty Next 50")
    # the job closes its price connection when it finishes, so hand out a fresh one per run
    monkeypatch.setattr(job, "open_price_db", lambda: init_price_db(tmp_path / "price.db"))
    return db_conn


def _stored(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM derived_financial_feeds").fetchone()[0]


def test_priority_order_is_nifty50_first(setup) -> None:
    assert job.priority_company_ids(setup) == ["A1", "B2", "C3"]


def test_limit_caps_companies_rebuilt_and_next_run_continues(setup) -> None:
    job.run_derived_feeds_refresh(setup, limit=2)
    assert _stored(setup) == 2 * 6  # A1 and B2, six feeds each
    job.run_derived_feeds_refresh(setup, limit=2)
    assert _stored(setup) == 3 * 6  # already-current companies don't use up the limit, so C3 is reached


def test_current_companies_are_not_rebuilt(setup, monkeypatch) -> None:
    job.run_derived_feeds_refresh(setup, limit=10)
    results = []
    real = job.refresh_company
    monkeypatch.setattr(job, "refresh_company", lambda *a, **k: results.append(real(*a, **k)) or results[-1])
    job.run_derived_feeds_refresh(setup, limit=10)
    assert results == [0, 0, 0]
