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
named gap, then a re-evaluation. That retrieval pass is itself
capability-targeted (research/investigation_planner.py::plan_and_gather's
`retry=True`) — it skips the capabilities that only ever depend on
(hypothesis, company_id) and so cannot return anything new a second time
(financial/indicator evidence, the per-company knowledge-graph lookup),
re-querying only what's actually driven by the new gap text. The LLM only
ever reports a verdict; this module decides whether that verdict means
"loop again" — never a fresh LLM call asking "should I keep going?".
Looping is bounded by 4 termination controls: evidence sufficiency (any
verdict other than INSUFFICIENT_EVIDENCE), MAX_EVIDENCE_ITERATIONS per
hypothesis, a wall-clock deadline (INVESTIGATION_TIMEOUT_SECONDS) shared
across the whole investigation, and a no-new-evidence check (a retry that
surfaces nothing beyond what the prior pass already had stops immediately
rather than paying for an identical re-evaluation).

A single hypothesis failing evaluation (LLM hiccup, unparseable response)
does not fail the whole investigation — it's recorded with verdict=None and
excluded from synthesis, same graceful-degradation spirit used throughout
this app (a partial investigation beats none). The investigation as a whole
only fails if hypothesis generation itself fails (nothing to investigate)
or every single hypothesis's evaluation fails (nothing to synthesize).
"""

from __future__ import annotations

import json
import logging
from storage.db_types import DBConnection
import time
import uuid
from dataclasses import dataclass, field

from llm import execution_metrics
from research.assistant import CaseCancelledError, InsufficientEvidenceError, gather_evidence
from research.capabilities import PlannerCapabilities, default_capabilities
from research.hypothesis_evaluator import HypothesisEvaluation, HypothesisEvaluationError, evaluate_hypothesis
from research.hypothesis_generator import Hypothesis, HypothesisGenerationError, generate_hypotheses
from research.investigation_planner import InvestigationPlan, plan_and_gather
from research.research_synthesis import ResearchSynthesis, ResearchSynthesisError, synthesize
from research.abstracts import generate_abstract
from research.temporal import normalize_as_of
from storage.document_store import default_document_store
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
    #: ISO date the evidence was restricted to, or None for "everything known
    #: now" — persisted on the investigation row so a historical conclusion
    #: states the information set it was reached under.
    as_of: str | None = None
    #: time.monotonic() when run started -- runtime metric only, never persisted as-is.
    started_monotonic: float | None = None


def _persist_evidence_item(hypothesis_id: str, stance: str, item, graph=None) -> dict:
    row = {"stance": stance, "kind": item.kind, "label": item.label, "value": item.value, "citation": item.citation}
    link = getattr(item, "chain_step", None)
    if link is not None:
        row["chain_step"] = link
        edge_id = graph.edge_id_for(hypothesis_id, link) if graph is not None else None
        if edge_id is not None:
            row["edge_id"] = edge_id
    return row


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
    conn: DBConnection, hypothesis: Hypothesis, question: str, *, model: str | None,
    capabilities: PlannerCapabilities, fact_store: FactStore, deadline: float,
) -> tuple[InvestigationPlan, HypothesisEvaluation | None]:
    """Runs the Step 2F -> 2G evidence-sufficiency loop for one hypothesis,
    bounded by the 4 termination controls documented at module level.
    Returns the final plan (whatever evidence was accumulated) and the final
    evaluation, or (plan, None) if evaluation itself failed — same failure
    shape run_investigation() already handled before this loop existed."""
    with execution_metrics.phase("planner"):
        plan = plan_and_gather(conn, hypothesis, question, capabilities=capabilities, fact_store=fact_store)
    evaluation: HypothesisEvaluation | None = None

    for attempt in range(1, MAX_EVIDENCE_ITERATIONS + 1):
        try:
            evaluation = evaluate_hypothesis(conn, hypothesis, plan, model=model)
            evaluation.iterations = attempt
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
        with execution_metrics.phase("planner"):
            retry_plan = plan_and_gather(
                conn, hypothesis, gap_query, capabilities=capabilities, fact_store=fact_store, retry=True
            )
        merged = _merge_plans(plan, retry_plan)
        if _evidence_key(merged) == _evidence_key(plan):
            logger.info("No new evidence found for %s — stopping evidence loop", hypothesis.hypothesis_id)
            return plan, evaluation  # inability-to-obtain-more-evidence control
        plan = merged

    return plan, evaluation


def run_investigation(
    conn: DBConnection, question: str, company_ids: list[str], *, statement_type: str = "consolidated",
    model: str | None = None, capabilities: PlannerCapabilities | None = None, fact_store: FactStore | None = None,
    as_of: str | None = None, investigation_id: str | None = None, case_id: str | None = None,
    complexity_level: int | None = None,
) -> Investigation:
    """Execution Analytics wrapper (llm/execution_metrics.py) around
    _run_investigation_impl, which does the actual Steps 2E-2H work -- see
    that function's docstring. investigation_id is resolved here (not left
    to the impl) so it can double as execution_metrics.run_id, the same
    value hypothesis generation/evaluation/synthesis's own llm_call_log
    rows already carry as investigation_id -- one id links every table.
    execution_mode="async" exactly when case_id is set, i.e. this call is
    running inside research/case_runner.py's background thread
    (web/app.py's /investigate/generate-async) rather than directly inside
    a request (/investigate/generate)."""
    investigation_id = investigation_id or uuid.uuid4().hex[:12]
    execution_mode = "async" if case_id is not None else "sync"
    with execution_metrics.start_run(conn, investigation_id, "investigation", execution_mode=execution_mode):
        return _run_investigation_impl(
            conn, question, company_ids, statement_type=statement_type, model=model, capabilities=capabilities,
            fact_store=fact_store, as_of=as_of, investigation_id=investigation_id, case_id=case_id,
            complexity_level=complexity_level,
        )


def _run_investigation_impl(
    conn: DBConnection, question: str, company_ids: list[str], *, statement_type: str = "consolidated",
    model: str | None = None, capabilities: PlannerCapabilities | None = None, fact_store: FactStore | None = None,
    as_of: str | None = None, investigation_id: str | None = None, case_id: str | None = None,
    complexity_level: int | None = None,
) -> Investigation:
    """`as_of` (ISO date) runs the whole investigation point-in-time: every
    evidence capability is bound to that cutoff (research/temporal.py via
    default_capabilities), so hypothesis generation, evidence gathering,
    evaluation and synthesis all see only what was on file then. It is
    enforced in retrieval, not asked for in a prompt — an "as of 2013"
    question whose evidence block contains 2024 figures has already leaked
    the answer. Explicitly-passed `capabilities` are used as given, on the
    assumption the caller has already bound whatever scope it wants.

    `investigation_id`, when given, is used as-is instead of generating a
    fresh one -- lets a caller (web/app.py's /investigate/generate-async)
    hand out the id up front, before this (potentially several-minute) call
    even starts, so it has something to poll progress against from the
    first response.

    `case_id` (only ever passed by research/case_runner.py's async/Cases
    path -- see research/assistant.py's answer_question() for the same
    parameter on the Quick Answer side) turns on current_activity updates
    between stages, an early sufficiency check before hypothesis
    generation even starts, and one cancellation checkpoint. Every
    pre-existing caller leaves this unset and sees identical behavior."""
    investigation_id = investigation_id or uuid.uuid4().hex[:12]
    fs = fact_store or default_fact_store()
    cutoff = normalize_as_of(as_of)
    caps = capabilities or default_capabilities(fact_store=fs, as_of=cutoff, investigation_id=investigation_id)

    if case_id is not None:
        from storage.repositories import update_case_activity

        update_case_activity(conn, case_id, "Checking evidence sufficiency")
        # Cheap (DB queries + a vector search, no LLM call) -- same
        # reasoning research/assistant.py's answer_question() gives for
        # calling gather_evidence() before committing to a full pass: no
        # point starting a multi-hypothesis, multi-LLM-call investigation
        # when there's nothing at all to ground even one hypothesis in.
        financial_evidence, variable_evidence = gather_evidence(conn, question, company_ids, statement_type)
        if not (financial_evidence + variable_evidence):
            if company_ids:
                message = (
                    f"No data ingested yet for {', '.join(company_ids)}. "
                    "Run `python main.py ingest ...` first, then try again."
                )
            else:
                message = (
                    "No matching evidence found for this question. Name a company to ground it in that "
                    "company's Financials/Docs, or ask about a macro topic that's been ingested "
                    "(e.g. rainfall, repo rate, credit growth)."
                )
            raise InsufficientEvidenceError(message)
        update_case_activity(conn, case_id, "Generating hypotheses")

    try:
        hypotheses = generate_hypotheses(
            conn, investigation_id, question, company_ids, model=model, fact_store=fs, capabilities=caps,
        )
    except HypothesisGenerationError as exc:
        raise InvestigationError(f"could not generate hypotheses: {exc}") from exc

    investigation = Investigation(
        investigation_id=investigation_id, question=question, company_ids=company_ids, as_of=cutoff
    )
    investigation.hypotheses = hypotheses
    investigation.started_monotonic = time.monotonic()
    deadline = time.monotonic() + INVESTIGATION_TIMEOUT_SECONDS

    for index, hypothesis in enumerate(hypotheses):
        if case_id is not None:
            from storage.repositories import is_case_cancel_requested, update_case_activity

            if is_case_cancel_requested(conn, case_id):
                raise CaseCancelledError()
            update_case_activity(conn, case_id, f"Evaluating hypothesis {index + 1} of {len(hypotheses)}")
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

    if case_id is not None:
        from storage.repositories import update_case_activity

        update_case_activity(conn, case_id, "Synthesizing findings")

    try:
        investigation.synthesis = synthesize(conn, question, hypotheses, investigation.evaluations, model=model)
    except ResearchSynthesisError as exc:
        logger.warning("Research synthesis failed for investigation %s: %s", investigation_id, exc, exc_info=True)
        investigation.synthesis = None

    if case_id is not None:
        from storage.repositories import update_case_activity

        update_case_activity(conn, case_id, "Persisting result")

    _persist(conn, investigation, statement_type, fs, complexity_level)
    return investigation


def investigation_artifact_key(investigation_id: str, version: int = 1, name: str = "artifact.json") -> str:
    """Versioned, write-once-per-version object key
    (docs/L5_MVP_TASK_PLAN.md, M2): investigations/<id>/v<N>/<name>. The
    investigations row's s3_key points at the current version's artifact.json;
    older rows keep their legacy investigations/<id>/v1.json key and keep
    rendering because the reader follows whatever key the row holds."""
    return f"investigations/{investigation_id}/v{version}/{name}"


def _build_graph_safely(investigation: Investigation):
    """The investigation graph, or None when disabled or on any failure --
    graph building is additive and must never fail a persist."""
    from config import settings

    if not settings.CAUSAL_GRAPH_ENABLED:
        return None
    try:
        from research.investigation_graph import build_graph

        return build_graph(investigation.investigation_id, investigation.hypotheses, investigation.evaluations)
    except Exception:  # noqa: BLE001
        logger.warning("Investigation graph build failed for %s", investigation.investigation_id, exc_info=True)
        return None


def _persist_causal_layer(conn: DBConnection, investigation: Investigation, graph, hypotheses_json: list[dict]) -> dict:
    """Version stamps, graph rows, metrics row, and the extra artifact fields.
    Returns {"artifact_fields": {...}, "companion_files": {...}}; both empty
    when disabled or if anything fails (logged, swallowed)."""
    empty = {"artifact_fields": {}, "companion_files": {}}
    if graph is None:
        return empty
    try:
        from config.versions import GRAPH_VERSION, version_stamp
        from research.investigation_metrics import compute_graph_metrics
        from storage.causal_repository import (
            replace_investigation_graph, save_investigation_metrics, sum_llm_usage_for_investigation,
            update_investigation_versions,
        )

        stamp = version_stamp()
        update_investigation_versions(conn, investigation.investigation_id, stamp)
        replace_investigation_graph(conn, investigation.investigation_id, graph.nodes, graph.edges)

        metrics = compute_graph_metrics(graph, investigation.evaluations, len(investigation.hypotheses))
        metrics["iterations"] = sum(getattr(ev, "iterations", 1) for ev in investigation.evaluations.values())
        if investigation.started_monotonic is not None:
            metrics["runtime_ms"] = (time.monotonic() - investigation.started_monotonic) * 1000
        try:
            metrics.update(sum_llm_usage_for_investigation(conn, investigation.investigation_id))
        except Exception:  # noqa: BLE001 -- llm_call_log may live in a different store; metrics stay NULL
            logger.info("LLM usage unavailable for metrics of %s", investigation.investigation_id, exc_info=True)
        metrics.update(stamp)
        save_investigation_metrics(conn, investigation.investigation_id, metrics)

        metrics_json = {**metrics, "materiality_basis": "none", "graph_version": GRAPH_VERSION}
        graph_json = {"nodes": graph.nodes, "edges": graph.edges, "graph_version": GRAPH_VERSION}
        return {
            "artifact_fields": {"graph": graph_json, "metrics": metrics_json, "versions": stamp},
            "companion_files": {"graph.json": graph_json, "metrics.json": metrics_json},
        }
    except Exception:  # noqa: BLE001
        logger.warning("Causal layer persist failed for %s", investigation.investigation_id, exc_info=True)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return empty


def _persist(
    conn: DBConnection, investigation: Investigation, statement_type: str, fact_store: FactStore,
    complexity_level: int | None = None,
) -> None:
    """Writes investigations/investigation_hypotheses/investigation_
    hypothesis_evidence exactly as before (unchanged -- nothing here
    should ever regress that pipeline), then ALSO assembles the same
    data into one JSON artifact and uploads it to S3, recording the S3
    key + a short abstract + the strongest verdict on the investigations
    row (storage/database.py::_migrate_investigation_s3_columns).
    web/app.py's investigate_view() prefers this artifact once s3_key is
    set -- see persistence architecture ADR (docs/adr/) for the full
    reasoning. Built from the same in-memory `investigation`/`synthesis`
    objects the table writes below already use, not a re-read, so this
    can never see a different version of the data than what was just
    written relationally."""
    synthesis = investigation.synthesis
    fact_store.save_investigation(
        conn, investigation_id=investigation.investigation_id, question=investigation.question,
        company_ids=investigation.company_ids, statement_type=statement_type,
        strongest_explanation=synthesis.strongest_explanation if synthesis else None,
        unanswered_questions=synthesis.unanswered_questions if synthesis else [],
        additional_evidence_needed=synthesis.additional_evidence_needed if synthesis else [],
        as_of=investigation.as_of, complexity_level=complexity_level,
    )

    rank_by_id = (
        {hid: i + 1 for i, hid in enumerate(synthesis.ranked_hypothesis_ids)} if synthesis else {}
    )
    graph = _build_graph_safely(investigation)
    hypotheses_json: list[dict] = []
    for hypothesis in investigation.hypotheses:
        evaluation = investigation.evaluations.get(hypothesis.hypothesis_id)
        verdict = evaluation.verdict if evaluation else None
        confidence_basis = evaluation.confidence_basis if evaluation else None
        confidence_score = evaluation.confidence_score if evaluation else None
        synthesis_rank = rank_by_id.get(hypothesis.hypothesis_id)
        fact_store.save_investigation_hypothesis(
            conn, hypothesis_id=hypothesis.hypothesis_id, investigation_id=investigation.investigation_id,
            statement=hypothesis.statement, mechanism=hypothesis.mechanism, category=hypothesis.category,
            rationale=hypothesis.rationale, unknowns=hypothesis.unknowns, generation_order=hypothesis.generation_order,
            chain_steps=hypothesis.chain_steps,
            verdict=verdict, confidence_basis=confidence_basis, confidence_score=confidence_score,
            synthesis_rank=synthesis_rank,
        )
        supporting_evidence = contradicting_evidence = []
        missing_evidence: list[dict] = []
        if evaluation is not None:
            supporting_evidence = [_persist_evidence_item(hypothesis.hypothesis_id, "supporting", item, graph) for item in evaluation.supporting_evidence]
            contradicting_evidence = [_persist_evidence_item(hypothesis.hypothesis_id, "contradicting", item, graph) for item in evaluation.contradicting_evidence]
            missing_evidence = [
                {"stance": "missing", "kind": "INFERENCE", "label": item, "value": None, "citation": None}
                for item in evaluation.missing_evidence
            ]
            evidence_rows = supporting_evidence + contradicting_evidence + missing_evidence
            if evidence_rows:
                fact_store.save_investigation_hypothesis_evidence(conn, hypothesis.hypothesis_id, evidence_rows)

        hypotheses_json.append({
            "hypothesis_id": hypothesis.hypothesis_id, "statement": hypothesis.statement,
            "mechanism": hypothesis.mechanism, "chain_steps": hypothesis.chain_steps or [],
            "category": hypothesis.category, "rationale": hypothesis.rationale,
            "unknowns": hypothesis.unknowns, "generation_order": hypothesis.generation_order,
            "verdict": verdict, "confidence_basis": confidence_basis, "confidence_score": confidence_score,
            "synthesis_rank": synthesis_rank,
            "supporting_evidence": supporting_evidence,
            "contradicting_evidence": contradicting_evidence,
            "missing_evidence": missing_evidence,
        })

    strongest_verdict = None
    if rank_by_id:
        top_hypothesis_id = min(rank_by_id, key=rank_by_id.get)
        top_evaluation = investigation.evaluations.get(top_hypothesis_id)
        strongest_verdict = top_evaluation.verdict if top_evaluation else None

    artifact = {
        "investigation_id": investigation.investigation_id, "question": investigation.question,
        "company_ids": investigation.company_ids, "statement_type": statement_type,
        "strongest_explanation": synthesis.strongest_explanation if synthesis else None,
        "unanswered_questions": synthesis.unanswered_questions if synthesis else [],
        "additional_evidence_needed": synthesis.additional_evidence_needed if synthesis else [],
        "as_of": investigation.as_of,
        "hypotheses": hypotheses_json,
    }
    causal = _persist_causal_layer(conn, investigation, graph, hypotheses_json)
    artifact.update(causal["artifact_fields"])
    store = default_document_store()
    s3_key = investigation_artifact_key(investigation.investigation_id, 1)
    store.store(s3_key, json.dumps(artifact, indent=2).encode("utf-8"))
    for name, payload in causal["companion_files"].items():
        try:
            store.store(investigation_artifact_key(investigation.investigation_id, 1, name), json.dumps(payload, indent=2).encode("utf-8"))
        except Exception:  # noqa: BLE001 -- companions are additive, never fail the investigation
            logger.warning("Could not store %s for investigation %s", name, investigation.investigation_id, exc_info=True)
    abstract = generate_abstract(conn, synthesis.strongest_explanation if synthesis else None)
    fact_store.update_investigation_s3_metadata(
        conn, investigation.investigation_id, s3_key=s3_key, abstract=abstract,
        version=1, strongest_verdict=strongest_verdict,
    )
