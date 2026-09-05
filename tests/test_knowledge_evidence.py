"""research/knowledge_evidence.py — Research Knowledge Graph claims (Step 2B)
folded into Q&A/Signals-report Evidence, closing the "not wired into Q&A or
Signals reports yet" gap. Real knowledge_claims data is built the same way
tests/test_knowledge_graph.py does, via research/knowledge_builder.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.evidence import EVIDENCE_KINDS
from research.knowledge_evidence import get_knowledge_graph_evidence
from tests.test_knowledge_graph import _extract_for


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def test_a_companys_own_claim_is_surfaced_without_any_entity_mention(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """The unconditional Company-node lookup means a question doesn't have
    to name anything specific to surface what's already connected to this
    company — same as research/investigation_planner.py's per-company
    "Company" lookup."""
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch)

    evidence = get_knowledge_graph_evidence(company_conn, "HDFCBANK", "how is the business doing?")

    assert evidence
    item = evidence[0]
    assert item.company_id == "HDFCBANK"
    assert item.kind in EVIDENCE_KINDS  # never crashes Evidence's own validation
    assert item.kind == "MANAGEMENT_STATEMENT"  # MANAGEMENT_OPINION -> MANAGEMENT_STATEMENT
    assert "knowledge graph" in item.citation
    assert item.label.startswith("Knowledge graph claim")


def test_a_mentioned_entity_surfaces_a_different_companys_claim_too(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """The whole point of wiring Step 2B into Q&A/Signals: a question about
    HDFCBANK naming "Widget Pro" (an entity ICICIBANK's own document also
    claims something about) surfaces ICICIBANK's claim too — a cross-company
    connection plain per-company evidence retrieval can't reach."""
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch, filename="hdfc.pdf")
    _extract_for(company_conn, tmp_path, "ICICIBANK", monkeypatch, filename="icici.pdf")

    evidence = get_knowledge_graph_evidence(company_conn, "HDFCBANK", "What about our Widget Pro line?")

    assert {e.company_id for e in evidence} == {"HDFCBANK", "ICICIBANK"}


def test_the_same_claim_reached_two_ways_is_not_duplicated(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """HDFCBANK's own claim is reachable both via the unconditional
    Company-node lookup and via the "Widget Pro" entity mention (the
    fixture's claim touches both) — it must appear once, not twice."""
    _extract_for(company_conn, tmp_path, "HDFCBANK", monkeypatch)

    evidence = get_knowledge_graph_evidence(company_conn, "HDFCBANK", "What about our Widget Pro line?")

    hdfc_claims = [e for e in evidence if e.company_id == "HDFCBANK"]
    assert len(hdfc_claims) == 1


def test_a_company_with_no_extracted_claims_yields_no_evidence(company_conn: sqlite3.Connection) -> None:
    assert get_knowledge_graph_evidence(company_conn, "HDFCBANK", "anything") == []


def test_claim_type_map_covers_every_ontology_value() -> None:
    """Asserted at import time in the module itself; re-checked here so a
    new config.knowledge_ontology.CLAIM_TYPES value that isn't mapped fails
    a normal test run, not a live request."""
    from config.knowledge_ontology import CLAIM_TYPES
    from research.knowledge_evidence import _CLAIM_KIND_MAP

    assert set(_CLAIM_KIND_MAP) == set(CLAIM_TYPES)
    assert set(_CLAIM_KIND_MAP.values()) <= set(EVIDENCE_KINDS)
