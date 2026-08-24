"""research/macro_evidence.py — the third evidence source research/assistant.py
grounds answers in. get_macro_evidence() tries an LLM call first (_plan_retrieval:
"which catalog series, what year range") and falls back to the original
keyword/regex heuristic (_matching_series/_year_range) only if that call is
unavailable or returns something unparseable — both paths are exercised below."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from llm.router import AllProvidersUnavailableError, Attempt
from research.macro_evidence import get_macro_evidence
from sources.macro import MacroNormalizedObservation
from storage.repositories import insert_macro_observations


def _insert(conn: sqlite3.Connection, series_key: str, period: str, value: float, unit: str, source: str) -> None:
    insert_macro_observations(
        conn,
        [
            MacroNormalizedObservation(
                series_key=series_key, period_type="annual" if len(period) == 4 else "monthly",
                period=period, value=value, unit=unit, source=source,
                source_file=f"data/raw/_macro/{source}/{series_key}.csv", parser_version="test-v1",
            )
        ],
    )


class _FakeMessages:
    def __init__(self, text: str, captured: list) -> None:
        self._text = text
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn")


class _FakeClient:
    def __init__(self, text: str, captured: list) -> None:
        self.messages = _FakeMessages(text, captured)


def _install_fake_planner(monkeypatch, text: str) -> list:
    """Mocks the planner's LLM call to return `text` verbatim as the
    SERIES:/YEARS: response — exercises the real _plan_retrieval parsing."""
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: _FakeClient(text, captured)
    )
    return captured


def _make_unavailable(monkeypatch) -> None:
    """Forces _plan_retrieval's route() call to fail, so get_macro_evidence
    exercises its keyword/regex fallback path instead."""
    monkeypatch.setattr(
        "research.macro_evidence.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )


# ------------------------------------------------------------------
# LLM planner path (_plan_retrieval) — the primary path.
# ------------------------------------------------------------------


def test_planner_picks_the_series_and_year_range_it_names(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _insert(db_conn, "repo_rate", "2020", 6.0, "PERCENT", "rbi")
    captured = _install_fake_planner(monkeypatch, "SERIES: rainfall_regional_annual\nYEARS: 2000-2026")

    evidence = get_macro_evidence(db_conn, "What was rainfall in India recently?")

    assert len(evidence) == 1
    assert evidence[0].label == "Rainfall Regional 2020"
    assert evidence[0].company_id == "INDIA"
    assert "1,100.5 MILLIMETRES" in evidence[0].value
    # the catalog sent to the model lists every candidate series with its
    # date coverage, not just the one it happened to pick
    sent = captured[0]["messages"][0]["content"]
    assert "rainfall_regional_annual (iitm, 2020-2020)" in sent
    assert "repo_rate (rbi, 2020-2020)" in sent


def test_planner_none_response_returns_no_evidence(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _install_fake_planner(monkeypatch, "SERIES: NONE\nYEARS: ALL")

    assert get_macro_evidence(db_conn, "How did net profit grow this quarter?") == []


def test_planner_ignores_a_hallucinated_series_key(db_conn: sqlite3.Connection, monkeypatch) -> None:
    """A series_key the model names that isn't in the real catalog is
    silently dropped, never fetched — the LLM only ever selects from real
    data, it doesn't get to invent what exists."""
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _install_fake_planner(monkeypatch, "SERIES: rainfall_regional_annual, made_up_series\nYEARS: ALL")

    evidence = get_macro_evidence(db_conn, "What was rainfall in India?")

    assert [e.label for e in evidence] == ["Rainfall Regional 2020"]


def test_planner_years_all_applies_no_lower_bound(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn, "rainfall_regional_annual", "1850", 1000.0, "MILLIMETRES", "iitm")
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _install_fake_planner(monkeypatch, "SERIES: rainfall_regional_annual\nYEARS: ALL")

    evidence = get_macro_evidence(db_conn, "How has rainfall in India changed historically?")

    assert {e.label.split()[-1] for e in evidence} == {"1850", "2020"}


def test_planner_unparseable_response_falls_back_to_heuristic(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _install_fake_planner(monkeypatch, "I'm not sure how to answer that.")

    evidence = get_macro_evidence(db_conn, "What was rainfall in India recently?")

    assert len(evidence) == 1  # keyword heuristic still matched "rainfall"
    assert evidence[0].label == "Rainfall Regional 2020"


def test_planner_skips_llm_call_when_no_macro_data_ingested(db_conn: sqlite3.Connection, monkeypatch) -> None:
    """No macro data ingested at all -> _plan_retrieval short-circuits before
    ever calling the LLM (nothing in the catalog to choose from)."""
    called = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: called.append(1) or _FakeClient("SERIES: NONE\nYEARS: ALL", []),
    )
    assert get_macro_evidence(db_conn, "What was rainfall in India?") == []
    assert called == []


# ------------------------------------------------------------------
# Keyword/regex fallback path (_matching_series/_year_range) — exercised
# when the LLM call is unavailable.
# ------------------------------------------------------------------


def test_fallback_matches_by_shared_word(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _make_unavailable(monkeypatch)
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _insert(db_conn, "repo_rate", "2020", 6.0, "PERCENT", "rbi")

    evidence = get_macro_evidence(db_conn, "What was rainfall in India recently?")
    assert len(evidence) == 1
    assert evidence[0].label == "Rainfall Regional 2020"
    assert evidence[0].company_id == "INDIA"
    assert "1,100.5 MILLIMETRES" in evidence[0].value


def test_fallback_returns_empty_for_unrelated_question(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _make_unavailable(monkeypatch)
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    assert get_macro_evidence(db_conn, "How did net profit grow this quarter?") == []


def test_fallback_respects_last_n_years_window(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _make_unavailable(monkeypatch)
    _insert(db_conn, "repo_rate", "1990", 10.0, "PERCENT", "rbi")
    _insert(db_conn, "repo_rate", "2020", 6.0, "PERCENT", "rbi")

    evidence = get_macro_evidence(db_conn, "What was the repo rate over the last 10 years?")
    periods = [e.label.split()[-1] for e in evidence]
    assert "2020" in periods
    assert "1990" not in periods


def test_fallback_excludes_iitm_monthly_suffix_series(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _make_unavailable(monkeypatch)
    _insert(db_conn, "rainfall_regional_annual", "2020", 1100.5, "MILLIMETRES", "iitm")
    _insert(db_conn, "rainfall_regional_jun", "2020", 200.0, "MILLIMETRES", "iitm")

    evidence = get_macro_evidence(db_conn, "What was rainfall in India?")
    assert [e.label for e in evidence] == ["Rainfall Regional 2020"]


def test_fallback_respects_absolute_decade_range(db_conn: sqlite3.Connection, monkeypatch) -> None:
    """"1950s to early 2000s" has no "last N years" phrasing — it must still
    resolve to an absolute range rather than silently falling back to
    DEFAULT_YEAR_WINDOW years back from today, which for an old series like
    IITM's 8-all_ind.txt (1813-2006) would drop the very period asked about."""
    _make_unavailable(monkeypatch)
    _insert(db_conn, "rainfall_regional_annual", "1955", 1100.0, "MILLIMETRES", "iitm")
    _insert(db_conn, "rainfall_regional_annual", "1999", 1200.0, "MILLIMETRES", "iitm")
    _insert(db_conn, "rainfall_regional_annual", "2020", 1300.0, "MILLIMETRES", "iitm")

    evidence = get_macro_evidence(
        db_conn, "how has rainfall in india from 1950s to early 2000s changed"
    )
    periods = {e.label.split()[-1] for e in evidence}
    assert periods == {"1955", "1999"}


def test_fallback_downsamples_to_one_point_per_year(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _make_unavailable(monkeypatch)
    for month in ("01", "02", "03"):
        _insert(db_conn, "policy_repo_rate", f"2020-{month}", 6.0, "PERCENT", "rbi")

    evidence = get_macro_evidence(db_conn, "What was the policy repo rate trend?")
    assert len(evidence) == 1
