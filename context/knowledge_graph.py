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
from storage.db_types import DBConnection, Row
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
    #: How many relationship-edge hops separate the ORIGINAL queried entity
    #: from the entity this claim is actually about — 1 (the default, and
    #: the only value find_claims_about_entity() above ever produces) means
    #: this claim touches the queried entity directly. find_multi_hop_claims()
    #: below is the only thing that ever sets this to 2+. Defaulting to 1
    #: keeps both existing construction sites (this module's SQLite path,
    #: context/graph_neo4j.py's Neo4j path) fully backward compatible — both
    #: already build every KnowledgeClaimView with keyword args only.
    hop_distance: int = 1
    #: Human-readable relationship chain from the queried entity to the
    #: entity this claim is about, e.g. "Risk:Input cost inflation
    #: --MAY_AFFECT--> Metric:Gross Margin" — empty for a direct (hop_distance
    #: == 1) claim, mirroring context/graph.py::GraphCandidate.path's existing
    #: convention for surfacing a multi-step reasoning chain.
    path: str = ""


def find_claims_about_entity(
    conn: DBConnection, entity_type: str, entity_name: str, *, fact_store: FactStore | None = None,
    as_of: str | None = None,
) -> list[KnowledgeClaimView]:
    """Every claim, from any company, whose extracted relationships touch
    this entity — e.g. entity_type="Risk", entity_name="Interest Rate
    Volatility" surfaces every company's claims connected to that risk, not
    just one company's own. Dispatches to Neo4j when GRAPH_BACKEND=neo4j and
    reachable, falling back to the SQLite join-based traversal otherwise —
    same graceful-degradation pattern as context/graph.py's
    find_related_investigations().

    `as_of` (ISO date) keeps only claims whose fiscal period had already
    ended by the cutoff — research/temporal.py. A claim with no fiscal period
    on record is dropped under a cutoff (fail closed): an undated management
    statement cannot be shown to predate the date being reasoned as-of. Both
    backends go through the same filter, so switching GRAPH_BACKEND cannot
    change what a point-in-time investigation is allowed to see."""
    fs = fact_store or default_fact_store()
    claims: list[KnowledgeClaimView]
    if GRAPH_BACKEND == "neo4j":
        try:
            from context import graph_neo4j

            driver = graph_neo4j.get_driver()
            graph_neo4j.sync_knowledge_graph(conn, driver, fact_store=fs)
            return _apply_as_of(graph_neo4j.find_claims_about_entity(driver, entity_type, entity_name), as_of)
        except Exception:
            logger.warning("Neo4j graph backend unavailable, falling back to SQLite traversal", exc_info=True)
    claims = _find_claims_about_entity_sqlite(conn, entity_type, entity_name, fs)
    return _apply_as_of(claims, as_of)


#: A per-hop frontier-size cap — a highly-connected entity (a popular Risk,
#: a MacroFactor referenced by dozens of companies) could otherwise expand
#: to an unbounded number of neighbors on one hop and turn a bounded BFS
#: into an effectively unbounded query. Truncation is deterministic (the
#: first N neighbors in the batched query's own result order, not a random
#: sample) and always logged — a caller sees a partial, not a silently
#: wrong, multi-hop result.
_MAX_HOP_FRONTIER = 50


def find_multi_hop_claims(
    conn: DBConnection, entity_type: str, entity_name: str, *, max_hops: int = 2,
    fact_store: FactStore | None = None, as_of: str | None = None,
) -> list[KnowledgeClaimView]:
    """Multi-hop counterpart to find_claims_about_entity() above — answers
    "which claims, from any company, are connected to an entity reached by
    walking OUTWARD from this one" (e.g. entity_type="Risk",
    entity_name="Input cost inflation" surfaces a claim about the "Gross
    Margin" Metric that risk MAY_AFFECT, even though that claim never
    mentions the risk itself). find_claims_about_entity() already answers
    every hop_distance==1 question (claims touching the queried entity
    directly) — this function only ever returns hop_distance >= 2, and a
    claim that also touches the queried entity directly (so it would
    already be in find_claims_about_entity()'s own result) is excluded here
    even if a longer path also reaches it, so the two functions' outputs
    never overlap.

    BFS outward from every entity_id matching (entity_type, entity_name),
    one batched query per hop across the whole frontier (never one query
    per node — storage/repositories.py::list_entity_neighbors()'s own
    docstring explains why that matters), bounded by `max_hops` (default 2,
    i.e. one edge beyond the queried entity's own direct neighbors) and a
    per-hop frontier-size cap (_MAX_HOP_FRONTIER). Same GRAPH_BACKEND
    dispatch/fallback and `as_of` filtering shape as find_claims_about_entity()."""
    fs = fact_store or default_fact_store()
    if GRAPH_BACKEND == "neo4j":
        try:
            from context import graph_neo4j

            driver = graph_neo4j.get_driver()
            graph_neo4j.sync_knowledge_graph(conn, driver, fact_store=fs)
            return _apply_as_of(
                graph_neo4j.find_multi_hop_claims(driver, entity_type, entity_name, max_hops=max_hops), as_of
            )
        except Exception:
            logger.warning("Neo4j graph backend unavailable, falling back to SQLite traversal", exc_info=True)
    claims = _find_multi_hop_claims_sqlite(conn, entity_type, entity_name, fs, max_hops)
    return _apply_as_of(claims, as_of)


#: An entity "node" in this BFS is really a (entity_type, name) pair, not a
#: single entity_id row — the same generic entity name (a Risk, a Metric)
#: gets its own separate, per-company-scoped knowledge_entities row every
#: time a different company's document extracts it (UNIQUE(entity_type,
#: name, company_id)), exactly the way find_claims_about_entity()'s own
#: query already matches by (entity_type, name) rather than one entity_id,
#: to surface every company's claims about "the same" entity. Pooling every
#: entity_id sharing a (entity_type, name) key together before expanding
#: neighbors/looking up claims is what lets the BFS cross company
#: boundaries at all — a pure entity_id graph walk never would, since a
#: relationship only ever links entity_id rows extracted from the SAME
#: document.
_EntityKey = tuple[str, str]


def _find_multi_hop_claims_sqlite(
    conn: DBConnection, entity_type: str, entity_name: str, fact_store: FactStore, max_hops: int,
) -> list[KnowledgeClaimView]:
    queried: _EntityKey = (entity_type, entity_name)
    start_ids = fact_store.list_knowledge_entity_ids_by_type_and_name(conn, entity_type, entity_name)
    if not start_ids:
        return []

    visited: set[_EntityKey] = {queried}
    #: (entity_type, name) -> the human-readable path from the queried entity out to it
    frontier_paths: dict[_EntityKey, str] = {queried: f"{entity_type}:{entity_name}"}
    #: (entity_type, name) -> every entity_id sharing that key (one per company)
    frontier_ids: dict[_EntityKey, list[int]] = {queried: start_ids}
    views: list[KnowledgeClaimView] = []
    seen_claim_ids: set[int] = set()

    for edge_distance in range(1, max_hops):
        if not frontier_paths:
            break
        id_to_key: dict[int, _EntityKey] = {
            entity_id: key for key, ids in frontier_ids.items() for entity_id in ids
        }
        edges = fact_store.list_entity_neighbors(conn, list(id_to_key.keys()))

        next_frontier_paths: dict[_EntityKey, str] = {}
        for edge in edges:
            # Each edge is examined from both directions — "either
            # direction" per the module's own contract, since a
            # relationship's source/target is an extraction artifact
            # (who was named first in the sentence), not a real directional
            # constraint on what's connected to what.
            for this_id, other_type, other_name, arrow in (
                (
                    edge["source_entity_id"],
                    edge["target_type"], edge["target_name"], f"--{edge['relationship_type']}-->",
                ),
                (
                    edge["target_entity_id"],
                    edge["source_type"], edge["source_name"], f"<--{edge['relationship_type']}--",
                ),
            ):
                this_key = id_to_key.get(this_id)
                other_key = (other_type, other_name)
                if this_key is None or other_key in visited:
                    continue
                candidate_path = f"{frontier_paths[this_key]} {arrow} {other_type}:{other_name}"
                # Keep the shortest-worded path found so far to this
                # neighbor if it's reachable via more than one edge this hop.
                if other_key not in next_frontier_paths or len(candidate_path) < len(next_frontier_paths[other_key]):
                    next_frontier_paths[other_key] = candidate_path

        if len(next_frontier_paths) > _MAX_HOP_FRONTIER:
            logger.warning(
                "find_multi_hop_claims: frontier at hop_distance=%d truncated from %d to %d entities "
                "(entity_type=%s, entity_name=%s) -- result is a partial, not exhaustive, traversal",
                edge_distance + 1, len(next_frontier_paths), _MAX_HOP_FRONTIER, entity_type, entity_name,
            )
            next_frontier_paths = dict(list(next_frontier_paths.items())[:_MAX_HOP_FRONTIER])

        visited.update(next_frontier_paths.keys())
        hop_distance = edge_distance + 1

        next_frontier_ids: dict[_EntityKey, list[int]] = {
            key: fact_store.list_knowledge_entity_ids_by_type_and_name(conn, key[0], key[1])
            for key in next_frontier_paths
        }
        all_next_ids = [entity_id for ids in next_frontier_ids.values() for entity_id in ids]
        if all_next_ids:
            id_to_next_key = {entity_id: key for key, ids in next_frontier_ids.items() for entity_id in ids}
            claim_rows = fact_store.find_knowledge_claims_for_entity_ids(conn, all_next_ids)
            claims_by_id: dict[int, Row] = {}
            via_key_by_claim: dict[int, _EntityKey] = {}
            for row in claim_rows:
                claim_id = row["claim_id"]
                if claim_id in seen_claim_ids or claim_id in claims_by_id:
                    continue
                claims_by_id[claim_id] = row
                via_key_by_claim[claim_id] = id_to_next_key[row["matched_entity_id"]]

            for claim_id, claim in claims_by_id.items():
                evidence = [
                    row["quote"] for row in fact_store.list_knowledge_evidence_for_claim(conn, claim_id) if row["quote"]
                ]
                related: set[tuple[str, str]] = set()
                for rel in fact_store.list_knowledge_relationships_for_claim(conn, claim_id):
                    related.add((rel["source_type"], rel["source_name"]))
                    related.add((rel["target_type"], rel["target_name"]))
                if queried in related:
                    # This claim ALSO touches the originally queried entity
                    # directly (a longer path happened to reach it too) —
                    # find_claims_about_entity() already surfaces it at
                    # hop_distance==1; never double-surface it here.
                    seen_claim_ids.add(claim_id)
                    continue
                seen_claim_ids.add(claim_id)
                views.append(
                    KnowledgeClaimView(
                        claim_id=claim["claim_id"], company_id=claim["company_id"], claim_text=claim["claim_text"],
                        claim_type=claim["claim_type"], category=claim["category"], speaker=claim["speaker"],
                        fiscal_year=claim["fiscal_year"], quarter=claim["quarter"],
                        confidence=claim["extraction_confidence"], document_id=claim["document_id"],
                        evidence_quotes=evidence, related_entities=sorted(related), backend="sqlite",
                        hop_distance=hop_distance, path=next_frontier_paths[via_key_by_claim[claim_id]],
                    )
                )

        frontier_paths = next_frontier_paths
        frontier_ids = next_frontier_ids

    return views


def mentioned_entities(
    conn: DBConnection, company_ids: list[str], text: str, *, fact_store: FactStore | None = None
) -> list[tuple[str, str]]:
    """Which already-extracted entities (any type, scoped to these
    companies) this text names — simple case-insensitive substring match
    against knowledge_entities.name. Not a fuzzy match — a real, if narrow,
    connection. Shared by research/investigation_planner.py (per-hypothesis
    entity mentions) and research/knowledge_evidence.py (Q&A/Signals-report
    entity mentions), so both surfaces detect "does this text name a known
    entity" the same way."""
    if not company_ids:
        return []
    fs = fact_store or default_fact_store()
    rows = fs.list_knowledge_entities_for_companies(conn, company_ids)
    text_lower = text.lower()
    return [(r["entity_type"], r["name"]) for r in rows if r["name"] and r["name"].lower() in text_lower]


def _apply_as_of(claims: list[KnowledgeClaimView], as_of: str | None) -> list[KnowledgeClaimView]:
    if not as_of:
        return claims
    from research.temporal import fiscal_year_visible

    return [c for c in claims if fiscal_year_visible(c.fiscal_year, as_of, quarter=c.quarter)]


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
