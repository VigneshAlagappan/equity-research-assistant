"""Vocabulary of the persistent causal graph (Neo4j).

The graph holds nodes, edges and each edge's current knowledge. Evidence,
validation and feedback references and the change history live in Postgres
(Neon) only, keyed by edge id -- nothing about them is stored in Neo4j.

The graph answers: what economic entities exist, how are they related, what
does Signal currently believe about those relationships, and what evidence,
validation and feedback supports that belief. It is NOT a data warehouse:
time series and financial facts stay in Neon, documents in S3/Qdrant.

Kept separate from config/knowledge_ontology.py on purpose. That file governs
the semantic layer (document-extracted entities and claims, not evidence-gated)
and also uses AFFECTS / DRIVES / EXPOSED_TO; the causal layer shares those
relationship names but never reads or writes the semantic edges (every causal
node carries CAUSAL_NODE_LABEL, every causal edge layer == CAUSAL_LAYER).
docs/economic-graph/PLAN.md section 5 and ADR-009 are the reason for the split.

Changing anything here is an ontology change: bump CAUSAL_GRAPH_SCHEMA_VERSION.
"""

from __future__ import annotations

CAUSAL_GRAPH_SCHEMA_VERSION = "cg-1"
CAUSAL_NODE_LABEL = "CausalNode"
CAUSAL_LAYER = "causal"

# --- nodes -------------------------------------------------------------------

NODE_FAMILIES: tuple[str, ...] = (
    "MacroIndicator", "EconomicDriver", "Commodity", "Sector", "Company", "BusinessDriver", "FinancialMetric",
)

#: Type-specific optional properties. Each is a REFERENCE into an existing
#: store, never a copy of its data.
NODE_REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "MacroIndicator": ("series_key",),   # macro_observations.series_key (Neon)
    "FinancialMetric": ("metric_key",),  # canonical_financials.metric_key (Neon)
    "Sector": ("sector_ref",),           # sectors.sector_name
    "Commodity": ("series_key",),        # macro_observations.series_key where a price series exists
    "EconomicDriver": (),
    "BusinessDriver": (),
    "Company": (),                       # id IS the companies.company_id
}

# --- relationships -----------------------------------------------------------

RELATIONSHIP_TYPES: tuple[str, ...] = (
    "AFFECTS", "DRIVES", "INCREASES", "DECREASES", "DEPENDS_ON",
    "SUPPLIES", "CONSUMES", "FINANCES", "EXPOSED_TO", "BELONGS_TO",
)

#: Names an LLM or a person is likely to use, mapped to the canonical type.
#: Anything not here and not canonical is rejected, never stored.
RELATIONSHIP_ALIASES: dict[str, str] = {
    "INFLUENCES": "AFFECTS", "IMPACTS": "AFFECTS", "MAY_AFFECT": "AFFECTS",
    "CAUSES": "DRIVES", "LEADS_TO": "DRIVES", "DRIVEN_BY": "DRIVES",
    "RAISES": "INCREASES", "BOOSTS": "INCREASES",
    "LOWERS": "DECREASES", "REDUCES": "DECREASES",
    "RELIES_ON": "DEPENDS_ON", "REQUIRES": "DEPENDS_ON",
    "PROVIDES": "SUPPLIES", "SELLS_TO": "SUPPLIES",
    "USES": "CONSUMES", "BUYS": "CONSUMES",
    "FUNDS": "FINANCES", "LENDS_TO": "FINANCES",
    "SENSITIVE_TO": "EXPOSED_TO", "VULNERABLE_TO": "EXPOSED_TO",
    "PART_OF": "BELONGS_TO", "MEMBER_OF": "BELONGS_TO", "IN_SECTOR": "BELONGS_TO",
}

#: Which way influence flows relative to the stored edge direction.
#:   FORWARD  source influences target        (A DRIVES B: A -> B)
#:   REVERSE  target influences source        (A DEPENDS_ON B: B -> A)
#:   NONE     structural, not a causal path   (A BELONGS_TO B)
FLOW: dict[str, str] = {
    "AFFECTS": "FORWARD", "DRIVES": "FORWARD", "INCREASES": "FORWARD", "DECREASES": "FORWARD",
    "SUPPLIES": "FORWARD", "FINANCES": "FORWARD",
    "DEPENDS_ON": "REVERSE", "CONSUMES": "REVERSE", "EXPOSED_TO": "REVERSE",
    "BELONGS_TO": "NONE",
}

STRUCTURAL_TYPES: frozenset[str] = frozenset({"BELONGS_TO"})

#: INCREASES / DECREASES fix the direction: the source going up pushes the
#: target up / down.
IMPLIED_DIRECTION: dict[str, str] = {"INCREASES": "POSITIVE", "DECREASES": "NEGATIVE"}

_CAUSAL_SOURCES = frozenset({"MacroIndicator", "EconomicDriver", "Commodity", "Sector", "BusinessDriver", "Company"})
_CAUSAL_TARGETS = frozenset({"EconomicDriver", "Commodity", "Sector", "BusinessDriver", "FinancialMetric", "Company"})
_DRIVER_FAMILIES = frozenset({"MacroIndicator", "EconomicDriver", "Commodity", "BusinessDriver"})

#: relationship type -> (allowed source families, allowed target families)
ENDPOINTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "AFFECTS": (_CAUSAL_SOURCES, _CAUSAL_TARGETS),
    "DRIVES": (_CAUSAL_SOURCES, _CAUSAL_TARGETS),
    "INCREASES": (_CAUSAL_SOURCES, _CAUSAL_TARGETS),
    "DECREASES": (_CAUSAL_SOURCES, _CAUSAL_TARGETS),
    "DEPENDS_ON": (frozenset({"Sector", "Company", "BusinessDriver", "EconomicDriver"}),
                   frozenset({"Commodity", "EconomicDriver", "Sector", "BusinessDriver", "MacroIndicator"})),
    "SUPPLIES": (frozenset({"Commodity", "Sector", "Company"}), frozenset({"Sector", "Company", "BusinessDriver"})),
    "CONSUMES": (frozenset({"Sector", "Company", "BusinessDriver"}), frozenset({"Commodity", "EconomicDriver"})),
    "FINANCES": (frozenset({"Sector", "Company"}), frozenset({"Sector", "Company", "BusinessDriver"})),
    "EXPOSED_TO": (frozenset({"Sector", "Company"}), _DRIVER_FAMILIES),
    "BELONGS_TO": (frozenset({"Company", "Sector"}), frozenset({"Sector"})),
}

# --- edge knowledge ----------------------------------------------------------

DIRECTIONS: tuple[str, ...] = ("POSITIVE", "NEGATIVE", "NON_MONOTONIC", "UNKNOWN")
STRUCTURAL_DIRECTION = "NONE"

EFFECT_STRENGTHS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")
LAG_UNITS: tuple[str, ...] = ("days", "weeks", "months", "quarters", "years")

#: Scope keys an edge may carry. One edge, optional scope: contexts are not
#: copied into one edge per context.
SCOPE_KEYS: tuple[str, ...] = ("geography", "sector", "sub_sector", "company_id", "regime", "period")

# --- lifecycle ---------------------------------------------------------------

STATUSES: tuple[str, ...] = (
    "CANDIDATE", "OBSERVED", "EVIDENCE_BACKED", "VALIDATED", "PROMOTED", "DEPRECATED",
)

STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "CANDIDATE": frozenset({"OBSERVED", "EVIDENCE_BACKED", "DEPRECATED"}),
    "OBSERVED": frozenset({"CANDIDATE", "EVIDENCE_BACKED", "DEPRECATED"}),
    "EVIDENCE_BACKED": frozenset({"OBSERVED", "VALIDATED", "DEPRECATED"}),
    "VALIDATED": frozenset({"EVIDENCE_BACKED", "PROMOTED", "DEPRECATED"}),
    "PROMOTED": frozenset({"VALIDATED", "DEPRECATED"}),
    "DEPRECATED": frozenset({"CANDIDATE"}),
}

ACTORS: tuple[str, ...] = ("seed", "human", "system", "validation", "llm")

#: What an actor may set a status to. An LLM can only ever propose; reaching
#: EVIDENCE_BACKED, VALIDATED or PROMOTED goes through evidence, validation
#: or a human.
ACTOR_ALLOWED_TARGETS: dict[str, frozenset[str]] = {
    "llm": frozenset({"CANDIDATE", "OBSERVED"}),
    "seed": frozenset({"CANDIDATE", "OBSERVED"}),
    "system": frozenset({"CANDIDATE", "OBSERVED", "EVIDENCE_BACKED", "DEPRECATED"}),
    "validation": frozenset({"CANDIDATE", "OBSERVED", "EVIDENCE_BACKED", "VALIDATED", "DEPRECATED"}),
    "human": frozenset(STATUSES),
}
#: Only these may mint an edge at a status above CANDIDATE/OBSERVED: nobody.
CREATE_STATUSES: frozenset[str] = frozenset({"CANDIDATE", "OBSERVED"})
#: Only a human may promote.
PROMOTION_ACTORS: frozenset[str] = frozenset({"human"})

# --- provenance, references --------------------------------------------------

PROVENANCE_TYPES: tuple[str, ...] = (
    "MANUAL_SEED", "INVESTIGATION", "DATA_ANALYSIS", "DOCUMENT_EXTRACTION", "VALIDATED_EVIDENCE",
)

EVIDENCE_REF_TYPES: tuple[str, ...] = ("S3", "NEON_OBSERVATION", "QDRANT_CHUNK", "INVESTIGATION")
EVIDENCE_STANCES: tuple[str, ...] = ("SUPPORTS", "CONTRADICTS")
VALIDATION_RESULTS: tuple[str, ...] = ("SUPPORT", "PARTIAL", "CONTRADICT", "INCONCLUSIVE")
FEEDBACK_TARGET_KINDS: tuple[str, ...] = ("edge", "path", "hypothesis", "investigation")

MAX_TRAVERSAL_DEPTH = 6
MAX_TRAVERSAL_NODES = 200
MAX_TRAVERSAL_EDGES = 500
MAX_NOTE_CHARS = 300
