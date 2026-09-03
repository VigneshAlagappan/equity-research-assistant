"""context/graph.py's backend dispatch — GRAPH_BACKEND="neo4j" routes to
context/graph_neo4j.py, and any failure there (unreachable server, bad
query) falls back to the SQLite traversal rather than failing the caller."""

from __future__ import annotations

import sqlite3

import context.graph as graph_module
from context import graph_neo4j
from context.graph import GraphCandidate, find_related_investigations


def test_default_backend_is_sqlite(db_conn: sqlite3.Connection) -> None:
    assert graph_module.GRAPH_BACKEND == "sqlite"
    # No sector data seeded -> the sqlite path's own early-return, proving
    # this call went through _find_related_investigations_sqlite, not neo4j.
    assert find_related_investigations(db_conn, "net interest margin", ["HDFCBANK"]) == []


def test_neo4j_backend_is_used_when_configured(monkeypatch, db_conn: sqlite3.Connection) -> None:
    """Patches the real context.graph_neo4j module's functions directly
    (not sys.modules) — `from context import graph_neo4j` inside the
    dispatcher resolves via the `context` package's cached attribute once
    graph_neo4j has been imported anywhere in the process, which a
    sys.modules patch alone doesn't intercept."""
    monkeypatch.setattr(graph_module, "GRAPH_BACKEND", "neo4j")
    called = {}
    monkeypatch.setattr(graph_neo4j, "get_driver", lambda: called.setdefault("get_driver", True) and object())
    monkeypatch.setattr(
        graph_neo4j, "sync_graph", lambda conn, driver, *, fact_store: called.setdefault("sync_graph", True)
    )
    monkeypatch.setattr(
        graph_neo4j, "find_related_investigations",
        lambda driver, question, company_ids: (
            called.setdefault("find_related_investigations", True),
            [GraphCandidate(
                thread_id="t1", company_ids=["HDFCBANK"], question="q",
                report_markdown="r", score=0.5, path="fake neo4j path",
            )],
        )[1],
    )

    result = find_related_investigations(db_conn, "NIM", ["ICICIBANK"])

    assert called == {"get_driver": True, "sync_graph": True, "find_related_investigations": True}
    assert len(result) == 1
    assert result[0].path == "fake neo4j path"


def test_neo4j_backend_falls_back_to_sqlite_on_failure(monkeypatch, db_conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(graph_module, "GRAPH_BACKEND", "neo4j")

    def _raise():
        raise ConnectionError("no server listening on bolt://localhost:7687")

    monkeypatch.setattr(graph_neo4j, "get_driver", _raise)

    # Falls through to the sqlite path instead of raising — same no-sector-data
    # early return as test_default_backend_is_sqlite proves it actually ran.
    result = find_related_investigations(db_conn, "net interest margin", ["HDFCBANK"])

    assert result == []
