"""Small, stable knowledge ontology (Step 2C) — the fixed vocabulary
research/knowledge_builder.py's extraction (Step 2A) validates against, and
the map of where a canonical value for each concept actually lives, for
whatever eventually needs to route a question to the right subsystem
(Step 2F's Investigation Planner, not built yet).

Deliberately minimal: three extraction vocabularies (entity/relationship/
claim types), a small structural-node list, and a canonical-home map — not
a full taxonomy or admin UI, which would be scope creep past what 2A/2B
actually require. Extend this file, not knowledge_builder.py or
context/knowledge_graph.py themselves, when a new type is genuinely needed.

This is schema/vocabulary only, never actual company data — same
"ontology defines the shape, canonical values live in their own tables"
split config/knowledge_graph_seed.py's own docstring already draws for the
sector-peer knowledge graph (context/graph.py).

ENTITY_TYPES vs. STRUCTURAL_NODE_TYPES: two different kinds of "thing" in
this ontology, not one flat list, despite the original spec sketching them
together. ENTITY_TYPES are *extractable content* — what
research/knowledge_builder.py's LLM call is allowed to name in a document
(a Product, a Risk, a Person, ...). STRUCTURAL_NODE_TYPES are the graph's
own scaffolding — a Claim, its Evidence, the Document it came from, a
TimePeriod it's valid during — created directly by code from SQL rows
(knowledge_claims, knowledge_evidence, documents), never something the
model is asked to invent by name. Validating extraction output against
STRUCTURAL_NODE_TYPES would be a category error: nothing should ever ask
the model to "extract a Claim entity."
"""

from __future__ import annotations

ENTITY_TYPES: frozenset[str] = frozenset({
    "Company", "ManagementPerson", "Product", "Segment", "Industry", "Strategy",
    "Risk", "Opportunity", "Metric", "MacroFactor", "Regulation",
})

#: The graph's own scaffolding node types (context/graph_neo4j.py's
#: sync_knowledge_graph(), Step 2B) — never extracted by name, always
#: derived directly from a SQL row. Listed here so the full node vocabulary
#: is discoverable in one place, not because anything validates against it
#: the way ENTITY_TYPES is validated against.
STRUCTURAL_NODE_TYPES: frozenset[str] = frozenset({"Claim", "Evidence", "Document", "TimePeriod"})

RELATIONSHIP_TYPES: frozenset[str] = frozenset({
    "OFFERS", "OPERATES_IN", "COMPETES_WITH", "SUPPLIES", "DEPENDS_ON",
    "STATES", "ABOUT", "SUPPORTED_BY", "CONTRADICTED_BY", "VALID_DURING",
    "MAY_AFFECT", "DRIVES", "EXPOSED_TO",
})

#: Shared with Step 2G's hypothesis evaluation (not built yet) — defining
#: the full vocabulary now, rather than only the subset 2A's document
#: extraction actually produces (mostly FACT/MANAGEMENT_OPINION/PREDICTION),
#: avoids a later migration when 2G needs CORRELATION/CAUSATION too.
CLAIM_TYPES: frozenset[str] = frozenset({
    "FACT", "CALCULATION", "MANAGEMENT_OPINION", "PREDICTION",
    "INFERENCE", "CORRELATION", "CAUSATION",
})

#: Where the canonical value for each concept actually lives — not a code
#: path anything dispatches on today, but a single, explicit answer for
#: "if I need the real number/text for X, where do I go" rather than that
#: knowledge living only as scattered comments across several modules.
#: Step 2F's Investigation Planner (not built) is the natural future
#: consumer: routing "what evidence does this hypothesis need" to the right
#: subsystem is exactly this table, made executable.
CANONICAL_HOME: dict[str, str] = {
    "Metric historical value": "canonical_financials (financials/, retrieval/structured_search.py)",
    "Macro/regulatory historical series": "macro_observations (research/macro_evidence.py)",
    "Management claim": "knowledge_claims (research/knowledge_builder.py, Step 2A)",
    "Claim relationship/history": "knowledge_relationships + the Neo4j-backed graph "
                                   "(context/knowledge_graph.py, Step 2B)",
    "Document passage (exact text)": "document_chunks / document_chunks_fts (research/document_chunker.py, Step 2D)",
    "Document passage (whole-document, ungraded)": "documents.raw_file_path / source_url (research/documents.py)",
    "Sector-peer prior investigation": "generated_reports + the Neo4j-backed graph (context/graph.py, context/graph_neo4j.py)",
    "Live market quote": "web/live_quote.py (display-only, never feeds valuation math)",
}


def is_valid_entity_type(entity_type: str) -> bool:
    return entity_type in ENTITY_TYPES


def is_valid_relationship_type(relationship_type: str) -> bool:
    return relationship_type in RELATIONSHIP_TYPES


def is_valid_claim_type(claim_type: str) -> bool:
    return claim_type in CLAIM_TYPES
