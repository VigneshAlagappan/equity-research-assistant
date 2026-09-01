"""Neo4j-backed implementation of the same knowledge-graph traversal
context/graph.py already does in pure Python/SQLite. An optional, swappable
backend (config.settings.GRAPH_BACKEND="neo4j") — context/graph.py's
find_related_investigations() picks this up automatically when a local
Neo4j server is reachable, and falls back to its own SQLite/Python
traversal otherwise (same graceful-degradation pattern as
llm/router.py's provider fallback: never hard-fail research/signals_report.py
just because the graph backend isn't running).

Why this exists at all, given context/graph.py already works: it's the
"real" knowledge graph — Company/Concept/Investigation nodes and
SAME_SECTOR_AS / AFFECTS / DISCUSSED_IN / ABOUT_CONCEPT relationships,
inspectable and visualizable with Cypher in Neo4j Browser, and a much more
natural fit for multi-hop traversal than nested Python loops as this graph
grows past a handful of sectors and seed edges.

The text-matching side (which concepts a question mentions) deliberately
stays in Python — context/graph.py's _metrics_mentioned/_expand_via_seed_edges
already do that well, and there's no graph-shaped reason to move keyword
matching into Cypher. Only the actual relationship traversal (a company's
sector peers -> their investigations -> which of those discuss a relevant
concept) is Cypher's job here.

SQLite stays the source of truth. sync_graph() does a full, idempotent
rebuild (MERGE everywhere) from companies/generated_reports/
research_thread_evidence plus the static seed edge list — cheap at this
app's scale (hundreds of companies, a handful of investigations), so it's
simplest to just resync before every traversal rather than build
incremental sync/invalidation logic.

Local setup (not managed by this app — start it yourself, same as Ollama):
    docker run -d --name neo4j -p 7474:7474 -p 7687:7687 \\
        -e NEO4J_AUTH=neo4j/<your-password> neo4j:5
Then set NEO4J_PASSWORD (and GRAPH_BACKEND=neo4j) in your environment.
Browse the graph at http://localhost:7474.
"""

from __future__ import annotations

import logging
import sqlite3

from neo4j import Driver, GraphDatabase

from config.knowledge_graph_seed import KNOWLEDGE_GRAPH_SEED_EDGES
from config.knowledge_ontology import RELATIONSHIP_TYPES
from config.settings import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from context.graph import (
    SECTOR_PEER_STRENGTH,
    GraphCandidate,
    _expand_via_seed_edges,
    _metrics_mentioned,
)
from storage.fact_store import FactStore

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 3
_driver: Driver | None = None


def get_driver() -> Driver:
    """Lazily-created, cached driver — a real driver pools its own
    connections and is meant to be long-lived, not recreated per call."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def _sync_companies(tx, companies: list[sqlite3.Row]) -> None:
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (c:Company {id: row.company_id}) "
        "SET c.sector = row.sector",
        rows=[{"company_id": r["company_id"], "sector": r["sector"]} for r in companies],
    )


def _sync_sector_edges(tx, companies: list[sqlite3.Row]) -> None:
    """One SAME_SECTOR_AS edge per pair sharing a non-null sector — a
    materialized relationship (rather than just matching on the shared
    `sector` property at query time) so it's actually visible as a graph
    edge in Neo4j Browser, which is most of the point of using Neo4j here."""
    by_sector: dict[str, list[str]] = {}
    for row in companies:
        if row["sector"]:
            by_sector.setdefault(row["sector"], []).append(row["company_id"])
    pairs = [
        {"a": ids[i], "b": ids[j]}
        for ids in by_sector.values() if len(ids) > 1
        for i in range(len(ids)) for j in range(i + 1, len(ids))
    ]
    if pairs:
        tx.run(
            "UNWIND $pairs AS pair "
            "MATCH (a:Company {id: pair.a}), (b:Company {id: pair.b}) "
            "MERGE (a)-[:SAME_SECTOR_AS]-(b)",
            pairs=pairs,
        )


def _sync_seed_edges(tx, seed_edges) -> None:
    tx.run(
        "UNWIND $edges AS edge "
        "MERGE (a:Concept {key: edge.source}) "
        "MERGE (b:Concept {key: edge.target}) "
        "MERGE (a)-[r:AFFECTS]->(b) "
        "SET r.strength = edge.strength, r.reason = edge.reason",
        edges=[
            {"source": source, "target": target, "strength": strength, "reason": reason}
            for source, _relationship, target, strength, reason in seed_edges
        ],
    )


def _sync_investigation(
    tx, thread_id: str, question: str, report_markdown: str, company_ids: list[str], concept_keys: list[str]
) -> None:
    tx.run(
        "MERGE (inv:Investigation {thread_id: $thread_id}) "
        "SET inv.question = $question, inv.report_markdown = $report_markdown",
        thread_id=thread_id, question=question, report_markdown=report_markdown,
    )
    tx.run(
        "UNWIND $company_ids AS cid "
        "MATCH (inv:Investigation {thread_id: $thread_id}), (c:Company {id: cid}) "
        "MERGE (inv)-[:DISCUSSED_IN]->(c)",
        thread_id=thread_id, company_ids=company_ids,
    )
    if concept_keys:
        tx.run(
            "UNWIND $concept_keys AS key "
            "MATCH (inv:Investigation {thread_id: $thread_id}) "
            "MERGE (m:Concept {key: key}) "
            "MERGE (inv)-[:ABOUT_CONCEPT]->(m)",
            thread_id=thread_id, concept_keys=concept_keys,
        )


def sync_graph(conn: sqlite3.Connection, driver: Driver, *, fact_store: FactStore) -> None:
    """Full, idempotent rebuild of the Neo4j graph from SQLite + the seed
    edge list. Safe to call before every traversal — MERGE never duplicates
    a node/relationship that already exists. fact_store is always passed
    explicitly by the two callers (context/graph.py, context/knowledge_graph.py)
    — never called directly from outside this module, so no default here."""
    companies = fact_store.list_companies_with_sector(conn)
    reports = fact_store.list_generated_reports(conn)

    with driver.session() as session:
        session.execute_write(_sync_companies, companies)
        session.execute_write(_sync_sector_edges, companies)
        session.execute_write(_sync_seed_edges, KNOWLEDGE_GRAPH_SEED_EDGES)
        for report in reports:
            evidence_text = " ".join(e["label"] for e in fact_store.list_report_evidence(conn, report["thread_id"]))
            concepts = _metrics_mentioned(evidence_text) | _metrics_mentioned(report["question"])
            session.execute_write(
                _sync_investigation, report["thread_id"], report["question"], report["report_markdown"],
                report["company_ids"], list(concepts),
            )


def _query_related(tx, company_id: str, relevant_concept_keys: list[str]):
    return list(tx.run(
        "MATCH (target:Company {id: $company_id})-[:SAME_SECTOR_AS]-(peer:Company) "
        "MATCH (inv:Investigation)-[:DISCUSSED_IN]->(peer) "
        "WHERE NOT (inv)-[:DISCUSSED_IN]->(target) "
        "MATCH (inv)-[:ABOUT_CONCEPT]->(concept:Concept) "
        "WHERE concept.key IN $relevant_concept_keys "
        "RETURN inv.thread_id AS thread_id, inv.question AS question, inv.report_markdown AS report_markdown, "
        "       peer.id AS peer_id, target.sector AS sector, concept.key AS concept_key",
        company_id=company_id, relevant_concept_keys=relevant_concept_keys,
    ))


def find_related_investigations(driver: Driver, question: str, company_ids: list[str]) -> list[GraphCandidate]:
    """Same contract and GraphCandidate shape as context/graph.py's
    pure-Python version — Cypher does the multi-hop traversal (sector peers
    -> their investigations -> matching concepts), Python still does the
    text matching and scoring."""
    if len(company_ids) != 1:
        return []
    company_id = company_ids[0]

    direct_metrics = _metrics_mentioned(question)
    if not direct_metrics:
        return []
    relevant_metrics = _expand_via_seed_edges(direct_metrics)

    with driver.session() as session:
        rows = session.execute_read(_query_related, company_id, list(relevant_metrics))

    best: dict[str, GraphCandidate] = {}
    for row in rows:
        strength = relevant_metrics.get(row["concept_key"], 0.0)
        score = SECTOR_PEER_STRENGTH * strength
        if row["thread_id"] in best and best[row["thread_id"]].score >= score:
            continue
        bridge = "" if row["concept_key"] in direct_metrics else f" (bridged via {row['concept_key']})"
        best[row["thread_id"]] = GraphCandidate(
            thread_id=row["thread_id"], company_ids=[row["peer_id"]], question=row["question"],
            report_markdown=row["report_markdown"],
            score=score,
            path=(
                f"{company_id} --SAME_SECTOR_AS[{row['sector']}]--> {row['peer_id']} "
                f"--DISCUSSED_IN--> investigation on {row['concept_key']}{bridge} (score {score:.2f}) [neo4j]"
            ),
        )

    return sorted(best.values(), key=lambda c: c.score, reverse=True)[:_MAX_CANDIDATES]


# ============================================================
# Research Knowledge Graph (Step 2B) — projects the Knowledge Builder's
# (Step 2A) knowledge_entities/knowledge_claims/knowledge_relationships/
# knowledge_evidence into this same Neo4j graph. A separate concern from
# find_related_investigations() above (sector-peer generated_reports), but
# the same graph/driver/database — Company nodes are deliberately SHARED
# between the two: a knowledge_entities row of type "Company" merges into
# the exact same (:Company {id: ...}) node _sync_companies() already
# creates, via the common :KGNode{kg_key} tag every node gets here, rather
# than creating a second, duplicate Company node. Every non-Company entity
# gets its own (:Entity {id: entity_id}) node instead, keyed by the SQL
# primary key (not by name) so two different companies' same-named "Growth"
# strategy entities never collide.
# ============================================================


def _sync_knowledge_entities(tx, entities: list[sqlite3.Row]) -> None:
    companies = [
        {"company_id": e["company_id"]} for e in entities if e["entity_type"] == "Company" and e["company_id"]
    ]
    others = [
        {"id": e["entity_id"], "type": e["entity_type"], "name": e["name"]}
        for e in entities if e["entity_type"] != "Company"
    ]
    if companies:
        tx.run(
            "UNWIND $rows AS row "
            "MERGE (c:Company {id: row.company_id}) "
            "SET c:KGNode, c.kg_key = 'company:' + row.company_id",
            rows=companies,
        )
    if others:
        tx.run(
            "UNWIND $rows AS row "
            "MERGE (e:Entity {id: row.id}) "
            "SET e.type = row.type, e.name = row.name, e:KGNode, e.kg_key = 'entity:' + toString(row.id)",
            rows=others,
        )


def _sync_knowledge_claims(tx, claims: list[sqlite3.Row]) -> None:
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (cl:Claim {id: row.claim_id}) "
        "SET cl.company_id = row.company_id, cl.document_id = row.document_id, "
        "    cl.claim_type = row.claim_type, cl.category = row.category, cl.text = row.claim_text, "
        "    cl.speaker = row.speaker, cl.fiscal_year = row.fiscal_year, cl.quarter = row.quarter, "
        "    cl.confidence = row.confidence",
        rows=[
            {
                "claim_id": c["claim_id"], "company_id": c["company_id"], "document_id": c["document_id"],
                "claim_type": c["claim_type"], "category": c["category"], "claim_text": c["claim_text"],
                "speaker": c["speaker"], "fiscal_year": c["fiscal_year"], "quarter": c["quarter"],
                "confidence": c["extraction_confidence"],
            }
            for c in claims
        ],
    )
    # STATES: the claim's own company asserted it (a coarser but always-
    # available link than trying to resolve `speaker` to a specific
    # ManagementPerson entity, which isn't attempted here).
    tx.run(
        "UNWIND $rows AS row "
        "MATCH (co:Company {id: row.company_id}), (cl:Claim {id: row.claim_id}) "
        "MERGE (co)-[:STATES]->(cl)",
        rows=[{"company_id": c["company_id"], "claim_id": c["claim_id"]} for c in claims if c["company_id"]],
    )
    # VALID_DURING: one TimePeriod node per distinct fiscal_year(+quarter).
    periods = [
        {"claim_id": c["claim_id"], "period_key": f"{c['fiscal_year']}-{c['quarter']}" if c["quarter"] else c["fiscal_year"]}
        for c in claims if c["fiscal_year"]
    ]
    if periods:
        tx.run(
            "UNWIND $rows AS row "
            "MERGE (tp:TimePeriod {key: row.period_key}) "
            "WITH tp, row "
            "MATCH (cl:Claim {id: row.claim_id}) "
            "MERGE (cl)-[:VALID_DURING]->(tp)",
            rows=periods,
        )


def _sync_knowledge_evidence(tx, evidence: list[sqlite3.Row]) -> None:
    if not evidence:
        return
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (ev:Evidence {id: row.evidence_id}) "
        "SET ev.quote = row.quote "
        "WITH ev, row "
        "MATCH (cl:Claim {id: row.claim_id}) "
        "MERGE (cl)-[:SUPPORTED_BY]->(ev)",
        rows=[{"evidence_id": e["evidence_id"], "claim_id": e["claim_id"], "quote": e["quote"]} for e in evidence],
    )


def _entity_kg_key(entity_by_id: dict[int, sqlite3.Row], entity_id: int) -> str | None:
    entity = entity_by_id.get(entity_id)
    if entity is None:
        return None
    if entity["entity_type"] == "Company":
        return f"company:{entity['company_id']}"
    return f"entity:{entity_id}"


def _sync_knowledge_relationships(tx, relationships: list[sqlite3.Row], entity_by_id: dict[int, sqlite3.Row]) -> None:
    # ABOUT: every entity a claim's relationships touch, so a claim is
    # findable from any entity it's connected to, not just via the
    # relationship's own (source, type, target) triple.
    about_rows = []
    by_type: dict[str, list[dict]] = {}
    for r in relationships:
        source_key = _entity_kg_key(entity_by_id, r["source_entity_id"])
        target_key = _entity_kg_key(entity_by_id, r["target_entity_id"])
        if source_key is None or target_key is None or r["relationship_type"] not in RELATIONSHIP_TYPES:
            continue  # a hallucinated/unresolvable relationship was already dropped at 2A persistence time — defensive only
        by_type.setdefault(r["relationship_type"], []).append({"source_key": source_key, "target_key": target_key})
        if r["claim_id"] is not None:
            about_rows.append({"claim_id": r["claim_id"], "entity_key": source_key})
            about_rows.append({"claim_id": r["claim_id"], "entity_key": target_key})

    for relationship_type, rows in by_type.items():
        # relationship_type is validated against RELATIONSHIP_TYPES (a fixed
        # ~13-value vocabulary, config/knowledge_ontology.py) just above —
        # safe to interpolate into the Cypher string; Cypher has no
        # parameter syntax for a relationship TYPE itself.
        tx.run(
            f"UNWIND $rows AS row "
            f"MATCH (s:KGNode {{kg_key: row.source_key}}), (t:KGNode {{kg_key: row.target_key}}) "
            f"MERGE (s)-[:{relationship_type}]->(t)",
            rows=rows,
        )
    if about_rows:
        tx.run(
            "UNWIND $rows AS row "
            "MATCH (cl:Claim {id: row.claim_id}), (n:KGNode {kg_key: row.entity_key}) "
            "MERGE (cl)-[:ABOUT]->(n)",
            rows=about_rows,
        )


def sync_knowledge_graph(conn: sqlite3.Connection, driver: Driver, *, fact_store: FactStore) -> None:
    """Full, idempotent rebuild of the knowledge-claim graph from SQLite —
    same "SQLite stays the source of truth, resync before every query"
    philosophy sync_graph() above already uses, not incremental sync/
    invalidation logic. Safe to call independently of sync_graph() — Company
    nodes are MERGEd either way, so whichever sync runs first creates them
    and the other just adds properties/relationships onto the same node.
    fact_store is always passed explicitly by find_claims_about_entity's
    caller in context/knowledge_graph.py — no default here."""
    entities = fact_store.list_all_knowledge_entities(conn)
    claims = fact_store.list_all_knowledge_claims(conn)
    relationships = fact_store.list_all_knowledge_relationships(conn)
    evidence = fact_store.list_all_knowledge_evidence(conn)
    entity_by_id = {e["entity_id"]: e for e in entities}

    with driver.session() as session:
        session.execute_write(_sync_knowledge_entities, entities)
        session.execute_write(_sync_knowledge_claims, claims)
        session.execute_write(_sync_knowledge_evidence, evidence)
        session.execute_write(_sync_knowledge_relationships, relationships, entity_by_id)


def _query_claims_about_entity(tx, entity_key: str):
    return list(tx.run(
        "MATCH (n:KGNode {kg_key: $entity_key})<-[:ABOUT]-(cl:Claim) "
        "OPTIONAL MATCH (cl)-[:SUPPORTED_BY]->(ev:Evidence) "
        "OPTIONAL MATCH (cl)-[:ABOUT]->(other:KGNode) WHERE other.kg_key <> $entity_key "
        "RETURN cl.id AS claim_id, cl.company_id AS company_id, cl.text AS claim_text, "
        "       cl.claim_type AS claim_type, cl.category AS category, cl.speaker AS speaker, "
        "       cl.fiscal_year AS fiscal_year, cl.quarter AS quarter, cl.confidence AS confidence, "
        "       cl.document_id AS document_id, "
        "       collect(DISTINCT ev.quote) AS evidence_quotes, "
        "       collect(DISTINCT [coalesce(other.type, 'Company'), coalesce(other.name, other.id)]) AS related_entities",
        entity_key=entity_key,
    ))


def _query_entity_id_by_name(tx, entity_type: str, entity_name: str):
    return tx.run(
        "MATCH (e:Entity {type: $type, name: $name}) RETURN e.id AS id", type=entity_type, name=entity_name
    ).single()


def find_claims_about_entity(driver: Driver, entity_type: str, entity_name: str):
    """Neo4j-backed implementation of context/knowledge_graph.py's
    find_claims_about_entity() — same contract and KnowledgeClaimView shape
    as its SQLite counterpart, Cypher does the "who else is connected to
    this entity" traversal in one query instead of the SQL path's per-claim
    follow-up lookups."""
    from context.knowledge_graph import KnowledgeClaimView

    entity_key = f"company:{entity_name}" if entity_type == "Company" else None
    if entity_key is None:
        with driver.session() as session:
            row = session.execute_read(_query_entity_id_by_name, entity_type, entity_name)
        if row is None:
            return []
        entity_key = f"entity:{row['id']}"

    with driver.session() as session:
        rows = session.execute_read(_query_claims_about_entity, entity_key)

    return [
        KnowledgeClaimView(
            claim_id=row["claim_id"], company_id=row["company_id"], claim_text=row["claim_text"],
            claim_type=row["claim_type"], category=row["category"], speaker=row["speaker"],
            fiscal_year=row["fiscal_year"], quarter=row["quarter"], confidence=row["confidence"],
            document_id=row["document_id"], evidence_quotes=[q for q in row["evidence_quotes"] if q],
            related_entities=sorted({tuple(pair) for pair in row["related_entities"] if pair[1] is not None}),
            backend="neo4j",
        )
        for row in rows
    ]


# ============================================================
# Financial figures (TRIAL) — projects canonical_financials, the
# deterministic layer architecture.md calls out as "SQLite knows the
# facts," into this same graph. Everything above (sync_graph/
# sync_knowledge_graph) is about relationships BETWEEN companies/claims;
# this is the first time an actual number crosses into Neo4j at all.
#
# (:Company)-[:REPORTED]->(:Observation {value, unit, statement_type})
#            -[:OF_METRIC]->(:Metric {key, display_name, category})
# (:Observation)-[:DURING]->(:TimePeriod {key})
#
# TimePeriod nodes are the SAME ones _sync_knowledge_claims()'s VALID_DURING
# already creates (identical "FY2024-Q1"/"FY2024" key format) -- a genuine
# payoff of putting financials in the graph at all: a metric's period and a
# document claim's period now MERGE onto one node, so "what did management
# say during the same quarter this ratio moved" becomes a real 2-hop
# traversal instead of a separate SQL join you'd have to write yourself.
#
# Deliberately NOT wired into sync_graph()/sync_knowledge_graph() or their
# callers' automatic resync -- canonical_financials is 1000+ rows per
# company, so syncing every registered company (2,500+) on every traversal
# is a real scale/cost decision to make deliberately later, not a trial
# default. Call sync_financials() directly, scoped to whichever
# company_ids you actually want in the graph right now.
# ============================================================


def _observation_id(row: sqlite3.Row) -> str:
    return "|".join(str(x) for x in (
        row["company_id"], row["metric_key"], row["period_type"],
        row["fiscal_year"], row["quarter"] or "", row["statement_type"] or "",
    ))


def _period_key(row: sqlite3.Row) -> str | None:
    if not row["fiscal_year"]:
        return None
    return f"{row['fiscal_year']}-{row['quarter']}" if row["quarter"] else row["fiscal_year"]


def _sync_metrics(tx, rows: list[sqlite3.Row]) -> None:
    by_key = {
        r["metric_key"]: {"key": r["metric_key"], "display_name": r["display_name"], "category": r["category"]}
        for r in rows
    }
    tx.run(
        "UNWIND $rows AS row "
        "MERGE (m:Metric {key: row.key}) "
        "SET m.display_name = row.display_name, m.category = row.category",
        rows=list(by_key.values()),
    )


def _sync_observations(tx, rows: list[sqlite3.Row]) -> None:
    tx.run(
        "UNWIND $rows AS row "
        "MATCH (c:Company {id: row.company_id}), (m:Metric {key: row.metric_key}) "
        "MERGE (o:Observation {obs_id: row.obs_id}) "
        "SET o.value = row.value, o.unit = row.unit, o.statement_type = row.statement_type, "
        "    o.period_type = row.period_type, o.fiscal_year = row.fiscal_year, o.quarter = row.quarter "
        "MERGE (c)-[:REPORTED]->(o) "
        "MERGE (o)-[:OF_METRIC]->(m)",
        rows=[
            {
                "obs_id": _observation_id(r), "company_id": r["company_id"], "metric_key": r["metric_key"],
                "value": r["canonical_value"], "unit": r["unit"], "statement_type": r["statement_type"],
                "period_type": r["period_type"], "fiscal_year": r["fiscal_year"], "quarter": r["quarter"],
            }
            for r in rows
        ],
    )
    periods = [{"obs_id": _observation_id(r), "period_key": _period_key(r)} for r in rows if _period_key(r)]
    if periods:
        tx.run(
            "UNWIND $rows AS row "
            "MERGE (tp:TimePeriod {key: row.period_key}) "
            "WITH tp, row "
            "MATCH (o:Observation {obs_id: row.obs_id}) "
            "MERGE (o)-[:DURING]->(tp)",
            rows=periods,
        )


def sync_financials(conn: sqlite3.Connection, driver: Driver, *, fact_store: FactStore, company_ids: list[str]) -> int:
    """TRIAL. Full, idempotent (re)sync of canonical_financials for exactly
    these companies -- see the module comment above for the graph shape and
    why this isn't wired into the automatic resync path yet. Returns the
    row count synced, for a caller to report back."""
    rows = fact_store.list_canonical_financials_for_companies(conn, company_ids)
    with driver.session() as session:
        session.execute_write(_sync_metrics, rows)
        session.execute_write(_sync_observations, rows)
    return len(rows)
