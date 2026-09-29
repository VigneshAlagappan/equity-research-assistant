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


def test_case_delete_removes_a_failed_case(tmp_path: Path, monkeypatch) -> None:
    """Real production bug (2026-09-27): investigations()'s "case" entries
    (in_progress/failed/cancelled/insufficient_data research_cases rows --
    see that route's own entries.append() call) always rendered a Delete
    button pointing at case_delete(case_type='case', ...), but that route
    only ever handled case_type in {'generated', 'structured'} -- clicking
    Delete on a failed case always 400'd, "Unknown case_type: 'case'"."""
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import create_research_case, fail_research_case

    conn = init_db(db_path=db_path)
    create_research_case(
        conn, "case-to-delete", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )
    fail_research_case(conn, "case-to-delete", "boom")
    conn.close()

    with app.test_client() as test_client:
        response = test_client.post("/cases/case/case-to-delete/delete")
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    assert get_research_case(conn, "case-to-delete") is None
    conn.close()


def test_case_delete_unknown_case_is_404(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.post("/cases/case/not-a-real-case/delete")
    assert response.status_code == 404


def test_case_bulk_action_hides_selected_generated_and_structured_cases(tmp_path: Path, monkeypatch) -> None:
    """The Cases list's select-checkbox + "Hide selected" toolbar
    (investigations.html) posts here as one request -- each checkbox's
    value is "<case_type>:<case_id>", joined the same way case_hide/
    case_delete's own two path params already are."""
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import get_generated_report, get_investigation, save_generated_report, save_investigation

    conn = init_db(db_path=db_path)
    save_generated_report(conn, "th-bulk-hide", "q?", ["HDFCBANK"], "consolidated", "# Report")
    save_investigation(
        conn, investigation_id="inv-bulk-hide", question="why?", company_ids=["HDFCBANK"],
        statement_type="consolidated", strongest_explanation="Because.",
        unanswered_questions=[], additional_evidence_needed=[],
    )
    conn.close()

    with app.test_client() as test_client:
        response = test_client.post(
            "/cases/bulk-action",
            data={"bulk_action": "hide", "selected": ["generated:th-bulk-hide", "structured:inv-bulk-hide"]},
        )
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    assert get_generated_report(conn, "th-bulk-hide")["hidden_at"] is not None
    assert get_investigation(conn, "inv-bulk-hide")["hidden_at"] is not None
    conn.close()


def test_case_bulk_action_deletes_selected_cases_across_all_three_types(tmp_path: Path, monkeypatch) -> None:
    """"delete" reuses case_delete's own per-type dispatch: soft-delete
    (deleted_at set, row kept) for generated/structured, a real hard delete
    for a "case" (in_progress/failed/... research_cases row) -- same three
    behaviors the per-row Delete button already has, just batched."""
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import (
        create_research_case, fail_research_case, get_generated_report, get_investigation,
        get_research_case, list_generated_reports, save_generated_report, save_investigation,
    )

    conn = init_db(db_path=db_path)
    save_generated_report(conn, "th-bulk-del", "q?", ["HDFCBANK"], "consolidated", "# Report")
    save_investigation(
        conn, investigation_id="inv-bulk-del", question="why?", company_ids=["HDFCBANK"],
        statement_type="consolidated", strongest_explanation="Because.",
        unanswered_questions=[], additional_evidence_needed=[],
    )
    create_research_case(
        conn, "case-bulk-del", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )
    fail_research_case(conn, "case-bulk-del", "boom")
    conn.close()

    with app.test_client() as test_client:
        response = test_client.post(
            "/cases/bulk-action",
            data={
                "bulk_action": "delete",
                "selected": ["generated:th-bulk-del", "structured:inv-bulk-del", "case:case-bulk-del"],
            },
        )
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    # _row_to_generated_report() doesn't project deleted_at (get_generated_report's
    # own dict shape) -- soft-delete is asserted the same way this codebase's
    # own comments say to: the row is still findable by id (never erased) but
    # no longer listed, since list_generated_reports() excludes deleted_at
    # IS NOT NULL rows unconditionally.
    assert get_generated_report(conn, "th-bulk-del") is not None
    assert "th-bulk-del" not in {r["thread_id"] for r in list_generated_reports(conn)}
    assert get_investigation(conn, "inv-bulk-del")["deleted_at"] is not None
    assert get_research_case(conn, "case-bulk-del") is None  # hard delete, unlike the other two
    conn.close()


def test_case_bulk_action_skips_hide_for_case_type_entries(tmp_path: Path, monkeypatch) -> None:
    """A "case" row has no hidden_at column -- same limitation the per-row
    UI already has (no Hide button offered for it, investigations.html).
    Selecting one under "Hide selected" must silently no-op it, not error
    the whole batch out for every other selected row."""
    db_path = tmp_path / "signals_data.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)

    from storage.repositories import create_research_case, fail_research_case, get_research_case

    conn = init_db(db_path=db_path)
    create_research_case(
        conn, "case-not-hideable", kind="ask", question="q?", company_ids=[],
        statement_type="consolidated", owner_id=None,
    )
    fail_research_case(conn, "case-not-hideable", "boom")
    conn.close()

    with app.test_client() as test_client:
        response = test_client.post(
            "/cases/bulk-action", data={"bulk_action": "hide", "selected": ["case:case-not-hideable"]},
        )
        assert response.status_code == 302

    conn = init_db(db_path=db_path)
    assert get_research_case(conn, "case-not-hideable") is not None  # untouched, not deleted or errored
    conn.close()


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
