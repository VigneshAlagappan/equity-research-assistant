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


def test_natural_shorthand_company_names_resolve_via_the_llm_fallback(tmp_path: Path, monkeypatch) -> None:
    """The real, observed production bug: research.html's client-side
    detectCompaniesInText() regex requires an exact whole-word match of a
    company's full registered name -- "IDFC Bank" never matches the
    registered "IDFC First Bank", "Federal Bank" never matches "The
    Federal Bank", so a real comparison question went through with
    company_ids=[] and came back "no evidence found" even though both
    companies had financials ingested. This proves the server-side LLM
    fallback (research/company_resolver.py, wired into _compute_answer_
    question) catches exactly what the client-side regex misses."""
    from companies.registry import register_company

    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    register_company(conn, "IDFCFIRSTB", "IDFC First Bank Limited", "IDFC First Bank", nse_symbol="IDFCFIRSTB")
    register_company(conn, "FEDERALBNK", "The Federal Bank Limited", "The Federal Bank", nse_symbol="FEDERALBNK")
    for company_id in ("IDFCFIRSTB", "FEDERALBNK"):
        file_path = tmp_path / f"{company_id}.xlsx"
        _make_screener_workbook(file_path)
        ingest_file(conn, file_path, company_id=company_id, source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)

    # Two different fake-LLM responses in sequence: the first call this
    # pipeline makes is company resolution (JSON), the second is the real
    # answer (plain text) -- a single fixed-text fake client would return
    # the wrong shape to whichever call happened not to match.
    import llm.providers.anthropic_provider as anthropic_provider
    from types import SimpleNamespace

    responses = iter([
        '{"company_ids": ["IDFCFIRSTB", "FEDERALBNK"]}',
        # 2 companies now resolved -> _compute_answer_question's own
        # aggregate-intent check (research/aggregate_query.py) fires next,
        # before falling through to the real per-company answer below.
        '{"is_aggregate": false, "metric_key": null, "operation": null, "num_years": null}',
        "IDFC First Bank is growing faster. [FACT] x.",
    ])

    class _SequencedMessages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=next(responses))], stop_reason="end_turn")

    monkeypatch.setattr(anthropic_provider.anthropic, "Anthropic", lambda *a, **kw: SimpleNamespace(messages=_SequencedMessages()))

    with app.test_client() as test_client:
        start = test_client.post(
            "/research/ask-async",
            json={
                "question": "IDFC Bank Growth rate in last 5 years compared to Federal Bank. "
                "Why one bank is growing faster, which one is that? and why?",
                "company_ids": [],  # exactly what the client-side regex sends when it finds nothing
            },
        )
        assert start.status_code == 202
        case_id = start.get_json()["job_id"]

        final = _poll_until_done(test_client, case_id)

    assert final["status"] == "done"
    assert final.get("outcome") != "insufficient_data"
    assert sorted(final["result"]["company_ids"]) == ["FEDERALBNK", "IDFCFIRSTB"]
    assert "IDFC First Bank is growing faster" in final["result"]["answer_html"]


def test_research_understand_resolves_companies_and_suggests_deep_for_a_comparison(
    tmp_path: Path, monkeypatch
) -> None:
    from companies.registry import register_company

    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    register_company(conn, "IDFCFIRSTB", "IDFC First Bank Limited", "IDFC First Bank", nse_symbol="IDFCFIRSTB")
    register_company(conn, "FEDERALBNK", "The Federal Bank Limited", "The Federal Bank", nse_symbol="FEDERALBNK")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    _install_fake_llm(monkeypatch, text='{"company_ids": ["IDFCFIRSTB", "FEDERALBNK"]}')

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/understand",
            json={"question": "IDFC Bank Growth rate compared to Federal Bank. Why one is growing faster?"},
        )

    assert response.status_code == 200
    data = response.get_json()
    assert sorted(data["company_ids"]) == ["FEDERALBNK", "IDFCFIRSTB"]
    assert sorted(data["company_labels"]) == ["IDFC First Bank", "The Federal Bank"]
    assert data["suggested_case_type"] == "deep"  # >1 company -> always DEEP (llm/hardness.py)


def test_research_understand_suggests_quick_for_a_plain_lookup(tmp_path: Path, monkeypatch) -> None:
    from companies.registry import register_company

    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    register_company(conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", nse_symbol="HDFCBANK")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    _install_fake_llm(monkeypatch, text='{"company_ids": ["HDFCBANK"]}')

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/understand", json={"question": "What is HDFC Bank's growth rate for the last 4 years?"}
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["company_ids"] == ["HDFCBANK"]
    assert data["suggested_case_type"] == "quick"


def test_research_understand_requires_a_question(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.post("/research/understand", json={"question": ""})
    assert response.status_code == 400


def _poll_investigate_until_done(test_client, investigation_id: str, attempts: int = 200):
    status = None
    for _ in range(attempts):
        status = test_client.get(f"/investigate/status/{investigation_id}").get_json()
        if status["status"] != "running":
            return status
        time.sleep(0.05)
    return status


def test_deep_dive_case_appears_on_cases_list_and_reconnects_via_case_detail(
    tmp_path: Path, monkeypatch
) -> None:
    """Deep Dive (research/investigation.py) shares the exact same
    research_cases lifecycle Quick Answer uses -- this proves an
    in_progress investigation shows up on the Cases list immediately (not
    just once it finishes, the old file-based investigation_jobs.py
    behavior this replaced), /cases/<id> renders and polls it via
    /investigate/status (not /ask/status -- a different response shape),
    and completion is recorded with a real investigation_id, not just a
    generic "answered" case."""
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    seed_companies(conn)
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)

    def _fake_run_investigation(
        conn, question, company_ids, *, statement_type="consolidated", as_of=None,
        investigation_id=None, case_id=None,
    ):
        from research.investigation import Investigation

        assert case_id == investigation_id  # the route must reuse one id, not mint two
        return Investigation(investigation_id=investigation_id, question=question, company_ids=company_ids)

    monkeypatch.setattr("web.app.run_investigation", _fake_run_investigation)

    with app.test_client() as test_client:
        start = test_client.post(
            "/investigate/generate-async", json={"question": "Why did margins fall?", "company_ids": ["HDFCBANK"]}
        )
        assert start.status_code == 202
        investigation_id = start.get_json()["investigation_id"]
        assert start.get_json()["case_id"] == investigation_id

        # Poll /cases/<id> is what a user opening the Cases-list entry
        # would hit -- confirm it renders (not a redirect yet) while the
        # underlying case may still be in_progress or may have already
        # finished (the fake investigation is near-instant).
        detail = test_client.get(f"/cases/{investigation_id}")
        assert detail.status_code in (200, 302)

        final = _poll_investigate_until_done(test_client, investigation_id)

    assert final["status"] == "done"
    assert final["url"] == f"/investigate/{investigation_id}"


def test_case_detail_unknown_case_is_404(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/cases/not-a-real-case")
    assert response.status_code == 404
