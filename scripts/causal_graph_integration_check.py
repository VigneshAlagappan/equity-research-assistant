"""Integration check of the Causal Knowledge Service against the REAL Neon and
Aura configured in the environment. Read-mostly: it never changes persistent
graph knowledge. The one write path exercised -- attaching an evidence, a
validation and a feedback reference -- uses rows tagged actor 'integration-test'
and deletes them (and their audit events) afterwards; a before/after snapshot of
every edge proves nothing else moved.

  python -m scripts.causal_graph_integration_check
"""

from __future__ import annotations

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from causal_graph.history import SqlHistory  # noqa: E402
from causal_graph.neo4j_store import Neo4jGraphStore  # noqa: E402
from causal_graph.service import CausalKnowledgeService  # noqa: E402
from causal_graph.validation import DuplicateNodeError, DuplicateRelationshipError  # noqa: E402
from config import causal_graph as cg  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402

TAG = "integration-test"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  -- {detail}" if detail else ""))


def snapshot(store) -> dict:
    with store._driver.session() as s:  # noqa: SLF001
        rows = s.run("MATCH (a:CausalNode)-[r]->(b:CausalNode) WHERE r.layer = 'causal' RETURN r.edge_id AS id, properties(r) AS p")
        return {r["id"]: dict(r["p"]) for r in rows}


def main() -> int:
    from companies.registry import get_company

    conn = open_db()
    store = Neo4jGraphStore()
    svc = CausalKnowledgeService(
        store, SqlHistory(conn),
        company_lookup=lambda cid: (lambda row: row["display_name"] if row else None)(get_company(conn, cid)),
    )
    before = snapshot(store)

    families = {f: svc.find_node(family=f, limit=100) for f in cg.NODE_FAMILIES}
    check("7 node families present", all(families.values()), {f: len(n) for f, n in families.items()}.__str__())
    total_nodes = sum(len(n) for n in families.values())
    check("23+ nodes", total_nodes >= 23, str(total_nodes))
    check("25+ edges", len(before) >= 25, str(len(before)))
    types = {e["type"] if "type" in e else None for e in before.values()}
    with store._driver.session() as s:  # noqa: SLF001
        used = {r["t"] for r in s.run("MATCH ()-[r]->() WHERE r.layer = 'causal' RETURN DISTINCT type(r) AS t")}
    check("canonical relationship types in use", used <= set(cg.RELATIONSHIP_TYPES) and len(used) >= 8, str(sorted(used)))

    check("node retrieval", svc.get_node("macro_indicator:rbi_policy_repo_rate")["references"] == {"series_key": "policy_repo_rate"})
    check("company node reuses the legacy Company node", svc.get_node("MARUTI")["family"] == "Company")
    rel_id = next(i for i, p in before.items() if p["provenance_type"] == "MANUAL_SEED" and p.get("status") == "CANDIDATE")
    edge = svc.get_relationship(rel_id)
    check("relationship retrieval with references", {"evidence_refs", "validation_refs", "feedback_refs"} <= set(edge), edge["type"])

    drivers = svc.get_drivers("economic_driver:credit_demand")
    check("drivers", [e["cause_id"] for e in drivers["edges"]] == ["economic_driver:lending_rate"])
    deps = svc.get_dependents("economic_driver:credit_demand")
    check("dependents", [e["effect_id"] for e in deps["edges"]] == ["business_driver:loan_growth"])

    up = svc.expand_causes("financial_metric:operating_margin", max_depth=6)
    nodes_up = set(up["nodes"])
    check("upstream traversal reaches the auto-financing chain", {"economic_driver:auto_financing_cost", "economic_driver:vehicle_demand", "business_driver:vehicle_volume"} <= nodes_up)
    check("upstream traversal reaches the competition chain", {"economic_driver:competitive_intensity", "business_driver:pricing_pressure"} <= nodes_up)
    check("upstream traversal reaches the commodity chain", {"commodity:iron_ore", "economic_driver:steel_price", "business_driver:material_cost"} <= nodes_up)
    down = svc.expand_effects("macro_indicator:rbi_policy_repo_rate", max_depth=6)
    check("downstream traversal repo rate -> revenue", "financial_metric:revenue" in down["nodes"] and "financial_metric:operating_margin" in down["nodes"])

    mech = svc.get_sector_mechanisms("sector:auto")
    check("sector mechanisms", any(e["target_id"] == "business_driver:material_cost" for e in mech["edges"]))
    exp = svc.get_company_exposures("MARUTI")
    check("company exposures", exp["sectors"] == ["sector:auto"] and [e["target_id"] for e in exp["exposures"]] == ["business_driver:material_cost"])
    cross = svc.get_cross_sector_dependencies("sector:auto")
    check("cross-sector traversal (sector -> sector)", cross["upstream_sectors"] == ["sector:steel"], str(cross["upstream_sectors"]))
    banking = svc.expand_effects("sector:banking", max_depth=6)
    check("cross-sector traversal (banking -> auto margin via auto financing)",
          {"economic_driver:auto_financing_cost", "financial_metric:operating_margin"} <= set(banking["nodes"]))

    capped = svc.expand_causes("financial_metric:operating_margin", max_depth=6, max_nodes=4, max_edges=3)
    check("bounded traversal enforced", len(capped["nodes"]) <= 4 and len(capped["edges"]) <= 3 and capped["truncated"])

    try:
        svc.create_node("Sector", "Auto")
        check("duplicate node rejected", False)
    except DuplicateNodeError:
        check("duplicate node rejected", True)
    try:
        svc.create_relationship("economic_driver:lending_rate", "DECREASES", "economic_driver:credit_demand", direction="NEGATIVE",
                                mechanism="m", confidence=0.5, effect_strength="LOW", provenance={"type": "MANUAL_SEED", "ref": TAG})
        check("duplicate relationship rejected", False)
    except DuplicateRelationshipError:
        check("duplicate relationship rejected", True)
    try:
        with store._driver.session() as s:  # noqa: SLF001
            s.run("CREATE (:Sector {id: 'sector:auto'})").consume()
        check("uniqueness constraint enforced by Aura", False)
    except Exception as exc:  # noqa: BLE001
        check("uniqueness constraint enforced by Aura", "already exists" in str(exc) or "Constraint" in str(exc), type(exc).__name__)

    ids = {}
    ids["evidence"] = svc.attach_evidence(rel_id, "NEON_OBSERVATION", f"{TAG}:row", "SUPPORTS", actor_kind="system", actor_id=TAG)
    ids["validation"] = svc.attach_validation(rel_id, "SUPPORT", TAG, actor_kind="validation", actor_id=TAG)
    ids["feedback"] = svc.attach_feedback("edge", rel_id, "CORRECT", actor_kind="human", actor_id=TAG)
    after_attach = svc.get_relationship(rel_id)
    check("evidence/validation/feedback references round-trip through Neon",
          after_attach["evidence_support_count"] >= 1 and after_attach["validation_count"] >= 1 and after_attach["feedback_count"] >= 1)
    check("references did not change the edge", after_attach["confidence"] == edge["confidence"] and after_attach["version"] == edge["version"]
          and after_attach["status"] == edge["status"])
    cur = conn.cursor()
    for table, key in (("causal_graph_evidence_refs", "ref_id"), ("causal_graph_validation_refs", "validation_id"), ("causal_graph_feedback_refs", "id")):
        cur.execute(f"DELETE FROM {table} WHERE added_by = %s", (TAG,))
    cur.execute("DELETE FROM causal_graph_events WHERE actor_id = %s", (TAG,))
    conn.commit()
    check("test references cleaned up", not svc.get_relationship(rel_id)["evidence_refs"] or all(r["added_by"] != TAG for r in svc.get_relationship(rel_id)["evidence_refs"]))

    after = snapshot(store)
    check("no persistent edge changed", before == after)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
