"""Follow-up answers for multi-turn Conversations (a case with origin=
'conversation' plus its case_turns rows).

A follow-up is the same evidence-grounded answer research/assistant.py's
answer_question() gives, with two differences that make it a conversation:

1. The prior turns are handed to the model as context, so "and what about
   last year?" resolves against what was just discussed.
2. Reuse-before-recompute is deliberately skipped -- a saved answer to a
   near-identical question was written without this conversation's context,
   so serving it back would be wrong for a follow-up.

Evidence retrieval is keyed on the question text (Docs/Macro/graph lookups
take it), and a bare follow-up ("why?") carries almost no retrieval signal on
its own -- so it's retrieved on the conversation's earlier questions plus the
new one. Financials evidence is per-company, not per-question, so it's
unaffected either way.
"""
from __future__ import annotations

from config.settings import ANTHROPIC_MODEL
from context.optimizer import OptimizedContext, optimize
from llm import observability
from llm.hardness import classify
from llm.router import AllProvidersUnavailableError, route
from research.assistant import MAX_TOKENS, SYSTEM_PROMPT, gather_evidence
from research.evidence import render_evidence_block
from storage.db_types import DBConnection

# Bounds on how much history rides along: the most recent turns only, each
# answer trimmed -- an unbounded transcript would eventually crowd out the
# evidence the answer is supposed to be grounded in.
MAX_HISTORY_TURNS = 6
MAX_HISTORY_ANSWER_CHARS = 2500

_FOLLOW_UP_ADDENDUM = """

This is a follow-up in an ongoing conversation. The earlier turns are given only so \
you can resolve what the new question refers to -- they are not evidence. Ground every \
claim in the Evidence block, exactly as for a first question; if the earlier answer \
said something the current evidence does not support, say so rather than repeating it."""


def render_history(history: list[tuple[str, str]]) -> str:
    """history is [(question, answer), ...] oldest first."""
    recent = history[-MAX_HISTORY_TURNS:]
    lines = []
    for question, answer in recent:
        answer = (answer or "").strip()
        if len(answer) > MAX_HISTORY_ANSWER_CHARS:
            answer = answer[:MAX_HISTORY_ANSWER_CHARS].rstrip() + " ..."
        lines.append(f"User: {question}\nAssistant: {answer}")
    return "\n\n".join(lines)


def answer_follow_up(
    conn: DBConnection, question: str, history: list[tuple[str, str]], company_ids: list[str],
    statement_type: str | None = "consolidated", *, run_id: str | None = None,
) -> str:
    retrieval_question = " ".join([q for q, _ in history[-MAX_HISTORY_TURNS:]] + [question])
    financial_evidence, variable_evidence = gather_evidence(conn, retrieval_question, company_ids, statement_type)
    if not (financial_evidence or variable_evidence):
        return (
            "There is no evidence on file to ground an answer to this follow-up. Tag a company on this "
            "conversation (or ask about a macro topic that has been ingested) and try again."
        )

    hardness = classify(retrieval_question, company_ids, len(financial_evidence) + len(variable_evidence))
    optimized_financial = optimize("", financial_evidence, hardness.tier)
    optimized_variable = optimize(retrieval_question, variable_evidence, hardness.tier)
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
    parts = [f"Conversation so far:\n{render_history(history)}"]
    if variable_block:
        parts.append(f"Evidence (Docs/Macro):\n{variable_block}")
    parts.append(f"Follow-up question: {question}")

    try:
        result = route(
            system=SYSTEM_PROMPT + _FOLLOW_UP_ADDENDUM, user_message="\n\n".join(parts), hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=ANTHROPIC_MODEL, cacheable_prefix=cacheable_prefix,
        )
    except AllProvidersUnavailableError:
        return "The assistant is temporarily unavailable (all configured models failed). Try again shortly."

    observability.record(
        conn, task_name="conversation_turn", company_ids=company_ids, question=question,
        result=result, optimized=optimized, thread_id=run_id,
    )
    response = result.response
    if response.stop_reason == "refusal":
        return "The assistant declined to answer this question. Try rephrasing it."
    return response.text or f"The assistant returned no answer (stop_reason: {response.stop_reason})."
