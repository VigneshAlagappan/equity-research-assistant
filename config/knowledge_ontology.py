"""Small, stable knowledge ontology (Step 2C) — the fixed vocabulary
research/knowledge_builder.py's extraction (Step 2A) validates against.
Deliberately minimal: just the three vocabularies 2A needs today
(entity/relationship/claim types), not a full taxonomy or admin UI — those
would be scope creep past what 2A actually requires. Extend this file, not
knowledge_builder.py itself, when a new type is genuinely needed.

This is schema/vocabulary only, never actual company data — same
"ontology defines the shape, canonical values live in their own tables"
split config/knowledge_graph_seed.py's own docstring already draws for the
sector-peer knowledge graph (context/graph.py).

Where canonical values for each concept actually live, once extracted:
  Metric historical value      -> canonical_financials (financials/)
  Macro historical series      -> macro_observations
  Management claim             -> knowledge_claims (this module's ENTITY/
                                   CLAIM types describe its shape)
  Document passage             -> documents / research/documents.py
  Relationship history         -> knowledge_relationships (Step 2A) /
                                   the Neo4j-backed graph (Step 2B, not built)
"""

from __future__ import annotations

ENTITY_TYPES: frozenset[str] = frozenset({
    "Company", "ManagementPerson", "Product", "Segment", "Industry", "Strategy",
    "Risk", "Opportunity", "Metric", "MacroFactor", "Regulation",
})

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
