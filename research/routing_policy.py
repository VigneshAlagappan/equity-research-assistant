"""Signals Complexity Classification and Execution Routing Policy
(docs/ADR/023-jev-llm-complexity-classification-and-routing.md) --
route_question() is the single entry point: classify a research question
with Jev (llm/complexity.py), then dispatch to the matching execution path:

  Level 1  Retrieve                Neon -> Answer
  Level 2  Retrieve + Calculate    Neon -> Calculation Code -> Answer
  Level 3  + Interpret             Neon -> Calculation Code -> configured LLM -> Answer
  Level 4  + Compare/Contextualize peer/macro grounding -> Neon -> Calculation -> LLM -> Answer
  Level 5  Hypothesis/Causal       research/investigation.py's existing hypothesis pipeline

Levels 1 and 2 are pure deterministic code -- no LLM call answers the
question itself (ADR-006). Each has a narrow, conservative extractor
(metric name, fiscal year, operation) matched only against the closed
metrics_dictionary vocabulary and explicit fiscal-year/operation wording;
when extraction can't confidently resolve what's being asked, the level
escalates to the next one rather than guessing (`_level1_retrieve`/
`_level2_calculate` return None to signal "escalate", never a fabricated
answer) -- this is the same "never invent missing data" rule the policy
states explicitly for Level 1, generalized as the escalation path's own
safety net.

Every call is audited end-to-end via llm/routing_audit.py, independent of
llm/observability.py's own per-model-call llm_call_log rows.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable
from storage.db_types import DBConnection

from config.settings import GRAPH_BACKEND, LEVEL_MODEL_CHAIN
from financials.calculations import CalculationError, MissingDataError, cagr_for_metric, format_currency_value, yoy_growth_for_metric
from financials.ratios import SectorMismatchError, roa_for_company, roe_for_company
from llm import observability
from llm.complexity import ComplexityClassification, ComplexityLevel, classify_complexity
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route_explicit_chain
from llm.routing_audit import RoutingAudit, new_run_id, persist
from normalization.periods import PeriodParseError, fiscal_year_number
from research.assistant import SYSTEM_PROMPT, InsufficientEvidenceError, gather_evidence
from research.evidence import render_evidence_block
from research.investigation import InvestigationError, run_investigation
from research.peer_resolver import resolve_comparison_group
from storage.repositories import get_canonical_series, get_canonical_value, list_all_metrics

MAX_TOKENS = 4096

# Short, human-facing names for each level -- used wherever a level needs to
# be shown to a user (web/app.py's /research/understand preview, the Cases
# list's per-entry tag and its Level filter dropdown) rather than just the
# bare integer.
LEVEL_LABELS: dict[int, str] = {
    1: "Retrieve", 2: "Calculate", 3: "Interpret", 4: "Compare", 5: "Hypothesize",
}


def case_type_for_level(level: int) -> str:
    """Which research_cases `kind` a Jev level dispatches to -- Level 5
    (Hypothesize) is the only one that needs the full hypothesis-driven
    investigation pipeline (research/investigation.py); everything else is
    answered through the single-pass assistant pipeline
    (research/assistant.py). This is now what decides "ask" vs
    "investigation" in the live app (web/app.py), replacing the old
    client-side Quick Answer/Deep Dive toggle -- see docs/ADR/023."""
    return "investigation" if level == ComplexityLevel.HYPOTHESIZE else "ask"


def classify_and_log(conn: DBConnection, question: str, company_ids: list[str]) -> ComplexityClassification:
    """Jev classification alone, logged into llm_call_log the same way
    route_question() logs its own Jev call -- for callers (web/app.py) that
    need the classification up front to decide which case/pipeline to run
    (Level 5 -> investigation, everything else -> ask), and to pass into
    attempt_deterministic_level() afterward for Levels 1/2 -- without
    route_question()'s own second classify_complexity() call or its Level
    3-5 dispatch."""
    classification, jev_route_result = classify_complexity(question, company_ids)
    if jev_route_result is not None:
        observability.record(
            conn, task_name="jev_complexity_classifier", company_ids=company_ids, question=question,
            result=jev_route_result,
        )
    return classification


# ------------------------------------------------------------------
# Level 1/2 deterministic extraction -- closed-vocabulary keyword matching
# only, never an LLM. Anything this can't confidently resolve escalates
# rather than guesses (module docstring).
# ------------------------------------------------------------------

_FISCAL_YEAR_RE = re.compile(r"\bFY\s?-?(\d{4})\b", re.IGNORECASE)
_BARE_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_CAGR_RE = re.compile(r"\bcagr\b", re.IGNORECASE)
_YOY_RE = re.compile(r"\b(yoy|year[\s-]over[\s-]year|y-o-y)\b", re.IGNORECASE)
_GROWTH_RE = re.compile(r"\bgrowth\b", re.IGNORECASE)
_NUM_YEARS_RE = re.compile(r"\b(\d{1,2})[\s-]year", re.IGNORECASE)
_CONFIDENCE_LINE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Moderate|Low)", re.IGNORECASE)

# Common abbreviations analysts actually type, layered on top of whatever
# metrics_dictionary itself already has as metric_key/display_name --
# storage.repositories.list_all_metrics() is the closed, DB-driven
# vocabulary; this dict only widens the surface form each entry can be
# recognized by, it never introduces a metric that isn't already on file.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "return_on_equity_percent": ("roe", "return on equity"),
    "return_on_assets_percent": ("roa", "return on assets"),
    "net_interest_margin": ("nim", "net interest margin"),
    "net_profit": ("net profit", "pat", "profit after tax"),
    "total_revenue": ("revenue", "total revenue", "total income"),
    "gross_npa_percent": ("gnpa", "gross npa"),
    "net_npa_percent": ("nnpa", "net npa"),
    "earnings_per_share": ("eps", "earnings per share"),
}

# Ratios not every vendor reports directly (Screener et al. sometimes omit
# ROE/ROA even though every input they're derived from -- net_profit,
# total_assets, total_shareholders_funds -- is on file), but which
# financials/ratios.py already knows how to compute deterministically from
# canonical_financials alone: no LLM, same "stable rule -> code, not a model"
# reasoning as CAGR/YoY above (ADR-006). _level1_retrieve consults this to
# decide whether a missing direct value should escalate to Level 2 (might be
# derivable) rather than give up with "no data on file"; _level2_calculate
# consults it to actually derive the value. Only ROE/ROA are wired here --
# financials/ratios.py's nim()/gnpa_ratio()/net_profit_margin() have no
# per-company DB-wired wrapper yet (unlike roe_for_company/roa_for_company,
# which are already used by financials/report.py, charts/financial_charts.py,
# and web/valuation_feed.py), so adding them here is future work, not a gap
# in this pass.
_DERIVED_RATIO_CALCULATORS: dict[str, Callable] = {
    "return_on_equity_percent": roe_for_company,
    "return_on_assets_percent": roa_for_company,
}


def _extract_fiscal_year(question: str) -> str | None:
    match = _FISCAL_YEAR_RE.search(question)
    if match:
        return f"FY{match.group(1)}"
    match = _BARE_YEAR_RE.search(question)
    return f"FY{match.group(1)}" if match else None


def _extract_metric_key(conn: DBConnection, question: str) -> str | None:
    """None both when nothing matches AND when more than one metric matches
    (an ambiguous question) -- either way, Level 1/2 must escalate rather
    than guess which one was meant."""
    text = question.lower()
    matches: set[str] = set()
    for metric_key, display_name in list_all_metrics(conn):
        candidates = {metric_key.replace("_", " "), (display_name or "").lower()}
        candidates.update(_METRIC_ALIASES.get(metric_key, ()))
        if any(name and name in text for name in candidates):
            matches.add(metric_key)
    return matches.pop() if len(matches) == 1 else None


def _latest_fiscal_year(conn: DBConnection, company_id: str, metric_key: str, statement_type: str | None) -> str | None:
    series = get_canonical_series(conn, company_id, metric_key, "annual", statement_type)
    return series[-1]["fiscal_year"] if series else None


@dataclass
class LevelOutcome:
    answer: str
    data_sources: list[str] = field(default_factory=list)
    neo4j_used: bool = False
    planner_used: bool = False
    tools_executed: list[str] = field(default_factory=list)
    calculations_performed: list[str] = field(default_factory=list)
    evidence_identifiers: list[str] = field(default_factory=list)
    missing_data_issues: list[str] = field(default_factory=list)
    final_confidence: str | None = None
    execution_status: str = "answered"  # answered | insufficient_data | error
    model_used: str | None = None
    fallback_model_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    answer_reference: str | None = None


def _level1_retrieve(conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None) -> LevelOutcome | None:
    if len(company_ids) != 1 or _CAGR_RE.search(question) or _YOY_RE.search(question) or _GROWTH_RE.search(question):
        return None  # a single company only; any growth/CAGR wording is Level 2's job
    metric_key = _extract_metric_key(conn, question)
    fiscal_year = _extract_fiscal_year(question)
    if metric_key is None or fiscal_year is None:
        return None

    company_id = company_ids[0]
    row = get_canonical_value(conn, company_id, metric_key, "annual", fiscal_year, None, statement_type)
    label = metric_key.replace("_", " ")
    if row is None:
        if metric_key in _DERIVED_RATIO_CALCULATORS:
            return None  # not vendor-reported, but Level 2 may be able to derive it (e.g. ROE from net_profit/equity)
        return LevelOutcome(
            answer=f"No reported {label} on file for {company_id} in {fiscal_year}.",
            data_sources=["neon:canonical_financials"],
            missing_data_issues=[f"no canonical_financials row for {company_id}/{metric_key}/{fiscal_year}"],
            execution_status="insufficient_data",
        )
    return LevelOutcome(
        answer=f"{label.title()} for {company_id} in {fiscal_year}: {format_currency_value(row['canonical_value'], row['unit'])}. "
        f"[FACT] source: canonical_financials.",
        data_sources=["neon:canonical_financials"],
        evidence_identifiers=[f"canonical_financials:{company_id}:{metric_key}:{fiscal_year}:{statement_type}"],
        execution_status="answered",
    )


def _level2_calculate(conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None) -> LevelOutcome | None:
    if len(company_ids) != 1:
        return None
    metric_key = _extract_metric_key(conn, question)
    if metric_key is None:
        return None
    company_id = company_ids[0]
    label = metric_key.replace("_", " ")

    if _CAGR_RE.search(question):
        num_years_match = _NUM_YEARS_RE.search(question)
        if num_years_match is None:
            return None  # no explicit timeframe -> can't resolve without guessing
        end_fy = _extract_fiscal_year(question) or _latest_fiscal_year(conn, company_id, metric_key, statement_type)
        if end_fy is None:
            return None
        try:
            start_fy = f"FY{fiscal_year_number(end_fy) - int(num_years_match.group(1))}"
            result = cagr_for_metric(conn, company_id, metric_key, start_fy, end_fy, statement_type=statement_type)
        except (CalculationError, MissingDataError, PeriodParseError) as exc:
            return LevelOutcome(
                answer=f"Could not calculate {label} CAGR for {company_id}: {exc}",
                data_sources=["neon:canonical_financials"], missing_data_issues=[str(exc)],
                execution_status="insufficient_data",
            )
        return LevelOutcome(
            answer=f"{result.label} for {company_id}: {result.value:.1f}%. [CALCULATION] {result.explanation}",
            data_sources=["neon:canonical_financials"], calculations_performed=[result.label],
            evidence_identifiers=[f"canonical_financials:{company_id}:{metric_key}:{start_fy}..{end_fy}:{statement_type}"],
            execution_status="answered",
        )

    if _YOY_RE.search(question) or _GROWTH_RE.search(question):
        fiscal_year = _extract_fiscal_year(question) or _latest_fiscal_year(conn, company_id, metric_key, statement_type)
        if fiscal_year is None:
            return None
        try:
            result = yoy_growth_for_metric(conn, company_id, metric_key, fiscal_year, statement_type=statement_type)
        except (CalculationError, MissingDataError, PeriodParseError) as exc:
            return LevelOutcome(
                answer=f"Could not calculate {label} YoY growth for {company_id}: {exc}",
                data_sources=["neon:canonical_financials"], missing_data_issues=[str(exc)],
                execution_status="insufficient_data",
            )
        return LevelOutcome(
            answer=f"{result.label} for {company_id}: {result.value:.1f}%. [CALCULATION] {result.explanation}",
            data_sources=["neon:canonical_financials"], calculations_performed=[result.label],
            evidence_identifiers=[f"canonical_financials:{company_id}:{metric_key}:{fiscal_year}:{statement_type}"],
            execution_status="answered",
        )

    if metric_key in _DERIVED_RATIO_CALCULATORS:
        fiscal_year = _extract_fiscal_year(question)
        if fiscal_year is None:
            return None  # no explicit period -> can't resolve without guessing
        try:
            result = _DERIVED_RATIO_CALCULATORS[metric_key](conn, company_id, fiscal_year, statement_type=statement_type)
        except (MissingDataError, SectorMismatchError) as exc:
            return LevelOutcome(
                answer=f"Could not calculate {label} for {company_id}: {exc}",
                data_sources=["neon:canonical_financials"], missing_data_issues=[str(exc)],
                execution_status="insufficient_data",
            )
        return LevelOutcome(
            answer=f"{result.label} for {company_id}: {result.value:.2f}%. [CALCULATION] {result.explanation}",
            data_sources=["neon:canonical_financials"], calculations_performed=[result.label],
            evidence_identifiers=[f"canonical_financials:{company_id}:{metric_key}:{fiscal_year}:{statement_type}"],
            execution_status="answered",
        )
    return None


# ------------------------------------------------------------------
# Levels 3-5 -- an LLM interprets already-deterministic evidence. Reuses
# research/assistant.py's evidence-gathering (gather_evidence) and system
# prompt (SYSTEM_PROMPT), but routes the actual model call through this
# policy's own configured chain (config.settings.LEVEL_MODEL_CHAIN) rather
# than assistant.answer_question's tier-derived auto-routing -- see
# config.settings' own comment on why Levels 3/4 need a distinct,
# explicitly-ordered, Anthropic-fallback chain.
# ------------------------------------------------------------------


def _extract_confidence(text: str) -> str | None:
    match = _CONFIDENCE_LINE_RE.search(text)
    return match.group(1).title() if match else None


def _no_evidence_message(company_ids: list[str]) -> str:
    if company_ids:
        return (
            f"No data ingested yet for {', '.join(company_ids)}. "
            "Run `python main.py ingest ...` first, then try again."
        )
    return (
        "No matching evidence found for this question. Name a company to ground it in that "
        "company's Financials/Docs, or ask about a macro topic that's been ingested "
        "(e.g. rainfall, repo rate, credit growth)."
    )


def _coarse_data_sources(company_ids: list[str], financial_evidence: list, variable_evidence: list) -> list[str]:
    """A deliberately coarse-grained list (which SUBSYSTEMS were queried),
    not a per-evidence-item source trace -- good enough for an audit record
    of what ran, without requiring research/evidence.py's Evidence dataclass
    to carry a new field just for this."""
    sources: list[str] = []
    if financial_evidence:
        sources.append("neon:canonical_financials")
    if variable_evidence:
        if len(company_ids) == 1:
            sources.append("neon:documents")
            sources.append("neo4j:knowledge_graph" if GRAPH_BACKEND == "neo4j" else "sqlite:knowledge_graph")
        sources.append("neon:macro_observations")
    return sources


_LEVEL3_ADDENDUM = """

LEVEL 3 SCOPE: interpret trends, consistency, acceleration/slowdown, volatility, and unusual \
periods strictly WITHIN the evidence above. Do NOT introduce industry, peer, macro, or other \
external comparisons that are not already present in the evidence block. Do not use unsupported \
model knowledge as evidence."""

_LEVEL4_ADDENDUM = """

LEVEL 4 SCOPE: you may compare ONLY the companies/datasets explicitly present in the evidence \
above (the question's own company plus any peer companies or macro series listed) -- cite each \
one's figures separately, never blend them into a single number. {limitation}Do not invent a peer, \
benchmark, or relationship that is not present in the evidence."""


def _level3_interpret(
    conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None, model_override: str | None,
) -> LevelOutcome:
    financial_evidence, variable_evidence = gather_evidence(conn, question, company_ids, statement_type)
    evidence = financial_evidence + variable_evidence
    if not evidence:
        return LevelOutcome(answer=_no_evidence_message(company_ids), execution_status="insufficient_data")

    system = SYSTEM_PROMPT + _LEVEL3_ADDENDUM
    user_message = f"Evidence:\n{render_evidence_block(evidence)}\n\nQuestion: {question}"
    hardness = fixed(Tier.STANDARD, "Signals Level 3 (Retrieve+Calculate+Interpret)")
    chain = [model_override] if model_override else LEVEL_MODEL_CHAIN[3]
    try:
        result = route_explicit_chain(
            system=system, user_message=user_message, hardness=hardness, model_chain=chain, max_tokens=MAX_TOKENS,
        )
    except AllProvidersUnavailableError:
        return LevelOutcome(
            answer="The assistant is temporarily unavailable (all configured models failed).", execution_status="error",
        )

    observability.record(conn, task_name="signals_level3", company_ids=company_ids, question=question, result=result)
    response = result.response
    return LevelOutcome(
        answer=response.text or "The assistant returned no answer.",
        data_sources=_coarse_data_sources(company_ids, financial_evidence, variable_evidence),
        calculations_performed=[e.label for e in evidence if e.kind == "CALCULATION"],
        evidence_identifiers=[f"{e.kind}:{e.company_id}:{e.label}" for e in evidence],
        final_confidence=_extract_confidence(response.text or ""),
        execution_status="answered" if response.text else "error",
        model_used=response.model,
        fallback_model_used=result.attempts[-1].model if result.fallback_used else None,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
    )


def _level4_compare(
    conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None, model_override: str | None,
) -> LevelOutcome:
    resolution = resolve_comparison_group(conn, company_ids, question)
    expanded_company_ids = list(dict.fromkeys(company_ids + resolution.peer_company_ids))
    financial_evidence, variable_evidence = gather_evidence(conn, question, expanded_company_ids, statement_type)
    evidence = financial_evidence + variable_evidence
    if not evidence:
        return LevelOutcome(
            answer=_no_evidence_message(expanded_company_ids), execution_status="insufficient_data",
            neo4j_used=resolution.neo4j_used, planner_used=resolution.planner_used, missing_data_issues=resolution.notes,
        )

    limitation = f"Limitation: {'; '.join(resolution.notes)}. " if resolution.notes else ""
    system = SYSTEM_PROMPT + _LEVEL4_ADDENDUM.format(limitation=limitation)
    user_message = f"Evidence:\n{render_evidence_block(evidence)}\n\nQuestion: {question}"
    hardness = fixed(Tier.DEEP, "Signals Level 4 (Compare+Contextualize)")
    chain = [model_override] if model_override else LEVEL_MODEL_CHAIN[4]
    try:
        result = route_explicit_chain(
            system=system, user_message=user_message, hardness=hardness, model_chain=chain, max_tokens=MAX_TOKENS,
        )
    except AllProvidersUnavailableError:
        return LevelOutcome(
            answer="The assistant is temporarily unavailable (all configured models failed).", execution_status="error",
            neo4j_used=resolution.neo4j_used, planner_used=resolution.planner_used,
        )

    observability.record(
        conn, task_name="signals_level4", company_ids=expanded_company_ids, question=question, result=result,
    )
    response = result.response
    return LevelOutcome(
        answer=response.text or "The assistant returned no answer.",
        data_sources=_coarse_data_sources(expanded_company_ids, financial_evidence, variable_evidence),
        neo4j_used=resolution.neo4j_used, planner_used=resolution.planner_used,
        calculations_performed=[e.label for e in evidence if e.kind == "CALCULATION"],
        evidence_identifiers=[f"{e.kind}:{e.company_id}:{e.label}" for e in evidence],
        missing_data_issues=resolution.notes,
        final_confidence=_extract_confidence(response.text or ""),
        execution_status="answered" if response.text else "error",
        model_used=response.model,
        fallback_model_used=result.attempts[-1].model if result.fallback_used else None,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
    )


def _render_investigation(investigation) -> str:
    lines = [f"## Investigation: {investigation.question}", ""]
    synthesis = investigation.synthesis
    if synthesis and synthesis.strongest_explanation:
        lines += [f"**Strongest explanation:** {synthesis.strongest_explanation}", ""]

    rank_by_id = {hid: i + 1 for i, hid in enumerate(synthesis.ranked_hypothesis_ids)} if synthesis else {}
    lines.append("### Hypotheses considered")
    for hypothesis in investigation.hypotheses:
        evaluation = investigation.evaluations.get(hypothesis.hypothesis_id)
        verdict = evaluation.verdict if evaluation else "EVALUATION_FAILED"
        rank = rank_by_id.get(hypothesis.hypothesis_id)
        lines.append(f"- **{hypothesis.statement}** -- verdict: {verdict}" + (f" (rank {rank})" if rank else ""))

    if synthesis and synthesis.unanswered_questions:
        lines += ["", "### Unanswered questions"] + [f"- {q}" for q in synthesis.unanswered_questions]
    if synthesis and synthesis.additional_evidence_needed:
        lines += ["", "### Additional evidence needed"] + [f"- {q}" for q in synthesis.additional_evidence_needed]
    return "\n".join(lines)


def _level5_hypothesize(
    conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None,
    model_override: str | None, case_id: str | None,
) -> LevelOutcome:
    try:
        investigation = run_investigation(
            conn, question, company_ids, statement_type=statement_type, model=model_override, case_id=case_id,
        )
    except InvestigationError as exc:
        return LevelOutcome(answer=f"Could not complete this investigation: {exc}", execution_status="error")
    except InsufficientEvidenceError as exc:
        return LevelOutcome(answer=str(exc), execution_status="insufficient_data")

    top_confidence = None
    if investigation.synthesis and investigation.synthesis.ranked_hypothesis_ids:
        top_evaluation = investigation.evaluations.get(investigation.synthesis.ranked_hypothesis_ids[0])
        top_confidence = top_evaluation.confidence_basis if top_evaluation else None

    sources_queried: set[str] = set()
    evidence_ids: list[str] = []
    calculations: list[str] = []
    for plan in investigation.plans.values():
        sources_queried.update(plan.sources_queried)
        evidence_ids += [f"{e.kind}:{e.company_id}:{e.label}" for e in plan.evidence]
        calculations += [e.label for e in plan.evidence if e.kind == "CALCULATION"]

    missing: list[str] = [m for e in investigation.evaluations.values() for m in e.missing_evidence]
    missing += [f"hypothesis {hid} evaluation failed" for hid in investigation.failed_hypothesis_ids]

    return LevelOutcome(
        answer=_render_investigation(investigation),
        data_sources=sorted(sources_queried),
        neo4j_used=(GRAPH_BACKEND == "neo4j"), planner_used=True,
        tools_executed=["hypothesis_generator", "investigation_planner", "hypothesis_evaluator", "research_synthesis"],
        calculations_performed=calculations,
        evidence_identifiers=evidence_ids,
        missing_data_issues=missing,
        final_confidence=top_confidence,
        execution_status="answered",
        answer_reference=investigation.investigation_id,
    )


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingResult:
    answer: str
    classification: ComplexityClassification
    audit: RoutingAudit


def _dispatch_levels_1_2(
    conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None, level: int,
) -> tuple[LevelOutcome | None, int, list[str]]:
    """Runs Level 1, escalating to Level 2 if Level 1 returns None (can't
    confidently resolve), per the module's own "never guess, escalate
    instead" policy. Returns (outcome, level, escalation_notes) -- outcome
    is None only when Level 2 also returns None (escalate past Level 2,
    caller's responsibility), and `level` reflects the last level actually
    attempted (needed by route_question() to pick the right Level 3/4/5
    dispatch below when this escalates all the way past Level 2)."""
    escalation_notes: list[str] = []
    outcome: LevelOutcome | None = None

    if level == ComplexityLevel.RETRIEVE:
        outcome = _level1_retrieve(conn, question, company_ids, statement_type)
        if outcome is None:
            escalation_notes.append("Level 1 could not deterministically resolve a single metric/period -- escalated to Level 2")
            level = ComplexityLevel.CALCULATE
    if outcome is None and level == ComplexityLevel.CALCULATE:
        outcome = _level2_calculate(conn, question, company_ids, statement_type)
        if outcome is None:
            escalation_notes.append("Level 2 could not deterministically resolve a calculation -- escalated to Level 3")
            level = ComplexityLevel.INTERPRET

    return outcome, level, escalation_notes


def attempt_deterministic_level(
    conn: DBConnection, question: str, company_ids: list[str], classification: ComplexityClassification, *,
    statement_type: str | None = "consolidated",
) -> LevelOutcome | None:
    """The live web app's entry point into Levels 1/2 ONLY -- unlike
    route_question(), this never calls classify_complexity() itself (the
    caller has already classified the question, e.g. via classify_and_log(),
    and passes that same classification in here to avoid a second, wasted
    Jev call). Returns None whenever Level 1/2 doesn't apply at all
    (classification.level isn't 1 or 2, or company_ids isn't exactly one
    company -- Levels 1/2 are single-company only) or when Level 1/2 both
    escalate (couldn't confidently resolve) -- either way, the caller's own
    existing Level 3+ pipeline should run instead, and no signals_routing_log
    row is written here, since a partial row would misrepresent what
    actually ends up answering the question in that case. Only a REAL Level
    1/2 outcome gets one, matching what route_question() itself would have
    persisted for the same question."""
    level = int(classification.level)
    if level not in (ComplexityLevel.RETRIEVE, ComplexityLevel.CALCULATE) or len(company_ids) != 1:
        return None

    run_id = new_run_id()
    start = time.monotonic()
    outcome, _final_level, escalation_notes = _dispatch_levels_1_2(conn, question, company_ids, statement_type, level)
    if outcome is None:
        return None

    audit = RoutingAudit(
        run_id=run_id, question=question, company_ids=company_ids,
        jev_level=level, jev_confidence=classification.confidence,
        jev_reason=classification.reason, jev_source=classification.source,
        model_selected=outcome.model_used, fallback_model_used=outcome.fallback_model_used,
        data_sources=outcome.data_sources, neo4j_used=outcome.neo4j_used, planner_used=outcome.planner_used,
        tools_executed=outcome.tools_executed, calculations_performed=outcome.calculations_performed,
        evidence_identifiers=outcome.evidence_identifiers,
        missing_data_issues=escalation_notes + outcome.missing_data_issues,
        final_confidence=outcome.final_confidence, execution_status=outcome.execution_status,
        latency_ms=(time.monotonic() - start) * 1000,
        input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
        estimated_cost_usd=outcome.estimated_cost_usd, answer_reference=outcome.answer_reference,
    )
    persist(conn, audit)
    return outcome


def route_question(
    conn: DBConnection, question: str, company_ids: list[str], *,
    statement_type: str | None = "consolidated", model: str | None = None, case_id: str | None = None,
) -> RoutingResult:
    """Classify `question` with Jev, dispatch to that level's execution
    path, and persist one signals_routing_log audit row. `model` overrides
    the configured model chain for Levels 3/4 only (tests do this) -- Level
    5 threads it through to research/investigation.py's own `model` param
    instead, same meaning it already has there."""
    run_id = new_run_id()
    start = time.monotonic()

    classification, jev_route_result = classify_complexity(question, company_ids)
    if jev_route_result is not None:
        observability.record(
            conn, task_name="jev_complexity_classifier", company_ids=company_ids, question=question,
            result=jev_route_result,
        )

    outcome, level, escalation_notes = _dispatch_levels_1_2(
        conn, question, company_ids, statement_type, int(classification.level),
    )

    if outcome is None:
        if level == ComplexityLevel.INTERPRET:
            outcome = _level3_interpret(conn, question, company_ids, statement_type, model)
        elif level == ComplexityLevel.COMPARE:
            outcome = _level4_compare(conn, question, company_ids, statement_type, model)
        else:
            outcome = _level5_hypothesize(conn, question, company_ids, statement_type, model, case_id)

    audit = RoutingAudit(
        run_id=run_id, question=question, company_ids=company_ids,
        jev_level=int(classification.level), jev_confidence=classification.confidence,
        jev_reason=classification.reason, jev_source=classification.source,
        model_selected=outcome.model_used, fallback_model_used=outcome.fallback_model_used,
        data_sources=outcome.data_sources, neo4j_used=outcome.neo4j_used, planner_used=outcome.planner_used,
        tools_executed=outcome.tools_executed, calculations_performed=outcome.calculations_performed,
        evidence_identifiers=outcome.evidence_identifiers,
        missing_data_issues=escalation_notes + outcome.missing_data_issues,
        final_confidence=outcome.final_confidence, execution_status=outcome.execution_status,
        latency_ms=(time.monotonic() - start) * 1000,
        input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
        estimated_cost_usd=outcome.estimated_cost_usd, answer_reference=outcome.answer_reference,
    )
    persist(conn, audit)
    return RoutingResult(answer=outcome.answer, classification=classification, audit=audit)
