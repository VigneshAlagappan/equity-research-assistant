"""Jev -- the Signals complexity classifier
(docs/ADR/023-jev-llm-complexity-classification-and-routing.md).

Given a user's research question, decides the MINIMUM complexity level
(1-5) required to answer it correctly, before any retrieval, calculation,
or reasoning begins. research/routing_policy.py reads the result and
dispatches to that level's execution path -- Jev itself never answers the
question, never selects a dataset, and never performs research (this
module's only public entry point returns a classification, nothing else).

This is a deliberate, narrow exception to ADR-006 ("prefer deterministic
code over an LLM wherever a stable rule exists"): unlike a metric name or a
macro series key, "how hard is this question" has no closed vocabulary a
regex can reliably match against -- llm/hardness.py's own keyword classifier
already had to hand-pick a handful of trigger words (compare/why/versus/...)
for its 3-tier version of this same problem, and silently misclassifies any
question that asks for the same thing in different words. See ADR-006's
"Revisit when" section and ADR-023 for the full reasoning and scope of this
exception -- it covers classification only, not the answer itself.

Deliberately NOT built on llm/hardness.py's Tier/HardnessResult (a 3-bucket
stand-in used for MODEL SELECTION on the final answer) or on
research/aggregate_query.py's intent-extraction pattern (maps free text onto
a small closed vocabulary of metric operations) -- this is a first-class
5-level classification in its own right, with its own model chain
(config.settings.JEV_CLASSIFIER_MODEL_CHAIN) run through
llm.router.route_explicit_chain, not llm.router.route()'s tier-derived
chain.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import IntEnum

from config.settings import JEV_CLASSIFIER_MODEL_CHAIN
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, RouteResult, route_explicit_chain

logger = logging.getLogger(__name__)


class ComplexityLevel(IntEnum):
    RETRIEVE = 1
    CALCULATE = 2
    INTERPRET = 3
    COMPARE = 4
    HYPOTHESIZE = 5


_LEVEL_DEFINITIONS = """Level 1 -- Retrieve: retrieve an existing structured fact and return it \
(e.g. "What was HDFC Bank ROE in FY2025?"). No calculation beyond formatting.

Level 2 -- Retrieve + Calculate: retrieve structured data and perform one deterministic \
calculation on it (e.g. "What was HDFC Bank's 5-year profit CAGR?").

Level 3 -- Retrieve + Calculate + Interpret: analyze/interpret a retrieved dataset -- trend, \
consistency, acceleration, slowdown, volatility, unusual periods -- WITHOUT bringing in another \
comparison dataset (e.g. "Analyze HDFC Bank's profit growth over the last five years.").

Level 4 -- Compare + Contextualize: answering requires another dataset -- a benchmark, peer, \
industry measure, or macro series (e.g. "Compare HDFC Bank credit growth with the Indian banking \
industry over the last five years.").

Level 5 -- Hypothesis / Causal Deep Research: requires hypothesis generation, causal reasoning, \
competing explanations, or a full research investigation (e.g. "Why has HDFC Bank's credit growth \
diverged from the banking system, and what factors are driving it?")."""

SYSTEM_PROMPT = f"""You are Jev, Signals' complexity classifier. Your ONLY job is to decide the \
MINIMUM complexity level (1-5) required to answer a research question correctly -- you never \
answer the question, never pick a dataset, and never perform research.

{_LEVEL_DEFINITIONS}

Rules:
- Choose the MINIMUM level capable of answering the question correctly, not the level that would \
merely be sufficient.
- Time horizon alone (e.g. "over 10 years" vs "last year") must not increase the level.
- Question length alone must not determine the level.
- A question naming or implying a second dataset (peer, industry, benchmark, macro series) is at \
least Level 4, even if it doesn't use the word "compare".
- A question asking "why" a divergence/trend exists, or asking for causal drivers/competing \
explanations, is Level 5.

Respond with ONLY a JSON object, no other text:
{{"complexity_level": <integer 1-5>, "confidence": <float 0-1>, "reason": "<one short sentence>"}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_TOKENS = 200
_TASK_NAME = "jev_complexity_classifier"


@dataclass(frozen=True)
class ComplexityClassification:
    level: ComplexityLevel
    confidence: float
    reason: str
    source: str  # "jev" | "deterministic_fallback"
    model_used: str | None = None


def _fallback(reason: str) -> ComplexityClassification:
    """Only reached when every model in JEV_CLASSIFIER_MODEL_CHAIN is
    unavailable (outage, no API keys configured at all) -- never the normal
    path. Defaults to Level 3, not Level 1: guessing too LOW risks answering
    with less rigor than the question needs (e.g. skipping interpretation
    entirely), while guessing too HIGH for a simple lookup only costs one
    extra, still-correct interpretation pass."""
    return ComplexityClassification(
        level=ComplexityLevel.INTERPRET, confidence=0.0, reason=reason, source="deterministic_fallback",
    )


def classify_complexity(question: str, company_ids: list[str]) -> tuple[ComplexityClassification, RouteResult | None]:
    """Returns (classification, route_result). route_result is None only on
    the deterministic-fallback path (no model actually ran) -- callers that
    want to log this call into llm_call_log (llm/observability.py) need the
    RouteResult; research/routing_policy.py's audit trail uses both."""
    user_message = f"Question: {question}\nCompanies referenced: {', '.join(company_ids) or '(none named)'}"
    hardness = fixed(Tier.QUICK, "Jev complexity classification")
    try:
        result = route_explicit_chain(
            system=SYSTEM_PROMPT, user_message=user_message, hardness=hardness,
            model_chain=JEV_CLASSIFIER_MODEL_CHAIN, max_tokens=_MAX_TOKENS,
        )
    except AllProvidersUnavailableError:
        logger.warning("Jev classifier: every configured model unavailable, defaulting to Level 3")
        return _fallback("Jev classifier unavailable (all configured models failed) -- defaulted to Level 3"), None

    text = result.response.text or ""
    match = _JSON_RE.search(text)
    if not match:
        logger.warning("Jev classifier returned unparseable output: %r", text)
        return _fallback("Jev classifier returned no parseable JSON -- defaulted to Level 3"), result

    try:
        payload = json.loads(match.group(0))
        level = ComplexityLevel(int(payload["complexity_level"]))
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.5))))
        reason = str(payload.get("reason") or "").strip() or "no reason given"
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Jev classifier returned malformed JSON (%s): %r", exc, text)
        return _fallback(f"Jev classifier returned malformed JSON ({exc}) -- defaulted to Level 3"), result

    return (
        ComplexityClassification(level=level, confidence=confidence, reason=reason, source="jev", model_used=result.response.model),
        result,
    )
