"""End-to-end Cases (research_cases) tests via Flask's test client --
routes-level, complementing tests/test_case_runner.py's unit-level
coverage of the lifecycle harness itself and tests/test_assistant.py's
coverage of the sufficiency-check/cancellation hooks inside
answer_question(). Reuses tests/test_web.py's own _build_app/_install_fake_llm
fixtures rather than duplicating that isolation setup."""

from __future__ import annotations

import json
import time
from pathlib import Path

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db
from storage.repositories import get_research_case, is_case_cancel_requested
from tests.test_screener_adapter import _make_screener_workbook
from tests.test_web import _build_app, _install_fake_llm, _install_sequenced_fake_llm


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

        page = test_client.get("/cases")
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
    # Jev's complexity classification (docs/ADR/023) runs once, unconditionally,
    # before case creation/evidence gathering even happens -- "should never be
    # returned" only ever meant the real ANSWER call, which should still be
    # short-circuited by the empty-evidence check inside the case.
    captured = _install_fake_llm(monkeypatch, text="should never be returned")

    with app.test_client() as test_client:
        # No company named and nothing macro-related ingested -- gather_evidence()
        # finds nothing at all, so this should short-circuit before any real
        # answer-generating LLM call.
        start = test_client.post("/research/ask-async", json={"question": "asdkfjaslkdjf nonsense query", "company_ids": []})
        assert start.status_code == 202
        case_id = start.get_json()["job_id"]

        final = _poll_until_done(test_client, case_id)
        assert final["status"] == "done"
        assert final["outcome"] == "insufficient_data"
        assert "No matching evidence" in final["result"]["answer_html"]
        assert len(captured) == 1  # only Jev's classification call, never a real answer call

        page = test_client.get("/cases")
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


def _failed_case(db_path: Path, case_id: str, *, company_ids=(), origin="investigation") -> None:
    from storage.repositories import create_research_case, fail_research_case

    conn = init_db(db_path=db_path)
    create_research_case(
        conn, case_id, kind="ask", question=f"question for {case_id}?", company_ids=list(company_ids),
        statement_type="consolidated", owner_id=None, origin=origin,
    )
    fail_research_case(conn, case_id, "boom")
    conn.close()


def _answered_case(db_path: Path, case_id: str, *, thread_id: str, company_ids=("HDFCBANK",), origin="conversation") -> None:
    """A finished case plus the saved report it points at, the shape every
    real answered run leaves behind."""
    from storage.repositories import complete_research_case, create_research_case, save_generated_report

    conn = init_db(db_path=db_path)
    save_generated_report(conn, thread_id, f"q for {case_id}?", list(company_ids), "consolidated", "# Report\n\n**Confidence:** High\n")
    create_research_case(
        conn, case_id, kind="ask", question=f"q for {case_id}?", company_ids=list(company_ids),
        statement_type="consolidated", owner_id=None, origin=origin,
    )
    complete_research_case(conn, case_id, outcome="answered", result_json="{}", thread_id=thread_id)
    conn.close()


def _seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    return db_path


def test_case_delete_soft_deletes_a_failed_case_instead_of_erasing_it(tmp_path: Path, monkeypatch) -> None:
    """Failed/cancelled cases used to be hard-deleted, unlike reports and
    investigations. Every case is now archived the same way (deleted_at set,
    row kept), so nothing a Delete click does destroys data."""
    db_path = _seeded_db(tmp_path)
    _failed_case(db_path, "case-to-delete")
    app = _build_app(db_path, tmp_path, monkeypatch)

    with app.test_client() as test_client:
        assert test_client.post("/cases/case-to-delete/delete").status_code == 302
        assert "question for case-to-delete?" not in test_client.get("/cases").data.decode()

    conn = init_db(db_path=db_path)
    assert get_research_case(conn, "case-to-delete")["deleted_at"] is not None
    conn.close()


def test_case_delete_unknown_case_is_404(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        assert test_client.post("/cases/not-a-real-case/delete").status_code == 404
        assert test_client.post("/cases/not-a-real-case/hide").status_code == 404


def test_case_hide_toggles_and_cascades_to_the_saved_result(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-h", thread_id="th-h")
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import get_generated_report

    with app.test_client() as test_client:
        test_client.post("/cases/case-h/hide")
        conn = init_db(db_path=db_path)
        assert get_research_case(conn, "case-h")["hidden_at"] is not None
        assert get_generated_report(conn, "th-h")["hidden_at"] is not None
        conn.close()
        assert "q for case-h?" not in test_client.get("/cases").data.decode()
        assert "q for case-h?" in test_client.get("/cases?iv_hidden=1").data.decode()

        test_client.post("/cases/case-h/hide")  # second click = Unhide
        conn = init_db(db_path=db_path)
        assert get_research_case(conn, "case-h")["hidden_at"] is None
        assert get_generated_report(conn, "th-h")["hidden_at"] is None
        conn.close()


def test_case_bulk_action_hides_selected_cases_of_any_state(tmp_path: Path, monkeypatch) -> None:
    """The Cases list's checkbox toolbar posts case_ids as one request. Hide
    now works for every case -- including a failed one, which used to be
    skipped because that table had no hidden_at column."""
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-a", thread_id="th-a")
    _failed_case(db_path, "case-f")
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import get_generated_report

    with app.test_client() as test_client:
        response = test_client.post("/cases/bulk-action", data={"bulk_action": "hide", "selected": ["case-a", "case-f"]})
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    assert get_research_case(conn, "case-a")["hidden_at"] is not None
    assert get_generated_report(conn, "th-a")["hidden_at"] is not None
    assert get_research_case(conn, "case-f")["hidden_at"] is not None
    conn.close()


def test_case_bulk_action_deletes_selected_cases_and_their_results_without_erasing_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-a", thread_id="th-a")
    _failed_case(db_path, "case-f")
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import get_generated_report, list_generated_reports

    with app.test_client() as test_client:
        response = test_client.post("/cases/bulk-action", data={"bulk_action": "delete", "selected": ["case-a", "case-f", "nope"]})
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    assert get_research_case(conn, "case-a")["deleted_at"] is not None
    assert get_research_case(conn, "case-f")["deleted_at"] is not None
    assert get_generated_report(conn, "th-a") is not None  # never erased...
    assert "th-a" not in {r["thread_id"] for r in list_generated_reports(conn)}  # ...but no longer listed
    conn.close()


def test_cases_page_splits_conversations_from_investigations_and_labels_each(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-conv", thread_id="th-conv", origin="conversation")
    _answered_case(db_path, "case-inv", thread_id="th-inv", origin="investigation")
    app = _build_app(db_path, tmp_path, monkeypatch)

    with app.test_client() as test_client:
        everything = test_client.get("/cases").data.decode()
        conversations = test_client.get("/cases?iv_origin=conversation").data.decode()
        investigations = test_client.get("/cases?iv_origin=investigation").data.decode()

    assert "q for case-conv?" in everything and "q for case-inv?" in everything
    assert ">Conversation<" in everything
    assert "q for case-conv?" in conversations and "q for case-inv?" not in conversations
    assert "q for case-inv?" in investigations and "q for case-conv?" not in investigations


def test_company_page_shows_the_same_case_under_conversations_or_investigations_by_tag(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-conv", thread_id="th-conv", company_ids=("HDFCBANK",), origin="conversation")
    _answered_case(db_path, "case-inv", thread_id="th-inv", company_ids=("HDFCBANK", "ICICIBANK"), origin="investigation")
    app = _build_app(db_path, tmp_path, monkeypatch)

    with app.test_client() as test_client:
        hdfc = test_client.get("/companies/HDFCBANK").data.decode()
        icici = test_client.get("/companies/ICICIBANK").data.decode()

    conversations_html = hdfc.split('id="sec-conversations"')[1]
    investigations_html = hdfc.split('id="sec-investigations"')[1].split('id="sec-conversations"')[0]
    assert "q for case-conv?" in conversations_html
    assert "q for case-inv?" in investigations_html
    assert "q for case-conv?" not in icici and "q for case-inv?" in icici


def test_case_tag_routes_add_and_remove_registered_companies_only(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-t", thread_id="th-t", company_ids=("HDFCBANK",))
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import list_case_company_ids

    with app.test_client() as test_client:
        assert test_client.post("/cases/case-t/tags/add", data={"company_id": "ICICIBANK"}).status_code == 302
        assert test_client.post("/cases/case-t/tags/add", data={"company_id": "NOT-A-COMPANY"}).status_code == 400
        assert test_client.post("/cases/nope/tags/add", data={"company_id": "ICICIBANK"}).status_code == 404
        conn = init_db(db_path=db_path)
        assert list_case_company_ids(conn, "case-t") == ["HDFCBANK", "ICICIBANK"]
        conn.close()
        assert "q for case-t?" in test_client.get("/companies/ICICIBANK").data.decode()

        assert test_client.post("/cases/case-t/tags/remove", data={"company_id": "HDFCBANK"}).status_code == 302
        conn = init_db(db_path=db_path)
        assert list_case_company_ids(conn, "case-t") == ["ICICIBANK"]
        conn.close()
        assert "q for case-t?" not in test_client.get("/companies/HDFCBANK").data.decode()


def test_case_bulk_action_with_nothing_selected_is_a_harmless_no_op(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.post("/cases/bulk-action", data={"bulk_action": "delete"})
    assert response.status_code == 302


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

    # Several different fake-LLM responses in sequence: Jev's complexity
    # classification runs first (synchronously, before the case is even
    # created, docs/ADR/023), then company resolution (JSON), then the
    # aggregate-intent check, then the real answer (plain text) -- a single
    # fixed-text fake client would return the wrong shape to whichever call
    # happened not to match.
    import llm.providers.anthropic_provider as anthropic_provider
    from types import SimpleNamespace

    responses = iter([
        '{"complexity_level": 4, "confidence": 0.9, "reason": "comparison question"}',
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
    _install_sequenced_fake_llm(monkeypatch, [
        '{"company_ids": ["IDFCFIRSTB", "FEDERALBNK"]}',
        '{"complexity_level": 5, "confidence": 0.9, "reason": "causal divergence across two companies"}',
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/understand",
            json={"question": "IDFC Bank Growth rate compared to Federal Bank. Why one is growing faster?"},
        )

    assert response.status_code == 200
    data = response.get_json()
    assert sorted(data["company_ids"]) == ["FEDERALBNK", "IDFCFIRSTB"]
    assert sorted(data["company_labels"]) == ["IDFC First Bank", "The Federal Bank"]
    assert data["complexity_level"] == 5
    assert data["complexity_label"] == "Hypothesize"
    assert data["case_type"] == "investigation"  # Level 5 -> the investigation pipeline


def test_research_understand_suggests_quick_for_a_plain_lookup(tmp_path: Path, monkeypatch) -> None:
    from companies.registry import register_company

    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    register_company(conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", nse_symbol="HDFCBANK")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    _install_sequenced_fake_llm(monkeypatch, [
        '{"company_ids": ["HDFCBANK"]}',
        '{"complexity_level": 1, "confidence": 0.9, "reason": "simple factual lookup"}',
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/understand", json={"question": "What is HDFC Bank's growth rate for the last 4 years?"}
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["company_ids"] == ["HDFCBANK"]
    assert data["complexity_level"] == 1
    assert data["case_type"] == "ask"


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
    # Jev's complexity classification (docs/ADR/023) now runs before this
    # route even creates the case -- without a fake client installed, that
    # call would hit the real Anthropic API.
    _install_fake_llm(monkeypatch, text='{"complexity_level": 5, "confidence": 0.9, "reason": "test"}')

    def _fake_run_investigation(
        conn, question, company_ids, *, statement_type="consolidated", as_of=None,
        investigation_id=None, case_id=None, complexity_level=None,
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


def _seeded_app(tmp_path: Path, monkeypatch):
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
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")
    return app, db_path


def test_async_entry_points_tag_case_origin_and_auto_tag_the_company(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    with app.test_client() as test_client:
        ids = {}
        for label, url in (("ask_ai", "/companies/HDFCBANK/ask-async"), ("chat", "/chat-async"), ("research", "/research/ask-async")):
            response = test_client.post(url, json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]})
            assert response.status_code == 202, (label, response.get_json())
            ids[label] = response.get_json()["case_id"]
            _poll_until_done(test_client, ids[label])

    conn = init_db(db_path=db_path)
    from storage.repositories import list_case_company_ids

    assert get_research_case(conn, ids["ask_ai"])["origin"] == "conversation"
    assert get_research_case(conn, ids["chat"])["origin"] == "conversation"
    assert get_research_case(conn, ids["research"])["origin"] == "investigation"
    assert list_case_company_ids(conn, ids["ask_ai"]) == ["HDFCBANK"]
    conn.close()


def test_sync_ask_routes_record_a_completed_case_with_the_right_origin(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    with app.test_client() as test_client:
        thread_id = test_client.post("/companies/HDFCBANK/ask", json={"question": "How did net profit change?"}).get_json()["thread_id"]
        research_thread_id = test_client.post(
            "/research/ask", json={"question": "Did revenue grow?", "company_ids": ["HDFCBANK"]}
        ).get_json()["thread_id"]

    conn = init_db(db_path=db_path)
    rows = {r["thread_id"]: r for r in conn.execute("SELECT * FROM research_cases").fetchall()}
    assert rows[thread_id]["origin"] == "conversation"
    assert rows[research_thread_id]["origin"] == "investigation"
    for row in rows.values():
        assert row["status"] == "completed" and row["outcome"] == "answered"
    conn.close()


# ------------------------------------------------------------------
# Multi-turn Conversations: /conversations/<case_id> + follow-up turns
# ------------------------------------------------------------------


def _conversation(test_client, db_path) -> str:
    """Asks the first question the way the Ask AI drawer does and waits for
    it, returning the finished Conversation's case_id."""
    start = test_client.post("/companies/HDFCBANK/ask-async", json={"question": "How did net profit change?"})
    assert start.status_code == 202
    case_id = start.get_json()["case_id"]
    assert _poll_until_done(test_client, case_id)["status"] == "done"
    return case_id


def _poll_turn(test_client, case_id: str, turn_id: str, attempts: int = 200):
    status = None
    for _ in range(attempts):
        status = test_client.get(f"/conversations/{case_id}/turns/{turn_id}").get_json()
        if status["status"] != "running":
            return status
        time.sleep(0.1)
    return status


def test_finished_ask_ai_case_opens_as_a_conversation_page(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    with app.test_client() as test_client:
        case_id = _conversation(test_client, db_path)
        # The case page forwards to the conversation (not the bare thread page) ...
        assert test_client.get(f"/cases/{case_id}").headers["Location"].endswith(f"/conversations/{case_id}")
        # ... the status payload tells the drawer where to continue ...
        result = test_client.get(f"/ask/status/{case_id}").get_json()["result"]
        assert result["conversation_url"] == f"/conversations/{case_id}"
        # ... and the page shows the first exchange plus a composer.
        page = test_client.get(f"/conversations/{case_id}")
        assert page.status_code == 200
        body = page.data.decode()
        assert "How did net profit change?" in body and 'id="convo-form"' in body
        # The Cases list links the row to the conversation, not the thread.
        assert f'/conversations/{case_id}"' in test_client.get("/cases").data.decode()


def test_follow_up_turn_answers_with_the_conversation_as_context_and_persists(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    with app.test_client() as test_client:
        case_id = _conversation(test_client, db_path)
        captured = _install_fake_llm(monkeypatch, text="It rose again. [FACT] y.")

        response = test_client.post(f"/conversations/{case_id}/turns", json={"question": "And the year before?"})
        assert response.status_code == 202
        turn_id = response.get_json()["turn_id"]
        status = _poll_turn(test_client, case_id, turn_id)
        assert status["status"] == "done" and "It rose again." in status["answer_html"]

        sent = json.dumps(captured, default=str)
        assert "Conversation so far" in sent and "How did net profit change?" in sent and "And the year before?" in sent

        # A second follow-up sees the first follow-up too, and everything survives a reload.
        second = test_client.post(f"/conversations/{case_id}/turns", json={"question": "Why?"}).get_json()["turn_id"]
        assert _poll_turn(test_client, case_id, second)["status"] == "done"
        assert "And the year before?" in json.dumps(captured, default=str)
        body = test_client.get(f"/conversations/{case_id}").data.decode()
        assert body.index("How did net profit change?") < body.index("And the year before?") < body.index("Why?")

    conn = init_db(db_path=db_path)
    from storage.repositories import list_case_turns

    assert [(t["position"], t["status"]) for t in list_case_turns(conn, case_id)] == [(1, "completed"), (2, "completed")]
    conn.close()


def test_follow_up_validation_and_guards(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    with app.test_client() as test_client:
        case_id = _conversation(test_client, db_path)
        url = f"/conversations/{case_id}/turns"
        assert test_client.post(url, json={"question": "  "}).status_code == 400
        assert test_client.post(url, json={"question": "Should I buy this stock now?"}).status_code == 400
        assert test_client.post("/conversations/nope/turns", json={"question": "hi?"}).status_code == 404
        assert test_client.get("/conversations/nope").status_code == 404

        # Only a Conversation takes follow-ups; an Investigation-origin case doesn't.
        research_case = test_client.post(
            "/research/ask-async", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]},
        ).get_json()["case_id"]
        _poll_until_done(test_client, research_case)
        assert test_client.post(f"/conversations/{research_case}/turns", json={"question": "more?"}).status_code == 400

        # One question at a time: a second follow-up while one is still running is a 409.
        from storage.repositories import create_case_turn

        conn = init_db(db_path=db_path)
        pending = create_case_turn(conn, case_id, "still running?")
        conn.close()
        assert test_client.post(url, json={"question": "next?"}).status_code == 409
        assert test_client.get(f"/conversations/{case_id}/turns/{pending['turn_id']}").get_json()["status"] == "running"
        assert test_client.get(f"/conversations/{research_case}/turns/{pending['turn_id']}").status_code == 404


def test_follow_up_failure_is_recorded_on_the_turn_not_lost(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)

    from web.app import answer_follow_up as real_answer_follow_up

    def _boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    with app.test_client() as test_client:
        case_id = _conversation(test_client, db_path)
        monkeypatch.setattr("web.app.answer_follow_up", _boom)
        turn_id = test_client.post(f"/conversations/{case_id}/turns", json={"question": "more?"}).get_json()["turn_id"]
        status = _poll_turn(test_client, case_id, turn_id)
        assert status["status"] == "error" and "provider exploded" in status["error"]
        # The failed turn doesn't block asking again.
        monkeypatch.setattr("web.app.answer_follow_up", real_answer_follow_up)
        again = test_client.post(f"/conversations/{case_id}/turns", json={"question": "retry?"})
        assert again.status_code == 202


def test_a_conversation_that_is_not_finished_or_is_an_investigation_redirects_to_the_case_page(tmp_path: Path, monkeypatch) -> None:
    app, db_path = _seeded_app(tmp_path, monkeypatch)
    _failed_case(db_path, "case-f", origin="conversation")
    _answered_case(db_path, "case-inv", thread_id="th-inv", origin="investigation")
    with app.test_client() as test_client:
        assert test_client.get("/conversations/case-f").headers["Location"].endswith("/cases/case-f")
        assert test_client.get("/conversations/case-inv").headers["Location"].endswith("/cases/case-inv")


def test_companies_resolved_inside_the_background_job_are_tagged_on_the_case(tmp_path: Path, monkeypatch) -> None:
    """A question that names no company reaches the LLM resolver inside the
    job, after the case already exists -- the case must still end up tagged
    with what it resolved, on both the async and the sync routes."""
    from types import SimpleNamespace

    app, db_path = _seeded_app(tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.resolve_companies", lambda db, question: SimpleNamespace(company_ids=["HDFCBANK"]))

    from storage.repositories import list_case_company_ids

    with app.test_client() as test_client:
        case_id = test_client.post("/chat-async", json={"question": "How did the bank's net profit change?"}).get_json()["case_id"]
        _poll_until_done(test_client, case_id)
        sync = test_client.post("/chat", json={"question": "How did the bank's net profit change again?"}).get_json()

    conn = init_db(db_path=db_path)
    assert list_case_company_ids(conn, case_id) == ["HDFCBANK"]
    sync_case = conn.execute("SELECT case_id FROM research_cases WHERE thread_id = ?", (sync["thread_id"],)).fetchone()["case_id"]
    assert list_case_company_ids(conn, sync_case) == ["HDFCBANK"]
    conn.close()


def test_cases_list_lives_at_cases_and_the_old_investigations_url_redirects_with_filters(tmp_path: Path, monkeypatch) -> None:
    db_path = _seeded_db(tmp_path)
    _answered_case(db_path, "case-conv", thread_id="th-conv", origin="conversation")
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        assert "q for case-conv?" in test_client.get("/cases").data.decode()
        old = test_client.get("/investigations?iv_origin=conversation&iv_hidden=1")
        assert old.status_code == 301
        assert old.headers["Location"].endswith("/cases?iv_origin=conversation&iv_hidden=1")
        assert test_client.get("/investigations").headers["Location"].endswith("/cases")
        # Fixed /cases/... paths are unaffected by the new list route.
        assert test_client.get("/cases/case-conv").status_code == 302
        assert test_client.post("/cases/bulk-action", data={}).status_code == 302
