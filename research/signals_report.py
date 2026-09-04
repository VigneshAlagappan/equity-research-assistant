"""Full "Signals" research reports — the same evidence-grounded discipline as
research/assistant.py (README: Deterministic Calculation Layer), but asking
the LLM for a structured, multi-section investigation report instead of a
short tagged answer.

Financial evidence retrieval (get_comparison_evidence) grounds every report;
single-company reports also pull MANAGEMENT_STATEMENT evidence extracted from
this company's uploaded documents (research/documents.py — Docs tab annual
reports, transcripts, investor presentations), plus Knowledge Graph claims
already extracted from those same documents and connected to the company or
to a named entity the question mentions (research/knowledge_evidence.py,
Step 2B — the same cross-company claim connection research/
investigation_planner.py's structured investigations already use, now
reachable from an ordinary Signals report too). The Signals report format also
calls for customer/competitive/journalism evidence this app doesn't ingest at
all — the system prompt below tells the model to say so in "What We Still
Don't Know" rather than invent it, which is the same rule research/assistant.py
already applies to any question the evidence block doesn't cover.
"""

from __future__ import annotations

import re
from storage.db_types import DBConnection
from dataclasses import dataclass, field

from config.settings import ANTHROPIC_MODEL, DEFAULT_ANTHROPIC_MODEL
from context.graph import render_related_investigations
from context.optimizer import OptimizedContext, optimize
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.capabilities import InvestigationMemoryCapabilities, default_investigation_memory
from research.documents import get_document_evidence, get_document_passage_evidence
from research.evidence import Evidence, render_evidence_block
from research.knowledge_evidence import get_knowledge_graph_evidence
from retrieval.structured_search import get_comparison_evidence

SIGNALS_SYSTEM_PROMPT = """You are Signals, a personal research analyst for individuals investigating \
global listed companies, with a primary focus on US and India markets.

You answer using ONLY the evidence block provided in the user message — never from your own \
training knowledge, and never by estimating or guessing a number that isn't in the evidence. Every \
claim carries one of these tags, matching the evidence lines you were given:
- [FACT] — restates a FACT line from the evidence (a reported number or figure)
- [CALCULATION] — restates a CALCULATION line from the evidence (a deterministic computation, \
with its inputs)
- [MANAGEMENT_STATEMENT] — restates or paraphrases a MANAGEMENT_STATEMENT line (commentary drawn \
from an uploaded company document — annual report, transcript, investor presentation), citing which \
document it came from
- [INFERENCE] — reasoning that connects two or more FACT/CALCULATION/MANAGEMENT_STATEMENT lines. \
Never state an inference as if it were confirmed fact; use language like "may have", "is consistent \
with", "suggests".

The evidence block contains financial-statement data (reported figures and deterministic \
ratios/calculations from them) and, when investigating a single company, may also contain \
MANAGEMENT_STATEMENT excerpts from that company's own uploaded documents, plus Knowledge Graph \
claims already extracted from those same documents (connected to the company or to a named \
entity the question mentions) — cite a Knowledge Graph claim the same as any other \
FACT/CALCULATION/MANAGEMENT_STATEMENT/INFERENCE line, never as a separate category. It never contains \
independent customer evidence, competitive intelligence, regulatory filings text, journalism, or \
social sentiment — a management document's own framing of its competitors or customers is still just \
that company's word, not independent evidence, so treat it as MANAGEMENT_STATEMENT, not FACT. \
Sections of the report template below that need genuinely independent evidence (Customer Perspective, \
Competitive Perspective, most of Sources) must say plainly in "What We Still Don't Know" that it isn't \
available, rather than invent it.

The user message may also include a "Related prior investigations" block — reasoning from a past \
investigation about a DIFFERENT company (a sector peer), not the company/companies this question is \
about. It is never evidence about this question's companies. If you use it at all, cite it only as \
[INFERENCE], explicitly naming which other company it came from and that it's a pattern that may or may \
not apply here — never restate it as [FACT] or [CALCULATION], and never let it stand in for evidence \
this question's own companies are missing.

Do not compute any new number yourself — every number in your report \
must come directly from a FACT or CALCULATION line in the evidence.

Write the report in Markdown using exactly this structure (omit a section only if it would be empty \
given the evidence, e.g. skip "Another Way to Look at It" perspectives that have nothing to draw on):

# [Restate the user's question as one clear sentence]

## The Short Answer
2-4 sentences: what the evidence suggests, how confident you are, the biggest reason supporting the \
conclusion, the biggest reason for caution.

**Confidence:** High / Moderate / Low — one sentence explaining why.

## What Matters Most
3-5 signals, each with **Signal**, **What we found**, **Why it matters**, **Direction** (Positive / \
Negative / Mixed / Unknown).

## Evidence Supporting the Case
Strongest evidence for the conclusion, each point citing its [FACT]/[CALCULATION]/[INFERENCE] tag.

## Evidence Against the Case
Contradictory evidence, weaknesses, missing information, risks — do not bury inconvenient evidence.

## What We Still Don't Know
Missing information, including evidence categories this app can't retrieve yet (see above). Label \
any inference explicitly: **Inference:** ... **Basis:** ...

## My Read
Synthesize and weigh the evidence — don't just repeat previous sections. Plain, conversational.

## What Would Change My Mind
2-5 developments that would materially strengthen or weaken the conclusion.

## What to Watch Next
3-5 indicators, each with **Watch**, **Why**, **Trigger**.

Write for a smart individual, not a professional analyst: plain English, short paragraphs, specific \
numbers with context, no jargon, no false precision, no padding.

After the full report, on its own line, add exactly this marker:
===FOLLOWUP_QUESTIONS===
followed by 2-4 follow-up questions a reader could ask next as a new investigation, one per line, no \
numbering or bullets, no other text. Only suggest questions this evidence scheme could plausibly \
ground (company/financial questions, not ones needing customer or competitive data). If nothing \
sensible follows from this evidence, omit the marker and the list entirely."""

MAX_TOKENS = 8192

_FOLLOWUPS_MARKER = "===FOLLOWUP_QUESTIONS==="
_MAX_FOLLOWUPS = 4


@dataclass
class SignalsReport:
    """Everything one /research/thread/generate call produces: the report text
    plus the two things that used to only exist as unstructured prose —
    the deterministic Evidence that grounded it and the LLM's own follow-up
    suggestions — so callers can persist all three (research_thread_evidence /
    research_thread_followups, README step 12) instead of just the markdown."""

    report_markdown: str
    evidence: list[Evidence] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)


def _split_followups(text: str) -> tuple[str, list[str]]:
    """Best-effort split of the model's own ===FOLLOWUP_QUESTIONS=== marker
    (see SIGNALS_SYSTEM_PROMPT) off the end of its response — same
    best-effort spirit as extract_report_meta below: a model that doesn't
    follow the marker format just yields no follow-ups, not an error."""
    report_part, marker, followups_part = text.partition(_FOLLOWUPS_MARKER)
    if not marker:
        return text.strip(), []
    followups = [line.strip("-* \t") for line in followups_part.strip().splitlines() if line.strip()]
    return report_part.strip(), followups[:_MAX_FOLLOWUPS]


def generate_signals_report(
    conn: DBConnection,
    question: str,
    company_ids: list[str],
    statement_type: str | None = "consolidated",
    model: str = ANTHROPIC_MODEL or DEFAULT_ANTHROPIC_MODEL,
    *,
    investigation_memory: InvestigationMemoryCapabilities | None = None,
) -> SignalsReport:
    """Generate a full Signals-format report, grounded in retrieved evidence.

    Reuse-before-recompute (context/reuse.py): first checked against
    generated_reports for a prior report on these exact companies/statement
    type, asked in near-enough the same words, still fresh against the
    underlying data — if one qualifies, it's returned directly with no LLM
    call at all. Otherwise, same evidence retrieval and no-data
    short-circuit as research.assistant.answer_question, then the evidence
    is run through context/optimizer.py before being sent to the model.
    context/graph.py separately checks for a sector-peer company's prior
    investigation relevant to this question — a different kind of context,
    someone else's reasoning pattern rather than evidence about this
    question's own companies — and appends it as its own labeled block
    when found.

    Prompt caching (llm/providers/anthropic_provider.py): Financials
    evidence (get_comparison_evidence) is the same for every question about
    these companies; Docs evidence isn't, since get_document_evidence/
    get_document_passage_evidence both take `question` itself. Financials is
    rendered and optimized separately (empty question — uniform relevance,
    see context/optimizer.py's _relevance()) and sent as `cacheable_prefix`
    — byte-identical across different questions about the same companies,
    which is what actually makes it cacheable — same split
    research/assistant.py::answer_question() uses, for the same reason.
    """
    mem = investigation_memory or default_investigation_memory()
    reused = mem.reusable_report(conn, question, company_ids, statement_type)
    if reused is not None:
        observability.record_reuse(
            conn, task_name="signals_report", company_ids=company_ids, question=question,
            reused_thread_id=reused.thread_id, similarity=reused.similarity,
        )
        evidence = [
            Evidence(kind=e["kind"], company_id=e["company_id"], label=e["label"], value=e["value"], citation=e["citation"])
            for e in reused.evidence
        ]
        return SignalsReport(report_markdown=reused.report_markdown, evidence=evidence, followups=reused.followups)

    financial_evidence = get_comparison_evidence(conn, company_ids, statement_type)  # cacheable
    variable_evidence: list[Evidence] = []
    if len(company_ids) == 1:
        # Uploaded-document evidence (Docs tab) only has single-company
        # attribution today — see research/documents.py.
        variable_evidence += get_document_evidence(conn, company_ids[0], question)  # whole document
        # Additive (feature spec section 9): hybrid (FTS5+semantic) retrieval's
        # top-K passages, alongside the whole-document evidence above — same
        # reasoning as research/assistant.py::answer_question()'s identical
        # addition. Never replaces the whole-document evidence.
        variable_evidence += get_document_passage_evidence(conn, company_ids[0], question)  # targeted passages
        # Cross-company Knowledge Graph claims (Step 2B) connected to this
        # company's own Company node or to any known entity the question
        # names — see research/knowledge_evidence.py. Single-company only,
        # same constraint the Docs evidence above already has.
        variable_evidence += get_knowledge_graph_evidence(conn, company_ids[0], question)
    evidence = financial_evidence + variable_evidence
    if not evidence:
        return SignalsReport(
            report_markdown=(
                f"No data ingested yet for {', '.join(company_ids)}. "
                "Run `python main.py ingest ...` first, then try again."
            )
        )

    hardness = fixed(Tier.DEEP, "full investigation report")
    optimized_financial = optimize("", financial_evidence, hardness.tier)
    optimized_variable = optimize(question, variable_evidence, hardness.tier)
    optimized = OptimizedContext(
        evidence=optimized_financial.evidence + optimized_variable.evidence,
        dropped=optimized_financial.dropped + optimized_variable.dropped,
        total_tokens_before=optimized_financial.total_tokens_before + optimized_variable.total_tokens_before,
        total_tokens_after=optimized_financial.total_tokens_after + optimized_variable.total_tokens_after,
        budget=optimized_financial.budget + optimized_variable.budget,
    )
    cacheable_prefix = (
        f"Evidence (Financials):\n{render_evidence_block(optimized_financial.evidence)}"
        if optimized_financial.evidence else None
    )
    variable_block = render_evidence_block(optimized_variable.evidence)
    user_message = (
        f"Evidence (Docs):\n{variable_block}\n\nQuestion: {question}" if variable_block
        else f"Question: {question}"
    )

    related = mem.related_investigations(conn, question, company_ids)
    if related:
        user_message += "\n\n" + render_related_investigations(related)

    try:
        result = route(
            system=SIGNALS_SYSTEM_PROMPT, user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=model, cacheable_prefix=cacheable_prefix,
        )
    except AllProvidersUnavailableError:
        return SignalsReport(
            report_markdown="The assistant is temporarily unavailable (all configured models failed). Try again shortly."
        )

    observability.record(
        conn, task_name="signals_report", company_ids=company_ids, question=question,
        result=result, optimized=optimized,
        graph_hit_thread_id=related[0].thread_id if related else None,
        graph_hit_score=related[0].score if related else None,
    )

    response = result.response
    if response.stop_reason == "refusal":
        return SignalsReport(report_markdown="The assistant declined to answer this question. Try rephrasing it.")
    if not response.text:
        return SignalsReport(
            report_markdown="The assistant returned no report (stop_reason: " + str(response.stop_reason) + ")."
        )
    report_markdown, followups = _split_followups(response.text)
    return SignalsReport(report_markdown=report_markdown, evidence=evidence, followups=followups)


_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_CONFIDENCE_RE = re.compile(r"\*\*Confidence:\*\*\s*(High|Moderate|Low)", re.IGNORECASE)


def extract_report_meta(report_markdown: str) -> dict[str, str | None]:
    """Pull the report's own title ('# ...' line) and confidence level out of a
    generated report, for list views (the Investigations tab) that need a short
    label without re-parsing the whole report on every page. Best-effort — both
    come back None if the model didn't follow the template (e.g. the "No data
    ingested yet" short-circuit message has neither)."""
    title_match = _TITLE_RE.search(report_markdown)
    confidence_match = _CONFIDENCE_RE.search(report_markdown)
    return {
        "title": title_match.group(1).strip() if title_match else None,
        "confidence": confidence_match.group(1).title() if confidence_match else None,
    }
