"""research/company_resolver.py -- the Anthropic client is mocked
throughout, same pattern as tests/test_knowledge_builder.py. Seeds
companies shaped exactly like the real production bug this module fixes:
"IDFC First Bank" (a user would naturally write "IDFC Bank") and "The
Federal Bank" (a user would naturally write "Federal Bank")."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from companies.registry import register_company
from research.company_resolver import CompanyResolution, resolve_companies


class _FakeMessages:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self._text = text
        self._stop_reason = stop_reason
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        content = [SimpleNamespace(type="text", text=self._text)] if self._text else []
        return SimpleNamespace(content=content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, text: str | None, stop_reason: str, captured: list) -> None:
        self.messages = _FakeMessages(text, stop_reason, captured)


def _install_fake_client(monkeypatch, text: str | None, stop_reason: str = "end_turn") -> list:
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(text, stop_reason, captured),
    )
    return captured


@pytest.fixture
def bank_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "IDFCFIRSTB", "IDFC First Bank Limited", "IDFC First Bank", nse_symbol="IDFCFIRSTB")
    register_company(db_conn, "FEDERALBNK", "The Federal Bank Limited", "The Federal Bank", nse_symbol="FEDERALBNK")
    register_company(db_conn, "IDFC", "IDFC Limited", "IDFC Limited", nse_symbol="IDFC")
    register_company(db_conn, "HDFCBANK", "HDFC Bank Limited", "HDFC Bank", nse_symbol="HDFCBANK")
    return db_conn


def test_resolves_shortened_names_that_the_old_regex_matcher_missed(bank_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, '{"company_ids": ["IDFCFIRSTB", "FEDERALBNK"]}')

    resolution = resolve_companies(
        bank_conn, "IDFC Bank Growth rate in last 5 years compared to Federal Bank. Why one bank is growing faster?"
    )

    assert resolution.company_ids == ["IDFCFIRSTB", "FEDERALBNK"]
    assert [c.display_name for c in resolution.companies] == ["IDFC First Bank", "The Federal Bank"]
    # the candidate pool actually offered to the model included the real
    # near-duplicate (IDFC Limited) it had to correctly disambiguate away from
    sent_system = captured[0]["system"]
    assert "IDFC Limited" in sent_system
    assert "IDFC First Bank" in sent_system


def test_no_candidates_at_all_returns_empty_without_calling_the_model(db_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, "should never be returned")

    resolution = resolve_companies(db_conn, "what was rainfall in India over the last 50 years?")

    assert resolution.companies == []
    assert captured == []


def test_hallucinated_company_id_outside_the_candidate_pool_is_dropped(bank_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": ["IDFCFIRSTB", "NOTAREALCOMPANY"]}')

    resolution = resolve_companies(bank_conn, "How is IDFC Bank doing?")

    assert resolution.company_ids == ["IDFCFIRSTB"]


def test_macro_question_with_no_company_returns_empty(bank_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"company_ids": []}')

    resolution = resolve_companies(bank_conn, "What has the repo rate done over the last 3 years?")

    assert resolution.companies == []


def test_unparseable_response_returns_empty_not_raises(bank_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json at all")

    resolution = resolve_companies(bank_conn, "How is HDFC Bank doing?")

    assert resolution.companies == []


def test_all_providers_unavailable_returns_empty_not_raises(bank_conn: sqlite3.Connection, monkeypatch) -> None:
    import anthropic

    class _FailingMessages:
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=SimpleNamespace())

    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: SimpleNamespace(messages=_FailingMessages()),
    )
    monkeypatch.setattr("llm.router._OLLAMA_AVAILABLE_CACHE", (False, 0.0), raising=False)

    resolution = resolve_companies(bank_conn, "How is HDFC Bank doing?")

    assert resolution.companies == []
