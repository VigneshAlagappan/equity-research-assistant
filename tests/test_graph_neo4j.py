"""context/graph_neo4j.py — unit tests against a mocked Neo4j driver/session
(no real server needed). Verifies the sync writes and the traversal query's
result-scoring logic; does NOT verify the Cypher actually executes correctly
against a real server — see the module docstring for how to smoke-test that
against a locally running Neo4j once you have one up."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from companies.registry import seed_companies
from context import graph_neo4j
from ingestion.pipeline import ingest_file
from storage.repositories import save_generated_report, save_report_evidence
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def two_bank_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    for company_id in ("HDFCBANK", "ICICIBANK"):
        file_path = tmp_path / f"{company_id}.xlsx"
        _make_screener_workbook(file_path)
        ingest_file(db_conn, file_path, company_id=company_id, source_id="screener")
    return db_conn


def _fake_driver():
    """A driver whose .session() context manager exposes execute_write/
    execute_read that just call the given function immediately with a
    MagicMock transaction — enough to exercise the Cypher-parameter-building
    code without a real server."""
    driver = MagicMock()
    session = MagicMock()
    tx = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    session.execute_write.side_effect = lambda fn, *a: fn(tx, *a)
    session.execute_read.side_effect = lambda fn, *a: fn(tx, *a)
    return driver, session, tx


def test_sync_graph_writes_companies_sector_edges_and_seed_edges(two_bank_conn: sqlite3.Connection) -> None:
    driver, session, tx = _fake_driver()

    graph_neo4j.sync_graph(two_bank_conn, driver)

    queries_run = [call.args[0] for call in tx.run.call_args_list]
    assert any("MERGE (c:Company" in q for q in queries_run)
    assert any("SAME_SECTOR_AS" in q for q in queries_run)
    assert any("AFFECTS" in q for q in queries_run)


def test_sync_graph_syncs_each_generated_report_as_an_investigation(two_bank_conn: sqlite3.Connection) -> None:
    save_generated_report(two_bank_conn, "t1", "How is NIM trending?", ["HDFCBANK"], "consolidated", "# Report")
    save_report_evidence(two_bank_conn, "t1", [
        {"kind": "FACT", "company_id": "HDFCBANK", "label": "net_interest_margin (FY2024)", "value": "1", "citation": "c"},
    ])
    driver, session, tx = _fake_driver()

    graph_neo4j.sync_graph(two_bank_conn, driver)

    investigation_calls = [c for c in tx.run.call_args_list if "MERGE (inv:Investigation" in c.args[0]]
    assert len(investigation_calls) == 1
    assert investigation_calls[0].kwargs["thread_id"] == "t1"


def test_find_related_investigations_scores_and_labels_neo4j_backend(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(
        graph_neo4j, "_query_related",
        lambda tx, company_id, concept_keys: [
            {
                "thread_id": "t1", "question": "How is NIM trending?", "report_markdown": "# Report",
                "peer_id": "HDFCBANK", "sector": "Private Sector Bank", "concept_key": "net_interest_margin",
            }
        ],
    )

    candidates = graph_neo4j.find_related_investigations(driver, "What is the NIM outlook?", ["ICICIBANK"])

    assert len(candidates) == 1
    assert candidates[0].thread_id == "t1"
    assert candidates[0].company_ids == ["HDFCBANK"]
    assert "[neo4j]" in candidates[0].path
    assert candidates[0].score > 0


def test_find_related_investigations_returns_nothing_for_multi_company(monkeypatch) -> None:
    driver, session, tx = _fake_driver()

    result = graph_neo4j.find_related_investigations(driver, "NIM", ["HDFCBANK", "ICICIBANK"])

    assert result == []


def test_find_related_investigations_keeps_best_score_per_investigation(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(
        graph_neo4j, "_query_related",
        lambda tx, company_id, concept_keys: [
            {
                "thread_id": "t1", "question": "q", "report_markdown": "r",
                "peer_id": "HDFCBANK", "sector": "s", "concept_key": "casa_percent",  # bridged, lower strength
            },
            {
                "thread_id": "t1", "question": "q", "report_markdown": "r",
                "peer_id": "HDFCBANK", "sector": "s", "concept_key": "net_interest_margin",  # direct match
            },
        ],
    )

    candidates = graph_neo4j.find_related_investigations(driver, "What is the NIM outlook?", ["ICICIBANK"])

    assert len(candidates) == 1  # deduped to one candidate per thread_id
    assert "net_interest_margin" in candidates[0].path  # kept the higher-scoring match
