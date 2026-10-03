"""The dynamic causal-chain stage inside the L5 investigation: wiring, persistence,
protection of the persistent graph, and degraded modes (graph unreachable, empty
graph, no target, disabled)."""

from __future__ import annotations

import copy
import json

import pytest

from causal_graph.history import InMemoryHistory
from causal_graph.seed import seed_graph
from causal_graph.service import CausalKnowledgeService
from causal_graph.store import InMemoryGraphStore
from research import causal_chain_stage
from research.causal_chain_stage import run_causal_chain_stage
from tests.test_dynamic_chain import SERVICE_COMPANIES

QUESTION = "Why did Maruti's operating margin decline between FY2023 and FY2026?"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr("config.settings.CAUSAL_CHAIN_ENABLED", True)
    monkeypatch.setattr(causal_chain_stage, "_geography", lambda conn, company_id: "IN")
    monkeypatch.setattr(causal_chain_stage, "_narrative", lambda conn, fs, as_of: None)
    monkeypatch.setattr("research.dynamic_chain.default_observers", lambda *a, **k: [])


def _svc(seeded=True):
    svc = CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory(), company_lookup=SERVICE_COMPANIES.get)
    if seeded:
        seed_graph(svc)
    return svc


def test_stage_is_off_when_disabled(db_conn) -> None:
    out = run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=_svc())
    assert out == {"status": "skipped", "reason": "CAUSAL_CHAIN_ENABLED is off"}


def test_stage_returns_a_chain_with_the_investigations_ids(db_conn, enabled) -> None:
    out = run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=_svc())
    assert out["status"] == "ok" and out["investigation_id"] == "inv1"
    assert out["target"]["node_id"] == "financial_metric:operating_margin"
    assert out["context"]["geography"] == "IN" and out["context"]["sectors"] == ["Auto"]
    assert out["trace"]["model_calls"] == 0 and out["trace"]["estimated_cost_usd"] == 0.0
    assert all(h["hypothesis_id"].startswith("inv1:h") for h in out["hypotheses"])
    json.dumps(out)


def test_stage_never_changes_the_persistent_graph_or_its_references(db_conn, enabled) -> None:
    svc = _svc()
    nodes, edges = copy.deepcopy(svc.store.nodes), copy.deepcopy(svc.store.edges)
    run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=svc)
    assert svc.store.nodes == nodes and svc.store.edges == edges
    assert not svc.history.evidence and not svc.history.validations and not svc.history.feedback
    assert [e["event_type"] for e in svc.history.events].count("causal_relationship_updated") == 0


def test_evidence_references_are_attached_only_when_switched_on(db_conn, enabled, monkeypatch) -> None:
    from research.dynamic_chain import Observation

    class Obs:
        def can_observe(self, node): return node["id"] in ("financial_metric:operating_margin", "business_driver:material_cost")
        def observe(self, node, ctx):
            d = -1 if node["id"].endswith("margin") else 1
            return Observation(node["id"], d, "stub", "NEON_OBSERVATION", f"stub:{node['id']}", "")

    monkeypatch.setattr("research.dynamic_chain.default_observers", lambda *a, **k: [Obs()])
    svc = _svc()
    run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=svc)
    assert not svc.history.evidence  # default: off
    monkeypatch.setattr("config.settings.CAUSAL_CHAIN_ATTACH_EVIDENCE", True)
    run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv2", service=svc)
    assert svc.history.evidence
    assert all(e["confidence"] == _svc().store.edges[k]["confidence"] for k, e in svc.store.edges.items())  # still untouched


def test_empty_graph_missing_target_and_missing_company_degrade_cleanly(db_conn, enabled) -> None:
    empty = run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=_svc(seeded=False))
    assert empty["status"] == "no_target" and empty["hypotheses"] == []
    none = run_causal_chain_stage(db_conn, "Why is the sky blue?", ["MARUTI"], "inv1", service=_svc())
    assert none["status"] == "no_target" and none["warnings"]
    no_company = run_causal_chain_stage(db_conn, "Why did operating margin fall in FY2023-FY2026?", [], "inv1", service=_svc())
    assert no_company["status"] == "ok" and no_company["context"]["company_id"] is None


def test_neo4j_unreachable_is_reported_not_raised(db_conn, enabled, monkeypatch) -> None:
    class Boom:
        def get_node(self, *a, **k): raise ConnectionError("neo4j down")
        def find_nodes(self, *a, **k): raise ConnectionError("neo4j down")

    svc = CausalKnowledgeService(Boom(), InMemoryHistory())
    out = run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=svc)
    assert out["status"] == "error" and "neo4j down" in out["reason"]
    monkeypatch.setattr(causal_chain_stage, "_open_service", lambda conn: (_ for _ in ()).throw(RuntimeError("no driver")))
    assert run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1")["status"] == "error"


def test_neon_failure_inside_the_stage_is_swallowed(db_conn, enabled, monkeypatch) -> None:
    import psycopg2

    monkeypatch.setattr("research.causal_chain_stage.build_dynamic_chain",
                        lambda *a, **k: (_ for _ in ()).throw(psycopg2.OperationalError("SSL SYSCALL error: EOF")))
    out = run_causal_chain_stage(db_conn, QUESTION, ["MARUTI"], "inv1", service=_svc())
    assert out["status"] == "unavailable" and "EOF" in out["reason"]


def test_report_component_renders_retained_and_rejected_paths_and_degraded_notes() -> None:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("reports/components"), autoescape=True)
    macro = env.get_template("dynamic_chain.html").module.dynamic_chain
    chain = {"status": "ok", "warnings": ["premise check"], "hypotheses": [
        {"retained": True, "status": "SUPPORTED", "labels": ["Steel Price", "Auto", "Margin"], "why_selected": "ranked 1",
         "supporting": [{"label": "Material cost up"}], "contradicting": []},
        {"retained": False, "labels": ["Competition", "Margin"], "rejection_reason": "contradicted: flat"},
    ]}
    html = str(macro(chain))
    assert "Steel Price → Auto → Margin" in html and "Material cost up" in html and "contradicted: flat" in html
    assert "premise check" in html
    assert "unavailable" in str(macro({"status": "unavailable", "reason": "neo4j down"}))
    assert str(macro(None)).strip() == ""


def test_l1_to_l4_pipelines_do_not_use_the_causal_chain_stage() -> None:
    from pathlib import Path

    for module in ("research/assistant.py", "research/routing_policy.py"):
        text = Path(module).read_text()
        assert "causal_chain" not in text and "dynamic_chain" not in text
