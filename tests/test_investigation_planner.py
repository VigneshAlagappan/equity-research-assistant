"""research/investigation_planner.py (Step 2F) — routes to existing
retrieval capabilities. Exercised against real ingested financial data and
real knowledge_entities/knowledge_claims (built via
tests/test_knowledge_builder.py's fixtures), no LLM mocking needed for
most of it (macro's own internal planner call is mocked to force its
fallback path, avoiding an extra Anthropic dependency in these tests)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from research.capabilities import PlannerCapabilities
from research.evidence import Evidence
from research.hypothesis_generator import Hypothesis
from research.investigation_planner import plan_and_gather
from research.knowledge_builder import extract_document_knowledge
from storage.repositories import save_company_document
from tests.test_documents import _make_minimal_pdf
from tests.test_knowledge_builder import _VALID_RESPONSE, _install_fake_client as _install_fake_llm_client
from tests.test_macro_evidence import _insert as _insert_macro
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _hypothesis(**overrides) -> Hypothesis:
    defaults = dict(
        hypothesis_id="inv1-h1", investigation_id="inv1", statement="Margins declined due to input costs.",
        mechanism="Higher COGS ate into margins.", category="financial", companies=["HDFCBANK"],
        rationale="plausible", known_relationships=[], unknowns=[], generation_order=0,
    )
    defaults.update(overrides)
    return Hypothesis(**defaults)


def test_gathers_financial_evidence_for_each_company(ingested_conn: sqlite3.Connection) -> None:
    plan = plan_and_gather(ingested_conn, _hypothesis(), "Why did margins decline?")

    assert len(plan.evidence) > 0
    assert any(e.company_id == "HDFCBANK" for e in plan.evidence)
    assert any("financial_engine:HDFCBANK" in s for s in plan.sources_queried)


def test_gathers_document_evidence_only_for_single_company(ingested_conn: sqlite3.Connection) -> None:
    save_company_document(
        ingested_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/annual-report",
    )
    hypothesis = _hypothesis(companies=["HDFCBANK"])

    plan = plan_and_gather(ingested_conn, hypothesis, "question")
    assert any("documents:HDFCBANK" in s for s in plan.sources_queried)

    multi_company_hypothesis = _hypothesis(companies=["HDFCBANK", "ICICIBANK"])
    plan2 = plan_and_gather(ingested_conn, multi_company_hypothesis, "question")
    assert not any("documents:" in s for s in plan2.sources_queried)


def test_macro_only_queried_for_macro_and_regulatory_categories(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert_macro(ingested_conn, "repo_rate", "2024", 6.5, "PERCENT", "rbi")
    from llm.router import AllProvidersUnavailableError, Attempt

    monkeypatch.setattr(
        "research.macro_evidence.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )

    financial_plan = plan_and_gather(ingested_conn, _hypothesis(category="financial"), "What was the repo rate?")
    assert not any("macro_engine" in s for s in financial_plan.sources_queried)

    macro_plan = plan_and_gather(ingested_conn, _hypothesis(category="macro"), "What was the repo rate?")
    assert any("macro_engine" in s for s in macro_plan.sources_queried)


def test_gathers_knowledge_graph_claims_for_the_company(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "report.pdf"
        _make_minimal_pdf(pdf_path, "some report text")
        doc = save_company_document(
            ingested_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
            added_by_user="tester", raw_file_path=str(pdf_path),
        )
        _install_fake_llm_client(monkeypatch, _VALID_RESPONSE)
        extract_document_knowledge(ingested_conn, doc)

    plan = plan_and_gather(ingested_conn, _hypothesis(), "question")

    assert len(plan.knowledge_claims) > 0
    assert all(c.company_id == "HDFCBANK" for c in plan.knowledge_claims)
    assert "knowledge_graph" in plan.sources_queried


def test_mentioned_entity_in_hypothesis_text_is_also_queried(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "report.pdf"
        _make_minimal_pdf(pdf_path, "some report text")
        doc = save_company_document(
            ingested_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
            added_by_user="tester", raw_file_path=str(pdf_path),
        )
        _install_fake_llm_client(monkeypatch, _VALID_RESPONSE)  # extracts "Widget Pro" (Product) and "Input cost inflation" (Risk)
        extract_document_knowledge(ingested_conn, doc)

    hypothesis = _hypothesis(statement="Widget Pro sales drove growth.", mechanism="Widget Pro adoption increased.")
    plan = plan_and_gather(ingested_conn, hypothesis, "question")

    assert any("knowledge_graph:Product:Widget Pro" in s for s in plan.sources_queried)


def test_injected_capabilities_are_used_instead_of_defaults(db_conn: sqlite3.Connection) -> None:
    """Proves the PlannerCapabilities seam (research/capabilities.py, the
    architecture guardrail's "Planner -> Capability Interface -> ...") is
    actually wired, not just present and unused — every one of the 5
    capability slots gets called, and the plan reflects what the injected
    fakes returned, not the real in-process implementations."""
    calls = {"financial": 0, "document": 0, "macro": 0, "search": 0, "graph": 0}

    def fake_financial(conn, company_id):
        calls["financial"] += 1
        return [Evidence(kind="FACT", company_id=company_id, label="fake evidence", value="1", citation="test")]

    def fake_document(conn, company_id, question):
        calls["document"] += 1
        return []

    def fake_macro(conn, question):
        calls["macro"] += 1
        return []

    def fake_search(conn, query, *, company_id, limit):
        calls["search"] += 1
        return []

    def fake_graph(conn, entity_type, entity_name):
        calls["graph"] += 1
        return []

    caps = PlannerCapabilities(
        financial_evidence=fake_financial, document_evidence=fake_document, macro_evidence=fake_macro,
        document_search=fake_search, knowledge_graph=fake_graph,
    )

    plan = plan_and_gather(db_conn, _hypothesis(category="macro"), "question", capabilities=caps)

    assert calls == {"financial": 1, "document": 1, "macro": 1, "search": 1, "graph": 1}
    assert plan.evidence == [Evidence(kind="FACT", company_id="HDFCBANK", label="fake evidence", value="1", citation="test")]


def test_retry_skips_capabilities_that_cannot_change(db_conn: sqlite3.Connection) -> None:
    """A gap-driven retry (retry=True, research/investigation.py's
    gap-driven re-pass) must skip capabilities keyed purely off (hypothesis,
    company_id) — financial_evidence, indicator_evidence, and the per-company
    "Company" knowledge_graph lookup — since a retry only ever changes the
    query text, and those three would return exactly what the first pass
    already got. Capabilities actually driven by the query text
    (document_evidence, macro_evidence, document_search) must still run."""
    calls = {"financial": 0, "indicator": 0, "document": 0, "macro": 0, "search": 0, "graph": 0}

    def fake_financial(conn, company_id):
        calls["financial"] += 1
        return []

    def fake_indicator(conn, company_id):
        calls["indicator"] += 1
        return []

    def fake_document(conn, company_id, question):
        calls["document"] += 1
        return []

    def fake_macro(conn, question):
        calls["macro"] += 1
        return []

    def fake_search(conn, query, *, company_id, limit):
        calls["search"] += 1
        return []

    def fake_graph(conn, entity_type, entity_name):
        calls["graph"] += 1
        return []

    caps = PlannerCapabilities(
        financial_evidence=fake_financial, indicator_evidence=fake_indicator, document_evidence=fake_document,
        macro_evidence=fake_macro, document_search=fake_search, knowledge_graph=fake_graph,
    )

    plan_and_gather(db_conn, _hypothesis(category="macro"), "gap query", capabilities=caps, retry=True)

    # db_conn has no ingested knowledge_entities, so the entity-mention
    # knowledge_graph lookup (query-sensitive, stays live on retry) finds
    # nothing to query either — "graph": 0 here reflects the skipped
    # "Company" lookup, not a second, separately-gated behavior.
    assert calls == {"financial": 0, "indicator": 0, "document": 1, "macro": 1, "search": 1, "graph": 0}


def test_gathers_document_passages(ingested_conn: sqlite3.Connection) -> None:
    import tempfile
    from research.document_chunker import chunk_and_index_document

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "report.pdf"
        _make_minimal_pdf(pdf_path, "Revenue grew due to strong widget demand")
        doc = save_company_document(
            ingested_conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
            added_by_user="tester", raw_file_path=str(pdf_path),
        )
        chunk_and_index_document(ingested_conn, doc)

    hypothesis = _hypothesis(statement="Widget demand drove revenue growth.", mechanism="widget demand")
    plan = plan_and_gather(ingested_conn, hypothesis, "widget demand revenue")

    assert len(plan.passages) >= 1
    assert "document_search" in plan.sources_queried
