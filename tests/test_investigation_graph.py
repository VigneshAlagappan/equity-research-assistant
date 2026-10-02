from dataclasses import dataclass, field

from research.hypothesis_evaluator import EvidenceItem
from research.investigation_graph import build_graph, normalize_label
from research.investigation_metrics import compute_graph_metrics


@dataclass
class H:
    hypothesis_id: str
    chain_steps: list = field(default_factory=list)


@dataclass
class Ev:
    verdict: str
    supporting_evidence: list = field(default_factory=list)
    contradicting_evidence: list = field(default_factory=list)


def item(step):
    return EvidenceItem(kind="FACT", label="x", chain_step=step)


def test_normalize_label():
    assert normalize_label("  RBI Repo-Rate  UP!! ") == "rbi repo rate up"
    assert normalize_label("") == ""


def test_chain_becomes_consecutive_edges():
    g = build_graph("inv", [H("h1", ["Rates up", "Funding cost up", "NIM down"])], {})
    assert [n["position"] for n in g.nodes] == [0, 1, 2]
    assert [e["edge_key"] for e in g.edges] == ["rates up->funding cost up", "funding cost up->nim down"]
    assert g.edges[0]["source_node_id"] == "inv:h1:n0" and g.edges[0]["target_node_id"] == "inv:h1:n1"
    assert not any(e["presented"] for e in g.edges)  # no evaluation -> nothing presented


def test_short_or_empty_chains_contribute_nothing():
    g = build_graph("inv", [H("a", []), H("b", ["only one"]), H("c", ["", "  "])], {})
    assert g.nodes == [] and g.edges == []


def test_evidence_counts_only_tagged_links_and_presented_follows_verdict():
    hyps = [H("h1", ["a", "b", "c"]), H("h2", ["x", "y"])]
    evals = {
        "h1": Ev("SUPPORTED", [item(0), item(0), item(None)], [item(1)]),
        "h2": Ev("REFUTED", [item(0)]),
    }
    g = build_graph("inv", hyps, evals)
    e = {x["edge_id"]: x for x in g.edges}
    assert e["inv:h1:e0"]["supporting_count"] == 2 and e["inv:h1:e0"]["presented"]
    assert e["inv:h1:e1"]["contradicting_count"] == 1 and e["inv:h1:e1"]["supporting_count"] == 0
    assert not e["inv:h2:e0"]["presented"]
    assert g.edge_id_for("h1", 1) == "inv:h1:e1" and g.edge_id_for("h1", None) is None


def test_metrics_basic_and_empty():
    hyps = [H("h1", ["a", "b", "c"]), H("h2", ["x", "y"])]
    evals = {"h1": Ev("SUPPORTED", [item(0), item(None)], [item(1)]), "h2": Ev("REFUTED", [item(0)])}
    m = compute_graph_metrics(build_graph("inv", hyps, evals), evals, hypotheses_total=3)
    assert (m["edges_explored"], m["edges_presented"], m["supported_edges"], m["unsupported_edges"]) == (3, 2, 1, 1)
    assert m["evidence_coverage"] == 0.5 and m["unsupported_edge_rate"] == 0.5
    assert m["investigation_efficiency"] == 1 / 3
    assert m["tagging_rate"] == 3 / 4  # 3 of 4 items tagged
    assert m["hypotheses_total"] == 3 and m["hypotheses_evaluated"] == 2

    empty = compute_graph_metrics(build_graph("inv", [], {}), {}, 0)
    assert empty["evidence_coverage"] is None and empty["tagging_rate"] is None and empty["edges_explored"] == 0
