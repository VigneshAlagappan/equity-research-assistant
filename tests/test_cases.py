"""End-to-end Cases (research_cases) tests via Flask's test client --
routes-level, complementing tests/test_case_runner.py's unit-level
coverage of the lifecycle harness itself and tests/test_assistant.py's
coverage of the sufficiency-check/cancellation hooks inside
answer_question(). Reuses tests/test_web.py's own _build_app/_install_fake_llm
fixtures rather than duplicating that isolation setup."""

from __future__ import annotations

import time
from pathlib import Path

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db
from storage.repositories import get_research_case, is_case_cancel_requested
from tests.test_screener_adapter import _make_screener_workbook
from tests.test_web import _build_app, _install_fake_llm


def _poll_until_done(test_client, job_id: str, attempts: int = 200):
    status = None
    for _ in range(attempts):
        status = test_client.get(f"/ask/status/{job_id}").get_json()
        if status["status"] != "running":
            return status
        time.sleep(0.1)
    return status


def test_in_progress_case_appears_on_the_cases_list(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)

    # A slow fake LLM call so the case is still in_progress when we check
    # the Cases list -- real behavior would be a slow retrieval/LLM call,
    # simulated here with a deliberate sleep inside the fake provider.
    import llm.providers.anthropic_provider as anthropic_provider
    from types import SimpleNamespace

    class _SlowMessages:
        def create(self, **kwargs):
            time.sleep(1.0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Net profit rose. [FACT] x.")], stop_reason="end_turn")

    monkeypatch.setattr(anthropic_provider.anthropic, "Anthropic", lambda *a, **kw: SimpleNamespace(messages=_SlowMessages()))

    with app.test_client() as test_client:
        start = test_client.post("/companies/HDFCBANK/ask-async", json={"question": "How did net profit change?"})
        assert start.status_code == 202
        case_id = start.get_json()["case_id"]

        page = test_client.get("/investigations")
        assert page.status_code == 200
        body = page.data.decode()
        assert "How did net profit change?" in body
        assert ">In progress<" in body
        assert f"/cases/{case_id}" in body

        detail = test_client.get(f"/cases/{case_id}")
        assert detail.status_code == 200
        assert b"How did net profit change?" in detail.data

        final = _poll_until_done(test_client, case_id)
        assert final["status"] == "done"


def test_insufficient_data_case_completes_gracefully_and_stays_on_the_cases_list(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    captured = _install_fake_llm(monkeypatch, text="should never be returned")

    with app.test_client() as test_client:
        # No company named and nothing macro-related ingested -- gather_evidence()
        # finds nothing at all, so this should short-circuit before any LLM call.
        start = test_client.post("/research/ask-async", json={"question": "asdkfjaslkdjf nonsense query", "company_ids": []})
        assert start.status_code == 202
        case_id = start.get_json()["job_id"]

        final = _poll_until_done(test_client, case_id)
        assert final["status"] == "done"
        assert final["outcome"] == "insufficient_data"
        assert "No matching evidence" in final["result"]["answer_html"]
        assert captured == []  # never called the API

        page = test_client.get("/investigations")
        body = page.data.decode()
        assert ">Insufficient data<" in body


def test_cancel_route_sets_the_cooperative_cancellation_flag(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)

    from storage.repositories import create_research_case

    conn = init_db(db_path=db_path)
    create_research_case(
        conn, "case-to-cancel", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )
    conn.close()

    with app.test_client() as test_client:
        response = test_client.post("/cases/case-to-cancel/cancel")
        assert response.status_code == 200

    conn = init_db(db_path=db_path)
    assert is_case_cancel_requested(conn, "case-to-cancel") is True
    conn.close()


def test_cancel_unknown_case_is_404(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.post("/cases/not-a-real-case/cancel")
    assert response.status_code == 404


def test_case_detail_unknown_case_is_404(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/cases/not-a-real-case")
    assert response.status_code == 404
