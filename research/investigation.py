"""Hypothesis-driven investigation orchestrator (Steps 2E-2H) — the full
loop the spec describes:

    Observation/Question
          |
    Generate competing hypotheses          (2E, research/hypothesis_generator.py)
          |
    Plan -> Retrieve -> Evaluate  <--loop--+ (2F/2G, research/investigation_planner.py
          |  (per hypothesis)              |          + research/hypothesis_evaluator.py)
          | sufficient?                    |
          | no -> gap -> retrieve more ----+
          | yes
          v
    Rank + synthesize findings             (2H, research/research_synthesis.py)

Distinct from research/signals_report.py's Signals reports — a Signals
report is one narrative answer grounded in one evidence block; an
investigation is structured around several independently-evaluated,
ranked hypotheses, persisted as such (investigations/investigation_hypotheses/
investigation_hypothesis_evidence, schemas/sqlite_schema.sql), not a single
markdown blob. Every hypothesis, its verdict, its evidence, and its rank
are individually queryable afterward — the point of Step 2H's "expose the
investigation process, not hide it."

Per hypothesis, this module — the Orchestrator, per the architecture
guardrails — controls an evidence-sufficiency loop, not just one
plan-then-evaluate pass: an INSUFFICIENT_EVIDENCE verdict (Step 2G's own
missing_evidence) triggers one more Step 2F retrieval pass targeted at the
named gap, then a re-evaluation. The LLM only ever reports a verdict; this
module decides whether that verdict means "loop again" — never a fresh LLM
call asking "should I keep going?". Looping is bounded by 4 termination
controls: evidence sufficiency (any verdict other than
INSUFFICIENT_EVIDENCE), MAX_EVIDENCE_ITERATIONS per hypothesis, a wall-clock
deadline (INVESTIGATION_TIMEOUT_SECONDS) shared across the whole
investigation, and a no-new-evidence check (a retry that surfaces nothing
beyond what the prior pass already had stops immediately rather than paying
for an identical re-evaluation).

A single hypothesis failing evaluation (LLM hiccup, unparseable response)
does not fail the whole investigation — it's recorded with verdict=None and
excluded from synthesis, same graceful-degradation spirit used throughout
this app (a partial investigation beats none). The investigation as a whole
only fails if hypothesis generation itself fails (nothing to investigate)
or every single hypothesis's evaluation fails (nothing to synthesize).
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from research.capabilities import PlannerCapabilities, default_capabilities
from research.hypothesis_evaluator import HypothesisEvaluation, HypothesisEvaluationError, evaluate_hypothesis
from research.hypothesis_generator import Hypothesis, HypothesisGenerationError, generate_hypotheses
from research.investigation_planner import InvestigationPlan, plan_and_gather
from research.research_synthesis import ResearchSynthesis, ResearchSynthesisError, synthesize
from storage.fact_store import FactStore, default_fact_store

logger = logging.getLogger(__name__)

#: First pass plus at most one gap-driven retry per hypothesis — a bound on
#: total LLM evaluation calls per hypothesis, not on how much evidence a
#: single plan_and_gather() pass can retrieve.
MAX_EVIDENCE_ITERATIONS = 2

#: Wall-clock budget for the whole per-hypothesis evidence loop (generation
#: and synthesis aren't counted against it) — one deadline computed once per
#: investigation and shared across every hypothesis's loop, so a slow first
#: hypothesis can't silently starve the timeout budget for the rest.
INVESTIGATION_TIMEOUT_SECONDS = 180


class InvestigationError(Exception):
    """Raised only when the investigation as a whole can't produce anything
    usable — hypothesis generation failed outright, or every hypothesis's
    evaluation failed. A partial investigation (some hypotheses evaluated,
    some not) is returned normally, not raised."""


@dataclass
class Investigation:
    investigation_id: str
    question: str
    company_ids: list[str]
    hypotheses: list[Hypothesis] = field(default_factory=list)
    plans: dict[str, InvestigationPlan] = field(default_factory=dict)
    evaluations: dict[str, HypothesisEvaluation] = field(default_factory=dict)
    synthesis: ResearchSynthesis | None = None
    failed_hypothesis_ids: list[str] = field(default_factory=list)


def _persist_evidence_item(hypothesis_id: str, stance: str, item) -> dict:
    return {"stance": stance, "kind": item.kind, "label": item.label, "value": item.value, "citation": item.citation}


def _evidence_key(plan: InvestigationPlan) -> set[tuple]:
    """A cheap fingerprint of everything a plan retrieved — used only to
    detect whether a gap-driven retry actually turned up anything new.
    Never shown to the LLM or persisted; pure loop-control bookkeeping."""
    keys = {("evidence", e.company_id, e.kind, e.label, e.value, e.citation) for e in plan.evidence}
    keys |= {("claim", c.claim_id) for c in plan.knowledge_claims}
    keys |= {("passage", p.chunk_id) for p in plan.passages}
    return keys


def _merge_plans(base: InvestigationPlan, addition: InvestigationPlan) -> InvestigationPlan:
    """Unions a retry's plan onto the running one, de-duped by _evidence_key
    so the same item retrieved twice doesn't get evaluated twice."""
    merged = InvestigationPlan(
        hypothesis_id=base.hypothesis_id, evidence=list(base.evidence), knowledge_claims=list(base.knowledge_claims),
        passages=list(base.passages), sources_queried=list(base.sources_queried),
    )
    seen = _evidence_key(base)
    for item in addition.evidence:
        key = ("evidence", item.company_id, item.kind, item.label, item.value, item.citation)
        if key not in seen:
            seen.add(key)
            merged.evidence.append(item)
    for claim in addition.knowledge_claims:
        key = ("claim", claim.claim_id)
        if key not in seen:
            seen.add(key)
            merged.knowledge_claims.append(claim)
    for passage in addition.passages:
        key = ("passage", passage.chunk_id)
        if key not in seen:
            seen.add(key)
            merged.passages.append(passage)
    merged.sources_queried.extend(s for s in addition.sources_queried if s not in merged.sources_queried)
    return merged


def _investigate_hypothesis(
    conn: sqlite3.Connection, hypothesis: Hypothesis, question: str, *, model: str | None,
    capabilities: PlannerCapabilities, fact_store: FactStore, deadline: float,
) -> tuple[InvestigationPlan, HypothesisEvaluation | None]:
    """Runs the Step 2F -> 2G evidence-sufficiency loop for one hypothesis,
    bounded by the 4 termination controls documented at module level.
    Returns the final plan (whatever evidence was accumulated) and the final
    evaluation, or (plan, None) if evaluation itself failed — same failure
    shape run_investigation() already handled before this loop existed."""
    plan = plan_and_gather(conn, hypothesis, question, capabilities=capabilities, fact_store=fact_store)
    evaluation: HypothesisEvaluation | None = None

    for attempt in range(1, MAX_EVIDENCE_ITERATIONS + 1):
        try:
            evaluation = evaluate_hypothesis(conn, hypothesis, plan, model=model)
        except HypothesisEvaluationError as exc:
            logger.warning("Hypothesis evaluation failed for %s: %s", hypothesis.hypothesis_id, exc, exc_info=True)
            return plan, None

        if evaluation.verdict != "INSUFFICIENT_EVIDENCE":
            return plan, evaluation  # evidence-sufficiency control
        if attempt == MAX_EVIDENCE_ITERATIONS:
            return plan, evaluation  # max-iterations control
        if time.monotonic() >= deadline:
            logger.info("Investigation evidence loop timed out for %s", hypothesis.hypothesis_id)
            return plan, evaluation  # timeout control

        gap_query = " ".join(evaluation.missing_evidence) or question
        retry_plan = plan_and_gather(conn, hypothesis, gap_query, capabilities=capabilities, fact_store=fact_store)
        merged = _merge_plans(plan, retry_plan)
        if _evidence_key(merged) == _evidence_key(plan):
            logger.info("No new evidence found for %s — stopping evidence loop", hypothesis.hypothesis_id)
            return plan, evaluation  # inability-to-obtain-more-evidence control
        plan = merged

    return plan, evaluation


def run_investigation(
    conn: sqlite3.Connection, question: str, company_ids: list[str], *, statement_type: str = "consolidated",
    model: str | None = None, capabilities: PlannerCapabilities | None = None, fact_store: FactStore | None = None,
) -> Investigation:
    investigation_id = uuid.uuid4().hex[:12]
    fs = fact_store or default_fact_store()
    caps = capabilities or default_capabilities(fact_store=fs)

    try:
        hypotheses = generate_hypotheses(conn, investigation_id, question, company_ids, model=model, fact_store=fs)
    except HypothesisGenerationError as exc:
        raise InvestigationError(f"could not generate hypotheses: {exc}") from exc

    investigation = Investigation(investigation_id=investigation_id, question=question, company_ids=company_ids)
    investigation.hypotheses = hypotheses
    deadline = time.monotonic() + INVESTIGATION_TIMEOUT_SECONDS

    for hypothesis in hypotheses:
        plan, evaluation = _investigate_hypothesis(
            conn, hypothesis, question, model=model, capabilities=caps, fact_store=fs, deadline=deadline,
        )
        investigation.plans[hypothesis.hypothesis_id] = plan
        if evaluation is None:
            investigation.failed_hypothesis_ids.append(hypothesis.hypothesis_id)
            continue
        investigation.evaluations[hypothesis.hypothesis_id] = evaluation

    if not investigation.evaluations:
        raise InvestigationError("every hypothesis's evaluation failed — nothing to synthesize")

    try:
        investigation.synthesis = synthesize(conn, question, hypotheses, investigation.evaluations, model=model)
    except ResearchSynthesisError as exc:
        logger.warning("Research synthesis failed for investigation %s: %s", investigation_id, exc, exc_info=True)
        investigation.synthesis = None

    _persist(conn, investigation, statement_type, fs)
    return investigation


def _persist(conn: sqlite3.Connection, investigation: Investigation, statement_type: str, fact_store: FactStore) -> None:
    synthesis = investigation.synthesis
    fact_store.save_investigation(
        conn, investigation_id=investigation.investigation_id, question=investigation.question,
        company_ids=investigation.company_ids, statement_type=statement_type,
        strongest_explanation=synthesis.strongest_explanation if synthesis else None,
        unanswered_questions=synthesis.unanswered_questions if synthesis else [],
        additional_evidence_needed=synthesis.additional_evidence_needed if synthesis else [],
    )

    rank_by_id = (
        {hid: i + 1 for i, hid in enumerate(synthesis.ranked_hypothesis_ids)} if synthesis else {}
    )
    for hypothesis in investigation.hypotheses:
        evaluation = investigation.evaluations.get(hypothesis.hypothesis_id)
        fact_store.save_investigation_hypothesis(
            conn, hypothesis_id=hypothesis.hypothesis_id, investigation_id=investigation.investigation_id,
            statement=hypothesis.statement, mechanism=hypothesis.mechanism, category=hypothesis.category,
            rationale=hypothesis.rationale, unknowns=hypothesis.unknowns, generation_order=hypothesis.generation_order,
            verdict=evaluation.verdict if evaluation else None,
            confidence_basis=evaluation.confidence_basis if evaluation else None,
            synthesis_rank=rank_by_id.get(hypothesis.hypothesis_id),
        )
        if evaluation is None:
            continue
        evidence_rows = (
            [_persist_evidence_item(hypothesis.hypothesis_id, "supporting", item) for item in evaluation.supporting_evidence]
            + [_persist_evidence_item(hypothesis.hypothesis_id, "contradicting", item) for item in evaluation.contradicting_evidence]
            + [
                {"stance": "missing", "kind": "INFERENCE", "label": item, "value": None, "citation": None}
                for item in evaluation.missing_evidence
            ]
        )
        if evidence_rows:
            fact_store.save_investigation_hypothesis_evidence(conn, hypothesis.hypothesis_id, evidence_rows)
