"""Investigation Planner (Step 2F) — for each hypothesis, determines what
evidence is relevant and retrieves it by routing to the existing
capabilities already built, rather than inventing a second retrieval
mechanism. Routed through research/capabilities.py's PlannerCapabilities
seam (a Protocol per capability), not direct module imports:

  SQL / financial engine   -> retrieval/structured_search.py (quantitative facts)
  Macro engine              -> research/macro_evidence.py (economic/regulatory series)
  Uploaded documents         -> research/documents.py (MANAGEMENT_STATEMENT evidence)
  Vector/document retrieval -> retrieval/document_search.py (keyword-matched passages, Step 2D)
  Knowledge graph            -> context/knowledge_graph.py (relationships/historical claims, Step 2B)

Routing is deterministic, not a fresh LLM call per hypothesis to "decide"
what to fetch — evidence retrieval itself is already cheap here (structured
SQL, FTS5, graph traversal), and macro evidence already has its own
internal LLM-based series selection (research/macro_evidence.py). Which
subsystems get queried varies by hypothesis category (e.g. a "regulatory"
or "macro" hypothesis also queries macro evidence; every hypothesis queries
its companies' own financials, documents, and the knowledge graph).

Answers "what should be investigated next" — never decides whether the
hypothesis is true. That's Step 2G's job entirely, working from exactly the
evidence this module hands it. Called iteratively, not just once per
hypothesis, by research/investigation.py's evidence-sufficiency loop
(Step 2G returning INSUFFICIENT_EVIDENCE triggers another pass here with the
gap it named) — plan_and_gather() itself stays a single deterministic pass;
the looping decision belongs to the Orchestrator, not this module.

A gap-driven retry (`retry=True`) is capability-targeted, not a blind
repeat of the first pass: it skips whichever capabilities can only ever
return what the first pass already got — see plan_and_gather()'s docstring.
"""

from __future__ import annotations

from storage.db_types import DBConnection
from dataclasses import dataclass, field

from context.knowledge_graph import KnowledgeClaimView
from research.capabilities import PlannerCapabilities, default_capabilities
from research.evidence import Evidence
from research.hypothesis_generator import Hypothesis
from retrieval.document_search import DocumentPassage
from storage.fact_store import FactStore, default_fact_store

#: Hypothesis categories where macro/regulatory series are worth pulling in
#: unconditionally — every other category still gets it if the question
#: itself mentions a macro topic (get_macro_evidence's own keyword/LLM
#: planner already handles that), this just widens the net for the
#: categories where it's almost always relevant.
_MACRO_RELEVANT_CATEGORIES = frozenset({"macro", "regulatory"})

_MAX_KNOWLEDGE_GRAPH_ENTITIES = 5
_MAX_DOCUMENT_PASSAGES = 8


@dataclass
class InvestigationPlan:
    hypothesis_id: str
    evidence: list[Evidence] = field(default_factory=list)
    knowledge_claims: list[KnowledgeClaimView] = field(default_factory=list)
    passages: list[DocumentPassage] = field(default_factory=list)
    sources_queried: list[str] = field(default_factory=list)


def _mentioned_entities(
    conn: DBConnection, company_ids: list[str], text: str, fact_store: FactStore
) -> list[tuple[str, str]]:
    """Which already-extracted entities (any type) this hypothesis's own
    text names — simple case-insensitive substring match against
    knowledge_entities.name, the same lightweight approach
    context/graph.py's _metrics_mentioned() already uses for its own
    keyword matching. Not a fuzzy match — a real, if narrow, connection."""
    if not company_ids:
        return []
    rows = fact_store.list_knowledge_entities_for_companies(conn, company_ids)
    text_lower = text.lower()
    return [(r["entity_type"], r["name"]) for r in rows if r["name"] and r["name"].lower() in text_lower]


def plan_and_gather(
    conn: DBConnection, hypothesis: Hypothesis, question: str, *, capabilities: PlannerCapabilities | None = None,
    fact_store: FactStore | None = None, retry: bool = False,
) -> InvestigationPlan:
    """capabilities defaults to the real in-process implementations
    (research/capabilities.py::default_capabilities) — pass a different
    PlannerCapabilities to route one or more of the five capabilities
    elsewhere (a remote service, a test double) without touching the routing
    logic below. fact_store is a separate, lower-level seam
    (storage/fact_store.py) — when capabilities isn't explicitly supplied, it
    also gets threaded into the default capability bindings, so one injected
    FactStore reaches every layer from this single call.

    `retry` marks a gap-driven re-pass (research/investigation.py, after an
    INSUFFICIENT_EVIDENCE verdict) rather than the hypothesis's first pass.
    A retry only ever changes `question` (Step 2G's missing_evidence, not a
    new hypothesis or company set) — so any capability keyed purely off
    (hypothesis, company_id) would return exactly what the first pass
    already retrieved, and is skipped: financial_evidence, indicator_evidence,
    and the per-company "Company" knowledge_graph lookup. Everything actually
    driven by `question`/search_text — document_evidence, entity-mention
    knowledge_graph, macro_evidence, document_search — stays live, since
    that's the only thing a retry can plausibly surface that's new."""
    fs = fact_store or default_fact_store()
    caps = capabilities or default_capabilities(fact_store=fs)
    plan = InvestigationPlan(hypothesis_id=hypothesis.hypothesis_id)

    for company_id in hypothesis.companies:
        if not retry:
            plan.evidence.extend(caps.financial_evidence(conn, company_id))
            plan.sources_queried.append(f"financial_engine:{company_id}")

            # Deterministic, rule-based, versioned findings over the same
            # canonical facts (indicators/, via research/indicator_evidence.py).
            # Retrieved per company like every other evidence source, so a
            # comparison hypothesis sees each company's own triggered indicators
            # and Step 2G can cite the rule rather than re-deriving it.
            indicator_evidence = caps.indicator_evidence(conn, company_id)
            if indicator_evidence:
                plan.evidence.extend(indicator_evidence)
                plan.sources_queried.append(f"indicators:{company_id}")

        if len(hypothesis.companies) == 1:
            # Single-company attribution only, same constraint
            # research/documents.py's own evidence path already has.
            plan.evidence.extend(caps.document_evidence(conn, company_id, question))
            plan.sources_queried.append(f"documents:{company_id}")

        if not retry:
            plan.knowledge_claims.extend(caps.knowledge_graph(conn, "Company", company_id))

    search_text = f"{question} {hypothesis.statement} {hypothesis.mechanism}"
    for entity_type, entity_name in _mentioned_entities(conn, hypothesis.companies, search_text, fs)[:_MAX_KNOWLEDGE_GRAPH_ENTITIES]:
        plan.knowledge_claims.extend(caps.knowledge_graph(conn, entity_type, entity_name))
        plan.sources_queried.append(f"knowledge_graph:{entity_type}:{entity_name}")
    if plan.knowledge_claims:
        plan.sources_queried.append("knowledge_graph")
        # De-dupe — a claim can be reachable via more than one entity match above.
        seen_claim_ids: set[int] = set()
        deduped: list[KnowledgeClaimView] = []
        for claim in plan.knowledge_claims:
            if claim.claim_id in seen_claim_ids:
                continue
            seen_claim_ids.add(claim.claim_id)
            deduped.append(claim)
        plan.knowledge_claims = deduped

    if hypothesis.category in _MACRO_RELEVANT_CATEGORIES:
        macro = caps.macro_evidence(conn, question)
        if macro:
            plan.evidence.extend(macro)
            plan.sources_queried.append("macro_engine")

    for company_id in hypothesis.companies or [None]:
        passages = caps.document_search(conn, search_text, company_id=company_id, limit=_MAX_DOCUMENT_PASSAGES)
        plan.passages.extend(passages)
    if plan.passages:
        plan.sources_queried.append("document_search")
        plan.passages = plan.passages[:_MAX_DOCUMENT_PASSAGES]

    return plan
