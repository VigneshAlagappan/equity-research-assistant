"""Gap-fill pass (docs/L5_MVP_TASK_PLAN.md, option B): for each PRESENTED causal
link that still has no evidence either way after the data-first checks
(research/link_evidence.py), run ONE targeted retrieval whose query is the link's
own wording ("<cause> <effect>") through the existing evidence capabilities, then
one small model call that judges only whether that retrieved evidence bears on
THAT link.

Bounded on purpose: at most `LINK_GAPFILL_MAX_LINKS` links per investigation, one
retrieval + one call each, the evaluation model is the configured cheap one
(config.settings.CAUSAL_EVALUATION_MODEL), and it stops at the investigation's
shared deadline. Items found are tagged to their link with source_tier
"RETRIEVED" and appended to the evaluation; the evaluator's verdict is NOT
recomputed. A link with nothing relevant simply stays "untested" -- an honest
coverage gap, not something to paper over.

Never invents evidence: the model may cite only what retrieval returned, and the
hard rules from research/hypothesis_evaluator.py (no CORRELATION -> CAUSATION, no
management opinion -> fact) are restated in the prompt.
"""

from __future__ import annotations

import json
import logging
import re
import time

from config.knowledge_ontology import CLAIM_TYPES
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.hypothesis_evaluator import EvidenceItem, _parse_evidence_items, _render_plan
from research.investigation_graph import PRESENTED_VERDICTS
from research.investigation_planner import plan_and_gather

logger = logging.getLogger(__name__)

_MAX_TOKENS = 700
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """You judge whether retrieved evidence bears on ONE causal link of a hypothesis's chain.

You are given the hypothesis, the link (a cause step and an effect step), and evidence retrieved for that link. \
Use ONLY the evidence provided — never outside or training knowledge.

- stance "supporting": the evidence shows the cause and the effect occurred / are connected as the link states.
- stance "contradicting": the evidence shows the opposite, or shows the cause or effect did not occur as stated.
- stance "none": the evidence does not bear on this link. This is a normal, correct answer — do not force a stance.

Hard rules: never upgrade a CORRELATION into CAUSATION; never upgrade a MANAGEMENT_OPINION or PREDICTION into FACT. \
Tag each cited item with exactly one kind from: {claim_types}. Cite at most 3 items, each directly relevant.

Respond with ONLY a JSON object:
{{"stance": "supporting" | "contradicting" | "none",
  "items": [{{"kind": "<kind>", "label": "<short label>", "value": "<figure/quote/fact>", "citation": "<source>"}}]}}"""


def untested_presented_links(hypothesis, evaluation) -> list[int]:
    """Links of a presented hypothesis with no evidence tagged either way."""
    if evaluation is None or evaluation.verdict not in PRESENTED_VERDICTS:
        return []
    steps = [s for s in (getattr(hypothesis, "chain_steps", None) or []) if isinstance(s, str) and s.strip()]
    tagged = {i.chain_step for i in list(evaluation.supporting_evidence) + list(evaluation.contradicting_evidence)
              if getattr(i, "chain_step", None) is not None}
    return [link for link in range(len(steps) - 1) if link not in tagged]


def parse_link_judgement(text: str, link: int) -> tuple[str, list[EvidenceItem]]:
    """(stance, items tagged to `link` with source_tier RETRIEVED); ('none', []) on anything unusable."""
    match = _JSON_RE.search(text or "")
    if not match:
        return "none", []
    try:
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError:
        return "none", []
    stance = parsed.get("stance") if isinstance(parsed, dict) else None
    if stance not in ("supporting", "contradicting"):
        return "none", []
    items = _parse_evidence_items(parsed.get("items"))[:3]
    for item in items:
        item.chain_step, item.source_tier = link, "RETRIEVED"
    return (stance, items) if items else ("none", [])


def gap_fill_investigation(
    conn, investigation, question: str, *, capabilities, fact_store, deadline: float, max_links: int, model: str | None,
) -> int:
    """Returns the number of items added. Never raises."""
    from config import settings

    pinned = model or settings.CAUSAL_EVALUATION_MODEL or None
    added = 0
    budget = max_links
    for hypothesis in investigation.hypotheses:
        evaluation = investigation.evaluations.get(hypothesis.hypothesis_id)
        steps = [s for s in (hypothesis.chain_steps or []) if isinstance(s, str) and s.strip()]
        for link in untested_presented_links(hypothesis, evaluation):
            if budget <= 0 or time.monotonic() >= deadline:
                return added
            budget -= 1
            cause, effect = steps[link], steps[link + 1]
            try:
                plan = plan_and_gather(
                    conn, hypothesis, f"{cause} {effect}", capabilities=capabilities, fact_store=fact_store, retry=True,
                )
                rendered = _render_plan(plan)
                if rendered.startswith("No evidence"):
                    continue
                user_message = (
                    f"Hypothesis: {hypothesis.statement}\nLink {link}: \"{cause}\" -> \"{effect}\"\n\n"
                    f"Evidence retrieved for this link:\n{rendered}"
                )
                result = route(
                    system=SYSTEM_PROMPT.format(claim_types=", ".join(sorted(CLAIM_TYPES))), user_message=user_message,
                    hardness=fixed(Tier.QUICK, "link gap-fill"), max_tokens=_MAX_TOKENS, pinned_model=pinned,
                )
                observability.record(
                    conn, task_name="link_gap_fill", company_ids=hypothesis.companies, question=hypothesis.statement,
                    result=result, investigation_id=hypothesis.investigation_id,
                )
                stance, items = parse_link_judgement(result.response.text or "", link)
                target = evaluation.supporting_evidence if stance == "supporting" else evaluation.contradicting_evidence
                if stance != "none":
                    target.extend(items)
                    added += len(items)
            except AllProvidersUnavailableError:
                logger.warning("Gap-fill: model unavailable for %s link %s", hypothesis.hypothesis_id, link)
            except Exception:  # noqa: BLE001 -- additive pass: log and move on
                logger.warning("Gap-fill failed for %s link %s", hypothesis.hypothesis_id, link, exc_info=True)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
    return added
