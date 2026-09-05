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
from storage.database import utcnow_iso
from storage.fact_store import default_fact_store
from storage.repositories import save_company_document, save_generated_report, save_report_evidence
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

    graph_neo4j.sync_graph(two_bank_conn, driver, fact_store=default_fact_store())

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

    graph_neo4j.sync_graph(two_bank_conn, driver, fact_store=default_fact_store())

    investigation_calls = [c for c in tx.run.call_args_list if "MERGE (inv:Investigation" in c.args[0]]
    assert len(investigation_calls) == 1
    assert investigation_calls[0].kwargs["thread_id"] == "t1"


def test_sync_financials_if_changed_writes_then_skips_when_unchanged(two_bank_conn: sqlite3.Connection) -> None:
    """The automatic counterpart to sync_financials() — a caller (context/
    graph.py's find_related_investigations) can call this on every graph
    read without paying for a Neo4j write when nothing has changed."""
    driver, session, tx = _fake_driver()
    fs = default_fact_store()

    first = graph_neo4j.sync_financials_if_changed(two_bank_conn, driver, fact_store=fs)
    assert first > 0
    calls_after_first = tx.run.call_count

    second = graph_neo4j.sync_financials_if_changed(two_bank_conn, driver, fact_store=fs)

    assert second == 0
    assert tx.run.call_count == calls_after_first  # no new Cypher — fingerprint matched, skipped


def test_sync_financials_if_changed_resyncs_after_a_value_is_revised(two_bank_conn: sqlite3.Connection) -> None:
    """canonical_financials is NOT append-only like knowledge_claims — a
    reconciled value can be UPDATEd in place (storage/repositories.py's
    upsert path), same row count, so row count alone can't be the whole
    fingerprint; decided_at (set on both INSERT and UPDATE) is what actually
    catches this."""
    driver, session, tx = _fake_driver()
    fs = default_fact_store()
    graph_neo4j.sync_financials_if_changed(two_bank_conn, driver, fact_store=fs)
    calls_after_first = tx.run.call_count

    two_bank_conn.execute(
        "UPDATE canonical_financials SET canonical_value = canonical_value + 1, decided_at = ? "
        "WHERE canonical_id = (SELECT canonical_id FROM canonical_financials LIMIT 1)",
        (utcnow_iso(),),
    )
    two_bank_conn.commit()

    resynced = graph_neo4j.sync_financials_if_changed(two_bank_conn, driver, fact_store=fs)

    assert resynced > 0
    assert tx.run.call_count > calls_after_first


def test_sync_financials_if_changed_is_a_noop_with_no_financial_data(db_conn: sqlite3.Connection) -> None:
    driver, session, tx = _fake_driver()

    result = graph_neo4j.sync_financials_if_changed(db_conn, driver, fact_store=default_fact_store())

    assert result == 0
    tx.run.assert_not_called()


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


# ------------------------------------------------------------------
# Research Knowledge Graph (Step 2B) — sync_knowledge_graph() and
# find_claims_about_entity(), same mocked-driver style as the sector-peer
# tests above.
# ------------------------------------------------------------------


@pytest.fixture
def knowledge_conn(tmp_path: Path, db_conn: sqlite3.Connection, monkeypatch) -> sqlite3.Connection:
    """Real knowledge_entities/knowledge_claims/knowledge_relationships/
    knowledge_evidence rows, built the same way tests/test_knowledge_builder.py
    does (mocked LLM, real extraction/persistence code)."""
    from tests.test_documents import _make_minimal_pdf
    from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client
    from research.knowledge_builder import extract_document_knowledge

    seed_companies(db_conn)
    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, "some report text")
    doc = save_company_document(
        db_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", raw_file_path=str(pdf_path),
    )

    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(db_conn, doc)
    return db_conn


def test_sync_knowledge_graph_writes_entities_claims_and_relationships(knowledge_conn: sqlite3.Connection) -> None:
    driver, session, tx = _fake_driver()

    graph_neo4j.sync_knowledge_graph(knowledge_conn, driver, fact_store=default_fact_store())

    queries_run = [call.args[0] if call.args else call.kwargs.get("query", "") for call in tx.run.call_args_list]
    assert any("MERGE (c:Company" in q for q in queries_run)
    assert any("MERGE (e:Entity" in q for q in queries_run)
    assert any("MERGE (cl:Claim" in q for q in queries_run)
    assert any(":STATES" in q for q in queries_run)
    assert any(":VALID_DURING" in q for q in queries_run)
    assert any(":SUPPORTED_BY" in q for q in queries_run)
    assert any(":ABOUT" in q for q in queries_run)
    assert any(":OFFERS" in q or ":EXPOSED_TO" in q for q in queries_run)  # the claim's own relationship types


def test_find_claims_about_entity_neo4j_backend(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, entity_type, entity_name: {"id": 7})
    monkeypatch.setattr(
        graph_neo4j, "_query_claims_about_entity",
        lambda tx, entity_key: [
            {
                "claim_id": 1, "company_id": "HDFCBANK", "claim_text": "x", "claim_type": "FACT",
                "category": "fact", "speaker": None, "fiscal_year": "FY2024", "quarter": None,
                "confidence": 0.9, "document_id": 386, "evidence_quotes": ["quote"],
                "related_entities": [["Company", "HDFCBANK"], ["Risk", "Some Risk"]],
            }
        ],
    )

    results = graph_neo4j.find_claims_about_entity(driver, "Product", "Widget Pro")

    assert len(results) == 1
    assert results[0].backend == "neo4j"
    assert results[0].claim_id == 1
    assert ("Risk", "Some Risk") in results[0].related_entities


def test_find_claims_about_entity_neo4j_returns_empty_for_unknown_entity(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, entity_type, entity_name: None)

    assert graph_neo4j.find_claims_about_entity(driver, "Product", "Nonexistent") == []


def test_find_claims_about_entity_company_skips_the_lookup(monkeypatch) -> None:
    """entity_type="Company" resolves directly to "company:<name>" — no
    Entity-node lookup needed, since Company nodes are keyed by id already."""
    driver, session, tx = _fake_driver()
    lookup_calls = []
    query_calls = []
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, *a: lookup_calls.append(a) or {"id": 999})
    monkeypatch.setattr(graph_neo4j, "_query_claims_about_entity", lambda tx, entity_key: query_calls.append(entity_key) or [])

    graph_neo4j.find_claims_about_entity(driver, "Company", "HDFCBANK")

    assert lookup_calls == []  # _query_entity_id_by_name never called for a Company
    assert query_calls == ["company:HDFCBANK"]


def test_find_multi_hop_claims_neo4j_backend(monkeypatch) -> None:
    """Same mocked-driver convention as the single-hop tests above —
    _query_multi_hop_claims is monkeypatched so this proves the entity_key
    resolution / result-mapping / hop_distance / path shape, not the actual
    Cypher execution (never run against a real server, same as every other
    Neo4j path in this codebase)."""
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, entity_type, entity_name: {"id": 7})
    monkeypatch.setattr(
        graph_neo4j, "_query_multi_hop_claims",
        lambda tx, entity_key, max_hop_range: [
            {
                "claim_id": 42, "company_id": "ICICIBANK", "claim_text": "Gross margin expanded.",
                "claim_type": "FACT", "category": "fact", "speaker": None, "fiscal_year": "FY2024", "quarter": None,
                "confidence": 0.9, "document_id": 501, "evidence_quotes": ["quote"],
                "related_entities": [["Company", "ICICIBANK"]],
                "hop_distance": 2,
                "path_nodes": ["Risk:Input cost inflation", "Metric:Gross Margin"],
                "path_rel_types": ["MAY_AFFECT"],
            }
        ],
    )

    results = graph_neo4j.find_multi_hop_claims(driver, "Risk", "Input cost inflation")

    assert len(results) == 1
    view = results[0]
    assert view.backend == "neo4j"
    assert view.claim_id == 42
    assert view.hop_distance == 2
    assert view.path == "Risk:Input cost inflation --MAY_AFFECT--> Metric:Gross Margin"


def test_find_multi_hop_claims_neo4j_max_hop_range_is_max_hops_minus_one(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, entity_type, entity_name: {"id": 7})
    captured = []
    monkeypatch.setattr(
        graph_neo4j, "_query_multi_hop_claims",
        lambda tx, entity_key, max_hop_range: captured.append((entity_key, max_hop_range)) or [],
    )

    graph_neo4j.find_multi_hop_claims(driver, "Risk", "Input cost inflation", max_hops=3)

    assert captured == [("entity:7", 2)]


def test_find_multi_hop_claims_neo4j_returns_empty_for_unknown_entity(monkeypatch) -> None:
    driver, session, tx = _fake_driver()
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, entity_type, entity_name: None)

    assert graph_neo4j.find_multi_hop_claims(driver, "Risk", "Nonexistent") == []


def test_find_multi_hop_claims_neo4j_max_hops_of_one_is_a_noop(monkeypatch) -> None:
    """max_hops=1 means no BFS expansion at all -- never even resolves the
    entity_key or issues a query, same "hop 1 is find_claims_about_entity()'s
    job" contract the SQLite path enforces."""
    driver, session, tx = _fake_driver()
    lookup_calls = []
    monkeypatch.setattr(graph_neo4j, "_query_entity_id_by_name", lambda tx, *a: lookup_calls.append(a) or {"id": 7})

    assert graph_neo4j.find_multi_hop_claims(driver, "Risk", "Input cost inflation", max_hops=1) == []
    assert lookup_calls == []


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
