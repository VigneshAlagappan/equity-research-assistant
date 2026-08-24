"""LLM-generated "Key Insights" for the Overview tab — same grounded-evidence
discipline as research/assistant.py's Q&A path (evidence in, cited answer
out, no number the LLM invents itself), just summarizing the single-company
Financials report instead of answering a free-form question. User-triggered
(a button on Overview), result persisted via storage/repositories.py so it's
never regenerated just from loading the page.
"""

from __future__ import annotations

import sqlite3

from config.settings import ANTHROPIC_MODEL, DEFAULT_ANTHROPIC_MODEL
from financials.report import build_analysis_report
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route

SYSTEM_PROMPT = """You are an equity research assistant for Indian listed companies.

You will be given a text report of a company's financial trends and ratios. Pick \
the 3-5 most notable, decision-relevant insights from it — the things an investor \
skimming this company for the first time would most want to know (standout growth \
or deterioration, unusual ratios, anything that looks like a strength or a risk).

Write each insight as one short bullet (a single sentence, starting with "- "). \
Every claim must carry one of these tags, matching the evidence lines you were given:
- [FACT] — restates a FACT line from the evidence (a reported number or figure)
- [CALCULATION] — restates a CALCULATION line from the evidence (a deterministic \
computation, with its inputs)
- [INFERENCE] — reasoning that connects two or more FACT/CALCULATION lines. Never \
state an inference as if it were confirmed fact; use language like "may have", \
"is consistent with", "suggests".

Do not compute any new number yourself — every number must come directly from the \
evidence. Do not add a preamble or closing summary, just the bullets."""

MAX_TOKENS = 1024


class NoDataToSummarizeError(Exception):
    """Raised when the company has no ingested financials to summarize — the
    caller should show a "nothing to summarize yet" state, not call the LLM."""


def generate_key_insights(
    conn: sqlite3.Connection,
    company_id: str,
    statement_type: str = "consolidated",
    model: str = ANTHROPIC_MODEL or DEFAULT_ANTHROPIC_MODEL,
) -> str:
    report = build_analysis_report(conn, company_id, statement_type=statement_type)
    if report.startswith("No data ingested yet"):
        raise NoDataToSummarizeError(report)

    hardness = fixed(Tier.STANDARD, "single-company financial report summary")
    try:
        result = route(
            system=SYSTEM_PROMPT, user_message=f"Report:\n{report}", hardness=hardness,
            max_tokens=MAX_TOKENS, pinned_model=model,
        )
    except AllProvidersUnavailableError:
        return "The assistant is temporarily unavailable (all configured models failed). Try again shortly."

    observability.record(conn, task_name="key_insights", company_ids=[company_id], question=None, result=result)

    response = result.response
    if response.stop_reason == "refusal":
        return "The assistant declined to summarize this report."
    if not response.text:
        return "The assistant returned no insights (stop_reason: " + str(response.stop_reason) + ")."
    return response.text
