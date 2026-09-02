"""Hypothesis Generator (Step 2E) — given an observation/question, produces
several plausible COMPETING explanations spanning different perspectives
(financial/operational/competitive/strategic/management/regulatory/macro/
industry), before any evidence is gathered for or against them.

Deliberately ordered before evidence-gathering (Step 2F), not after —
entertaining multiple explanations before looking at evidence is what
keeps this from just rationalizing whatever the model would have said
anyway. Grounded in cheap, already-available context (company sector/
industry, and entity names research/knowledge_builder.py has already
extracted for these companies — "existing graph relationships" and "the
ontology," per the spec) rather than a full evidence-retrieval pass, which
hasn't run yet at this point in the pipeline.

Does not decide whether any hypothesis is true — that's Step 2G's job.
"""

from __future__ import annotations

import json
import re
from storage.db_types import DBConnection
from dataclasses import dataclass, field

from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from storage.fact_store import FactStore, default_fact_store

HYPOTHESIS_CATEGORIES: frozenset[str] = frozenset({
    "financial", "operational", "competitive", "strategic",
    "management", "regulatory", "macro", "industry",
})

MAX_HYPOTHESES = 6
MAX_TOKENS = 3072

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class HypothesisGenerationError(Exception):
    """Raised when hypothesis generation can't complete — the LLM call
    failed, or its response didn't parse into the expected shape."""


@dataclass
class Hypothesis:
    hypothesis_id: str
    investigation_id: str
    statement: str
    mechanism: str
    category: str
    companies: list[str]
    rationale: str
    known_relationships: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    generation_order: int = 0


HYPOTHESIS_GENERATOR_SYSTEM_PROMPT = """You generate multiple plausible, COMPETING explanations for a research \
observation or question about one or more companies — never a single "obvious" answer. Do not assume the first \
explanation that comes to mind is correct; the point of this step is to entertain real alternatives before any \
evidence is gathered for or against them.

Cover different perspectives where genuinely applicable — financial, operational, competitive, strategic, \
management, regulatory, macro, industry — but only propose a hypothesis that's a real, distinct possible \
explanation, not padding just to cover every category.

Respond with ONLY a JSON array, no other text, in exactly this shape:

[
  {{
    "statement": "<one clear sentence stating the hypothesis>",
    "mechanism": "<how this would actually work — the causal chain, one or two sentences>",
    "category": "<one of: {categories}>",
    "rationale": "<why this is plausible given what's already known — one or two sentences>",
    "known_relationships": ["<any already-known fact/relationship this hypothesis draws on, if any>"],
    "unknowns": ["<what would need to be true, or what isn't known yet, for this hypothesis to hold>"]
  }}
]

Propose {max_hypotheses} hypotheses at most — genuinely distinct explanations, not variations on the same idea."""


def _build_system_prompt() -> str:
    return HYPOTHESIS_GENERATOR_SYSTEM_PROMPT.format(
        categories=", ".join(sorted(HYPOTHESIS_CATEGORIES)), max_hypotheses=MAX_HYPOTHESES
    )


def _company_context(conn: DBConnection, company_ids: list[str], fact_store: FactStore) -> str:
    lines = []
    for company_id in company_ids:
        company = fact_store.get_company(conn, company_id)
        if company is None:
            continue
        descriptors = [d for d in (company["sector"], company["industry"]) if d]
        lines.append(f"{company_id} ({company['display_name']}" + (f", {', '.join(descriptors)}" if descriptors else "") + ")")

        entities = fact_store.list_knowledge_entities_for_companies(
            conn, [company_id], entity_types=("Risk", "Opportunity", "Strategy", "Product"), limit=15,
        )
        if entities:
            lines.append("  Already-known entities: " + ", ".join(f"{e['entity_type']}: {e['name']}" for e in entities))
    return "\n".join(lines) if lines else "No prior context available for these companies."


def _parse_response(text: str) -> list[dict]:
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        raise HypothesisGenerationError(f"model response contained no JSON array: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise HypothesisGenerationError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise HypothesisGenerationError(f"model response wasn't a JSON array: {text[:200]!r}")
    return parsed


def generate_hypotheses(
    conn: DBConnection, investigation_id: str, question: str, company_ids: list[str], *, model: str | None = None,
    fact_store: FactStore | None = None,
) -> list[Hypothesis]:
    """Generate competing hypotheses for `question`. Raises
    HypothesisGenerationError if the LLM call fails or its response doesn't
    parse — the caller (research/investigation.py) decides what that means
    for the investigation as a whole; this function never returns a
    fabricated hypothesis to paper over a failure."""
    fs = fact_store or default_fact_store()
    context = _company_context(conn, company_ids, fs)
    hardness = fixed(Tier.DEEP, "hypothesis generation")
    # No DEFAULT_ANTHROPIC_MODEL fallback — see research/knowledge_builder.py's
    # identical comment; leaving this unset lets llm/router.py respect
    # TIER_PREFERRED_MODEL[hardness.tier] (the operator's actual configured
    # policy) instead of silently pinning to sonnet on every call.
    pinned_model = model or ANTHROPIC_MODEL
    user_message = f"Company context:\n{context}\n\nQuestion/observation: {question}"

    try:
        result = route(
            system=_build_system_prompt(), user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError as exc:
        raise HypothesisGenerationError(f"all configured models failed: {exc}") from exc

    observability.record(conn, task_name="hypothesis_generation", company_ids=company_ids, question=question, result=result)

    response = result.response
    if response.stop_reason == "refusal" or not response.text:
        raise HypothesisGenerationError(f"model returned no usable response (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        raise HypothesisGenerationError(f"model response was truncated at the {MAX_TOKENS}-token limit before finishing")

    raw_hypotheses = _parse_response(response.text)
    hypotheses: list[Hypothesis] = []
    for order, raw in enumerate(raw_hypotheses[:MAX_HYPOTHESES]):
        category = raw.get("category")
        statement = (raw.get("statement") or "").strip()
        if category not in HYPOTHESIS_CATEGORIES or not statement:
            continue  # a hallucinated category or empty statement is dropped, not stored as-is
        hypotheses.append(
            Hypothesis(
                hypothesis_id=f"{investigation_id}-h{order + 1}", investigation_id=investigation_id,
                statement=statement, mechanism=(raw.get("mechanism") or "").strip(), category=category,
                companies=company_ids, rationale=(raw.get("rationale") or "").strip(),
                known_relationships=[str(r) for r in (raw.get("known_relationships") or [])],
                unknowns=[str(u) for u in (raw.get("unknowns") or [])], generation_order=order,
            )
        )

    if not hypotheses:
        raise HypothesisGenerationError("model produced no usable hypotheses")
    return hypotheses
