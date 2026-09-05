"""main.py's `graph-backfill` CLI command — one-time, explicit full sync of
SQLite facts into Neo4j (context/graph_neo4j.py). Exercised against a mocked
driver (no real Neo4j server needed) — the Cypher itself is already covered
by tests/test_graph_neo4j.py; this only proves the CLI's own orchestration
(backend check, connectivity check, which syncs run, financials scoping)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import main
from companies.registry import seed_companies
from context import graph_neo4j
from storage.database import init_db
from storage.repositories import list_batch_job_items, list_batch_job_runs


@pytest.fixture
def backfill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    monkeypatch.setattr("config.settings.GRAPH_BACKEND", "neo4j")
    conn = init_db(db_path=db_path)
    seed_companies(conn)
    conn.close()

    fake_driver = MagicMock()
    fake_driver.verify_connectivity.return_value = None
    monkeypatch.setattr(graph_neo4j, "get_driver", lambda: fake_driver)

    calls: dict = {"sync_graph": 0, "sync_knowledge_graph": 0}

    def fake_sync_graph(conn, driver, *, fact_store):
        calls["sync_graph"] += 1

    def fake_sync_knowledge_graph(conn, driver, *, fact_store):
        calls["sync_knowledge_graph"] += 1

    def fake_sync_financials(conn, driver, *, fact_store, company_ids):
        calls["sync_financials_company_ids"] = company_ids
        return len(company_ids)

    monkeypatch.setattr(graph_neo4j, "sync_graph", fake_sync_graph)
    monkeypatch.setattr(graph_neo4j, "sync_knowledge_graph", fake_sync_knowledge_graph)
    monkeypatch.setattr(graph_neo4j, "sync_financials", fake_sync_financials)

    yield calls, db_path


def _run_graph_backfill(*, company_ids: list[str] | None = None, all_financials: bool = False) -> None:
    flags = ["graph-backfill"]
    for company_id in company_ids or []:
        flags += ["--company-id", company_id]
    if all_financials:
        flags.append("--all-financials")
    args = main.build_parser().parse_args(flags)
    args.func(args)


def test_exits_when_graph_backend_is_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    monkeypatch.setattr("config.settings.GRAPH_BACKEND", "sqlite")
    init_db(db_path=db_path).close()

    with pytest.raises(SystemExit):
        _run_graph_backfill()


def test_exits_when_neo4j_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    monkeypatch.setattr("config.settings.GRAPH_BACKEND", "neo4j")
    init_db(db_path=db_path).close()

    unreachable_driver = MagicMock()
    unreachable_driver.verify_connectivity.side_effect = Exception("connection refused")
    monkeypatch.setattr(graph_neo4j, "get_driver", lambda: unreachable_driver)

    with pytest.raises(SystemExit):
        _run_graph_backfill()


def test_always_syncs_graph_and_knowledge_graph(backfill_env) -> None:
    calls, _db_path = backfill_env
    _run_graph_backfill()

    assert calls["sync_graph"] == 1
    assert calls["sync_knowledge_graph"] == 1


def test_skips_financials_by_default(backfill_env) -> None:
    calls, _db_path = backfill_env
    _run_graph_backfill()

    assert "sync_financials_company_ids" not in calls


def test_company_id_scopes_financials_sync(backfill_env) -> None:
    calls, _db_path = backfill_env
    _run_graph_backfill(company_ids=["HDFCBANK", "ICICIBANK"])

    assert calls["sync_financials_company_ids"] == ["HDFCBANK", "ICICIBANK"]


def test_all_financials_syncs_every_registered_company(backfill_env) -> None:
    calls, _db_path = backfill_env
    _run_graph_backfill(all_financials=True)

    assert set(calls["sync_financials_company_ids"]) == {"HDFCBANK", "ICICIBANK"}


def test_records_a_queryable_audit_run(backfill_env) -> None:
    """Every graph-backfill run and its per-phase outcome must land in
    batch_job_runs/batch_job_items (ingestion/batch_log.py) — the same
    durable audit trail scripts/batch_fetch_nse.py uses — not just
    stdout/logs/app.log, so `main.py list-batch-runs`/`show-batch-run` can
    answer "did this actually run, and what happened" after the fact."""
    _calls, db_path = backfill_env
    _run_graph_backfill(company_ids=["HDFCBANK"])

    conn = init_db(db_path=db_path)
    runs = list_batch_job_runs(conn)
    assert runs and runs[0]["job_name"] == "graph_backfill"
    assert runs[0]["status"] == "completed"
    assert runs[0]["items_succeeded"] == 3  # sync_graph, sync_knowledge_graph, sync_financials
    assert "financials=HDFCBANK" in runs[0]["scope_label"]

    items = list_batch_job_items(conn, runs[0]["run_id"])
    assert [i["status"] for i in items] == ["ok", "ok", "ok"]
    assert any("financial observation" in (i["detail"] or "") for i in items)
    conn.close()
