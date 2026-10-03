"""research/scope_resolver.py -- the Anthropic client is mocked, same pattern as
tests/test_company_resolver.py. Seeds the real production miss: a question about
Nvidia that says "services mix" was scoped to the Services sector's companies."""

from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from research.scope_resolver import resolve_scope
from tests.test_company_resolver import _install_fake_client


@pytest.fixture
def scope_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "NVDA", "NVIDIA Corporation", "Nvidia", nse_symbol="NVDA")
    register_company(db_conn, "DELHIVERY", "Delhivery Limited", "Delhivery", nse_symbol="DELHIVERY")
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", nse_symbol="HDFCBANK")
    return db_conn


QUESTION = "Why did Nvidia (USA) operating margin expand from FY2018 to FY2025: services mix, gross margin, or operating leverage?"


def test_named_company_wins_over_an_incidental_sector_word(scope_conn, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": ["NVDA"], "groups": []}')
    # Text matching alone would return the Services sector here; it must not even be consulted.
    monkeypatch.setattr(
        "research.scope_resolver.resolve_tags_in_text", lambda *a, **k: pytest.fail("tags consulted")
    )
    scope = resolve_scope(scope_conn, QUESTION)
    assert scope.company_ids == ["NVDA"] and scope.source == "companies"


def test_invented_company_ids_are_dropped(scope_conn, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": ["TSLA"], "groups": []}')
    scope = resolve_scope(scope_conn, "How is Nvidia doing?")
    assert scope.company_ids == [] and scope.source == "none"


def test_group_phrases_not_the_whole_question_go_through_the_tag_resolver(scope_conn, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": [], "groups": ["Nifty 50"]}')
    seen = []
    monkeypatch.setattr(
        "research.scope_resolver.resolve_tags_in_text", lambda conn, text: seen.append(text) or ["HDFCBANK"]
    )
    scope = resolve_scope(scope_conn, "Which Nifty 50 companies grew margins, mix of services aside?")
    assert scope.company_ids == ["HDFCBANK"] and scope.source == "groups"
    assert seen[-1] == "Nifty 50"  # the group phrase only, never the whole question


def test_macro_question_has_no_scope(scope_conn, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": [], "groups": []}')
    assert resolve_scope(scope_conn, "What has the repo rate done over 3 years?").company_ids == []


def test_no_candidates_and_no_group_skips_the_llm_call(scope_conn, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, '{"company_ids": [], "groups": []}')
    scope = resolve_scope(scope_conn, "asdkfjaslkdjf nonsense query")
    assert scope.source == "none" and captured == []


def test_unparseable_response_falls_back_to_text_matching(scope_conn, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json at all")
    monkeypatch.setattr("research.scope_resolver.resolve_tags_in_text", lambda *a, **k: ["DELHIVERY"])
    scope = resolve_scope(scope_conn, QUESTION)
    assert scope.company_ids == ["DELHIVERY"] and scope.source == "fallback"
