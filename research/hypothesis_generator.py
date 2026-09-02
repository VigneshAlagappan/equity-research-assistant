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
from research.capabilities import PlannerCapabilities
from storage.fact_store import FactStore, default_fact_store

HYPOTHESIS_CATEGORIES: frozenset[str] = frozenset({
    "financial", "operational", "competitive", "strategic",
    "management", "regulatory", "macro", "industry",
})

MAX_HYPOTHESES = 6
#: 6 hypotheses x {statement, mechanism, category, rationale,
#: known_relationships[], unknowns[]} is a genuinely large JSON object, and
#: 3072 was not enough for it: real golden-loop runs came in at 2155 and 2221
#: output tokens (so within a rounding error of the old cap), and a
#: multi-clause question ("could Signal have detected deterioration before it
#: became obvious? identify which indicators changed and evaluate competing
#: explanations") truncated outright and failed the whole investigation —
#: generation failing is the one failure run_investigation() cannot degrade
#: past, since there is then nothing to investigate. Same headroom, for the
#: same measured reason, that research/hypothesis_evaluator.py's MAX_TOKENS
#: comment already documents for the evaluation call.
MAX_TOKENS = 8192

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


#: Bounds how many triggered indicators one company contributes to the
#: hypothesis-generation prompt — they arrive most-severe-first, so a
#: company with many findings still leads with the ones worth explaining.
_MAX_INDICATORS_PER_COMPANY = 6


def _company_context(
    conn: DBConnection, company_ids: list[str], fact_store: FactStore,
    capabilities: PlannerCapabilities | None = None,
) -> str:
    """Cheap, already-available grounding for Step 2E: what each company IS
    (sector/industry), what the knowledge graph already knows about it, and —
    when an indicator capability is supplied — which deterministic indicators
    are currently TRIGGERED for it.

    The last of those is the spec's own starting point ("Trusted Facts /
    Indicators / Question -> hypotheses"): a rule that has already fired,
    deterministically and with provenance, is precisely the observation a
    competing-explanation step should be reasoning about. It is context only,
    never a conclusion — every indicator still has to be re-grounded against
    retrieved evidence in Steps 2F/2G, exactly like the knowledge-graph
    entities alongside it."""
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

        if capabilities is not None:
            indicators = capabilities.indicator_evidence(conn, company_id)[:_MAX_INDICATORS_PER_COMPANY]
            for item in indicators:
                lines.append(f"  Triggered indicator — {item.label}: {item.value}")
    return "\n".join(lines) if lines else "No prior context available for these companies."


def _parse_response(text: str) -> list[dict]:
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        raise HypothesisGenerationError(f"model response contained no JSON array: {text[:200]!r}")
    try:
        # strict=False tolerates literal control characters (raw newlines/tabs)
        # inside quoted strings — the same robustness fix
        # research/knowledge_builder.py's _parse_response already documents,
        # applied here after a real golden-loop run failed an entire
        # investigation on "Invalid control character at line 20 column 361"
        # from an unescaped newline inside one "mechanism" field. Structural
        # validation is unchanged: a non-array response, a hallucinated
        # category and an empty statement are all still rejected below.
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError as exc:
        raise HypothesisGenerationError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise HypothesisGenerationError(f"model response wasn't a JSON array: {text[:200]!r}")
    return parsed


def generate_hypotheses(
    conn: DBConnection, investigation_id: str, question: str, company_ids: list[str], *, model: str | None = None,
    fact_store: FactStore | None = None, capabilities: PlannerCapabilities | None = None,
) -> list[Hypothesis]:
    """Generate competing hypotheses for `question`. Raises
    HypothesisGenerationError if the LLM call fails or its response doesn't
    parse — the caller (research/investigation.py) decides what that means
    for the investigation as a whole; this function never returns a
    fabricated hypothesis to paper over a failure.

    `capabilities` is optional and used only for context: when supplied, each
    company's currently-triggered deterministic indicators are surfaced
    alongside its sector and known entities (see _company_context). Left out,
    generation behaves exactly as before — this step never *depends* on an
    indicator having fired."""
    fs = fact_store or default_fact_store()
    context = _company_context(conn, company_ids, fs, capabilities)
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
