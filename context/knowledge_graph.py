"""Research Knowledge Graph (Step 2B) — projects the Knowledge Builder's
(Step 2A) structured claims/entities/relationships into graph form and
answers a cross-entity, cross-company question plain per-company SQL
doesn't do well: "which claims, from ANY company, are connected to this
entity?" Same GRAPH_BACKEND-driven backend choice and graceful degradation
as context/graph.py's sector-peer traversal — SQLite by default (a real
join-based traversal, not a stub), a real Neo4j graph when
GRAPH_BACKEND=neo4j, with automatic fallback to SQLite if unreachable.

Every result stays traceable back to its source evidence — this module
answers "what is connected to what, when was it true, and what evidence
supports that relationship," never inventing a connection the underlying
knowledge_relationships/knowledge_evidence rows don't already have.

The graph is not the canonical home for the claims themselves — SQLite
(knowledge_claims etc., Step 2A) stays the source of truth; this module
only projects/queries it, the same relationship context/graph_neo4j.py
already has with context/graph.py's sector-peer data.
"""

from __future__ import annotations

import logging
from storage.db_types import DBConnection
from dataclasses import dataclass, field

from config.settings import GRAPH_BACKEND
from storage.fact_store import FactStore, default_fact_store

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeClaimView:
    """One claim, with enough context to answer 2B's own question about it
    without a second round-trip — same shape regardless of which backend
    (SQLite or Neo4j) answered the query."""

    claim_id: int
    company_id: str | None
    claim_text: str
    claim_type: str
    category: str | None
    speaker: str | None
    fiscal_year: str | None
    quarter: str | None
    confidence: float | None
    document_id: int
    evidence_quotes: list[str] = field(default_factory=list)
    related_entities: list[tuple[str, str]] = field(default_factory=list)  # (entity_type, entity_name)
    backend: str = "sqlite"


def find_claims_about_entity(
    conn: DBConnection, entity_type: str, entity_name: str, *, fact_store: FactStore | None = None
) -> list[KnowledgeClaimView]:
    """Every claim, from any company, whose extracted relationships touch
    this entity — e.g. entity_type="Risk", entity_name="Interest Rate
    Volatility" surfaces every company's claims connected to that risk, not
    just one company's own. Dispatches to Neo4j when GRAPH_BACKEND=neo4j and
    reachable, falling back to the SQLite join-based traversal otherwise —
    same graceful-degradation pattern as context/graph.py's
    find_related_investigations()."""
    fs = fact_store or default_fact_store()
    if GRAPH_BACKEND == "neo4j":
        try:
            from context import graph_neo4j

            driver = graph_neo4j.get_driver()
            graph_neo4j.sync_knowledge_graph(conn, driver, fact_store=fs)
            return graph_neo4j.find_claims_about_entity(driver, entity_type, entity_name)
        except Exception:
            logger.warning("Neo4j graph backend unavailable, falling back to SQLite traversal", exc_info=True)
    return _find_claims_about_entity_sqlite(conn, entity_type, entity_name, fs)


def _find_claims_about_entity_sqlite(
    conn: DBConnection, entity_type: str, entity_name: str, fact_store: FactStore
) -> list[KnowledgeClaimView]:
    claims = fact_store.find_knowledge_claims_about_entity(conn, entity_type, entity_name)
    views: list[KnowledgeClaimView] = []
    for claim in claims:
        evidence = [
            row["quote"] for row in fact_store.list_knowledge_evidence_for_claim(conn, claim["claim_id"]) if row["quote"]
        ]
        related: set[tuple[str, str]] = set()
        for rel in fact_store.list_knowledge_relationships_for_claim(conn, claim["claim_id"]):
            related.add((rel["source_type"], rel["source_name"]))
            related.add((rel["target_type"], rel["target_name"]))
        related.discard((entity_type, entity_name))
        views.append(
            KnowledgeClaimView(
                claim_id=claim["claim_id"], company_id=claim["company_id"], claim_text=claim["claim_text"],
                claim_type=claim["claim_type"], category=claim["category"], speaker=claim["speaker"],
                fiscal_year=claim["fiscal_year"], quarter=claim["quarter"], confidence=claim["extraction_confidence"],
                document_id=claim["document_id"], evidence_quotes=evidence,
                related_entities=sorted(related), backend="sqlite",
            )
        )
    return views
