"""Per-call observability for the Context Optimization + Model Routing +
Fallback layer. Every llm/router.py route() outcome gets one structured log
line (console + logs/app.log, via config.settings.setup_logging) and one
llm_call_log row, so token usage, model/provider choice, and fallback
behavior are inspectable instead of invisible.

Cost is a rough estimate from a small static price table, not a billed-amount
lookup — good enough to compare tiers/providers over time, not to reconcile
an invoice.
"""

from __future__ import annotations

import json
import logging
from storage.db_types import DBConnection

from context.optimizer import OptimizedContext
from llm.router import RouteResult
from storage.repositories import insert_llm_call_log

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Local/Ollama models aren't priced here —
# _estimate_cost_usd returns 0 for any model not in this table.
_PRICE_PER_MILLION_TOKENS = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (0.8, 4.0),
}


def _estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int,
    cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0,
) -> float:
    """Rough estimate, not a billed-amount lookup (module docstring). A cache
    write costs 1.25x the base input rate, a cache read 0.1x it (Anthropic's
    published prompt-caching pricing) — input_tokens/output_tokens from the
    API already EXCLUDE cache-written/cache-read tokens, so leaving these two
    out on a call that used cacheable_prefix (llm/providers/
    anthropic_provider.py) would silently undercount cost, not just
    approximate it."""
    prices = _PRICE_PER_MILLION_TOKENS.get(model)
    if prices is None:
        return 0.0
    input_price, output_price = prices
    return (
        (input_tokens / 1_000_000) * input_price
        + (output_tokens / 1_000_000) * output_price
        + (cache_creation_input_tokens / 1_000_000) * input_price * 1.25
        + (cache_read_input_tokens / 1_000_000) * input_price * 0.1
    )


def _attempts_json(attempts) -> str:
    return json.dumps(
        [{"model": a.model, "provider": a.provider, "outcome": a.outcome, "detail": a.detail} for a in attempts]
    )


def record(
    conn: DBConnection,
    *,
    task_name: str,
    company_ids: list[str],
    question: str | None,
    result: RouteResult,
    thread_id: str | None = None,
    optimized: OptimizedContext | None = None,
    graph_hit_thread_id: str | None = None,
    graph_hit_score: float | None = None,
    investigation_id: str | None = None,
) -> None:
    """`graph_hit_thread_id`/`graph_hit_score` are set on this same row (not
    a second one) when context/graph.py's sector-peer traversal
    (research/signals_report.py) found a candidate and appended it to this
    call's prompt — the graph hit augments the call it accompanies rather
    than replacing it, unlike a context/reuse.py hit (record_reuse below),
    which skips the call entirely. `investigation_id` tags a call made as
    part of one research/investigation.py run (hypothesis generation/
    evaluation, research synthesis, or a macro-retrieval-plan call along the
    way) so its cost can be totalled per investigation."""
    response = result.response
    cost = _estimate_cost_usd(
        response.model, response.input_tokens, response.output_tokens,
        response.cache_creation_input_tokens, response.cache_read_input_tokens,
    )

    logger.info(
        "llm_call task=%s companies=%s tier=%s level=%s model=%s provider=%s fallback_used=%s "
        "attempts=%d input_tokens=%d output_tokens=%d cache_write_tokens=%d cache_read_tokens=%d "
        "cost_usd=%.4f latency_ms=%.0f reason=%s "
        "context_tokens_before=%s context_tokens_after=%s context_dropped=%s "
        "graph_hit=%s graph_hit_thread_id=%s graph_hit_score=%s investigation_id=%s",
        task_name, ",".join(company_ids), result.hardness.tier.value, result.hardness.level,
        response.model, response.provider, result.fallback_used, len(result.attempts),
        response.input_tokens, response.output_tokens,
        response.cache_creation_input_tokens, response.cache_read_input_tokens,
        cost, result.latency_ms, result.hardness.reason,
        optimized.total_tokens_before if optimized else None,
        optimized.total_tokens_after if optimized else None,
        len(optimized.dropped) if optimized else None,
        graph_hit_thread_id is not None, graph_hit_thread_id, graph_hit_score, investigation_id,
    )

    insert_llm_call_log(
        conn,
        task_name=task_name,
        company_ids=",".join(company_ids),
        question=question,
        thread_id=thread_id,
        complexity_tier=result.hardness.tier.value,
        complexity_level=result.hardness.level,
        complexity_reason=result.hardness.reason,
        model_used=response.model,
        provider_used=response.provider,
        fallback_used=result.fallback_used,
        attempts_json=_attempts_json(result.attempts),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        estimated_cost_usd=cost,
        latency_ms=result.latency_ms,
        stop_reason=response.stop_reason,
        cache_creation_input_tokens=response.cache_creation_input_tokens,
        cache_read_input_tokens=response.cache_read_input_tokens,
        context_tokens_before=optimized.total_tokens_before if optimized else None,
        context_tokens_after=optimized.total_tokens_after if optimized else None,
        context_items_dropped=len(optimized.dropped) if optimized else None,
        graph_hit=graph_hit_thread_id is not None,
        graph_hit_thread_id=graph_hit_thread_id,
        graph_hit_score=graph_hit_score,
        investigation_id=investigation_id,
    )


def record_reuse(
    conn: DBConnection,
    *,
    task_name: str,
    company_ids: list[str],
    question: str | None,
    reused_thread_id: str,
    similarity: float,
) -> None:
    """Log a context/reuse.py hit — a prior fresh investigation answered this
    call instead of a new LLM request, so no model/provider ran and the
    token/cost fields are all zero."""
    logger.info(
        "llm_call task=%s companies=%s reuse_hit=true reused_thread_id=%s similarity=%.2f",
        task_name, ",".join(company_ids), reused_thread_id, similarity,
    )
    insert_llm_call_log(
        conn,
        task_name=task_name,
        company_ids=",".join(company_ids),
        question=question,
        thread_id=None,
        complexity_tier="n/a",
        complexity_level=0,
        complexity_reason=f"reused prior investigation (similarity {similarity:.2f})",
        model_used="reused",
        provider_used="cache",
        fallback_used=False,
        attempts_json="[]",
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0.0,
        stop_reason="reused",
        reuse_hit=True,
        reused_thread_id=reused_thread_id,
    )
