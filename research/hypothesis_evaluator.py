"""Hypothesis Evaluation (Step 2G) — evaluates ONE hypothesis independently
against the evidence Step 2F retrieved for it. Independent means exactly
that: this call never sees how the hypothesis was framed as "the answer" —
only its statement/mechanism and the evidence gathered for it — and every
hypothesis in an investigation gets its own separate call, never a single
batched judgment across all of them, so one hypothesis's evidence can't
anchor the verdict on another.

Every cited claim is tagged with the full config/knowledge_ontology.py
CLAIM_TYPES vocabulary (FACT/CALCULATION/MANAGEMENT_OPINION/PREDICTION/
INFERENCE/CORRELATION/CAUSATION) — not just Evidence's narrower FACT/
CALCULATION/MANAGEMENT_STATEMENT/INFERENCE, since hypothesis evaluation
specifically needs to distinguish a correlation the evidence shows from a
causal mechanism the evidence actually asserts, and a management prediction
from a verified fact. The hard rule (never promote correlation to
causation, never promote a management opinion to fact) is enforced by
instruction, not just vocabulary — the model is told explicitly not to
upgrade either.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from config.knowledge_ontology import CLAIM_TYPES
from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.hypothesis_generator import Hypothesis
from research.investigation_planner import InvestigationPlan

VERDICTS: frozenset[str] = frozenset({"SUPPORTED", "PARTIALLY_SUPPORTED", "REFUTED", "INSUFFICIENT_EVIDENCE"})

MAX_TOKENS = 3072
_MAX_QUOTE_CHARS = 240
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class HypothesisEvaluationError(Exception):
    """Raised when evaluation can't complete — the LLM call failed, or its
    response didn't parse into the expected shape."""


@dataclass
class EvidenceItem:
    kind: str  # config.knowledge_ontology.CLAIM_TYPES
    label: str
    value: str | None = None
    citation: str | None = None


@dataclass
class HypothesisEvaluation:
    hypothesis_id: str
    verdict: str
    confidence_basis: str
    supporting_evidence: list[EvidenceItem] = field(default_factory=list)
    contradicting_evidence: list[EvidenceItem] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT = """You independently evaluate ONE hypothesis against the evidence retrieved \
for it. Judge only what THIS evidence actually supports — never assume the hypothesis is correct just because it \
was proposed, and never let how plausible it sounds substitute for what the evidence shows.

Verdicts — exactly one of: SUPPORTED, PARTIALLY_SUPPORTED, REFUTED, INSUFFICIENT_EVIDENCE.

Every piece of evidence you cite must be tagged with exactly one of these kinds:
- FACT — a reported number or figure stated in the evidence
- CALCULATION — a deterministic computation, with its inputs, from the evidence
- MANAGEMENT_OPINION — a view/plan/belief a company's own management stated
- PREDICTION — guidance/outlook management or the evidence itself stated about the future
- INFERENCE — your own reasoning connecting two or more evidence items — never present it as independently verified
- CORRELATION — a statistical association the evidence shows, nothing more
- CAUSATION — a causal mechanism the evidence ITSELF explicitly asserts, not one you're inferring from a correlation

Hard rules:
- Never upgrade a CORRELATION into CAUSATION yourself — if the evidence only shows two things moved together, tag \
it CORRELATION even if a causal story seems plausible to you.
- Never upgrade a MANAGEMENT_OPINION or PREDICTION into FACT — a confidently-stated management belief or guidance \
is not independently verified just because it's confident.
- If the evidence doesn't cover something this hypothesis needs, say so under missing_evidence — do not guess or \
fill the gap from outside/training knowledge.

Respond with ONLY a JSON object, no other text, in exactly this shape:

{{
  "verdict": "<one of: SUPPORTED, PARTIALLY_SUPPORTED, REFUTED, INSUFFICIENT_EVIDENCE>",
  "confidence_basis": "<one or two sentences: why this verdict, referencing the evidence>",
  "supporting_evidence": [
    {{"kind": "<one of: {claim_types}>", "label": "<short label>", "value": "<the figure/quote/fact>", "citation": "<source>"}}
  ],
  "contradicting_evidence": [
    {{"kind": "<one of: {claim_types}>", "label": "<short label>", "value": "<the figure/quote/fact>", "citation": "<source>"}}
  ],
  "missing_evidence": ["<what would be needed to evaluate this further, but isn't available>"]
}}"""


def _build_system_prompt() -> str:
    return HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT.format(claim_types=", ".join(sorted(CLAIM_TYPES)))


def _render_plan(plan: InvestigationPlan) -> str:
    lines: list[str] = []
    if plan.evidence:
        lines.append("Structured evidence (financials/macro/uploaded documents):")
        lines.extend(f"  {e.as_prompt_line()}" for e in plan.evidence)
    if plan.knowledge_claims:
        lines.append("\nKnowledge graph claims (extracted from documents, Step 2A/2B):")
        for claim in plan.knowledge_claims:
            period = f"{claim.quarter} {claim.fiscal_year}" if claim.quarter else (claim.fiscal_year or "period unknown")
            lines.append(
                f"  [{claim.claim_type}] {claim.company_id} — {claim.claim_text} "
                f"({period}, speaker={claim.speaker or 'n/a'}, confidence={claim.confidence})"
            )
            if claim.evidence_quotes:
                lines.append(f"    Quote: {claim.evidence_quotes[0][:_MAX_QUOTE_CHARS]}")
    if plan.passages:
        lines.append("\nDocument passages (keyword-matched, Step 2D):")
        for passage in plan.passages:
            period = f"{passage.quarter} {passage.fiscal_year}" if passage.quarter else (passage.fiscal_year or "period unknown")
            lines.append(
                f"  [{passage.document_type or 'document'}] {passage.company_id} "
                f"({period}, page {passage.page_number}): {passage.text[:_MAX_QUOTE_CHARS]}"
            )
    if not lines:
        return "No evidence was retrieved for this hypothesis."
    return "\n".join(lines)


def _parse_response(text: str) -> dict:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        raise HypothesisEvaluationError(f"model response contained no JSON object: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise HypothesisEvaluationError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        raise HypothesisEvaluationError(f"model response missing a top-level 'verdict': {text[:200]!r}")
    return parsed


def _parse_evidence_items(raw_items: object) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        label = (raw.get("label") or "").strip()
        if kind not in CLAIM_TYPES or not label:
            continue  # a hallucinated kind or empty label is dropped, not stored as-is
        items.append(EvidenceItem(kind=kind, label=label, value=raw.get("value"), citation=raw.get("citation")))
    return items


def evaluate_hypothesis(
    conn: sqlite3.Connection, hypothesis: Hypothesis, plan: InvestigationPlan, *, model: str | None = None
) -> HypothesisEvaluation:
    hardness = fixed(Tier.DEEP, "hypothesis evaluation")
    # No DEFAULT_ANTHROPIC_MODEL fallback — see research/knowledge_builder.py's
    # identical comment; leaving this unset lets llm/router.py respect
    # TIER_PREFERRED_MODEL[hardness.tier] (the operator's actual configured
    # policy) instead of silently pinning to sonnet on every call.
    pinned_model = model or ANTHROPIC_MODEL
    user_message = (
        f"Hypothesis: {hypothesis.statement}\n"
        f"Mechanism: {hypothesis.mechanism}\n"
        f"Category: {hypothesis.category}\n\n"
        f"Evidence retrieved for this hypothesis:\n{_render_plan(plan)}"
    )

    try:
        result = route(
            system=_build_system_prompt(), user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError as exc:
        raise HypothesisEvaluationError(f"all configured models failed: {exc}") from exc

    observability.record(
        conn, task_name="hypothesis_evaluation", company_ids=hypothesis.companies,
        question=hypothesis.statement, result=result,
    )

    response = result.response
    if response.stop_reason == "refusal" or not response.text:
        raise HypothesisEvaluationError(f"model returned no usable response (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        raise HypothesisEvaluationError(f"model response was truncated at the {MAX_TOKENS}-token limit before finishing")

    parsed = _parse_response(response.text)
    verdict = parsed.get("verdict")
    if verdict not in VERDICTS:
        raise HypothesisEvaluationError(f"model returned an invalid verdict: {verdict!r}")

    return HypothesisEvaluation(
        hypothesis_id=hypothesis.hypothesis_id, verdict=verdict,
        confidence_basis=(parsed.get("confidence_basis") or "").strip(),
        supporting_evidence=_parse_evidence_items(parsed.get("supporting_evidence")),
        contradicting_evidence=_parse_evidence_items(parsed.get("contradicting_evidence")),
        missing_evidence=[str(m) for m in (parsed.get("missing_evidence") or [])],
    )
