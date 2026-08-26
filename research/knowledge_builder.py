"""Knowledge Builder (Step 2A) — extracts structured research knowledge
(entities, claims, relationships) from one processed document, with full
provenance, into knowledge_entities/knowledge_claims/knowledge_relationships/
knowledge_evidence (schemas/sqlite_schema.sql). Plain SQL storage only — no
Neo4j at this stage (Step 2B, a separate later step this module doesn't
touch). Validated against config/knowledge_ontology.py's fixed vocabulary
(Step 2C) rather than trusting whatever the model names.

Wired into ingestion/coordinator.py::process_documents(): "processing" a
document (Step 1's action) is now also the trigger for this extraction to
run, not a bare status flip.

Every extraction is additive — a new document's claims are always fresh
INSERTs (storage/repositories.py::insert_knowledge_claim), never an UPDATE
to a prior document's claims, same "never overwrite" discipline
financial_observations already follows. Entities ARE deduped/shared across
documents (get_or_create_knowledge_entity) — the same "Product: iPhone"
entity shouldn't get a new row every time a new document mentions it.

No chunking yet (Step 2D, not built) — a document's text is capped at
MAX_CHARS_FOR_EXTRACTION before being sent to the model, the same
"don't build a full chunking pipeline yet" tradeoff research/documents.py's
own MAX_CHARS_PER_DOCUMENT already makes for evidence rendering.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from config.knowledge_ontology import CLAIM_TYPES, ENTITY_TYPES, RELATIONSHIP_TYPES
from config.settings import ANTHROPIC_MODEL, DEFAULT_ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.documents import document_text
from storage.repositories import (
    get_or_create_knowledge_entity,
    insert_knowledge_claim,
    insert_knowledge_evidence,
    insert_knowledge_relationship,
)

MAX_CHARS_FOR_EXTRACTION = 40_000
MAX_CLAIMS_PER_DOCUMENT = 20
# Up to 20 claims, each with a quote and relationships, plus ~30+ entities
# for a dense real document, comfortably exceeds 4096 output tokens and
# gets silently truncated mid-JSON (stop_reason="max_tokens") — matches
# research/signals_report.py's own full-report ceiling.
MAX_TOKENS = 8192

_COMPANY_PLACEHOLDER = "COMPANY"

KNOWLEDGE_BUILDER_SYSTEM_PROMPT = """You extract structured research knowledge from one company document (annual \
report, earnings transcript, investor presentation, or similar) — entities, claims, and the relationships between \
them — strictly grounded in the text given to you. Never use outside/training knowledge about this company; only \
extract what the text actually says.

Respond with ONLY a JSON object, no other text, in exactly this shape:

{{
  "entities": [
    {{"type": "<one of: {entity_types}>", "name": "<short name>"}}
  ],
  "claims": [
    {{
      "text": "<one clear sentence stating the claim>",
      "claim_type": "<one of: {claim_types}>",
      "category": "<one of: strategy, guidance, risk, opportunity, fact, competitive, regulatory, other>",
      "speaker": "<who said it, e.g. 'CEO', 'CFO', or null if not attributable>",
      "confidence": <0.0-1.0, how clearly/directly the text states this>,
      "quote": "<the exact or near-exact supporting sentence from the source text>",
      "relationships": [
        {{"relationship_type": "<one of: {relationship_types}>", "source_entity": "COMPANY or an entity name from the entities list above", "target_entity": "an entity name from the entities list above"}}
      ]
    }}
  ]
}}

Rules:
- claim_type "FACT" is only for something the document states as a reported fact, not management's opinion about \
it — an executive's stated belief/plan/outlook is MANAGEMENT_OPINION or PREDICTION, never FACT.
- Never invent a claim the text doesn't support. If the document has nothing worth extracting, return \
{{"entities": [], "claims": []}}.
- "{company_placeholder}" in a relationship's source_entity means this document's own company — use it rather \
than guessing the company's exact name.
- Extract at most {max_claims} claims — the most material ones (strategy shifts, guidance, risks, competitive \
dynamics, notable facts), not every sentence."""

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class KnowledgeExtractionError(Exception):
    """Raised when extraction can't complete — the LLM call failed, or its
    response didn't parse into the expected shape. Caller (the Ingest
    queue's coordinator) turns this into a FAILED, retryable document
    rather than silently marking it processed with nothing extracted."""


@dataclass
class KnowledgeExtractionResult:
    claims_created: int = 0
    entities_created: int = 0
    relationships_created: int = 0
    skipped_relationships: list[str] = field(default_factory=list)


def _build_system_prompt() -> str:
    return KNOWLEDGE_BUILDER_SYSTEM_PROMPT.format(
        entity_types=", ".join(sorted(ENTITY_TYPES)),
        claim_types=", ".join(sorted(CLAIM_TYPES)),
        relationship_types=", ".join(sorted(RELATIONSHIP_TYPES)),
        company_placeholder=_COMPANY_PLACEHOLDER,
        max_claims=MAX_CLAIMS_PER_DOCUMENT,
    )


def _parse_response(text: str) -> dict:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        raise KnowledgeExtractionError(f"model response contained no JSON object: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise KnowledgeExtractionError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, dict) or "claims" not in parsed:
        raise KnowledgeExtractionError(f"model response missing a top-level 'claims' list: {text[:200]!r}")
    return parsed


def _persist(
    conn: sqlite3.Connection, *, document_id: int, company_id: str | None,
    fiscal_year: str | None, quarter: str | None, parsed: dict,
) -> KnowledgeExtractionResult:
    result = KnowledgeExtractionResult()

    # COMPANY is resolved once per document to a real Company-type entity —
    # auto-created if this is the first extraction ever to reference it.
    company_entity = None
    if company_id is not None:
        company_entity = get_or_create_knowledge_entity(conn, "Company", company_id, company_id)

    named_entities: dict[str, sqlite3.Row] = {}
    for raw_entity in parsed.get("entities") or []:
        entity_type = raw_entity.get("type")
        name = (raw_entity.get("name") or "").strip()
        if entity_type not in ENTITY_TYPES or not name:
            continue  # not trusted blindly — validated against config/knowledge_ontology.py
        row = get_or_create_knowledge_entity(conn, entity_type, name, company_id)
        named_entities[name] = row
        result.entities_created += 1

    def _resolve_entity(name: str) -> sqlite3.Row | None:
        if name == _COMPANY_PLACEHOLDER:
            return company_entity
        return named_entities.get(name)

    for raw_claim in (parsed.get("claims") or [])[:MAX_CLAIMS_PER_DOCUMENT]:
        claim_type = raw_claim.get("claim_type")
        claim_text = (raw_claim.get("text") or "").strip()
        if claim_type not in CLAIM_TYPES or not claim_text:
            continue  # a hallucinated claim_type or empty claim is dropped, not stored as-is

        confidence = raw_claim.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        claim = insert_knowledge_claim(
            conn, document_id=document_id, company_id=company_id, claim_type=claim_type,
            category=raw_claim.get("category"), claim_text=claim_text,
            speaker=raw_claim.get("speaker") or None, fiscal_year=fiscal_year, quarter=quarter,
            extraction_confidence=confidence,
        )
        result.claims_created += 1

        quote = raw_claim.get("quote")
        if quote:
            insert_knowledge_evidence(conn, claim_id=claim["claim_id"], document_id=document_id, quote=quote)

        for raw_rel in raw_claim.get("relationships") or []:
            relationship_type = raw_rel.get("relationship_type")
            source = _resolve_entity(raw_rel.get("source_entity", ""))
            target = _resolve_entity(raw_rel.get("target_entity", ""))
            if relationship_type not in RELATIONSHIP_TYPES or source is None or target is None:
                result.skipped_relationships.append(str(raw_rel))
                continue
            insert_knowledge_relationship(
                conn, claim_id=claim["claim_id"], source_entity_id=source["entity_id"],
                relationship_type=relationship_type, target_entity_id=target["entity_id"],
            )
            result.relationships_created += 1

    return result


def extract_document_knowledge(
    conn: sqlite3.Connection, document_row: sqlite3.Row, *, model: str | None = None
) -> KnowledgeExtractionResult:
    """Extract and persist structured knowledge from one document row
    (the same shape `documents` table rows have). Returns an empty result
    (zero cost, no LLM call) if the document has no extractable text —
    same "absence isn't an error" rule research/documents.py's own
    get_document_evidence() already follows for a non-PDF/unfetchable link.
    Raises KnowledgeExtractionError if there IS text but extraction itself
    fails (LLM unavailable, unparseable response) — the caller decides what
    that means for the document's processing_status.
    """
    text = document_text(document_row)
    if text is None:
        return KnowledgeExtractionResult()

    hardness = fixed(Tier.STANDARD, "document knowledge extraction")
    pinned_model = model or ANTHROPIC_MODEL or DEFAULT_ANTHROPIC_MODEL
    user_message = f"Document text:\n{text[:MAX_CHARS_FOR_EXTRACTION]}"

    try:
        result = route(
            system=_build_system_prompt(), user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError as exc:
        raise KnowledgeExtractionError(f"all configured models failed: {exc}") from exc

    observability.record(
        conn, task_name="knowledge_extraction", company_ids=[document_row["company_id"]] if document_row["company_id"] else [],
        question=None, result=result,
    )

    response = result.response
    if response.stop_reason == "refusal" or not response.text:
        raise KnowledgeExtractionError(f"model returned no usable response (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        # The response was cut off mid-JSON before it could close its
        # brackets — _parse_response would just report a confusing "not
        # valid JSON" error; this is a more actionable diagnosis (the
        # document is unusually dense, or MAX_TOKENS needs raising again).
        raise KnowledgeExtractionError(
            f"model response was truncated at the {MAX_TOKENS}-token limit before finishing — "
            "this document may be too dense to extract in one pass (Step 2D's chunking isn't built yet)"
        )

    parsed = _parse_response(response.text)
    return _persist(
        conn, document_id=document_row["document_id"], company_id=document_row["company_id"],
        fiscal_year=document_row["fiscal_year"], quarter=document_row["quarter"], parsed=parsed,
    )
