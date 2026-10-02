"""L5 causal MVP wiring in research/investigation.py::_persist -- graph rows,
metrics row, version stamps, evidence tags and versioned artifact keys, with
the same canned-LLM harness tests/test_investigation.py uses."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from research.abstracts import _SYSTEM_PROMPT as ABSTRACT_SYSTEM_PROMPT
from research.hypothesis_evaluator import HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT
from research.hypothesis_generator import HYPOTHESIS_GENERATOR_SYSTEM_PROMPT
from research.investigation import run_investigation
from research.research_synthesis import RESEARCH_SYNTHESIS_SYSTEM_PROMPT
from storage.causal_repository import get_investigation_metrics, list_graph_edges, list_graph_nodes
from storage.document_store import default_document_store
from storage.repositories import get_investigation, list_investigation_hypothesis_evidence

_ID = "abcdef012345"
_H1, _H2 = f"{_ID}-h1", f"{_ID}-h2"

_HYPOTHESES = json.dumps([
    {"statement": "Funding costs rose.", "mechanism": "m1", "category": "financial", "rationale": "r",
     "known_relationships": [], "unknowns": [], "chain_steps": ["Repo rate up", "Deposit cost up", "NIM down"]},
    {"statement": "Competition.", "mechanism": "m2", "category": "competitive", "rationale": "r",
     "known_relationships": [], "unknowns": [], "chain_steps": ["Price cuts"]},
])

_EVALUATION = json.dumps({
    "verdict": "SUPPORTED", "confidence_basis": "ok", "confidence_score": 70,
    "supporting_evidence": [
        {"kind": "FACT", "label": "repo rate", "value": "6.5%", "citation": "rbi", "link": 0},
        {"kind": "FACT", "label": "deposit cost", "value": "x", "citation": "filing", "link": 7},  # out of range
        {"kind": "INFERENCE", "label": "overall", "value": "y", "citation": "c"},
    ],
    "contradicting_evidence": [{"kind": "FACT", "label": "nim stable", "value": "z", "citation": "q", "link": 1}],
    "missing_evidence": [],
})

_SYNTHESIS = json.dumps({
    "strongest_explanation": "Funding costs.", "ranked_hypothesis_ids": [_H1, _H2],
    "unanswered_questions": [], "additional_evidence_needed": [],
})


class _Messages:
    def create(self, **kwargs):
        system = kwargs.get("system", "")
        if system.startswith(HYPOTHESIS_GENERATOR_SYSTEM_PROMPT[:40]):
            text = _HYPOTHESES
        elif system.startswith(HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT[:40]):
            text = _EVALUATION
        elif system.startswith(RESEARCH_SYNTHESIS_SYSTEM_PROMPT[:40]):
            text = _SYNTHESIS
        elif system.startswith(ABSTRACT_SYSTEM_PROMPT[:40]):
            text = "abstract"
        else:
            raise AssertionError(system[:60])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)

    class _Fixed:
        hex = _ID + "f" * 20

    monkeypatch.setattr("research.investigation.uuid.uuid4", lambda: _Fixed())
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: SimpleNamespace(messages=_Messages())
    )


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def test_graph_metrics_versions_and_artifact_layout(conn):
    run_investigation(conn, "Why did margins decline?", ["HDFCBANK"])

    nodes = list_graph_nodes(conn, _ID)
    edges = list_graph_edges(conn, _ID)
    assert len(nodes) == 3 and len(edges) == 2  # h2 has one step -> no edges
    e0, e1 = sorted(edges, key=lambda e: e["position"])
    assert e0["edge_key"] == "repo rate up->deposit cost up" and e0["presented"] == 1
    assert (e0["supporting_count"], e0["contradicting_count"]) == (1, 0)
    assert (e1["supporting_count"], e1["contradicting_count"]) == (0, 1)

    # evidence rows carry the tag; out-of-range link and untagged stay hypothesis-level
    ev = list_investigation_hypothesis_evidence(conn, _H1)
    tagged = {r["label"]: (r["chain_step"], r["edge_id"]) for r in ev if r["stance"] != "missing"}
    assert tagged["repo rate"] == (0, f"{_ID}:{_H1}:e0")
    assert tagged["deposit cost"] == (None, None) and tagged["overall"] == (None, None)

    metrics = get_investigation_metrics(conn, _ID)
    assert metrics["edges_explored"] == 2 and metrics["edges_presented"] == 2
    assert metrics["supported_edges"] == 1 and metrics["unsupported_edges"] == 1
    assert metrics["evidence_coverage"] == 0.5
    assert metrics["tagging_rate"] == 0.25  # 2 of 8 items tagged (h2 has a one-step chain: nothing to tag)
    assert metrics["iterations"] == 2 and metrics["runtime_ms"] is not None

    row = get_investigation(conn, _ID)
    assert row["engine_version"] and row["prompt_version"] and row["metrics_definition_version"] == "mvp-1"
    assert row["s3_key"] == f"investigations/{_ID}/v1/artifact.json"

    store = default_document_store()
    artifact = json.loads(store.retrieve(row["s3_key"]))
    assert artifact["graph"]["edges"] and artifact["metrics"]["materiality_basis"] == "none"
    assert artifact["versions"]["engine_version"] == row["engine_version"]
    assert json.loads(store.retrieve(f"investigations/{_ID}/v1/graph.json"))["nodes"]
    assert json.loads(store.retrieve(f"investigations/{_ID}/v1/metrics.json"))["edges_explored"] == 2


def test_disabled_flag_persists_exactly_as_before(conn, monkeypatch):
    monkeypatch.setattr("config.settings.CAUSAL_GRAPH_ENABLED", False)
    run_investigation(conn, "Why did margins decline?", ["HDFCBANK"])
    assert list_graph_edges(conn, _ID) == [] and get_investigation(conn, _ID)["engine_version"] is None
    artifact = json.loads(default_document_store().retrieve(get_investigation(conn, _ID)["s3_key"]))
    assert "graph" not in artifact and artifact["hypotheses"]


def test_causal_failure_never_fails_the_investigation(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("storage.causal_repository.replace_investigation_graph", boom)
    investigation = run_investigation(conn, "Why did margins decline?", ["HDFCBANK"])
    assert investigation.synthesis is not None
    assert get_investigation(conn, _ID)["s3_key"]
