"""System Insights (Tools tab) — cross-company insight cards grounded in the
Knowledge Graph's knowledge_claims, generated in one batch call rather than
per-question (distinct from research/insights.py's per-company Key Insights,
which never touches the knowledge graph at all — only canonical_financials).

Not auto-scheduled: generation is trigger-button-initiated, same
user-controls-when-it-runs discipline the existing Key Insights "Generate"
button already follows. Every insight persists with a status ("new" until
the user retains or archives it) — storage/repositories.py's
save_system_insight/list_system_insights/update_system_insight_status.

Candidates are the highest-confidence recent claims of the "insight-like"
claim types (INFERENCE/CORRELATION/CAUSATION/MANAGEMENT_OPINION/PREDICTION —
config/knowledge_ontology.py's CLAIM_TYPES vocabulary minus the two purely
factual ones, FACT/CALCULATION, which don't need synthesizing, they're
already evidence lines elsewhere). One LLM call for the whole batch, not one
per claim — there are only a handful of candidates in practice today, same
"one call, several structured items" shape research/hypothesis_generator.py
already uses. Returns [] (not an error) when there are no candidates — a
thin knowledge graph is a real, expected state, not a failure.
"""

from __future__ import annotations

import json
import re
from storage.db_types import DBConnection, Row
import uuid
from dataclasses import dataclass, field

from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from storage.fact_store import FactStore, default_fact_store

#: The claim types actually worth synthesizing into an insight — FACT/
#: CALCULATION are already plain evidence lines elsewhere, nothing to add by
#: restating them here.
_INSIGHT_CLAIM_TYPES = ("INFERENCE", "CORRELATION", "CAUSATION", "MANAGEMENT_OPINION", "PREDICTION")

MAX_CANDIDATE_CLAIMS = 10
MAX_INSIGHTS = 5
MAX_TOKENS = 2048

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class SystemInsightGenerationError(Exception):
    """Raised when generation can't complete — the LLM call failed, or its
    response didn't parse. Never raised for "no candidates" — that's a
    normal empty result, not a failure."""


@dataclass
class SystemInsight:
    insight_id: str
    company_ids: list[str]
    insight_text: str
    source_claim_ids: list[int] = field(default_factory=list)


SYSTEM_INSIGHTS_SYSTEM_PROMPT = """You write short, evidence-grounded research insights from a batch of \
already-extracted claims about one or more companies. Each claim below is tagged with its type \
(INFERENCE/CORRELATION/CAUSATION/MANAGEMENT_OPINION/PREDICTION), the company it's about, and how confident the \
extraction was.

Write at most {max_insights} insights. Each insight must be grounded in one or more of the claims given — never \
invent a fact, number, or connection the claims don't support. A claim tagged CORRELATION must not be restated as \
if it were CAUSATION; a MANAGEMENT_OPINION or PREDICTION must not be restated as if it were a verified FACT — carry \
the same distinction the claims already have into how you phrase the insight.

If nothing in the claims is genuinely worth surfacing as an insight, return fewer than {max_insights} — quality \
over quantity, never pad.

Respond with ONLY a JSON array, no other text, in exactly this shape:

[
  {{
    "insight_text": "<one or two clear sentences>",
    "company_ids": ["<company_id this insight is about>"],
    "source_claim_ids": [<claim_id integers this insight is grounded in>]
  }}
]"""


def _render_candidates(claims: list[Row]) -> str:
    lines = []
    for claim in claims:
        period = f"{claim['quarter']} {claim['fiscal_year']}" if claim["quarter"] else (claim["fiscal_year"] or "period unknown")
        lines.append(
            f"[claim_id={claim['claim_id']}] [{claim['claim_type']}] {claim['company_id']} — {claim['claim_text']} "
            f"({period}, confidence={claim['extraction_confidence']})"
        )
    return "\n".join(lines)


def _parse_response(text: str) -> list[dict]:
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        raise SystemInsightGenerationError(f"model response contained no JSON array: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError as exc:
        raise SystemInsightGenerationError(f"model response wasn't valid JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise SystemInsightGenerationError(f"model response wasn't a JSON array: {text[:200]!r}")
    return parsed


def generate_system_insights(
    conn: DBConnection, *, model: str | None = None, fact_store: FactStore | None = None,
    limit: int = MAX_CANDIDATE_CLAIMS,
) -> list[SystemInsight]:
    fs = fact_store or default_fact_store()
    candidates = fs.list_recent_high_confidence_claims(conn, claim_types=_INSIGHT_CLAIM_TYPES, limit=limit)
    if not candidates:
        return []

    valid_claim_ids = {c["claim_id"] for c in candidates}
    valid_company_ids = {c["company_id"] for c in candidates if c["company_id"]}

    hardness = fixed(Tier.STANDARD, "system insight generation")
    pinned_model = model or ANTHROPIC_MODEL
    user_message = f"Claims:\n{_render_candidates(candidates)}"

    try:
        result = route(
            system=SYSTEM_INSIGHTS_SYSTEM_PROMPT.format(max_insights=MAX_INSIGHTS),
            user_message=user_message, hardness=hardness, max_tokens=MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError as exc:
        raise SystemInsightGenerationError(f"all configured models failed: {exc}") from exc

    observability.record(
        conn, task_name="system_insight_generation", company_ids=sorted(valid_company_ids),
        question=None, result=result,
    )

    response = result.response
    if response.stop_reason == "refusal" or not response.text:
        raise SystemInsightGenerationError(f"model returned no usable response (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        raise SystemInsightGenerationError(f"model response was truncated at the {MAX_TOKENS}-token limit before finishing")

    raw_insights = _parse_response(response.text)
    insights: list[SystemInsight] = []
    for raw in raw_insights[:MAX_INSIGHTS]:
        insight_text = (raw.get("insight_text") or "").strip()
        if not insight_text:
            continue
        # A hallucinated claim_id/company_id is dropped, not stored as-is —
        # same "never trust an LLM-named reference without checking it's
        # real" discipline research/macro_evidence.py's series_key
        # validation already follows.
        company_ids = [c for c in (raw.get("company_ids") or []) if c in valid_company_ids]
        source_claim_ids = [i for i in (raw.get("source_claim_ids") or []) if i in valid_claim_ids]
        if not source_claim_ids:
            continue  # an insight grounded in nothing real isn't an insight
        insight = SystemInsight(
            insight_id=uuid.uuid4().hex[:12], company_ids=company_ids,
            insight_text=insight_text, source_claim_ids=source_claim_ids,
        )
        fs.save_system_insight(
            conn, insight_id=insight.insight_id, company_ids=insight.company_ids,
            insight_text=insight.insight_text, source_claim_ids=insight.source_claim_ids,
        )
        insights.append(insight)

    return insights
