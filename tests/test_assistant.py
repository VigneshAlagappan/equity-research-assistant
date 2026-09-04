"""research/assistant.py tests — the Anthropic client is mocked throughout.
No real API key or network access is required for this suite; a real key is
only used for the manual live smoke test, never for committed tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from research.assistant import _select_model, answer_question
from tests.test_screener_adapter import _make_screener_workbook


class _FakeMessages:
    def __init__(self, content: list, stop_reason: str, captured: list) -> None:
        self._content = content
        self._stop_reason = stop_reason
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(content=self._content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, content: list, stop_reason: str, captured: list) -> None:
        self.messages = _FakeMessages(content, stop_reason, captured)


def _content_text(content) -> str:
    """Flattens a message's `content` into one string regardless of shape —
    a plain string (no cacheable_prefix) or a list of content blocks (one
    per text block, cache_control marked or not — llm/providers/
    anthropic_provider.py). Lets prompt-content assertions stay shape-
    agnostic instead of caring which form a given call happened to use."""
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content)


def _install_fake_client(monkeypatch, text: str = "answer", stop_reason: str = "end_turn"):
    captured: list = []
    content = [SimpleNamespace(type="text", text=text)] if text else []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: _FakeClient(content, stop_reason, captured),
    )
    return captured


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_answer_question_returns_llm_text(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, text="Net profit grew. [FACT] ...")

    result = answer_question(ingested_conn, "How did net profit change?", ["HDFCBANK"])

    assert result == "Net profit grew. [FACT] ..."


def test_answer_question_sends_evidence_in_the_prompt(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch)

    answer_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])

    assert len(captured) == 1
    sent = _content_text(captured[0]["messages"][0]["content"])
    assert "Net Profit FY2024" in sent
    assert "20,500.00" in sent
    assert "Question: What was net profit in FY2024?" in sent
    assert captured[0]["system"]  # system prompt was sent


def test_answer_question_without_any_data_skips_the_api_call(db_conn: sqlite3.Connection, monkeypatch) -> None:
    seed_companies(db_conn)
    captured = _install_fake_client(monkeypatch, text="should never be returned")

    result = answer_question(db_conn, "How is HDFC doing?", ["HDFCBANK"])

    assert "No data ingested yet" in result
    assert captured == []  # never called the API — no evidence to ground an answer in


def test_answer_question_handles_refusal(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, text="", stop_reason="refusal")

    result = answer_question(ingested_conn, "test question", ["HDFCBANK"])

    assert "declined" in result.lower()


def test_answer_question_handles_empty_non_refusal_response(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, text="", stop_reason="max_tokens")

    result = answer_question(ingested_conn, "test question", ["HDFCBANK"])

    assert "no answer" in result.lower()


def test_injected_investigation_memory_capability_is_used(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    """Proves the Investigation Memory capability seam
    (research/capabilities.py's InvestigationMemoryCapabilities, architecture
    guardrail #2) is actually wired into answer_question — an injected fake
    `reusable_report` callable short-circuits the whole evidence/LLM path,
    same as a real context/reuse.py hit would."""
    from context.reuse import ReuseCandidate
    from research.capabilities import InvestigationMemoryCapabilities

    captured = _install_fake_client(monkeypatch, text="should never be returned")
    fake_candidate = ReuseCandidate(
        thread_id="fake-t1", report_markdown="# fake reused answer", evidence=[], followups=[],
        similarity=1.0, generated_at="2099-01-01T00:00:00Z",
    )
    mem = InvestigationMemoryCapabilities(
        reusable_report=lambda conn, question, company_ids, statement_type: fake_candidate,
        related_investigations=lambda conn, question, company_ids: [],
    )

    result = answer_question(
        ingested_conn, "How did net profit change?", ["HDFCBANK"], investigation_memory=mem
    )

    assert result == "# fake reused answer"
    assert captured == []  # never called the API — served from the injected capability


def test_answer_question_comparison_includes_both_companies(
    tmp_path: Path, ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    file_path = tmp_path / "ICICIBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(ingested_conn, file_path, company_id="ICICIBANK", source_id="screener")

    captured = _install_fake_client(monkeypatch)
    answer_question(ingested_conn, "Compare these banks", ["HDFCBANK", "ICICIBANK"])

    sent = _content_text(captured[0]["messages"][0]["content"])
    assert "HDFCBANK —" in sent
    assert "ICICIBANK —" in sent


def test_financial_evidence_is_sent_as_a_stable_cacheable_prefix(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    """The whole point of the Financials/Docs-Macro evidence split
    (research/assistant.py's docstring): the Financials block must be
    marked cache_control AND byte-identical across two different questions
    about the same company, since that's what actually makes Anthropic's
    prompt cache hit on the second call — a block that changes every call
    would never cache regardless of the marker."""
    captured = _install_fake_client(monkeypatch)

    answer_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])
    answer_question(ingested_conn, "How did the CASA ratio trend?", ["HDFCBANK"])

    assert len(captured) == 2
    for call in captured:
        content = call["messages"][0]["content"]
        assert isinstance(content, list)  # split into blocks, not one flat string
        financial_block, rest_block = content
        assert financial_block.get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in rest_block
        assert financial_block["text"].startswith("Evidence (Financials):")

    first_financial_text = captured[0]["messages"][0]["content"][0]["text"]
    second_financial_text = captured[1]["messages"][0]["content"][0]["text"]
    assert first_financial_text == second_financial_text  # byte-identical -> actually cacheable

    first_question_text = captured[0]["messages"][0]["content"][1]["text"]
    second_question_text = captured[1]["messages"][0]["content"][1]["text"]
    assert first_question_text != second_question_text  # the variable part still varies


# ------------------------------------------------------------------
# Auto-routing — no ANTHROPIC_MODEL env override and no explicit `model`
# argument means answer_question picks a model tier per question itself
# (_select_model), rather than always calling the top-tier model.
# ------------------------------------------------------------------


def test_select_model_routes_quick_lookup_to_the_cheap_tier() -> None:
    assert _select_model("What was net profit in FY2024?", ["HDFCBANK"], evidence_count=8) == "claude-haiku-4-5"


def test_select_model_routes_analysis_question_to_the_top_tier() -> None:
    # All three tiers currently prefer Haiku (config.settings.TIER_PREFERRED_MODEL
    # — a deliberate cost policy) — _select_model is a thin wrapper around that
    # config, not the router's actual fallback chain, which still enforces
    # DEEP's higher TIER_MIN_REASONING_STRENGTH and would skip Haiku there.
    assert _select_model("Why did net profit decline in FY2020?", ["HDFCBANK"], evidence_count=20) == "claude-haiku-4-5"


def test_select_model_routes_peer_comparison_to_the_top_tier_regardless_of_wording() -> None:
    assert _select_model("net profit", ["HDFCBANK", "ICICIBANK"], evidence_count=5) == "claude-haiku-4-5"


def test_select_model_routes_generic_question_to_the_mid_tier() -> None:
    assert _select_model("How has net profit grown over the years?", ["HDFCBANK"], evidence_count=25) == "claude-haiku-4-5"


def test_answer_question_auto_routes_without_an_explicit_model(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    """No `model` argument and no ANTHROPIC_MODEL override means the tier
    comes from _select_model, applied to whatever evidence this question
    actually retrieves — not a hardcoded tier, since the exact evidence
    count depends on the ingestion fixture."""
    from research.documents import get_document_evidence
    from retrieval.structured_search import get_comparison_evidence

    monkeypatch.setattr("config.settings.ANTHROPIC_MODEL", None)
    monkeypatch.setattr("research.assistant.ANTHROPIC_MODEL", None)
    captured = _install_fake_client(monkeypatch)
    question = "What was net profit in FY2024?"

    answer_question(ingested_conn, question, ["HDFCBANK"])

    evidence_count = len(get_comparison_evidence(ingested_conn, ["HDFCBANK"], "consolidated")) + len(
        get_document_evidence(ingested_conn, "HDFCBANK", question)
    )
    assert captured[0]["model"] == _select_model(question, ["HDFCBANK"], evidence_count)


def test_answer_question_honors_env_pinned_model_over_auto_routing(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    """A quick-lookup question would normally route to Haiku — but an operator
    who pinned ANTHROPIC_MODEL wants every call on that one model."""
    monkeypatch.setattr("research.assistant.ANTHROPIC_MODEL", "claude-sonnet-5")
    captured = _install_fake_client(monkeypatch)

    answer_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"])

    assert captured[0]["model"] == "claude-sonnet-5"


def test_answer_question_explicit_model_wins_over_everything(
    ingested_conn: sqlite3.Connection, monkeypatch
) -> None:
    monkeypatch.setattr("research.assistant.ANTHROPIC_MODEL", "claude-sonnet-5")
    captured = _install_fake_client(monkeypatch)

    answer_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"], model="claude-haiku-4-5")

    assert captured[0]["model"] == "claude-haiku-4-5"


def test_answer_question_explicit_opus_pin_is_blocked(ingested_conn: sqlite3.Connection, monkeypatch) -> None:
    """Opus is disabled by operator policy (llm/capability_registry.py) —
    even an explicit model="claude-opus-5" pin can't reach it; the call
    degrades to the same "temporarily unavailable" message an outage would
    produce, rather than silently using Opus anyway."""
    captured = _install_fake_client(monkeypatch)

    result = answer_question(ingested_conn, "What was net profit in FY2024?", ["HDFCBANK"], model="claude-opus-5")

    assert captured == []
    assert "temporarily unavailable" in result
