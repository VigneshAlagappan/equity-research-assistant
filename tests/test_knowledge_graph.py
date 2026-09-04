"""context/knowledge_graph.py (Step 2B) — the SQLite-default path, exercised
directly against real knowledge_claims/knowledge_relationships data built
via research/knowledge_builder.py (same Anthropic mocking as
tests/test_knowledge_builder.py). Neo4j-path tests live in
tests/test_graph_neo4j.py, alongside the rest of that mocked-driver suite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from context.knowledge_graph import find_claims_about_entity, find_multi_hop_claims
from research.knowledge_builder import extract_document_knowledge
from storage.repositories import (
    get_or_create_knowledge_entity,
    insert_knowledge_relationship,
    save_company_document,
)
from tests.test_documents import _make_minimal_pdf
from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _extract_for(conn: sqlite3.Connection, tmp_path: Path, company_id: str, monkeypatch, filename: str = "report.pdf") -> None:
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, "some report text")
    doc = save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(conn, doc)


def test_finds_claims_connected_to_an_entity(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch)

    results = find_claims_about_entity(company_conn, "Product", "Widget Pro")

    assert len(results) == 1
    view = results[0]
    assert view.backend == "sqlite"
    assert view.company_id == "HDFCBANK"
    assert view.claim_type == "MANAGEMENT_OPINION"
    assert "central to our growth strategy" in view.evidence_quotes[0]
    assert ("Company", "HDFCBANK") in view.related_entities  # COMPANY resolved and included
    assert ("Risk", "Input cost inflation") in view.related_entities  # the claim's OTHER relationship target


def test_unknown_entity_returns_empty(company_conn: sqlite3.Connection) -> None:
    assert find_claims_about_entity(company_conn, "Product", "Nonexistent Thing") == []


def test_surfaces_claims_across_different_companies(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    """The whole point of 2B over plain per-company SQL — the same entity
    name mentioned by two different companies' documents surfaces both."""
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch, filename="hdfc.pdf")
    _extract_for(company_conn, tmp_path, "ICICIBANK", monkeypatch, filename="icici.pdf")

    results = find_claims_about_entity(company_conn, "Product", "Widget Pro")

    assert {r.company_id for r in results} == {"HDFCBANK", "ICICIBANK"}


def test_finds_a_company_as_the_entity(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    """Querying entity_type="Company" resolves to the same canonical
    company entity the COMPANY placeholder already maps claims to."""
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch)

    results = find_claims_about_entity(company_conn, "Company", "HDFCBANK")

    assert len(results) == 1
    assert results[0].claim_type == "MANAGEMENT_OPINION"


def test_injected_fact_store_is_used_for_the_sqlite_path(company_conn: sqlite3.Connection) -> None:
    """Proves the FactStore seam (storage/fact_store.py) is wired into
    find_claims_about_entity's SQLite path — an injected fake FactStore
    surfaces a claim even though nothing was actually extracted into
    company_conn's real knowledge_* tables."""
    from dataclasses import replace

    from storage.fact_store import default_fact_store

    fake_claim = {
        "claim_id": 999, "document_id": 1, "company_id": "HDFCBANK", "claim_type": "FACT", "category": "financial",
        "claim_text": "fake claim", "speaker": None, "fiscal_year": "FY2024", "quarter": None, "extraction_confidence": 0.9,
    }
    fs = replace(
        default_fact_store(),
        find_knowledge_claims_about_entity=lambda conn, entity_type, entity_name: [fake_claim],
        list_knowledge_evidence_for_claim=lambda conn, claim_id: [],
        list_knowledge_relationships_for_claim=lambda conn, claim_id: [],
    )

    results = find_claims_about_entity(company_conn, "Risk", "Nonexistent Risk", fact_store=fs)

    assert len(results) == 1
    assert results[0].claim_id == 999
    assert results[0].claim_text == "fake claim"


# ------------------------------------------------------------------
# find_multi_hop_claims() — the multi-hop counterpart to
# find_claims_about_entity() above, exercised against a genuine 2-hop chain
# across two different companies' documents.
# ------------------------------------------------------------------

_RISK_MAY_AFFECT_METRIC_RESPONSE = """{
  "entities": [
    {"type": "Risk", "name": "Input cost inflation"},
    {"type": "Metric", "name": "Gross Margin"}
  ],
  "claims": [
    {
      "text": "Management believes input cost inflation will affect gross margin.",
      "claim_type": "MANAGEMENT_OPINION",
      "category": "risk",
      "speaker": "CFO",
      "confidence": 0.8,
      "quote": "Input cost inflation could weigh on our gross margin.",
      "relationships": [
        {"relationship_type": "MAY_AFFECT", "source_entity": "Input cost inflation", "target_entity": "Gross Margin"}
      ]
    }
  ]
}"""

_METRIC_ONLY_CLAIM_RESPONSE = """{
  "entities": [
    {"type": "Metric", "name": "Gross Margin"}
  ],
  "claims": [
    {
      "text": "Gross margin expanded due to a better product mix.",
      "claim_type": "FACT",
      "category": "fact",
      "speaker": null,
      "confidence": 0.9,
      "quote": "Gross margin expanded 200bps this quarter on a better product mix.",
      "relationships": [
        {"relationship_type": "DRIVES", "source_entity": "COMPANY", "target_entity": "Gross Margin"}
      ]
    }
  ]
}"""


def _extract_custom(conn, tmp_path, company_id, monkeypatch, response_text, filename):
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, "some report text")
    doc = save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", raw_file_path=str(pdf_path),
    )
    _install_fake_client(monkeypatch, response_text)
    extract_document_knowledge(conn, doc)


def test_finds_a_genuine_two_hop_chain_across_two_companies(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """HDFCBANK's own document asserts Risk:Input cost inflation
    --MAY_AFFECT--> Metric:Gross Margin (a hop-1 claim, already
    find_claims_about_entity()'s job). ICICIBANK separately has a claim
    directly ABOUT Gross Margin that never mentions the risk at all -- that
    claim is exactly the 2-hop payload: "which companies have a claim about
    a Metric that some Risk MAY_AFFECT.\""""
    _extract_custom(company_conn, tmp_path, "HDFCBANK", monkeypatch, _RISK_MAY_AFFECT_METRIC_RESPONSE, "hdfc.pdf")
    _extract_custom(company_conn, tmp_path, "ICICIBANK", monkeypatch, _METRIC_ONLY_CLAIM_RESPONSE, "icici.pdf")

    results = find_multi_hop_claims(company_conn, "Risk", "Input cost inflation")

    assert len(results) == 1
    view = results[0]
    assert view.company_id == "ICICIBANK"
    assert view.hop_distance == 2
    assert view.path == "Risk:Input cost inflation --MAY_AFFECT--> Metric:Gross Margin"

    # The existing single-hop function stays the ONLY source of the HDFCBANK
    # claim (it touches the Risk directly) -- multi-hop must never re-surface it.
    hop1 = find_claims_about_entity(company_conn, "Risk", "Input cost inflation")
    assert {c.company_id for c in hop1} == {"HDFCBANK"}
    assert all(c.claim_id != view.claim_id for c in hop1)


def test_unknown_entity_returns_empty_for_multi_hop_too(company_conn: sqlite3.Connection) -> None:
    assert find_multi_hop_claims(company_conn, "Risk", "Nonexistent Risk") == []


def test_max_hops_of_one_returns_nothing(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    """max_hops=1 means no BFS expansion at all -- hop 1 is entirely
    find_claims_about_entity()'s job, per this function's own contract."""
    _extract_custom(company_conn, tmp_path, "HDFCBANK", monkeypatch, _RISK_MAY_AFFECT_METRIC_RESPONSE, "hdfc.pdf")
    _extract_custom(company_conn, tmp_path, "ICICIBANK", monkeypatch, _METRIC_ONLY_CLAIM_RESPONSE, "icici.pdf")

    assert find_multi_hop_claims(company_conn, "Risk", "Input cost inflation", max_hops=1) == []


def test_frontier_cap_truncates_deterministically_instead_of_hanging(
    company_conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A highly-connected entity (60 direct neighbors here, well past the
    50-entity per-hop cap) must truncate deterministically with a logged
    warning, never attempt an unbounded query."""
    start = get_or_create_knowledge_entity(company_conn, "MacroFactor", "Interest Rates", None)
    for i in range(60):
        metric = get_or_create_knowledge_entity(company_conn, "Metric", f"Metric {i}", None)
        insert_knowledge_relationship(
            company_conn, claim_id=None, source_entity_id=start["entity_id"],
            relationship_type="MAY_AFFECT", target_entity_id=metric["entity_id"],
        )

    with caplog.at_level("WARNING", logger="context.knowledge_graph"):
        results = find_multi_hop_claims(company_conn, "MacroFactor", "Interest Rates")

    assert results == []  # none of the 60 synthetic metrics has any claim about it
    assert any("truncated" in record.message for record in caplog.records)
