"""scripts/run_signals_eval.py -- the periodic Jev-accuracy eval runner
(docs/ADR/023). Exercises the real BatchRun/batch_job_items audit trail
end-to-end, same convention tests/test_vector_backfill.py's own audit-run
test already established for a different batch job, with a small
monkeypatched EVAL_CASES list (not the real 11-case set, which needs three
companies fully ingested and would run the real, expensive Level 5
investigation pipeline) so this stays fast and fully deterministic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from llm.complexity import ComplexityLevel
from llm.providers.base import ProviderResponse
from research.signals_eval_cases import EvalCase
from scripts.run_signals_eval import run_signals_eval
from storage.repositories import list_batch_job_items, list_batch_job_runs
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)  # net_profit: FY2023=17,000, FY2024=20,500
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _jev_response(level: int) -> ProviderResponse:
    payload = json.dumps({"complexity_level": level, "confidence": 0.9, "reason": "test"})
    return ProviderResponse(
        text=payload, stop_reason="end_turn", input_tokens=10, output_tokens=10,
        model="claude-haiku-4-5", provider="anthropic",
    )


def test_eval_run_records_match_and_mismatch(ingested_conn, monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_signals_eval.ANTHROPIC_API_KEY_SET", True)
    # Jev always classifies this Level-1-shaped question as Level 1 --
    # deterministic dispatch, no LLM call beyond this one classification.
    monkeypatch.setattr("llm.router.anthropic_provider.generate", lambda **kw: _jev_response(1))

    cases = [
        EvalCase("case_match", "What was net profit in FY2024?", ["HDFCBANK"], ComplexityLevel.RETRIEVE),
        EvalCase("case_mismatch", "What was net profit in FY2024?", ["HDFCBANK"], ComplexityLevel.CALCULATE),
    ]
    monkeypatch.setattr("scripts.run_signals_eval.EVAL_CASES", cases)

    run_id = run_signals_eval(ingested_conn)

    runs = list_batch_job_runs(ingested_conn)
    run = next(r for r in runs if r["run_id"] == run_id)
    assert run["job_name"] == "signals_eval"
    assert run["status"] == "completed"  # a per-item mismatch doesn't fail the whole run
    assert run["items_succeeded"] == 1
    assert run["items_failed"] == 1

    items = {item["company_id"]: item for item in list_batch_job_items(ingested_conn, run_id)}
    assert items["case_match"]["status"] == "ok"
    assert "expected=L1 actual=L1" in items["case_match"]["detail"]
    assert items["case_mismatch"]["status"] == "failed"
    assert "expected=L2 actual=L1" in items["case_mismatch"]["detail"]


def test_eval_run_requires_api_key(ingested_conn, monkeypatch) -> None:
    monkeypatch.setattr("scripts.run_signals_eval.ANTHROPIC_API_KEY_SET", False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_signals_eval(ingested_conn)


def test_eval_cases_have_unique_names_and_cover_every_level() -> None:
    from research.signals_eval_cases import EVAL_CASES

    names = [case.name for case in EVAL_CASES]
    assert len(names) == len(set(names)), "eval case names must be unique (they key batch_job_items rows)"

    levels_covered = {case.expected_level for case in EVAL_CASES}
    assert levels_covered == set(ComplexityLevel), "every Signals complexity level should have eval coverage"
