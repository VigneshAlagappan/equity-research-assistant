"""L5 dynamic causal-chain traversal (research/dynamic_chain.py and friends) over
the seeded persistent graph, with stub observers standing in for stored data."""

from __future__ import annotations

import copy

import pytest

from causal_graph.history import InMemoryHistory
from causal_graph.seed import seed_graph
from causal_graph.service import CausalKnowledgeService
from causal_graph.store import InMemoryGraphStore
from research.causal_chain_paths import ChainLimits, Trace, generate_candidate_paths
from research.causal_chain_ranking import Context, contextual_relevance, path_score, score_edge, temporal_fit
from research.dynamic_chain import (
    CONTRADICTED, PLAUSIBLE, SUPPORTED, UNRESOLVED, WEAK, build_dynamic_chain, classify, identify_target, render_explanation,
)

MARGIN = "financial_metric:operating_margin"
QUESTION = "Why did Maruti's operating margin decline between FY2023 and FY2026?"
SERVICE_COMPANIES = {"HDFCBANK": "HDFC Bank", "IDFCFIRSTB": "IDFC First Bank", "MARUTI": "Maruti Suzuki", "TATASTEEL": "Tata Steel"}


class StubObserver:
    """node id -> direction (+1/-1/0). Nodes not listed cannot be observed."""

    def __init__(self, directions: dict[str, int]):
        self.directions = directions
        self.calls: list[str] = []

    def can_observe(self, node: dict) -> bool:
        return node["id"] in self.directions

    def observe(self, node, ctx):
        from research.dynamic_chain import Observation

        self.calls.append(node["id"])
        return Observation(node["id"], self.directions[node["id"]], f"{node['display_name']} stub", "NEON_OBSERVATION",
                           f"stub:{node['id']}", "stub")


@pytest.fixture
def svc() -> CausalKnowledgeService:
    service = CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory(), company_lookup=SERVICE_COMPANIES.get)
    seed_graph(service)
    return service


def _run(svc, directions, **kw):
    kw.setdefault("company_id", "MARUTI")
    kw.setdefault("geography", "IN")
    return build_dynamic_chain(svc, QUESTION, observers=[StubObserver(directions)], **kw)


# Material cost and steel price rose, margin fell; volume rose and rates fell (so the demand story fails).
STEEL_STORY = {MARGIN: -1, "business_driver:material_cost": 1, "economic_driver:steel_price": 1,
               "business_driver:vehicle_volume": 1, "economic_driver:auto_financing_cost": -1}


# --- target, context ----------------------------------------------------------------

def test_target_is_identified_from_the_question_or_given_explicitly(svc) -> None:
    assert identify_target(svc, QUESTION)["id"] == MARGIN
    assert identify_target(svc, "How did sales do?")["id"] == "financial_metric:revenue"
    assert identify_target(svc, "Why is the sky blue?") is None
    assert identify_target(svc, "x", explicit=MARGIN)["id"] == MARGIN
    assert identify_target(svc, "x", explicit="financial_metric:ghost") is None


def test_unknown_target_returns_an_empty_explained_result(svc) -> None:
    out = build_dynamic_chain(svc, "Why is the sky blue?", company_id="MARUTI")
    assert out["target"] is None and out["hypotheses"] == [] and out["warnings"]


def test_context_comes_from_the_graph_and_the_question(svc) -> None:
    out = _run(svc, STEEL_STORY)
    assert out["context"] == {"company_id": "MARUTI", "sectors": ["Auto"], "geography": "IN", "period": [2023, 2026]}


# --- bounded traversal, candidate generation ----------------------------------------

def _candidates(svc, limits=None, ctx=None):
    ctx = ctx or Context("MARUTI", ("Auto",), "IN", (2023, 2026))
    trace = Trace()
    nodes, paths = generate_candidate_paths(svc, MARGIN, ctx, limits or ChainLimits(), trace, lambda c, e: False)
    return nodes, paths, trace


def test_candidate_paths_are_reusable_graph_paths_ending_at_the_target(svc) -> None:
    _, paths, _ = _candidates(svc)
    keys = {p.key for p in paths}
    assert "commodity:iron_ore>economic_driver:steel_price>sector:auto>business_driver:material_cost>financial_metric:operating_margin" in keys
    assert any(k.endswith("business_driver:vehicle_volume>financial_metric:operating_margin") and k.startswith("macro_indicator:rbi_policy_repo_rate") for k in keys)
    assert all(p.node_ids[-1] == MARGIN for p in paths)
    assert [p.score for p in paths] == sorted((p.score for p in paths), reverse=True)  # best first


def test_traversal_is_deterministic(svc) -> None:
    a = [(p.key, p.score) for p in _candidates(svc)[1]]
    b = [(p.key, p.score) for p in _candidates(svc)[1]]
    assert a == b


def test_depth_limit_is_enforced(svc) -> None:
    _, paths, _ = _candidates(svc, ChainLimits(max_depth=2))
    assert paths and max(len(p.edges) for p in paths) <= 2


def test_branch_limit_keeps_only_the_best_causes_per_node(svc) -> None:
    _, _, trace = _candidates(svc, ChainLimits(max_branches_per_node=2))
    assert any(r["reason"] == "branch_limit" for r in trace.rejected_edges)
    full_nodes, _, _ = _candidates(svc)
    capped_nodes, _, _ = _candidates(svc, ChainLimits(max_branches_per_node=1))
    assert len(capped_nodes) < len(full_nodes)


def test_node_and_edge_budgets_are_enforced_and_recorded(svc) -> None:
    nodes, _, trace = _candidates(svc, ChainLimits(max_nodes=4))
    assert len(nodes) <= 4 and any(r["reason"] == "node_budget" for r in trace.rejected_edges)
    _, paths, trace = _candidates(svc, ChainLimits(max_edges=3))
    assert sum(len(p.edges) for p in paths) >= 0 and any(r["reason"] == "edge_budget" for r in trace.rejected_edges)


def test_cross_sector_hop_limit(svc) -> None:
    ctx = Context("MARUTI", ("Auto",), "IN", (2023, 2026))
    _, with_hops, _ = _candidates(svc, ChainLimits(max_cross_sector_hops=2), ctx)
    through_steel = [p for p in with_hops if "sector:steel" in p.node_ids]
    assert through_steel and all(p.cross_sector_nodes for p in through_steel)  # Iron Ore -> Steel(sector) -> Auto -> ...
    _, none, trace = _candidates(svc, ChainLimits(max_cross_sector_hops=0), ctx)
    assert not any("sector:steel" in p.node_ids or "sector:banking" in p.node_ids for p in none)
    assert any(r["reason"] == "cross_sector_hop_limit" for r in trace.rejected_paths)


def test_own_sector_is_not_a_cross_sector_hop(svc) -> None:
    _, paths, _ = _candidates(svc, ChainLimits(max_cross_sector_hops=0))
    assert any("sector:auto" in p.node_ids for p in paths)


def test_low_materiality_cross_sector_edges_are_rejected(svc) -> None:
    _, _, trace = _candidates(svc, ChainLimits(cross_sector_min_materiality=0.99))
    assert any(r["reason"] == "cross_sector_low_materiality" for r in trace.rejected_edges)


# --- ranking, relevance, lag ---------------------------------------------------------

def _edge(**over):
    base = {"edge_id": "e1", "cause_id": "a", "effect_id": "b", "confidence": 0.8, "effect_strength": "MEDIUM",
            "scope": {}, "lag_min": 0, "lag_max": 3, "lag_unit": "months", "evidence_refs": [], "validation_refs": [],
            "feedback_refs": []}
    base.update(over)
    return base


def test_scope_decides_contextual_relevance() -> None:
    ctx = Context("MARUTI", ("Auto",), "IN", (2023, 2026))
    assert contextual_relevance(_edge(), ctx) == pytest.approx(0.6)  # global: credible, not specific
    assert contextual_relevance(_edge(scope={"geography": "IN", "sector": "Auto"}), ctx) == pytest.approx(1.0)
    assert contextual_relevance(_edge(scope={"geography": "US"}), ctx) is None
    assert contextual_relevance(_edge(scope={"sector": "Banking"}), ctx) is None
    assert contextual_relevance(_edge(scope={"company_id": "HDFCBANK"}), ctx) is None
    assert contextual_relevance(_edge(scope={"period": "FY2010-FY2012"}), ctx) is None
    assert contextual_relevance(_edge(scope={"geography": "US"}), Context()) == pytest.approx(0.6)  # unknown context is neutral


def test_lag_fits_inside_the_window_or_the_edge_is_dropped() -> None:
    ctx = Context(period=(2023, 2024))  # 24 months
    assert temporal_fit(_edge(lag_min=1, lag_max=2, lag_unit="quarters"), ctx) == 1.0
    assert temporal_fit(_edge(lag_min=1, lag_max=4, lag_unit="years"), ctx) == 0.6
    assert temporal_fit(_edge(lag_min=3, lag_max=4, lag_unit="years"), ctx) is None
    assert temporal_fit(_edge(lag_min=None, lag_max=None, lag_unit=None), ctx) == 0.8
    assert score_edge(_edge(lag_min=3, lag_max=4, lag_unit="years"), ctx, measurable=False) == "lag_exceeds_window"


def test_runtime_score_keeps_persistent_facts_separate_and_uses_history() -> None:
    ctx = Context(period=(2023, 2026))
    base = score_edge(_edge(), ctx, measurable=False)
    validated = score_edge(_edge(validation_refs=[{"result": "SUPPORT"}, {"result": "SUPPORT"}]), ctx, measurable=False)
    refuted = score_edge(_edge(feedback_refs=[{"feedback_type": "WRONG_RELATIONSHIP"}]), ctx, measurable=False)
    backed = score_edge(_edge(evidence_refs=[{"stance": "SUPPORTS"}]), ctx, measurable=False)
    assert validated.score > base.score > refuted.score
    assert backed.components["evidence"] == 1.0 > base.components["evidence"]
    assert set(base.components) == {"confidence", "relevance", "strength", "temporal", "evidence", "validation", "feedback"}
    assert path_score([0.8, 0.8]) < path_score([0.8])  # shorter explanation preferred at equal quality
    assert path_score([0.9, 0.2]) < path_score([0.55, 0.55])  # a weak link drags the path


def test_deprecated_and_low_confidence_edges_are_rejected_with_reasons(svc) -> None:
    eid = next(e["edge_id"] for e in svc.store.edges.values() if e["target_id"] == MARGIN and e["source_id"] == "business_driver:pricing_pressure")
    svc.transition_status(eid, "DEPRECATED", actor_kind="human", reason="superseded")
    svc.update_relationship(next(e["edge_id"] for e in svc.store.edges.values() if e["source_id"] == "business_driver:vehicle_volume" and e["target_id"] == MARGIN),
                            confidence=0.1, actor_kind="human", source="t", reason="t")
    _, paths, trace = _candidates(svc)
    reasons = {r["reason"] for r in trace.rejected_edges}
    assert {"deprecated", "below_min_confidence"} <= reasons
    assert not any("business_driver:pricing_pressure" in p.node_ids for p in paths)


def test_scope_mismatch_edges_are_rejected(svc) -> None:
    _, _, trace = _candidates(svc, ctx=Context("MARUTI", ("Banking",), "IN", (2023, 2026)))
    assert any(r["reason"] == "scope_mismatch" for r in trace.rejected_edges)


# --- hypothesis testing, contradictions, alternatives -------------------------------

def test_classification_rules() -> None:
    assert classify(2, 0, 0.8, 0.35, 2) == SUPPORTED
    assert classify(1, 0, 0.8, 0.35, 1) == PLAUSIBLE
    assert classify(0, 1, 0.8, 0.35, 1) == CONTRADICTED
    assert classify(1, 1, 0.8, 0.35, 2) == CONTRADICTED
    assert classify(2, 1, 0.8, 0.35, 3) == WEAK
    assert classify(0, 0, 0.8, 0.35, 0) == UNRESOLVED
    assert classify(0, 0, 0.2, 0.35, 0) == WEAK


def test_supported_path_is_retained_and_contradicted_alternatives_are_rejected(svc) -> None:
    out = _run(svc, STEEL_STORY)
    by_labels = {" > ".join(h["labels"]): h for h in out["hypotheses"]}
    steel = by_labels["Iron Ore > Steel Price > Auto > Material Cost > Operating Margin"]
    assert steel["status"] == SUPPORTED and steel["retained"] and steel["why_selected"]
    assert steel["supporting"] and not steel["contradicting"]
    demand = [h for h in out["hypotheses"] if "Vehicle Volume" in h["labels"] and "Auto Financing Cost" in h["labels"]]
    assert demand and all(h["status"] == CONTRADICTED and not h["retained"] for h in demand)
    assert all("contradicted:" in h["rejection_reason"] for h in demand)
    assert steel["path_id"] in out["retained_paths"]
    assert any(r["status"] == CONTRADICTED for r in out["rejected_paths"] if "status" in r)
    assert out["alternative_explanations"]


def test_every_retained_path_explains_itself_against_its_alternatives(svc) -> None:
    out = _run(svc, STEEL_STORY)
    retained = [h for h in out["hypotheses"] if h["retained"]]
    assert retained
    for h in retained:
        assert h["alternatives"] and all({"path_id", "status", "reason"} <= set(a) for a in h["alternatives"])
        assert "supporting" in h and "contradicting" in h and h["contextual_relevance"] > 0


def test_contradiction_search_flat_series_weakens_a_hypothesis(svc) -> None:
    flat = dict(STEEL_STORY, **{"business_driver:material_cost": 0})  # material cost stayed flat
    steel = next(h for h in _run(svc, flat)["hypotheses"] if "Steel Price" in h["labels"] and "Material Cost" in h["labels"])
    assert steel["status"] in (CONTRADICTED, WEAK) and any("flat" in f["label"] for f in steel["contradicting"])


def test_narrative_hedging_evidence_contradicts_a_path(svc) -> None:
    def search(query, company_id):
        if "Steel Price" in query:
            return [{"chunk_id": "chunk-42", "text": "We have hedged most of our steel exposure through fixed-price contracts."}]
        return []

    out = _run(svc, STEEL_STORY, narrative=search)
    steel = next(h for h in out["hypotheses"] if "Steel Price" in h["labels"] and "Material Cost" in h["labels"])
    hit = [f for f in steel["contradicting"] if f["kind"] == "NARRATIVE"]
    assert hit and hit[0]["ref_type"] == "QDRANT_CHUNK" and hit[0]["locator"] == "chunk-42"
    assert steel["status"] in (CONTRADICTED, WEAK)


def test_narrative_queries_are_budgeted(svc) -> None:
    calls = []
    _run(svc, STEEL_STORY, narrative=lambda q, c: calls.append(q) or [], limits=ChainLimits(max_narrative_queries=2))
    assert len(calls) == 2


def test_untestable_paths_are_unresolved_and_only_kept_when_nothing_else_survives(svc) -> None:
    out = _run(svc, {MARGIN: -1})  # nothing else measurable
    assert all(h["status"] in (UNRESOLVED, WEAK) for h in out["hypotheses"])
    assert 0 < len(out["retained_paths"]) <= 2  # best leads, flagged unresolved
    assert all(h["status"] == UNRESOLVED for h in out["hypotheses"] if h["retained"])


def test_weak_paths_are_rejected_before_they_can_be_retained(svc) -> None:
    out = _run(svc, {MARGIN: -1}, limits=ChainLimits(min_path_score=0.99))
    assert all(h["status"] == WEAK for h in out["hypotheses"]) and not out["retained_paths"]
    assert all(h["rejection_reason"].startswith("weak:") for h in out["hypotheses"])


def test_alternatives_are_tested_across_iterations_until_a_supported_path_and_a_rival(svc) -> None:
    out = _run(svc, STEEL_STORY, limits=ChainLimits(paths_per_iteration=1, max_iterations=6))
    assert out["trace"]["iterations"] >= 2 and out["trace"]["paths_tested"] >= 2
    capped = _run(svc, STEEL_STORY, limits=ChainLimits(paths_per_iteration=1, max_iterations=1))
    assert capped["trace"]["paths_tested"] == 1
    assert any("iteration budget" in r["reason"] for r in capped["rejected_paths"])


def test_cross_sector_chain_through_banking_appears_when_allowed(svc) -> None:
    story = {MARGIN: -1, "business_driver:vehicle_volume": -1, "economic_driver:vehicle_demand": -1,
             "economic_driver:auto_financing_cost": 1, "economic_driver:lending_rate": 1}
    out = _run(svc, story, limits=ChainLimits(max_cross_sector_hops=2))
    demand = [h for h in out["hypotheses"] if "Vehicle Demand" in h["labels"] and "Banking" in h["labels"]]
    assert demand and demand[0]["cross_sector_nodes"] == ["sector:banking"]
    blocked = _run(svc, story, limits=ChainLimits(max_cross_sector_hops=0))
    assert not any("Banking" in h["labels"] for h in blocked["hypotheses"])
    assert any(h["status"] == SUPPORTED and "Lending Rate" in h["labels"] for h in out["hypotheses"])


# --- evidence attachment, persistence safety, ids, observability -----------------------

def test_evidence_is_attached_as_references_without_touching_persistent_edges(svc) -> None:
    before = {eid: copy.deepcopy(e) for eid, e in svc.store.edges.items()}
    out = _run(svc, STEEL_STORY)
    assert out["trace"]["evidence_attached"] > 0
    assert svc.history.evidence and all(r["ref_type"] == "NEON_OBSERVATION" for r in svc.history.evidence)
    assert set(svc.store.edges) == set(before)
    for eid, edge in svc.store.edges.items():  # nothing about the graph edge changed
        assert edge == before[eid]
    again = _run(svc, STEEL_STORY)
    assert again["trace"]["evidence_attached"] == 0  # the same evidence is not attached twice


def test_attaching_references_can_be_switched_off(svc) -> None:
    out = _run(svc, STEEL_STORY, attach_references=False)
    assert out["trace"]["evidence_attached"] == 0 and not svc.history.evidence


def test_identifiers_support_structured_feedback(svc) -> None:
    out = _run(svc, STEEL_STORY, investigation_id="inv-fixed")
    assert out["investigation_id"] == "inv-fixed"
    for h in out["hypotheses"]:
        assert h["hypothesis_id"].startswith("inv-fixed:h") and h["path_id"].startswith("inv-fixed:p")
        assert all(e["edge_id"].startswith("e_") and e["edge_ref"].startswith(h["path_id"] + ":e") for e in h["edges"])
    h = next(h for h in out["hypotheses"] if h["retained"])
    svc.attach_feedback("hypothesis", h["hypothesis_id"], "NOT_RELEVANT", edge_id=h["edges"][0]["edge_id"])
    svc.attach_feedback("edge", h["edges"][0]["edge_id"], "WRONG_TIMING")
    assert svc.get_relationship(h["edges"][0]["edge_id"])["confidence"] == svc.store.edges[h["edges"][0]["edge_id"]]["confidence"]


def test_result_is_plain_data_with_the_documented_shape(svc) -> None:
    import json

    out = _run(svc, STEEL_STORY)
    json.dumps(out)  # no library objects: ready for any visualization
    assert {"nodes", "edges"} == set(out["graph"])
    edge = out["graph"]["edges"][0]
    for key in ("confidence", "effect_strength", "lag", "scope", "contextual_relevance", "runtime_score", "score_components", "path_ids"):
        assert key in edge
    retained_nodes = {n for h in out["hypotheses"] if h["retained"] for n in h["node_ids"]}
    assert {n["node_id"] for n in out["graph"]["nodes"]} == retained_nodes  # only what the final chain uses


def test_trace_reconstructs_what_happened(svc) -> None:
    out = _run(svc, STEEL_STORY, narrative=lambda q, c: [])
    t = out["trace"]
    assert t["starting_target"] == MARGIN and t["nodes_expanded"][0] == MARGIN
    assert t["edges_considered"] >= len(t["edges_rejected"]) and t["candidate_paths"] >= t["paths_tested"] > 0
    assert t["evidence_retrieved"] > 0 and t["contradictions_found"] > 0 and t["iterations"] >= 1
    assert t["final_paths"] == out["retained_paths"] and t["model_calls"] == 0 and t["runtime_ms"] >= 0
    assert t["tool_calls"]["graph_service"] == len(t["nodes_expanded"])
    assert t["cross_sector_hops_max"] >= 1


def test_premise_not_supported_by_data_is_flagged(svc) -> None:
    out = _run(svc, dict(STEEL_STORY, **{MARGIN: 1}))  # the question says margin fell; the data says it rose
    assert any("premise not supported" in w for w in out["warnings"])


def test_explanation_is_deterministic_text(svc) -> None:
    out = _run(svc, STEEL_STORY, investigation_id="inv-x")
    text = render_explanation(out)
    assert "Iron Ore -> Steel Price -> Auto -> Material Cost -> Operating Margin" in text and "[SUPPORTED]" in text
    assert text == render_explanation(out)


def test_same_inputs_give_the_same_result(svc) -> None:
    strip = lambda o: {k: v for k, v in o.items() if k != "trace"}  # noqa: E731
    a = strip(_run(svc, STEEL_STORY, investigation_id="i", attach_references=False))
    b = strip(_run(svc, STEEL_STORY, investigation_id="i", attach_references=False))
    assert a == b
