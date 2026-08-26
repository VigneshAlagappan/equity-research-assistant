"""Investigation Planner (Step 2F) — for each hypothesis, determines what
evidence is relevant and retrieves it by routing to the existing
capabilities already built, rather than inventing a second retrieval
mechanism:

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
evidence this module hands it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from context.knowledge_graph import KnowledgeClaimView, find_claims_about_entity
from research.documents import get_document_evidence
from research.evidence import Evidence
from research.hypothesis_generator import Hypothesis
from research.macro_evidence import get_macro_evidence
from retrieval.document_search import DocumentPassage, search_documents
from retrieval.structured_search import get_company_evidence

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


def _mentioned_entities(conn: sqlite3.Connection, company_ids: list[str], text: str) -> list[tuple[str, str]]:
    """Which already-extracted entities (any type) this hypothesis's own
    text names — simple case-insensitive substring match against
    knowledge_entities.name, the same lightweight approach
    context/graph.py's _metrics_mentioned() already uses for its own
    keyword matching. Not a fuzzy match — a real, if narrow, connection."""
    if not company_ids:
        return []
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"SELECT DISTINCT entity_type, name FROM knowledge_entities WHERE company_id IN ({placeholders})",
        company_ids,
    ).fetchall()
    text_lower = text.lower()
    return [(r["entity_type"], r["name"]) for r in rows if r["name"] and r["name"].lower() in text_lower]


def plan_and_gather(conn: sqlite3.Connection, hypothesis: Hypothesis, question: str) -> InvestigationPlan:
    plan = InvestigationPlan(hypothesis_id=hypothesis.hypothesis_id)

    for company_id in hypothesis.companies:
        plan.evidence.extend(get_company_evidence(conn, company_id))
        plan.sources_queried.append(f"financial_engine:{company_id}")

        if len(hypothesis.companies) == 1:
            # Single-company attribution only, same constraint
            # research/documents.py's own evidence path already has.
            plan.evidence.extend(get_document_evidence(conn, company_id, question))
            plan.sources_queried.append(f"documents:{company_id}")

        plan.knowledge_claims.extend(find_claims_about_entity(conn, "Company", company_id))

    search_text = f"{question} {hypothesis.statement} {hypothesis.mechanism}"
    for entity_type, entity_name in _mentioned_entities(conn, hypothesis.companies, search_text)[:_MAX_KNOWLEDGE_GRAPH_ENTITIES]:
        plan.knowledge_claims.extend(find_claims_about_entity(conn, entity_type, entity_name))
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
        macro = get_macro_evidence(conn, question)
        if macro:
            plan.evidence.extend(macro)
            plan.sources_queried.append("macro_engine")

    for company_id in hypothesis.companies or [None]:
        passages = search_documents(conn, search_text, company_id=company_id, limit=_MAX_DOCUMENT_PASSAGES)
        plan.passages.extend(passages)
    if plan.passages:
        plan.sources_queried.append("document_search")
        plan.passages = plan.passages[:_MAX_DOCUMENT_PASSAGES]

    return plan
