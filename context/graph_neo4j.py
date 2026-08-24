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
from config.settings import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from context.graph import (
    SECTOR_PEER_STRENGTH,
    GraphCandidate,
    _expand_via_seed_edges,
    _metrics_mentioned,
)
from storage.repositories import list_generated_reports, list_report_evidence

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


def sync_graph(conn: sqlite3.Connection, driver: Driver) -> None:
    """Full, idempotent rebuild of the Neo4j graph from SQLite + the seed
    edge list. Safe to call before every traversal — MERGE never duplicates
    a node/relationship that already exists."""
    companies = conn.execute(
        "SELECT company_id, COALESCE(NULLIF(basic_industry, ''), NULLIF(macro_economic_sector, '')) AS sector "
        "FROM companies"
    ).fetchall()
    reports = list_generated_reports(conn)

    with driver.session() as session:
        session.execute_write(_sync_companies, companies)
        session.execute_write(_sync_sector_edges, companies)
        session.execute_write(_sync_seed_edges, KNOWLEDGE_GRAPH_SEED_EDGES)
        for report in reports:
            evidence_text = " ".join(e["label"] for e in list_report_evidence(conn, report["thread_id"]))
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
