"""Hypothesis-driven investigation orchestrator (Steps 2E-2H) — the full
loop the spec describes:

    Observation/Question
          |
    Generate competing hypotheses          (2E, research/hypothesis_generator.py)
          |
    Determine + retrieve evidence          (2F, research/investigation_planner.py)
          |
    Evaluate each hypothesis independently (2G, research/hypothesis_evaluator.py)
          |
    Rank + synthesize findings             (2H, research/research_synthesis.py)

Distinct from research/signals_report.py's Signals reports — a Signals
report is one narrative answer grounded in one evidence block; an
investigation is structured around several independently-evaluated,
ranked hypotheses, persisted as such (investigations/investigation_hypotheses/
investigation_hypothesis_evidence, schemas/sqlite_schema.sql), not a single
markdown blob. Every hypothesis, its verdict, its evidence, and its rank
are individually queryable afterward — the point of Step 2H's "expose the
investigation process, not hide it."

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
import uuid
from dataclasses import dataclass, field

from research.hypothesis_evaluator import HypothesisEvaluation, HypothesisEvaluationError, evaluate_hypothesis
from research.hypothesis_generator import Hypothesis, HypothesisGenerationError, generate_hypotheses
from research.investigation_planner import InvestigationPlan, plan_and_gather
from research.research_synthesis import ResearchSynthesis, ResearchSynthesisError, synthesize
from storage.repositories import save_investigation, save_investigation_hypothesis, save_investigation_hypothesis_evidence

logger = logging.getLogger(__name__)


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


def run_investigation(
    conn: sqlite3.Connection, question: str, company_ids: list[str], *, statement_type: str = "consolidated",
    model: str | None = None,
) -> Investigation:
    investigation_id = uuid.uuid4().hex[:12]

    try:
        hypotheses = generate_hypotheses(conn, investigation_id, question, company_ids, model=model)
    except HypothesisGenerationError as exc:
        raise InvestigationError(f"could not generate hypotheses: {exc}") from exc

    investigation = Investigation(investigation_id=investigation_id, question=question, company_ids=company_ids)
    investigation.hypotheses = hypotheses

    for hypothesis in hypotheses:
        plan = plan_and_gather(conn, hypothesis, question)
        investigation.plans[hypothesis.hypothesis_id] = plan
        try:
            evaluation = evaluate_hypothesis(conn, hypothesis, plan, model=model)
        except HypothesisEvaluationError as exc:
            logger.warning(
                "Hypothesis evaluation failed for %s: %s", hypothesis.hypothesis_id, exc, exc_info=True
            )
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

    _persist(conn, investigation, statement_type)
    return investigation


def _persist(conn: sqlite3.Connection, investigation: Investigation, statement_type: str) -> None:
    synthesis = investigation.synthesis
    save_investigation(
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
        save_investigation_hypothesis(
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
            save_investigation_hypothesis_evidence(conn, hypothesis.hypothesis_id, evidence_rows)
