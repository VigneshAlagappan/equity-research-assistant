"""LLM research assistant — answers questions using only retrieved,
deterministic evidence.

    User -> LLM interprets request -> Retrieve financial observations
         -> Python calculation -> Validated result -> LLM explains result

(README: Deterministic Calculation Layer.) The LLM never performs a
calculation Python can do deterministically — every number in its answer
must restate a FACT/CALCULATION line from the evidence block, not be derived
by the model. No multi-agent framework, no tool-use loop: one call, evidence
in, cited answer out (README: High-Level Architecture — "no
orchestrator/planner agents ... one research assistant backed by
deterministic tools and retrieval is sufficient").
"""

from __future__ import annotations

from storage.db_types import DBConnection

from config.settings import ANTHROPIC_MODEL
from context.optimizer import OptimizedContext, optimize
from llm import observability
from llm.hardness import classify
from llm.router import TIER_PREFERRED_MODEL, AllProvidersUnavailableError, route
from research.capabilities import InvestigationMemoryCapabilities, default_investigation_memory
from research.documents import get_document_evidence, get_document_passage_evidence
from research.evidence import render_evidence_block
from research.macro_evidence import get_macro_evidence
from retrieval.structured_search import get_comparison_evidence

SYSTEM_PROMPT = """You are an equity research assistant for global listed companies, with a primary \
focus on US and India markets.

HARD RULE — evidence sources: the evidence block below is built ONLY from three \
sources: the company's ingested Financials (reported metrics, ratios, YoY/CAGR \
growth), its uploaded Docs (annual reports, transcripts, investor \
presentations), and Macro/regulatory data for India and the US (RBI, IITM \
rainfall, FRED — labeled with company_id "INDIA" or "USA" rather than a \
company ticker, since it isn't about any one company). You must answer using ONLY that evidence block — never from \
your own training knowledge, never by estimating or guessing a number that \
isn't in the evidence, and never by inventing a source, document, or figure \
that isn't present. Do not hallucinate. If the evidence doesn't cover what the \
question asks, say so plainly instead of filling the gap.

Every claim in your answer must carry one of these tags, matching the evidence \
lines you were given:
- [FACT] — restates a FACT line from the evidence (a reported number or figure)
- [CALCULATION] — restates a CALCULATION line from the evidence (a deterministic \
computation, with its inputs)
- [MANAGEMENT_STATEMENT] — restates or paraphrases a MANAGEMENT_STATEMENT line \
(commentary drawn from an uploaded company document — annual report, transcript, \
investor presentation), citing which document it came from
- [INFERENCE] — reasoning that connects two or more FACT/CALCULATION/MANAGEMENT_STATEMENT \
lines. Never state an inference as if it were confirmed fact; use language like "may have", \
"is consistent with", "suggests".

Do not compute any new number yourself — every number in your answer must come \
directly from a FACT or CALCULATION line in the evidence. If the evidence doesn't \
cover something the question asks about, say so plainly rather than guessing or \
filling the gap from general knowledge."""

MAX_TOKENS = 4096


def _select_model(question: str, company_ids: list[str], evidence_count: int) -> str:
    """Back-compat wrapper: which model llm/router.py's fallback chain would
    prefer for this question, per the tiering rules now shared by all three
    LLM call sites — see llm/hardness.py's classify() for the actual
    heuristic (peer comparison / deep-analysis wording / evidence volume /
    short factual lookup)."""
    return TIER_PREFERRED_MODEL[classify(question, company_ids, evidence_count).tier.value]


def answer_question(
    conn: DBConnection,
    question: str,
    company_ids: list[str],
    statement_type: str | None = "consolidated",
    model: str | None = None,
    *,
    investigation_memory: InvestigationMemoryCapabilities | None = None,
) -> str:
    """Answer a research question about one or more companies, and/or about
    macro/regulatory data (India or US), grounded in retrieved evidence.

    company_ids may be empty — a question with no company evidence can still
    be answered from Macro evidence alone (e.g. "what was rainfall in India
    over the last 50 years?" or "what's the Fed funds rate been since 2020?"),
    which is why this isn't rejected upfront.

    Returns a plain-text answer with [FACT]/[CALCULATION]/[MANAGEMENT_STATEMENT]/
    [INFERENCE] tags. Never calls the API if there's no evidence to ground an
    answer in. Backs every research-question entry point — the per-company Ask
    AI icon, /research/ask, and /chat.

    Model selection: pass `model` to pin a specific model for this call (tests
    do this). Otherwise, ANTHROPIC_MODEL (env var) pins one model for every
    call if the operator set it; if not, llm/router.py auto-routes to a tier
    per question (llm/hardness.py's classify) and falls back through other
    models/providers if the preferred one is unavailable — this is the
    default behavior.

    Evidence-source rule: the calls below (Financials + Docs + Macro) are the
    only evidence this function is allowed to gather — SYSTEM_PROMPT tells
    the model to answer from this evidence alone, so widening it further
    (e.g. pulling in Notes or News) changes what the model is allowed to
    cite and must be a deliberate choice, not an incidental one.
    get_document_passage_evidence() (hybrid FTS5+semantic retrieval,
    research/documents.py) is exactly such a deliberate widening — still
    "Docs" evidence, just the specific relevant passage instead of/alongside
    each whole document's opening text, per this feature's spec section 9.

    Reuse-before-recompute (context/reuse.py): first checked against
    generated_reports for a prior saved answer/report on these exact
    companies/statement type, asked in near-enough the same words, still
    fresh against the underlying data — if one qualifies, it's returned
    directly with no LLM call at all, same as
    research.signals_report.generate_signals_report already does for the
    full-report path. The web layer (web/app.py's _answer_question_response)
    always saves every answer_question() result into generated_reports, so
    this is what makes asking the same/near-same question twice free the
    second time, and is the mechanism that populates the Investigations list
    in the first place.

    Prompt caching (llm/providers/anthropic_provider.py): Financials
    evidence (get_comparison_evidence) is the same for every question about
    these companies — Docs/Macro evidence isn't, since get_document_evidence/
    get_document_passage_evidence/get_macro_evidence all take `question`
    itself. So Financials is rendered and optimized SEPARATELY, with an
    empty question string (uniform relevance — pure freshness/confidence/
    token-cost ranking, see context/optimizer.py's _relevance()), and sent as
    `cacheable_prefix` — byte-identical across different questions about the
    same companies (until the underlying data changes), which is what
    actually makes it cacheable. Docs/Macro evidence keeps the normal
    question-scored optimize() pass and rides along in the variable
    `user_message`. A second (or third, ...) question about the same
    company within Anthropic's cache TTL reads that Financials block back at
    a fraction of the input-token cost instead of paying full price again.
    """
    mem = investigation_memory or default_investigation_memory()
    reused = mem.reusable_report(conn, question, company_ids, statement_type)
    if reused is not None:
        observability.record_reuse(
            conn, task_name="assistant_qa", company_ids=company_ids, question=question,
            reused_thread_id=reused.thread_id, similarity=reused.similarity,
        )
        return reused.report_markdown

    financial_evidence = get_comparison_evidence(conn, company_ids, statement_type)  # Financials — cacheable
    variable_evidence: list = []
    if len(company_ids) == 1:
        # Uploaded-document evidence (Docs tab) only has single-company
        # attribution today — see research/documents.py.
        variable_evidence += get_document_evidence(conn, company_ids[0], question)  # Docs (whole document)
        # Additive (section 9): the specific passage(s) hybrid retrieval
        # judges most relevant to THIS question, not just each document's
        # opening ~12,000 characters — finds a paraphrased answer buried
        # deep in a long filing that get_document_evidence() above would
        # never reach. Never removes the whole-document evidence above.
        variable_evidence += get_document_passage_evidence(conn, company_ids[0], question)  # Docs (targeted passages)
    variable_evidence += get_macro_evidence(conn, question)  # Macro (RBI, IITM, FRED, ...)
    evidence = financial_evidence + variable_evidence
    if not evidence:
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

    hardness = classify(question, company_ids, len(evidence))
    optimized_financial = optimize("", financial_evidence, hardness.tier)
    optimized_variable = optimize(question, variable_evidence, hardness.tier)
    optimized = OptimizedContext(
        evidence=optimized_financial.evidence + optimized_variable.evidence,
        dropped=optimized_financial.dropped + optimized_variable.dropped,
        total_tokens_before=optimized_financial.total_tokens_before + optimized_variable.total_tokens_before,
        total_tokens_after=optimized_financial.total_tokens_after + optimized_variable.total_tokens_after,
        budget=optimized_financial.budget + optimized_variable.budget,
    )
    pinned_model = model or ANTHROPIC_MODEL
    cacheable_prefix = (
        f"Evidence (Financials):\n{render_evidence_block(optimized_financial.evidence)}"
        if optimized_financial.evidence else None
    )
    variable_block = render_evidence_block(optimized_variable.evidence)
    user_message = (
        f"Evidence (Docs/Macro):\n{variable_block}\n\nQuestion: {question}" if variable_block
        else f"Question: {question}"
    )

    try:
        result = route(
            system=SYSTEM_PROMPT, user_message=user_message, hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=pinned_model, cacheable_prefix=cacheable_prefix,
        )
    except AllProvidersUnavailableError:
        return "The assistant is temporarily unavailable (all configured models failed). Try again shortly."

    observability.record(
        conn, task_name="assistant_qa", company_ids=company_ids, question=question,
        result=result, optimized=optimized,
    )

    response = result.response
    if response.stop_reason == "refusal":
        return "The assistant declined to answer this question. Try rephrasing it."
    if not response.text:
        return "The assistant returned no answer (stop_reason: " + str(response.stop_reason) + ")."
    return response.text
