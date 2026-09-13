"""LLM-generated abstracts for investigations/threads (ADR-021) — a short,
professional-style preview stored on the investigations/generated_reports
row (Postgres metadata) alongside the full-content S3 artifact, so a Cases
list/future search result can show something better than a raw text
truncation of the synthesis narrative or report markdown.

QUICK tier (llm/hardness.py) — summarizing already-generated text is a
short, low-reasoning task, not an investigation-grade one; same tier
research/insights.py's key-insights summary already uses for a comparable
"condense existing analysis" job. Falls back to a plain truncation (never
raises) if every provider is unavailable — an abstract is a nice-to-have
preview, not something that should ever block persisting an investigation
or report."""

from __future__ import annotations

import logging

from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from storage.db_types import DBConnection

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You write short, professional abstracts for equity research reports and investigations. \
Given the full text of a report, write a single abstract of at most 100 words (never exceed 300) in a neutral, \
professional tone — third person, no marketing language, no bullet points, plain prose. State what was examined \
and the main finding(s), not a recommendation or opinion. Output only the abstract text, nothing else — no \
heading, no quotation marks, no preamble."""

_MAX_TOKENS = 200
_TRUNCATION_FALLBACK_CHARS = 500
#: Below this, the text is already about as short as the abstract itself
#: would be (~100 words preferred / 300 max, per spec) -- summarizing it
#: would just paraphrase the same handful of sentences at real LLM cost
#: for no informational gain. Deterministic short answers (e.g. research/
#: assistant.py's "no matching evidence found..." fallback, ~230 chars)
#: are the common case this catches; a genuine report/investigation
#: narrative is routinely thousands of characters and always clears it.
_SKIP_LLM_BELOW_CHARS = 400


def generate_abstract(conn: DBConnection, text: str, *, task_name: str = "report_abstract") -> str | None:
    """text is the full report/investigation content (markdown or plain
    prose) — truncated to a generous character budget before the prompt,
    not because the model can't handle more, but so a very long report
    doesn't blow the QUICK tier's cost/latency expectations for what's
    meant to be a cheap, incidental call. Returns None only if `text`
    itself is empty/None — a provider failure degrades to a plain
    truncation instead of returning None, so a persist path always gets
    *something* to store."""
    if not text or not text.strip():
        return None
    if len(text) < _SKIP_LLM_BELOW_CHARS:
        return text.strip()

    hardness = fixed(Tier.QUICK, "short report/investigation abstract")
    try:
        result = route(
            system=_SYSTEM_PROMPT, user_message=f"Report:\n{text[:8000]}",
            hardness=hardness, max_tokens=_MAX_TOKENS,
        )
    except AllProvidersUnavailableError:
        logger.warning("Abstract generation: all LLM providers unavailable, falling back to truncation")
        return text[:_TRUNCATION_FALLBACK_CHARS].strip()

    observability.record(conn, task_name=task_name, company_ids=[], question=None, result=result)
    response = result.response
    if response.stop_reason == "refusal" or not response.text.strip():
        return text[:_TRUNCATION_FALLBACK_CHARS].strip()
    return response.text.strip()
