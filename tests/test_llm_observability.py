"""llm/observability.py — every route() outcome is logged to llm_call_log
(storage/repositories.py) with token counts, model/provider, and fallback
status, so cost and routing behavior are inspectable after the fact."""

from __future__ import annotations

import sqlite3

from llm import observability
from llm.hardness import Tier, fixed
from llm.providers.base import ProviderResponse
from llm.router import Attempt, RouteResult
from storage.repositories import get_llm_usage_summary, list_llm_call_log


def _route_result(fallback_used: bool = False) -> RouteResult:
    response = ProviderResponse(
        text="answer", stop_reason="end_turn", input_tokens=1000, output_tokens=200,
        model="claude-sonnet-5", provider="anthropic",
    )
    attempts = [Attempt("claude-sonnet-5", "anthropic", "success")]
    if fallback_used:
        attempts.insert(0, Attempt("claude-opus-5", "anthropic", "unavailable", "rate limited"))
    return RouteResult(response=response, hardness=fixed(Tier.STANDARD, "test"), attempts=attempts,
                        fallback_used=fallback_used, latency_ms=123.0)


def test_record_persists_a_llm_call_log_row(db_conn: sqlite3.Connection) -> None:
    observability.record(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q?", result=_route_result()
    )

    rows = list_llm_call_log(db_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["task_name"] == "assistant_qa"
    assert row["model_used"] == "claude-sonnet-5"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["fallback_used"] == 0
    assert row["estimated_cost_usd"] > 0


def test_record_marks_fallback_used(db_conn: sqlite3.Connection) -> None:
    observability.record(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q?",
        result=_route_result(fallback_used=True),
    )

    row = list_llm_call_log(db_conn)[0]
    assert row["fallback_used"] == 1
    assert "claude-opus-5" in row["attempts_json"]


def test_unknown_model_estimates_zero_cost() -> None:
    assert observability._estimate_cost_usd("llama3.1:8b", 1000, 1000) == 0.0


# ------------------------------------------------------------------
# get_llm_usage_summary — backs the /admin/usage page (web/app.py).
# ------------------------------------------------------------------


def test_usage_summary_totals_across_calls(db_conn: sqlite3.Connection) -> None:
    observability.record(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q1", result=_route_result()
    )
    observability.record(
        db_conn, task_name="macro_retrieval_plan", company_ids=[], question="q2", result=_route_result()
    )

    summary = get_llm_usage_summary(db_conn)
    assert summary["calls"] == 2
    assert summary["input_tokens"] == 2000
    assert summary["output_tokens"] == 400
    assert summary["cost_usd"] > 0
    assert summary["reused_calls"] == 0


def test_usage_summary_breaks_down_by_task_and_model(db_conn: sqlite3.Connection) -> None:
    observability.record(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q1", result=_route_result()
    )
    observability.record(
        db_conn, task_name="macro_retrieval_plan", company_ids=[], question="q2", result=_route_result()
    )

    summary = get_llm_usage_summary(db_conn)
    task_names = {row["task_name"] for row in summary["by_task"]}
    assert task_names == {"assistant_qa", "macro_retrieval_plan"}
    assert {row["model_used"] for row in summary["by_model"]} == {"claude-sonnet-5"}


def test_usage_summary_counts_reuse_hits_separately_from_model_breakdown(db_conn: sqlite3.Connection) -> None:
    """A reuse hit costs $0 and used no model — it must not show up as a
    free "claude-opus-5"-style row diluting the by-model cost breakdown."""
    observability.record(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q1", result=_route_result()
    )
    observability.record_reuse(
        db_conn, task_name="assistant_qa", company_ids=["HDFCBANK"], question="q1 again",
        reused_thread_id="abc123", similarity=0.95,
    )

    summary = get_llm_usage_summary(db_conn)
    assert summary["calls"] == 2
    assert summary["reused_calls"] == 1
    assert {row["model_used"] for row in summary["by_model"]} == {"claude-sonnet-5"}  # not "reused"


def test_usage_summary_empty_database(db_conn: sqlite3.Connection) -> None:
    summary = get_llm_usage_summary(db_conn)
    assert summary == {
        "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "reused_calls": 0,
        "by_task": [], "by_model": [],
    }
