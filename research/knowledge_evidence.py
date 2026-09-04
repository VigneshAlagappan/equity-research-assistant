"""Research Knowledge Graph claims as Q&A/Signals-report Evidence — the
piece connecting `context/knowledge_graph.py` (Step 2B) to the two normal
research surfaces (`research/assistant.py`'s Q&A, `research/
signals_report.py`'s Signals reports), the same way `research/
indicator_evidence.py` connects the Configurable Indicator Framework to the
investigation pipeline. Before this module, a cross-company claim
connection could only ever surface inside a structured investigation
(`research/investigation_planner.py`) — a normal question never saw it,
even when a known entity (e.g. a named risk, product, or regulator) it
mentions has real extracted claims attached.

Single-company only, same constraint `research/documents.py`'s own evidence
path already has — entity mentions are detected against one company's own
extracted entities (`context/knowledge_graph.py::mentioned_entities()`),
the same lightweight substring approach `research/investigation_planner.py`
uses for a hypothesis's own text.

Each `KnowledgeClaimView` is folded into a plain `Evidence` line rather than
kept as its own separate block (unlike `context/graph.py`'s "Related prior
investigations," which is a genuinely different kind of thing — a different
company's reasoning, never evidence about this question's companies) —
these claims ARE evidence about this question's own company, extracted from
its own documents (Step 2A), so they belong in the same Evidence list that
already flows through `context/optimizer.py`'s dedup/scoring/budgeting and
`llm/providers/anthropic_provider.py`'s prompt-caching split, not a
bypass-everything third block. `_CLAIM_KIND_MAP` narrows the ontology's
7-value `CLAIM_TYPES` down to `research/evidence.py`'s 4-value
`EVIDENCE_KINDS` the existing Q&A/Signals system prompts already know how to
cite — MANAGEMENT_OPINION reads as a management statement,
PREDICTION/CORRELATION/CAUSATION all read as INFERENCE (none of the three
is a confirmed fact or a deterministic computation, exactly the boundary
INFERENCE already exists to mark).
"""

from __future__ import annotations

from storage.db_types import DBConnection

from config.knowledge_ontology import CLAIM_TYPES
from context.knowledge_graph import KnowledgeClaimView, find_claims_about_entity, mentioned_entities
from research.evidence import Evidence
from storage.fact_store import FactStore, default_fact_store

#: config.knowledge_ontology.CLAIM_TYPES -> research.evidence.EVIDENCE_KINDS.
#: Every CLAIM_TYPES value must appear here — asserted at import time so a
#: new claim type added to the ontology can't silently fall through and
#: crash Evidence's own kind validation deep inside a live request instead.
_CLAIM_KIND_MAP: dict[str, str] = {
    "FACT": "FACT",
    "CALCULATION": "CALCULATION",
    "MANAGEMENT_OPINION": "MANAGEMENT_STATEMENT",
    "PREDICTION": "INFERENCE",
    "INFERENCE": "INFERENCE",
    "CORRELATION": "INFERENCE",
    "CAUSATION": "INFERENCE",
}
assert set(_CLAIM_KIND_MAP) == set(CLAIM_TYPES), "every CLAIM_TYPES value must map to an EVIDENCE_KINDS value"

#: Mirrors research/investigation_planner.py's own cap on how many mentioned
#: entities are worth querying per question — a question naming many known
#: entities at once is unusual, and each one is its own knowledge-graph query.
_MAX_ENTITIES = 5
#: Mirrors research/investigation_planner.py's per-hypothesis cap — bounds
#: prompt size before context/optimizer.py's own budgeting even runs.
_MAX_CLAIMS = 8
_MAX_QUOTE_CHARS = 240


def _claim_to_evidence(claim: KnowledgeClaimView) -> Evidence:
    period = f"{claim.quarter} {claim.fiscal_year}" if claim.quarter else (claim.fiscal_year or "period unknown")
    citation_parts = [f"knowledge graph, document {claim.document_id}", period]
    if claim.speaker:
        citation_parts.append(f"speaker={claim.speaker}")
    if claim.confidence is not None:
        citation_parts.append(f"confidence={claim.confidence}")
    value = claim.claim_text
    if claim.evidence_quotes:
        value += f' (quote: "{claim.evidence_quotes[0][:_MAX_QUOTE_CHARS]}")'
    return Evidence(
        kind=_CLAIM_KIND_MAP[claim.claim_type],
        company_id=claim.company_id or "UNKNOWN",
        label=f"Knowledge graph claim ({claim.category or claim.claim_type})",
        value=value,
        citation=" · ".join(citation_parts),
    )


def get_knowledge_graph_evidence(
    conn: DBConnection, company_id: str, question: str, *, fact_store: FactStore | None = None,
    as_of: str | None = None,
) -> list[Evidence]:
    """Every claim connected to this company's own `Company` node, plus
    every claim connected to any known entity (any type) `question`
    mentions by name — capped and deduped by `claim_id`, since the same
    claim can be reachable through more than one entity match. Returns []
    for a company with no extracted entities/claims yet, same "absence isn't
    an error" rule the rest of research/ already follows."""
    fs = fact_store or default_fact_store()
    claims: list[KnowledgeClaimView] = []
    claims.extend(find_claims_about_entity(conn, "Company", company_id, fact_store=fs, as_of=as_of))
    for entity_type, entity_name in mentioned_entities(conn, [company_id], question, fact_store=fs)[:_MAX_ENTITIES]:
        claims.extend(find_claims_about_entity(conn, entity_type, entity_name, fact_store=fs, as_of=as_of))

    seen_claim_ids: set[int] = set()
    deduped: list[KnowledgeClaimView] = []
    for claim in claims:
        if claim.claim_id in seen_claim_ids:
            continue
        seen_claim_ids.add(claim.claim_id)
        deduped.append(claim)

    return [_claim_to_evidence(claim) for claim in deduped[:_MAX_CLAIMS]]
