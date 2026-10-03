"""Persistent causal graph foundation: service rules (in-memory store), SQL
history, the Neo4j store's Cypher shape (fake driver), and -- when
CAUSAL_GRAPH_TEST_NEO4J_URI is set -- the same behaviour against a real,
throwaway Neo4j."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from causal_graph.history import InMemoryHistory, SqlHistory
from causal_graph.neo4j_store import Neo4jGraphStore
from causal_graph.seed import NODES, seed_graph
from causal_graph.service import CausalKnowledgeService
from causal_graph.store import InMemoryGraphStore
from causal_graph.validation import (
    DuplicateNodeError, DuplicateRelationshipError, NotFoundError, TransitionError, ValidationError,
    normalize_relationship_type,
)
from config import causal_graph as cg

PROV = {"type": "MANUAL_SEED", "ref": "test"}


@pytest.fixture
def svc() -> CausalKnowledgeService:
    return CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory())


def _pair(svc):
    a = svc.create_node("MacroIndicator", "RBI Repo Rate", references={"series_key": "policy_repo_rate"})
    b = svc.create_node("EconomicDriver", "Lending Rate")
    return a["id"], b["id"]


def _edge(svc, **over):
    a, b = _pair(svc) if not svc.store.nodes else ("macro_indicator:rbi_repo_rate", "economic_driver:lending_rate")
    kw = dict(direction="POSITIVE", mechanism="banks reprice off the policy rate", confidence=0.9,
              effect_strength="HIGH", lag={"min": 0, "max": 3, "unit": "months"}, scope={"geography": "IN"},
              provenance=PROV, actor_kind="seed")
    kw.update(over)
    return svc.create_relationship(a, "AFFECTS", b, **kw)


# --- nodes ------------------------------------------------------------------------

def test_node_creation_carries_identity_metadata_and_references(svc) -> None:
    node = svc.create_node("MacroIndicator", "RBI Repo Rate", description="policy rate",
                           references={"series_key": "policy_repo_rate"})
    assert node["id"] == "macro_indicator:rbi_repo_rate"
    assert node["family"] == "MacroIndicator" and node["version"] == 1
    assert node["references"] == {"series_key": "policy_repo_rate"}  # a reference, no observations
    assert node["created_at"] and node["updated_at"]


def test_duplicate_node_is_prevented_but_ensure_is_idempotent(svc) -> None:
    svc.create_node("Sector", "Banking")
    with pytest.raises(DuplicateNodeError):
        svc.create_node("Sector", "banking")  # same canonical name
    node, created = svc.ensure_node("Sector", "Banking")
    assert created is False and node["id"] == "sector:banking"


def test_unknown_family_and_foreign_reference_fields_are_rejected(svc) -> None:
    with pytest.raises(ValidationError):
        svc.create_node("Person", "X")
    with pytest.raises(ValidationError):
        svc.create_node("Sector", "Banking", references={"series_key": "x"})


def test_company_must_exist_in_the_registry() -> None:
    svc = CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory(),
                                 company_lookup=lambda cid: {"HDFCBANK": "HDFC Bank"}.get(cid))
    assert svc.create_node("Company", "HDFCBANK")["display_name"] == "HDFC Bank"
    with pytest.raises(ValidationError):
        svc.create_node("Company", "NOPE")


def test_macro_series_reference_is_checked_when_a_lookup_is_given() -> None:
    svc = CausalKnowledgeService(InMemoryGraphStore(), InMemoryHistory(), series_exists=lambda k: k == "fedfunds")
    svc.create_node("MacroIndicator", "Fed Funds", references={"series_key": "fedfunds"})
    with pytest.raises(ValidationError):
        svc.create_node("MacroIndicator", "Made Up", references={"series_key": "invented"})


# --- relationships ----------------------------------------------------------------

def test_valid_relationship_stores_every_knowledge_field_separately(svc) -> None:
    edge = _edge(svc)
    assert edge["type"] == "AFFECTS" and edge["status"] == "CANDIDATE" and edge["version"] == 1
    assert (edge["direction"], edge["confidence"], edge["effect_strength"]) == ("POSITIVE", 0.9, "HIGH")
    assert (edge["lag_min"], edge["lag_max"], edge["lag_unit"]) == (0.0, 3.0, "months")
    assert edge["scope"] == {"geography": "IN"} and edge["provenance"]["type"] == "MANUAL_SEED"
    assert "weight" not in edge


def test_relationship_type_is_normalized_and_unknown_types_rejected(svc) -> None:
    assert normalize_relationship_type("influences") == "AFFECTS"
    assert normalize_relationship_type("part of") == "BELONGS_TO"
    a, b = _pair(svc)
    with pytest.raises(ValidationError):
        svc.create_relationship(a, "CAUSES_MAGICALLY", b, direction="POSITIVE", mechanism="m", confidence=0.5,
                                effect_strength="LOW", provenance=PROV)


def test_endpoint_families_are_enforced(svc) -> None:
    a, b = _pair(svc)
    with pytest.raises(ValidationError):  # nothing flows INTO a macro indicator
        svc.create_relationship(b, "AFFECTS", a, direction="POSITIVE", mechanism="m", confidence=0.5,
                                effect_strength="LOW", provenance=PROV)
    with pytest.raises(NotFoundError):
        svc.create_relationship(a, "AFFECTS", "economic_driver:ghost", direction="POSITIVE", mechanism="m",
                                confidence=0.5, effect_strength="LOW", provenance=PROV)


def test_equivalent_relationship_is_not_duplicated(svc) -> None:
    first = _edge(svc)
    with pytest.raises(DuplicateRelationshipError) as exc:
        _edge(svc, confidence=0.5)
    assert exc.value.edge_id == first["edge_id"]


@pytest.mark.parametrize("bad", [-0.1, 1.5, "high", True, None])
def test_confidence_is_validated(svc, bad) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, confidence=bad)


@pytest.mark.parametrize("bad", ["HUGE", "", None, 3])
def test_effect_strength_is_validated(svc, bad) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, effect_strength=bad)


@pytest.mark.parametrize("bad", [
    {"min": 3, "max": 1, "unit": "months"}, {"min": 0, "max": 1, "unit": "fortnights"},
    {"min": -1, "max": 1, "unit": "days"}, {"min": "a", "max": 1, "unit": "days"},
])
def test_lag_is_validated(svc, bad) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, lag=bad)


def test_lag_is_structured_and_optional(svc) -> None:
    assert _edge(svc, lag=None)["lag_min"] is None


def test_scope_keys_are_whitelisted_and_persist(svc) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, scope={"colour": "red"})
    edge = _edge(svc, scope={"geography": "IN", "sector": "Banking", "regime": "high-rate", "period": "FY2023-FY2026"})
    assert svc.get_relationship(edge["edge_id"])["scope"]["regime"] == "high-rate"


@pytest.mark.parametrize("prov", [None, {}, {"type": "GUESS", "ref": "x"}, {"type": "MANUAL_SEED", "ref": ""}])
def test_provenance_is_required(svc, prov) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, provenance=prov)


def test_mechanism_is_required_for_causal_edges(svc) -> None:
    with pytest.raises(ValidationError):
        _edge(svc, mechanism="")


def test_increases_and_decreases_fix_the_direction(svc) -> None:
    c = svc.create_node("Commodity", "Crude Oil")["id"]
    f = svc.create_node("EconomicDriver", "Fuel Cost")["id"]
    with pytest.raises(ValidationError):
        svc.create_relationship(c, "DECREASES", f, direction="POSITIVE", mechanism="m", confidence=0.5,
                                effect_strength="LOW", provenance=PROV)
    assert svc.create_relationship(c, "INCREASES", f, direction="POSITIVE", mechanism="m", confidence=0.5,
                                   effect_strength="LOW", provenance=PROV)["direction"] == "POSITIVE"


def test_belongs_to_is_structural_and_needs_no_causal_fields(svc) -> None:
    s = svc.create_node("Sector", "Banking")["id"]
    c = CausalKnowledgeService(svc.store, svc.history, company_lookup=lambda i: "HDFC Bank").create_node("Company", "HDFCBANK")["id"]
    edge = svc.create_relationship(c, "BELONGS_TO", s, provenance=PROV)
    assert edge["direction"] == "NONE" and edge["confidence"] == 1.0 and edge["effect_strength"] is None


# --- lifecycle, versions, history -------------------------------------------------

def test_new_relationships_start_at_candidate_or_observed_only(svc) -> None:
    with pytest.raises(TransitionError):
        _edge(svc, status="VALIDATED")


def test_llm_can_only_propose_never_validate_or_promote_or_edit(svc) -> None:
    edge = _edge(svc, actor_kind="llm")
    eid = edge["edge_id"]
    svc.attach_evidence(eid, "S3", "raw/x", "SUPPORTS")
    for target in ("EVIDENCE_BACKED", "VALIDATED", "PROMOTED"):
        with pytest.raises(TransitionError):
            svc.transition_status(eid, target, actor_kind="llm", reason="because")
    with pytest.raises(TransitionError):
        svc.update_relationship(eid, confidence=0.99, actor_kind="llm", source="x", reason="y")
    assert svc.get_relationship(eid)["confidence"] == 0.9


def test_status_changes_follow_the_table_and_the_gates(svc) -> None:
    eid = _edge(svc)["edge_id"]
    with pytest.raises(TransitionError):  # CANDIDATE -> VALIDATED skips steps
        svc.transition_status(eid, "VALIDATED", actor_kind="human", reason="r")
    with pytest.raises(TransitionError):  # EVIDENCE_BACKED needs a supporting reference
        svc.transition_status(eid, "EVIDENCE_BACKED", actor_kind="system", reason="r")
    svc.attach_evidence(eid, "NEON_OBSERVATION", "macro_observations:policy_repo_rate", "SUPPORTS")
    assert svc.transition_status(eid, "EVIDENCE_BACKED", actor_kind="system", reason="r")["status"] == "EVIDENCE_BACKED"
    with pytest.raises(TransitionError):  # VALIDATED needs a SUPPORT validation
        svc.transition_status(eid, "VALIDATED", actor_kind="validation", reason="r")
    svc.attach_validation(eid, "SUPPORT", "macro_edge_pilot", ref="run1")
    assert svc.transition_status(eid, "VALIDATED", actor_kind="validation", reason="r")["status"] == "VALIDATED"
    with pytest.raises(TransitionError):  # only a human promotes
        svc.transition_status(eid, "PROMOTED", actor_kind="validation", reason="r")
    assert svc.transition_status(eid, "PROMOTED", actor_kind="human", reason="reviewed")["status"] == "PROMOTED"
    with pytest.raises(ValidationError):
        svc.transition_status(eid, "DEPRECATED", actor_kind="human", reason="")


def test_every_change_is_versioned_with_old_and_new_values(svc) -> None:
    eid = _edge(svc)["edge_id"]
    updated = svc.update_relationship(eid, confidence=0.7, lag={"min": 1, "max": 2, "unit": "quarters"},
                                      actor_kind="human", actor_id="reviewer1", source="review-2026-10",
                                      reason="longer pass-through observed")
    assert updated["version"] == 2
    history = svc.get_relationship_history(eid)
    assert [e["event_type"] for e in history] == ["causal_relationship_created", "causal_relationship_updated"]
    change = history[-1]["changes"]
    assert change["confidence"] == {"old": 0.9, "new": 0.7}
    assert change["lag_unit"] == {"old": "months", "new": "quarters"}
    assert history[-1]["actor_id"] == "reviewer1" and history[-1]["source"] == "review-2026-10"
    svc.transition_status(eid, "OBSERVED", actor_kind="system", reason="seen in data")
    assert svc.get_relationship(eid)["version"] == 3
    assert svc.get_relationship_history(eid)[-1]["changes"]["status"] == {"old": "CANDIDATE", "new": "OBSERVED"}


def test_a_no_op_update_does_not_bump_the_version(svc) -> None:
    eid = _edge(svc)["edge_id"]
    assert svc.update_relationship(eid, confidence=0.9, actor_kind="human", source="s", reason="r")["version"] == 1


def test_updates_need_a_source_and_reason(svc) -> None:
    eid = _edge(svc)["edge_id"]
    with pytest.raises(ValidationError):
        svc.update_relationship(eid, confidence=0.5, actor_kind="human", source="", reason="r")


# --- references -------------------------------------------------------------------

def test_evidence_references_are_stored_as_pointers_and_never_change_confidence(svc) -> None:
    eid = _edge(svc)["edge_id"]
    for ref_type, loc in (("S3", "raw/rbi/2026.pdf"), ("NEON_OBSERVATION", "macro_observations:policy_repo_rate"),
                          ("QDRANT_CHUNK", "chunk-123"), ("INVESTIGATION", "b5d4a32a1927")):
        svc.attach_evidence(eid, ref_type, loc, "SUPPORTS")
    svc.attach_evidence(eid, "S3", "raw/other.pdf", "CONTRADICTS", note="x" * 1000)
    edge = svc.get_relationship(eid)
    assert edge["confidence"] == 0.9
    assert (edge["evidence_support_count"], edge["evidence_contradict_count"]) == (4, 1)
    assert len(edge["evidence_refs"]) == 5 and len(edge["evidence_refs"][-1]["note"]) == cg.MAX_NOTE_CHARS
    raw = svc.store.get_edge(eid)  # the graph edge itself carries no reference data
    assert not any(k in raw for k in ("evidence_refs", "evidence_support_count", "validation_count", "feedback_count"))
    with pytest.raises(ValidationError):
        svc.attach_evidence(eid, "PDF_TEXT", "x", "SUPPORTS")


def test_validation_references_connect_to_the_edge(svc) -> None:
    eid = _edge(svc)["edge_id"]
    for result in ("SUPPORT", "SUPPORT", "PARTIAL"):
        svc.attach_validation(eid, result, "macro_edge_pilot")
    edge = svc.get_relationship(eid)
    assert [v["result"] for v in edge["validation_refs"]] == ["SUPPORT", "SUPPORT", "PARTIAL"]
    assert (edge["validation_count"], edge["last_validation_result"]) == (3, "PARTIAL")
    with pytest.raises(ValidationError):
        svc.attach_validation(eid, "MAYBE", "m")


def test_feedback_is_referenced_but_never_mutates_confidence(svc) -> None:
    eid = _edge(svc)["edge_id"]
    svc.attach_feedback("edge", eid, "WRONG_RELATIONSHIP", feedback_id=7)
    svc.attach_feedback("edge", eid, "OVERSTATED")
    svc.attach_feedback("hypothesis", "h1", "NOT_RELEVANT", edge_id=eid)
    svc.attach_feedback("investigation", "inv1", "MISSING_DRIVER")
    edge = svc.get_relationship(eid)
    assert edge["confidence"] == 0.9 and edge["version"] == 1 and edge["feedback_count"] == 3
    assert {f["feedback_type"] for f in edge["feedback_refs"]} == {"WRONG_RELATIONSHIP", "OVERSTATED", "NOT_RELEVANT"}
    with pytest.raises(ValidationError):  # taxonomy and level rules are the existing ones
        svc.attach_feedback("edge", eid, "MISSING_DRIVER")
    with pytest.raises(ValidationError):
        svc.attach_feedback("edge", eid, "LIKED_IT")
    with pytest.raises(NotFoundError):
        svc.attach_feedback("edge", "e_missing", "CORRECT")


def test_audit_events_are_recorded_for_each_kind_of_change(svc) -> None:
    eid = _edge(svc)["edge_id"]
    svc.attach_evidence(eid, "S3", "k", "SUPPORTS")
    svc.attach_validation(eid, "SUPPORT", "m")
    svc.attach_feedback("edge", eid, "CORRECT")
    svc.transition_status(eid, "EVIDENCE_BACKED", actor_kind="system", reason="r")
    kinds = {e["event_type"] for e in svc.history.events}
    assert {"causal_node_created", "causal_relationship_created", "causal_evidence_attached",
            "causal_validation_attached", "causal_feedback_attached", "causal_relationship_status_changed"} <= kinds


# --- traversal --------------------------------------------------------------------

@pytest.fixture
def seeded(svc) -> CausalKnowledgeService:
    seed_graph(svc)
    return svc


def test_seed_is_deterministic_idempotent_and_covers_every_family(seeded) -> None:
    again = seed_graph(seeded)
    assert again["nodes_created"] == 0 and again["edges_created"] == 0 and again["edges_already_present"] == again["edges_total"]
    assert {n["family"] for n in seeded.store.nodes.values()} == set(cg.NODE_FAMILIES)
    assert all(e["status"] == "CANDIDATE" and e["provenance"]["type"] == "MANUAL_SEED" for e in seeded.store.edges.values())
    assert len(NODES) + 4 == len(seeded.store.nodes)


def test_upstream_traversal_returns_the_banking_chain_with_full_edge_knowledge(seeded) -> None:
    res = seeded.expand_causes("financial_metric:revenue", max_depth=5)
    chain = [(e["cause_id"], e["type"], e["effect_id"]) for e in res["edges"] if e["depth"] <= 3]
    assert ("business_driver:loan_growth", "DRIVES", "financial_metric:revenue") in chain
    assert ("economic_driver:credit_demand", "DRIVES", "business_driver:loan_growth") in chain
    assert ("economic_driver:lending_rate", "DECREASES", "economic_driver:credit_demand") in chain
    assert "macro_indicator:rbi_policy_repo_rate" in res["nodes"]
    edge = next(e for e in res["edges"] if e["cause_id"] == "economic_driver:lending_rate")
    for key in ("type", "direction", "mechanism", "confidence", "effect_strength", "lag_min", "lag_unit", "scope",
                "status", "version", "provenance", "evidence_refs", "validation_refs", "feedback_refs"):
        assert key in edge


def test_downstream_traversal_from_repo_rate_reaches_revenue(seeded) -> None:
    res = seeded.expand_effects("macro_indicator:rbi_policy_repo_rate", max_depth=6)
    assert "financial_metric:revenue" in res["nodes"]
    into_revenue = [e for e in res["edges"] if e["effect_id"] == "financial_metric:revenue"]
    assert [e["depth"] for e in into_revenue] == [4]  # repo -> lending -> credit demand -> loan growth -> revenue


def test_traversal_respects_depth_node_and_edge_limits(seeded) -> None:
    shallow = seeded.expand_effects("macro_indicator:rbi_policy_repo_rate", max_depth=1)
    assert {e["depth"] for e in shallow["edges"]} == {1}
    capped_nodes = seeded.expand_effects("macro_indicator:rbi_policy_repo_rate", max_depth=6, max_nodes=3)
    assert len(capped_nodes["nodes"]) <= 3 and capped_nodes["truncated"]
    capped_edges = seeded.expand_effects("macro_indicator:rbi_policy_repo_rate", max_depth=6, max_edges=2)
    assert len(capped_edges["edges"]) <= 2 and capped_edges["truncated"]
    huge = seeded.expand_causes("financial_metric:revenue", max_depth=999, max_nodes=10**9, max_edges=10**9)
    assert max(e["depth"] for e in huge["edges"]) <= cg.MAX_TRAVERSAL_DEPTH


def test_direct_drivers_and_dependents(seeded) -> None:
    assert [e["cause_id"] for e in seeded.get_drivers("economic_driver:credit_demand")["edges"]] == ["economic_driver:lending_rate"]
    assert [e["effect_id"] for e in seeded.get_dependents("economic_driver:credit_demand")["edges"]] == ["business_driver:loan_growth"]


def test_reverse_flow_edges_are_oriented_by_who_influences_whom(seeded) -> None:
    # Banking DEPENDS_ON Deposits: deposits influence banking, so deposits are a cause of banking.
    causes = seeded.get_drivers("sector:banking")
    assert any(e["type"] == "DEPENDS_ON" and e["cause_id"] == "business_driver:deposits" for e in causes["edges"])


def test_structural_edges_are_not_causal_paths(seeded) -> None:
    res = seeded.expand_causes("sector:banking", max_depth=3)
    assert all(e["type"] != "BELONGS_TO" for e in res["edges"])
    assert "HDFCBANK" not in res["nodes"]


def test_company_exposures_inherit_sector_knowledge_without_copying_it(seeded) -> None:
    out = seeded.get_company_exposures("MARUTI")
    assert out["sectors"] == ["sector:auto"]
    assert [e["target_id"] for e in out["exposures"]] == ["business_driver:material_cost"]
    assert out["exposures"][0]["scope"] == {"company_id": "MARUTI"}
    assert out["sector_mechanisms"]["sector:auto"]["edges"]
    assert not any(e["source_id"] == "MARUTI" and e["type"] not in ("BELONGS_TO", "EXPOSED_TO") for e in seeded.store.edges.values())


def test_sector_mechanisms_and_cross_sector_dependencies(seeded) -> None:
    mech = seeded.get_sector_mechanisms("sector:auto")
    assert any(e["target_id"] == "business_driver:material_cost" for e in mech["edges"])
    cross = seeded.get_cross_sector_dependencies("sector:auto")
    assert cross["upstream_sectors"] == ["sector:steel"]
    assert seeded.get_cross_sector_dependencies("sector:steel")["downstream_sectors"] == ["sector:auto"]
    with pytest.raises(ValidationError):
        seeded.get_sector_mechanisms("economic_driver:lending_rate")


def test_deprecated_edges_and_low_confidence_edges_can_be_excluded(seeded) -> None:
    eid = next(e["edge_id"] for e in seeded.store.edges.values() if e["type"] == "DECREASES" and e["target_id"] == "economic_driver:credit_demand")
    seeded.transition_status(eid, "DEPRECATED", actor_kind="human", reason="superseded")
    assert not seeded.get_drivers("economic_driver:credit_demand")["edges"]
    assert seeded.get_drivers("economic_driver:credit_demand", include_deprecated=True)["edges"]
    assert not seeded.get_dependents("macro_indicator:rbi_policy_repo_rate", min_confidence=0.95)["edges"]


# --- SQL history (sqlite) ----------------------------------------------------------

def test_history_and_references_persist_in_sql(db_conn) -> None:
    svc = CausalKnowledgeService(InMemoryGraphStore(), SqlHistory(db_conn))
    eid = _edge(svc)["edge_id"]
    svc.attach_evidence(eid, "S3", "raw/a", "SUPPORTS")
    svc.attach_validation(eid, "SUPPORT", "macro_edge_pilot", ref="run1")
    svc.attach_feedback("edge", eid, "CORRECT", feedback_id=3)
    svc.update_relationship(eid, confidence=0.8, actor_kind="human", source="s", reason="r")
    edge = svc.get_relationship(eid)
    assert edge["evidence_refs"][0]["locator"] == "raw/a" and edge["validation_refs"][0]["ref"] == "run1"
    assert edge["feedback_refs"][0]["feedback_id"] == 3
    history = svc.get_relationship_history(eid)
    assert history[-1]["changes"]["confidence"] == {"old": 0.9, "new": 0.8}


# --- Neo4j store: Cypher shape with a fake driver -----------------------------------

def _fake_driver():
    driver, session = MagicMock(), MagicMock()
    driver.session.return_value.__enter__.return_value = session
    return driver, session


def test_neo4j_schema_statements_cover_every_family_and_relationship_type() -> None:
    driver, session = _fake_driver()
    statements = Neo4jGraphStore(driver).ensure_schema()
    assert sum("CONSTRAINT" in s for s in statements) == len(cg.NODE_FAMILIES)
    assert sum("INDEX" in s for s in statements) == len(cg.RELATIONSHIP_TYPES)
    assert all("IF NOT EXISTS" in s for s in statements)
    assert session.run.call_count == len(statements)


def test_neo4j_writes_are_marked_causal_and_labels_come_from_the_whitelist() -> None:
    driver, session = _fake_driver()
    store = Neo4jGraphStore(driver)
    store.put_node({"id": "sector:banking", "family": "Sector", "canonical_name": "banking", "display_name": "Banking",
                    "description": None, "version": 1, "created_at": "t", "updated_at": "t", "references": {}})
    node_query = session.run.call_args_list[0].args[0]
    assert "MERGE (n:Sector {id: $id})" in node_query and "n:CausalNode" in node_query
    with pytest.raises(ValueError):
        store.put_node({"id": "x", "family": "Sector) DETACH DELETE (n", "created_at": "t"})
    with pytest.raises(ValueError):
        store.find_edge("a", "AFFECTS]->() DETACH DELETE (a", "b")


def test_neo4j_reads_filter_on_the_causal_layer() -> None:
    driver, session = _fake_driver()
    store = Neo4jGraphStore(driver)
    store.incident_edges(["a"], limit=10)
    store.get_edge("e_1")
    store.find_edge("a", "DRIVES", "b")
    for call in session.run.call_args_list:
        assert "r.layer = $layer" in call.args[0] and call.kwargs.get("layer") == cg.CAUSAL_LAYER
    assert store.incident_edges([], limit=5) == []


# --- live Neo4j (opt-in, throwaway instance only) -----------------------------------

@pytest.mark.skipif(not os.environ.get("CAUSAL_GRAPH_TEST_NEO4J_URI"), reason="set CAUSAL_GRAPH_TEST_NEO4J_URI to a throwaway Neo4j")
def test_live_neo4j_round_trip_and_isolation_from_the_semantic_layer() -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["CAUSAL_GRAPH_TEST_NEO4J_URI"],
        auth=(os.environ.get("CAUSAL_GRAPH_TEST_NEO4J_USER", "neo4j"), os.environ["CAUSAL_GRAPH_TEST_NEO4J_PASSWORD"]),
    )
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
        # A semantic-layer edge with a colliding relationship name and a Company node that already exists.
        session.run("CREATE (:Company {id: 'MARUTI', kg_key: 'company:MARUTI'})-[:EXPOSED_TO]->(:Entity:KGNode {name: 'steel'})")
    store = Neo4jGraphStore(driver)
    store.ensure_schema()
    store.ensure_schema()  # idempotent
    svc = CausalKnowledgeService(store, InMemoryHistory())
    seed_graph(svc)
    assert seed_graph(svc)["edges_created"] == 0
    res = svc.expand_causes("financial_metric:revenue", max_depth=5)
    assert ("economic_driver:lending_rate", "DECREASES", "economic_driver:credit_demand") in {
        (e["cause_id"], e["type"], e["effect_id"]) for e in res["edges"]}
    out = svc.get_company_exposures("MARUTI")  # the pre-existing Company node, enriched
    assert [e["target_id"] for e in out["exposures"]] == ["business_driver:material_cost"]
    assert svc.get_cross_sector_dependencies("sector:auto")["upstream_sectors"] == ["sector:steel"]
    svc.update_relationship(res["edges"][0]["edge_id"], confidence=0.5, actor_kind="human", source="t", reason="t")
    assert store.get_edge(res["edges"][0]["edge_id"])["confidence"] == 0.5
    with driver.session() as session:
        legacy = session.run("MATCH (:Company {id:'MARUTI'})-[r:EXPOSED_TO]->(:Entity) RETURN count(r) AS n").single()["n"]
        assert legacy == 1  # the semantic edge is untouched
        session.run("MATCH (n) DETACH DELETE n")
    driver.close()
