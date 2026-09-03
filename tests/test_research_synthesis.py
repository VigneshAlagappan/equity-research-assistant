"""research/research_synthesis.py (Step 2H) — Anthropic client mocked, real
db_conn fixture (synthesize() writes to llm_call_log)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from research.hypothesis_evaluator import HypothesisEvaluation
from research.hypothesis_generator import Hypothesis
from research.research_synthesis import ResearchSynthesisError, synthesize


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


def _hyp(hid: str, category: str = "financial") -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid, investigation_id="inv1", statement=f"Statement for {hid}", mechanism="mechanism",
        category=category, companies=["HDFCBANK"], rationale="rationale", generation_order=0,
    )


def _eval(hid: str, verdict: str) -> HypothesisEvaluation:
    return HypothesisEvaluation(hypothesis_id=hid, verdict=verdict, confidence_basis="basis")


_VALID_RESPONSE = """{
  "strongest_explanation": "H1 is the strongest explanation given the supporting evidence.",
  "ranked_hypothesis_ids": ["inv1-h1", "inv1-h2"],
  "unanswered_questions": ["What drove the specific timing of the change?"],
  "additional_evidence_needed": ["Peer comparison data"]
}"""


def test_synthesizes_and_ranks(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    hypotheses = [_hyp("inv1-h1"), _hyp("inv1-h2")]
    evaluations = {"inv1-h1": _eval("inv1-h1", "SUPPORTED"), "inv1-h2": _eval("inv1-h2", "PARTIALLY_SUPPORTED")}

    synthesis = synthesize(db_conn, "Why did margins decline?", hypotheses, evaluations)

    assert synthesis.strongest_explanation.startswith("H1 is the strongest")
    assert synthesis.ranked_hypothesis_ids == ["inv1-h1", "inv1-h2"]
    assert synthesis.unanswered_questions == ["What drove the specific timing of the change?"]
    assert synthesis.additional_evidence_needed == ["Peer comparison data"]


def test_hallucinated_hypothesis_id_is_dropped_from_ranking(db_conn: sqlite3.Connection, monkeypatch) -> None:
    response = (
        '{"strongest_explanation": "x", "ranked_hypothesis_ids": ["inv1-h1", "inv1-h999"], '
        '"unanswered_questions": [], "additional_evidence_needed": []}'
    )
    _install_fake_client(monkeypatch, response)
    hypotheses = [_hyp("inv1-h1")]
    evaluations = {"inv1-h1": _eval("inv1-h1", "SUPPORTED")}

    synthesis = synthesize(db_conn, "question", hypotheses, evaluations)
    assert synthesis.ranked_hypothesis_ids == ["inv1-h1"]  # inv1-h999 never existed, silently dropped


def test_an_unevaluated_hypothesis_cannot_be_ranked(db_conn: sqlite3.Connection, monkeypatch) -> None:
    """A hypothesis whose own evaluation failed (missing from `evaluations`)
    is excluded from the valid-ids set, even if the model tries to rank it."""
    response = (
        '{"strongest_explanation": "x", "ranked_hypothesis_ids": ["inv1-h1", "inv1-h2"], '
        '"unanswered_questions": [], "additional_evidence_needed": []}'
    )
    _install_fake_client(monkeypatch, response)
    hypotheses = [_hyp("inv1-h1"), _hyp("inv1-h2")]
    evaluations = {"inv1-h1": _eval("inv1-h1", "SUPPORTED")}  # h2 never evaluated

    synthesis = synthesize(db_conn, "question", hypotheses, evaluations)
    assert synthesis.ranked_hypothesis_ids == ["inv1-h1"]


def test_unparseable_response_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json")

    with pytest.raises(ResearchSynthesisError):
        synthesize(db_conn, "question", [_hyp("inv1-h1")], {"inv1-h1": _eval("inv1-h1", "SUPPORTED")})


def test_refusal_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "won't answer", stop_reason="refusal")

    with pytest.raises(ResearchSynthesisError):
        synthesize(db_conn, "question", [_hyp("inv1-h1")], {"inv1-h1": _eval("inv1-h1", "SUPPORTED")})


def test_truncated_response_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '{"strongest_explanation": "cut off', stop_reason="max_tokens")

    with pytest.raises(ResearchSynthesisError, match="truncated"):
        synthesize(db_conn, "question", [_hyp("inv1-h1")], {"inv1-h1": _eval("inv1-h1", "SUPPORTED")})


def test_all_providers_unavailable_raises(db_conn: sqlite3.Connection, monkeypatch) -> None:
    from llm.router import AllProvidersUnavailableError, Attempt

    monkeypatch.setattr(
        "research.research_synthesis.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )

    with pytest.raises(ResearchSynthesisError):
        synthesize(db_conn, "question", [_hyp("inv1-h1")], {"inv1-h1": _eval("inv1-h1", "SUPPORTED")})


def test_render_excludes_unevaluated_hypotheses(db_conn: sqlite3.Connection, monkeypatch) -> None:
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)
    hypotheses = [_hyp("inv1-h1"), _hyp("inv1-h2")]
    evaluations = {"inv1-h1": _eval("inv1-h1", "SUPPORTED")}  # h2 unevaluated

    synthesize(db_conn, "question", hypotheses, evaluations)

    user_message = captured[0]["messages"][0]["content"]
    assert "inv1-h1" in user_message
    assert "inv1-h2" not in user_message
