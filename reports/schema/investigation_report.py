"""Normalized, presentation-agnostic report schema for Signal Research Reports.

    Investigation Engine (research/investigation.py, research/hypothesis_evaluator.py)
          v
    normalized InvestigationReport  <-- this module
          v
    Signal Report Design System (reports/components, reports/templates)
          v
    Web / Print PDF / future DOCX (reports/renderers)

Nothing in this module talks to a database, S3, or an LLM. It is pure data
(dataclasses) plus `from_investigation_data()`, an adapter that reshapes the
dict/row shapes web/app.py's `investigate_view` already assembles (see
storage/investigation_repository.py and research/hypothesis_evaluator.py's
HypothesisEvaluation/EvidenceItem dataclasses, and research/investigation.py's
`_persist()` for the exact persisted evidence-dict shape) into a stable
schema report components can render without knowing anything about how an
investigation was produced, scored, or stored.

Backward compatibility: every field below has a safe default. Older
investigations predate newer columns entirely (see storage/database.py's
incremental `_migrate_investigation_*` migrations) — the adapter fills gaps
with None/[] rather than raising, and report components (reports/components/)
render a field only when it's present. A hypothesis with confidence_score is
None is not an error; it renders as "Unscored", never as a crash on a None
comparison against the leading/contested thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Score is the evaluator's own 0-100 read on how strongly the evidence IT
# reviewed supports the stated verdict — NOT a probability the hypothesis is
# true, and not comparable across investigations (see the methodology
# component, reports/components/methodology.html, for the exact caveat shown
# to users).
SCORE_LEADING_MIN = 60
SCORE_CONTESTED_MIN = 30

SCORE_BAND_LABELS: dict[str, str] = {
    "leading": "Leading",
    "contested": "Contested",
    "weak": "Weak",
    "unscored": "Unscored",
}


def score_band(score: int | float | None) -> str:
    """Map a hypothesis's confidence_score to a display band.

    None (no score returned by the evaluator, or a hypothesis generated
    before scoring existed) maps to "unscored" — never compared against the
    numeric thresholds, so this never raises on a None score.
    """
    if score is None:
        return "unscored"
    if score >= SCORE_LEADING_MIN:
        return "leading"
    if score >= SCORE_CONTESTED_MIN:
        return "contested"
    return "weak"


@dataclass
class ReportMetadata:
    """Report letterhead: identifies the report and where it came from."""

    report_id: str
    title: str
    company_ids: list[str] = field(default_factory=list)
    statement_type: str | None = None
    generated_at: str | None = None
    as_of: str | None = None
    hypothesis_count: int = 0
    cost_usd: float | None = None
    cost_calls: int | None = None


@dataclass
class EvidenceRow:
    """One row of a hypothesis's evidence matrix.

    `status` is exactly one of "have" (supporting), "contradicts", or
    "missing" — the three states the evidence-matrix component renders as
    checkmark / warning / hollow-circle. `kind` is one of
    config.knowledge_ontology's CLAIM_TYPES (FACT, CALCULATION, ...) and is
    optional — missing-evidence rows in older data may not carry one.
    """

    status: str
    label: str = ""
    kind: str | None = None
    value: str | None = None
    citation: str | None = None


@dataclass
class KeyMetric:
    """A single headline number in the key-metrics strip.

    Not produced by today's investigation pipeline (which reasons over facts
    without picking out a fixed "headline metrics" set) — the field exists
    so a future producer (or a hand-authored addition) has somewhere to put
    one; the metric_cards component renders nothing when the list is empty.
    """

    label: str
    value: str
    delta: str | None = None
    tone: str | None = None  # "positive" | "negative" | "neutral"


@dataclass
class TrendPoint:
    """A single labeled point in the trend snapshot strip. See KeyMetric's
    docstring — not populated by today's pipeline, renders only when present.
    """

    label: str
    value: str


@dataclass
class Hypothesis:
    """One hypothesis, normalized from research/hypothesis_generator.py's
    Hypothesis + research/hypothesis_evaluator.py's HypothesisEvaluation as
    persisted by research/investigation.py's `_persist()`.
    """

    hypothesis_id: str
    claim: str
    rank: int | None = None
    category: str | None = None
    verdict: str | None = None
    score: int | float | None = None
    assessment: str | None = None
    #: Short causal stages ("A", "B", "C", ...) rendered as a connected
    #: chain. Empty for hypotheses generated before this existed (Step 2E)
    #: — `mechanism` below is the pre-2E prose fallback, never fabricated.
    causal_chain: list[str] = field(default_factory=list)
    mechanism: str | None = None
    supporting_evidence: list[EvidenceRow] = field(default_factory=list)
    contradicting_evidence: list[EvidenceRow] = field(default_factory=list)
    missing_evidence: list[EvidenceRow] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        return score_band(self.score)

    @property
    def band_label(self) -> str:
        return SCORE_BAND_LABELS[self.band]


@dataclass
class InvestigationReport:
    """The full normalized report. See module docstring for where this sits
    in the data flow."""

    metadata: ReportMetadata
    research_question: str
    executive_summary: str | None = None
    strongest_explanation: str | None = None
    overall_verdict: str | None = None
    key_metrics: list[KeyMetric] = field(default_factory=list)
    trend_snapshot: list[TrendPoint] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)
    additional_evidence_needed: list[str] = field(default_factory=list)
    #: Free-text override for the methodology section; None (the normal
    #: case) means the methodology component renders its own standard
    #: explanation of the pipeline rather than anything investigation-specific.
    methodology: str | None = None


def _adapt_evidence(items: list | None, status: str) -> list[EvidenceRow]:
    """Adapts a persisted evidence list (list[dict] with stance/kind/label/
    value/citation keys — research/investigation.py's `_persist_evidence_item`
    / missing_evidence dict shape) into EvidenceRow objects. Tolerates plain
    strings too, since some legacy/synthetic callers pass label-only lists.
    """
    rows: list[EvidenceRow] = []
    for item in items or []:
        if isinstance(item, dict):
            rows.append(
                EvidenceRow(
                    status=status,
                    label=item.get("label") or "",
                    kind=item.get("kind"),
                    value=item.get("value"),
                    citation=item.get("citation"),
                )
            )
        else:
            rows.append(EvidenceRow(status=status, label=str(item)))
    return rows


def _adapt_hypothesis(h: dict) -> Hypothesis:
    chain_steps = list(h.get("chain_steps") or [])
    mechanism = None if chain_steps else h.get("mechanism")
    assessment = h.get("confidence_basis") or h.get("rationale")
    return Hypothesis(
        hypothesis_id=str(h.get("hypothesis_id") or ""),
        claim=h.get("statement") or h.get("claim") or "",
        rank=h.get("synthesis_rank"),
        category=h.get("category"),
        verdict=h.get("verdict"),
        score=h.get("confidence_score"),
        assessment=assessment,
        causal_chain=chain_steps,
        mechanism=mechanism,
        supporting_evidence=_adapt_evidence(h.get("supporting_evidence"), "have"),
        contradicting_evidence=_adapt_evidence(h.get("contradicting_evidence"), "contradicts"),
        missing_evidence=_adapt_evidence(h.get("missing_evidence"), "missing"),
        unknowns=list(h.get("unknowns") or []),
    )


def from_investigation_data(
    investigation: dict,
    hypotheses: list[dict] | None = None,
    cost: dict | None = None,
) -> InvestigationReport:
    """Builds an InvestigationReport from the exact shapes web/app.py's
    `investigate_view` already assembles from either the S3 artifact
    (research/investigation.py's `_persist()`) or, for pre-S3-migration
    investigations, the SQLite/Postgres table read. Read-only, pure —
    performs no I/O and duplicates no research logic; it only renames/
    regroups fields that already exist.

    `investigation` keys used (all optional except question):
        investigation_id, question, company_ids, statement_type,
        strongest_explanation, unanswered_questions,
        additional_evidence_needed, generated_at, as_of.
    `hypotheses` is a list of dicts shaped like research/investigation.py's
    `_persist()` hypotheses_json entries (see that function for the exact
    keys). `cost` is the dict returned by
    storage/investigation_repository.py's cost-summary helper, or None.
    """
    hypotheses = hypotheses or []

    metadata = ReportMetadata(
        report_id=str(investigation.get("investigation_id") or ""),
        title=investigation.get("question") or "Untitled investigation",
        company_ids=list(investigation.get("company_ids") or []),
        statement_type=investigation.get("statement_type"),
        generated_at=investigation.get("generated_at"),
        as_of=investigation.get("as_of"),
        hypothesis_count=len(hypotheses),
        cost_usd=(cost or {}).get("cost_usd"),
        cost_calls=(cost or {}).get("calls"),
    )

    report_hypotheses = [_adapt_hypothesis(h) for h in hypotheses]

    top_hypothesis = next((h for h in report_hypotheses if h.rank == 1), None)
    overall_verdict = top_hypothesis.verdict if top_hypothesis else None

    strongest_explanation = investigation.get("strongest_explanation")

    return InvestigationReport(
        metadata=metadata,
        research_question=investigation.get("question") or "",
        executive_summary=strongest_explanation,
        strongest_explanation=strongest_explanation,
        overall_verdict=overall_verdict,
        key_metrics=[],
        trend_snapshot=[],
        hypotheses=report_hypotheses,
        follow_up_questions=list(investigation.get("unanswered_questions") or []),
        additional_evidence_needed=list(investigation.get("additional_evidence_needed") or []),
        methodology=None,
    )
