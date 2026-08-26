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
from context.knowledge_graph import find_claims_about_entity
from research.knowledge_builder import extract_document_knowledge
from storage.repositories import save_company_document
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
