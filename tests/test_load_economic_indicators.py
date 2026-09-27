"""Tests for scripts/load_economic_indicators.py -- the loader that reads
infrastructure/economic_graph/indicators/*.yaml into economic_indicator_
registry (+ source_organizations/source_datasets/source_endpoints/
economic_series for the indicators that have real sourcing). SQLite-only
(db_conn): this is about the YAML -> DB contract and loader idempotency,
not backend-specific SQL, so one backend is enough -- storage/repositories
.py and storage/repositories_pg.py's own functions are already covered
against both backends in tests/test_economic_graph.py.
"""

from __future__ import annotations

import sqlite3

from scripts.load_economic_indicators import load_category_files, run
from storage import repositories as repo


def test_repo_files_describe_exactly_94_indicators() -> None:
    categories = load_category_files()
    total = sum(len(payload["indicators"]) for payload in categories)
    assert total == 94


def test_every_indicator_entry_has_a_unique_name_and_valid_status() -> None:
    categories = load_category_files()
    names = []
    for payload in categories:
        for entry in payload["indicators"]:
            names.append(entry["name"])
            assert entry.get("status", "registered_only") == "registered_only"
    assert len(names) == len(set(names)), "duplicate indicator name across category files"


def test_loader_populates_94_registry_rows(db_conn: sqlite3.Connection) -> None:
    summary = run(db_conn)
    assert summary["indicators"] == 94
    rows = repo.list_economic_indicators(db_conn)
    assert len(rows) == 94


def test_loader_is_idempotent(db_conn: sqlite3.Connection) -> None:
    run(db_conn)
    rows_after_first = repo.list_economic_indicators(db_conn)
    run(db_conn)
    rows_after_second = repo.list_economic_indicators(db_conn)
    assert len(rows_after_first) == len(rows_after_second) == 94

    series_after_first = db_conn.execute("SELECT COUNT(*) c FROM economic_series").fetchone()["c"]
    run(db_conn)
    series_after_third = db_conn.execute("SELECT COUNT(*) c FROM economic_series").fetchone()["c"]
    assert series_after_first == series_after_third


def test_exactly_12_indicators_have_real_linked_sourcing(db_conn: sqlite3.Connection) -> None:
    """The pilot set of well-established indicators get a real
    source_datasets/source_endpoints/economic_series row; the remaining 82
    are metadata-only (registered_only, no dataset/endpoint link) -- never
    fabricated sourcing."""
    run(db_conn)
    with_series = db_conn.execute(
        "SELECT COUNT(DISTINCT indicator_id) c FROM economic_series"
    ).fetchone()["c"]
    assert with_series == 12

    without_series = db_conn.execute(
        """
        SELECT COUNT(*) c FROM economic_indicator_registry r
        WHERE r.indicator_id NOT IN (SELECT indicator_id FROM economic_series)
        """
    ).fetchone()["c"]
    assert without_series == 94 - 12


def test_every_registry_row_is_registered_only_after_load(db_conn: sqlite3.Connection) -> None:
    """Phase 1 never claims live ingestion for anything -- every one of
    the 94 rows must be 'registered_only', whether or not it has a real
    linked source."""
    run(db_conn)
    rows = repo.list_economic_indicators(db_conn)
    assert all(r["status"] == "registered_only" for r in rows)


def test_source_endpoints_never_have_a_fabricated_placeholder_url(db_conn: sqlite3.Connection) -> None:
    """Every URL that does get filled in must come from the 12 real
    endpoints the loader creates for the pilot indicators -- and none of
    them are empty-string placeholders masquerading as real data."""
    run(db_conn)
    urls = [row["url"] for row in db_conn.execute("SELECT url FROM source_endpoints").fetchall()]
    assert len(urls) == 12
    assert all(u and u.startswith("https://") for u in urls)
