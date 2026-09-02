"""Research Synthesis (Step 2H) — only after every hypothesis has been
independently evaluated (Step 2G) does this module rank them and explain
the investigation as a whole: the strongest surviving explanation(s), why
they rank highest, what was rejected, what's still unresolved, and what
additional evidence would be worth collecting next.

The point is to expose the investigation process, not hide it behind a
single confident-sounding answer — a synthesis that only ever showed the
winning hypothesis would look identical whether five hypotheses were
genuinely tested and four fell away, or whether the "winner" was just the
first idea confirmed with no real competition. Never makes an investment
decision for the user — states evidence, competing explanations,
uncertainty, and open questions, same discipline as every other research/
call site's evidence-only framing.
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
from research.hypothesis_evaluator import HypothesisEvaluation
from research.hypothesis_generator import Hypothesis

MAX_TOKENS = 4096
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Verdicts that count as "still standing" for ranking purposes — REFUTED
#: hypotheses are reported (never silently dropped, per 2H's own "expose
#: the process" charter) but never ranked as a leading explanation.
_RANKABLE_VERDICTS = ("SUPPORTED", "PARTIALLY_SUPPORTED", "INSUFFICIENT_EVIDENCE")


class ResearchSynthesisError(Exception):
    """Raised when synthesis can't complete — the LLM call failed, or its
    response didn't parse into the expected shape."""


@dataclass
class ResearchSynthesis:
    strongest_explanation: str
    ranked_hypothesis_ids: list[str] = field(default_factory=list)  # best first; REFUTED ones excluded
    unanswered_questions: list[str] = field(default_factory=list)
    additional_evidence_needed: list[str] = field(default_factory=list)


RESEARCH_SYNTHESIS_SYSTEM_PROMPT = """You synthesize a completed investigation: several hypotheses have each \
already been independently evaluated against their own evidence (verdict: SUPPORTED, PARTIALLY_SUPPORTED, \
REFUTED, or INSUFFICIENT_EVIDENCE). Your job is to rank the surviving hypotheses and explain the investigation as \
a whole — never to re-evaluate any hypothesis's evidence yourself, and never to state or imply a buy/sell/hold \
recommendation or any other investment decision. You are providing research intelligence — evidence, competing \
explanations, uncertainty, and open questions — not a decision.

You will be given each hypothesis's id, statement, category, verdict, and confidence basis. Rank only the \
hypotheses whose verdict is SUPPORTED, PARTIALLY_SUPPORTED, or INSUFFICIENT_EVIDENCE — never rank a REFUTED one as \
a leading explanation (still acknowledge it exists, just don't include its id in ranked_hypothesis_ids). A \
SUPPORTED hypothesis with strong, specific evidence should generally rank above a PARTIALLY_SUPPORTED or \
INSUFFICIENT_EVIDENCE one, but use judgment — a PARTIALLY_SUPPORTED hypothesis with more directly relevant \
evidence can matter more than a thinly-supported SUPPORTED one.

Respond with ONLY a JSON object, no other text, in exactly this shape:

{
  "strongest_explanation": "<2-4 sentences: which hypothesis/hypotheses explain the observation best, why they \
rank highest, the biggest reason for caution, and how the rejected/unresolved hypotheses factor in>",
  "ranked_hypothesis_ids": ["<hypothesis_id>", "..."],
  "unanswered_questions": ["<a real open question this investigation didn't resolve>"],
  "additional_evidence_needed": ["<specific evidence that would meaningfully change confidence in the ranking>"]
}"""


def _render_hypotheses(hypotheses: list[Hypothesis], evaluations: dict[str, HypothesisEvaluation]) -> str:
    lines = []
    for hypothesis in hypotheses:
        evaluation = evaluations.get(hypothesis.hypothesis_id)
        if evaluation is None:
            continue  # a hypothesis whose evaluation itself failed is excluded, not synthesized over incomplete data
        lines.append(
            f"{hypothesis.hypothesis_id} [{hypothesis.category}] verdict={evaluation.verdict}\n"
            f"  Statement: {hypothesis.statement}\n"
            f"  Confidence basis: {evaluation.confidence_basis}\n"
            f"  Supporting evidence: {len(evaluation.supporting_evidence)} item(s), "
            f"Contradicting: {len(evaluation.contradicting_evidence)} item(s), "
            f"Missing: {len(evaluation.missing_evidence)} item(s)"
        )
    return "\n\n".join(lines) if lines else "No hypotheses were successfully evaluated."


def _parse_response(text: str) -> dict:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        raise ResearchSynthesisError(f"model response contained no JSON object: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError as exc:
        raise ResearchSynthesisError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, dict) or "strongest_explanation" not in parsed:
        raise ResearchSynthesisError(f"model response missing 'strongest_explanation': {text[:200]!r}")
    return parsed


def synthesize(
    conn: DBConnection,
    question: str,
    hypotheses: list[Hypothesis],
    evaluations: dict[str, HypothesisEvaluation],
    *,
    model: str | None = None,
) -> ResearchSynthesis:
    valid_ids = {h.hypothesis_id for h in hypotheses if evaluations.get(h.hypothesis_id) is not None}
    hardness = fixed(Tier.DEEP, "research synthesis")
    pinned_model = model or ANTHROPIC_MODEL
    user_message = f"Question/observation: {question}\n\nEvaluated hypotheses:\n{_render_hypotheses(hypotheses, evaluations)}"

    try:
        result = route(
            system=RESEARCH_SYNTHESIS_SYSTEM_PROMPT, user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError as exc:
        raise ResearchSynthesisError(f"all configured models failed: {exc}") from exc

    observability.record(conn, task_name="research_synthesis", company_ids=[], question=question, result=result)

    response = result.response
    if response.stop_reason == "refusal" or not response.text:
        raise ResearchSynthesisError(f"model returned no usable response (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        raise ResearchSynthesisError(f"model response was truncated at the {MAX_TOKENS}-token limit before finishing")

    parsed = _parse_response(response.text)
    # Never trust a model-named hypothesis_id without checking it's real —
    # a hallucinated or REFUTED one is silently dropped, not ranked.
    ranked = [hid for hid in (parsed.get("ranked_hypothesis_ids") or []) if hid in valid_ids]

    return ResearchSynthesis(
        strongest_explanation=(parsed.get("strongest_explanation") or "").strip(),
        ranked_hypothesis_ids=ranked,
        unanswered_questions=[str(q) for q in (parsed.get("unanswered_questions") or [])],
        additional_evidence_needed=[str(e) for e in (parsed.get("additional_evidence_needed") or [])],
    )
