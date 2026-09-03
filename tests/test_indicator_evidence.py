"""research/indicator_evidence.py — deterministic triggered indicators as
investigation Evidence, and its wiring into Steps 2E/2F.

What matters here is that a *frozen, versioned rule* reaches the reasoning
layer as citable evidence rather than being re-derived: the label names the
rule, the citation carries its id and version, and the kind is CALCULATION
(a computation over facts), never FACT (a reported figure).
"""

from __future__ import annotations

import sqlite3

import pytest

from companies.registry import seed_companies
from research.capabilities import PlannerCapabilities, default_capabilities
from research.evidence import Evidence
from research.hypothesis_generator import Hypothesis
from research.indicator_evidence import get_indicator_evidence
from research.investigation_planner import plan_and_gather
from storage.repositories import insert_shareholding_observations
from tests.test_indicators import _quarter


@pytest.fixture
def ingested_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    """HDFCBANK with a 2.50pp promoter-holding decline between the two most
    recent quarters — over the 1.0pp system default, so the shareholding
    warning rule genuinely fires. Same fixture shape tests/test_indicators.py
    uses, via the real write path."""
    seed_companies(db_conn)
    insert_shareholding_observations(
        db_conn, "HDFCBANK", [_quarter("FY2025", "Q1", 26.00), _quarter("FY2025", "Q2", 23.50)]
    )
    return db_conn


def test_a_triggered_rule_becomes_citable_calculation_evidence(ingested_conn: sqlite3.Connection) -> None:
    evidence = get_indicator_evidence(ingested_conn, "HDFCBANK")
    assert evidence, "expected the seeded promoter-holding decline to trigger a rule"

    item = evidence[0]
    assert item.kind == "CALCULATION"  # a computation over facts, never a reported FACT
    assert item.company_id == "HDFCBANK"
    assert item.label.startswith("Indicator: ")
    assert "indicator rule " in item.citation and " v" in item.citation
    assert "classification=" in item.citation and "severity=" in item.citation


def test_gathering_indicator_evidence_never_writes_to_the_audit_trail(
    ingested_conn: sqlite3.Connection,
) -> None:
    """`indicator_evaluations` records what a USER was shown on a company
    page. An investigation reading indicators is not that event and must not
    append to it."""
    before = ingested_conn.execute("SELECT COUNT(*) FROM indicator_evaluations").fetchone()[0]
    assert get_indicator_evidence(ingested_conn, "HDFCBANK")
    after = ingested_conn.execute("SELECT COUNT(*) FROM indicator_evaluations").fetchone()[0]
    assert before == after == 0


def test_a_company_with_no_data_yields_no_indicator_evidence(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert get_indicator_evidence(db_conn, "HDFCBANK") == []


def test_the_planner_routes_through_the_capability_seam_not_a_direct_import(
    ingested_conn: sqlite3.Connection,
) -> None:
    """Same contract the other capabilities have: swapping the field changes
    what the plan contains, so nothing downstream depends on how indicators
    happen to be computed."""
    calls = {"n": 0}

    def fake_indicators(conn, company_id):
        calls["n"] += 1
        return [Evidence(kind="CALCULATION", company_id=company_id, label="Indicator: fake", value="x", citation="rule")]

    real = default_capabilities()
    caps = PlannerCapabilities(
        financial_evidence=lambda conn, company_id: [],
        document_evidence=lambda conn, company_id, question: [],
        macro_evidence=lambda conn, question: [],
        document_search=lambda conn, query, *, company_id, limit: [],
        knowledge_graph=lambda conn, entity_type, entity_name: [],
        indicator_evidence=fake_indicators,
    )
    hypothesis = Hypothesis(
        hypothesis_id="h1", investigation_id="inv", statement="s", mechanism="m",
        category="financial", companies=["HDFCBANK"], rationale="r",
    )
    plan = plan_and_gather(ingested_conn, hypothesis, "why?", capabilities=caps)

    assert calls["n"] == 1
    assert [e.label for e in plan.evidence] == ["Indicator: fake"]
    assert "indicators:HDFCBANK" in plan.sources_queried
    # The real bundle binds a real implementation into the same slot.
    assert real.indicator_evidence(ingested_conn, "HDFCBANK")


def test_an_empty_indicator_result_adds_no_source_line(ingested_conn: sqlite3.Connection) -> None:
    caps = PlannerCapabilities(
        financial_evidence=lambda conn, company_id: [],
        document_evidence=lambda conn, company_id, question: [],
        macro_evidence=lambda conn, question: [],
        document_search=lambda conn, query, *, company_id, limit: [],
        knowledge_graph=lambda conn, entity_type, entity_name: [],
        indicator_evidence=lambda conn, company_id: [],
    )
    hypothesis = Hypothesis(
        hypothesis_id="h1", investigation_id="inv", statement="s", mechanism="m",
        category="financial", companies=["HDFCBANK"], rationale="r",
    )
    plan = plan_and_gather(ingested_conn, hypothesis, "why?", capabilities=caps)
    assert not any(s.startswith("indicators:") for s in plan.sources_queried)
