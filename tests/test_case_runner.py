"""research/case_runner.py -- run_case_in_background() starts a real daemon
thread with its own DB connection, so these tests poll for completion
(same shape tests/test_web.py's async-route tests already use) rather than
asserting anything synchronously right after the call returns."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from research.assistant import CaseCancelledError, InsufficientEvidenceError
from research.case_runner import run_case_in_background, start_case
from storage.database import get_connection
from storage.repositories import get_research_case


def _open_conn_factory(db_path: Path):
    def _open() -> sqlite3.Connection:
        return get_connection(db_path)

    return _open


def _wait_for_terminal_status(conn: sqlite3.Connection, case_id: str, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get_research_case(conn, case_id)
        if row["status"] != "in_progress":
            return row
        time.sleep(0.05)
    raise AssertionError(f"case {case_id} never left in_progress within {timeout}s")


def test_successful_case_completes_with_answered_outcome(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    row = start_case(
        db_conn, case_id="case-ok", kind="ask", question="q?", company_ids=["HDFCBANK"],
        statement_type="consolidated", owner_id=None,
    )
    assert row["status"] == "in_progress"

    def compute(conn) -> dict:
        return {"answer_html": "<p>hi</p>", "thread_id": "t-ok"}

    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-ok", "ask", compute)

    final = _wait_for_terminal_status(db_conn, "case-ok")
    assert final["status"] == "completed"
    assert final["outcome"] == "answered"
    assert final["thread_id"] == "t-ok"
    assert '"answer_html"' in final["result_json"]


def test_insufficient_evidence_completes_with_that_outcome_not_failed(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    start_case(
        db_conn, case_id="case-insufficient", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )

    def compute(conn) -> dict:
        raise InsufficientEvidenceError("No matching evidence found for this question.")

    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-insufficient", "ask", compute)

    final = _wait_for_terminal_status(db_conn, "case-insufficient")
    assert final["status"] == "completed"
    assert final["outcome"] == "insufficient_data"
    assert "No matching evidence" in final["result_json"]


def test_cancelled_case_ends_with_cancelled_status(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    start_case(
        db_conn, case_id="case-cancel", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )

    def compute(conn) -> dict:
        raise CaseCancelledError()

    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-cancel", "ask", compute)

    final = _wait_for_terminal_status(db_conn, "case-cancel")
    assert final["status"] == "cancelled"
    assert final["outcome"] is None


def test_unexpected_exception_marks_the_case_failed_not_stuck(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    start_case(
        db_conn, case_id="case-boom", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )

    def compute(conn) -> dict:
        raise RuntimeError("provider unavailable")

    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-boom", "ask", compute)

    final = _wait_for_terminal_status(db_conn, "case-boom")
    assert final["status"] == "failed"
    assert "provider unavailable" in final["error_message"]


def test_two_cases_run_concurrently_without_cross_contaminating_results(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Case isolation: two cases started back to back, each computing a
    result that includes its own case_id, must each see only their own
    data -- proves compute() isn't sharing any state across the two
    background threads/connections."""
    start_case(
        db_conn, case_id="case-a", kind="ask", question="qa?", company_ids=["HDFCBANK"],
        statement_type="consolidated", owner_id=None,
    )
    start_case(
        db_conn, case_id="case-b", kind="ask", question="qb?", company_ids=["INFY"],
        statement_type="consolidated", owner_id=None,
    )

    def make_compute(case_id: str, company_id: str):
        def compute(conn) -> dict:
            time.sleep(0.1)  # encourage interleaving between the two threads
            return {"answer_html": f"answer for {company_id}", "thread_id": f"thread-{case_id}"}

        return compute

    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-a", "ask", make_compute("case-a", "HDFCBANK"))
    run_case_in_background(_open_conn_factory(tmp_path / "test.db"), "case-b", "ask", make_compute("case-b", "INFY"))

    final_a = _wait_for_terminal_status(db_conn, "case-a")
    final_b = _wait_for_terminal_status(db_conn, "case-b")

    assert "HDFCBANK" in final_a["result_json"] and "INFY" not in final_a["result_json"]
    assert "INFY" in final_b["result_json"] and "HDFCBANK" not in final_b["result_json"]
    assert final_a["thread_id"] == "thread-case-a"
    assert final_b["thread_id"] == "thread-case-b"
