"""research/investigation.py — the 2E-2H orchestrator end-to-end (Anthropic
mocked, real db_conn). A single fake client dispatches on the `system`
kwarg since generation/evaluation/synthesis all route through the same
patched anthropic.Anthropic() call point. investigation_id is pinned via a
fixed uuid4 so hypothesis_ids (f"{investigation_id}-h{n}") are known ahead
of time and can be embedded in the canned synthesis response."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from research.hypothesis_evaluator import HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT
from research.hypothesis_generator import HYPOTHESIS_GENERATOR_SYSTEM_PROMPT
from research.investigation import InvestigationError, run_investigation
from research.research_synthesis import RESEARCH_SYNTHESIS_SYSTEM_PROMPT
from storage.repositories import (
    get_investigation,
    list_investigation_hypotheses,
    list_investigation_hypothesis_evidence,
)

_GENERATOR_PREFIX = HYPOTHESIS_GENERATOR_SYSTEM_PROMPT[:40]
_EVALUATOR_PREFIX = HYPOTHESIS_EVALUATOR_SYSTEM_PROMPT[:40]
_SYNTHESIS_PREFIX = RESEARCH_SYNTHESIS_SYSTEM_PROMPT[:40]

_FIXED_INVESTIGATION_ID = "abcdef012345"
_H1 = f"{_FIXED_INVESTIGATION_ID}-h1"
_H2 = f"{_FIXED_INVESTIGATION_ID}-h2"

_HYPOTHESES_RESPONSE = """[
  {
    "statement": "Input costs increased, compressing margins.",
    "mechanism": "Higher raw material prices raised COGS faster than revenue grew.",
    "category": "financial",
    "rationale": "A common driver of margin decline.",
    "known_relationships": [],
    "unknowns": []
  },
  {
    "statement": "Competitive pricing pressure reduced margins.",
    "mechanism": "A competitor's price cuts forced matching price reductions.",
    "category": "competitive",
    "rationale": "Plausible given the sector.",
    "known_relationships": [],
    "unknowns": []
  }
]"""

_EVALUATION_RESPONSE = """{
  "verdict": "SUPPORTED",
  "confidence_basis": "Consistent with the limited evidence available.",
  "supporting_evidence": [],
  "contradicting_evidence": [],
  "missing_evidence": ["More granular cost data"]
}"""

_SYNTHESIS_RESPONSE = f"""{{
  "strongest_explanation": "Input cost inflation is the most likely driver.",
  "ranked_hypothesis_ids": ["{_H1}", "{_H2}"],
  "unanswered_questions": ["What specific inputs rose in price?"],
  "additional_evidence_needed": ["Supplier contract terms"]
}}"""


class _DispatchMessages:
    def __init__(self, captured: list, *, evaluation_text: str = _EVALUATION_RESPONSE) -> None:
        self._captured = captured
        self._evaluation_text = evaluation_text

    def create(self, **kwargs):
        self._captured.append(kwargs)
        system = kwargs.get("system", "")
        if system.startswith(_GENERATOR_PREFIX):
            text = _HYPOTHESES_RESPONSE
        elif system.startswith(_EVALUATOR_PREFIX):
            text = self._evaluation_text
        elif system.startswith(_SYNTHESIS_PREFIX):
            text = _SYNTHESIS_RESPONSE
        else:
            raise AssertionError(f"unrecognized system prompt: {system[:60]!r}")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


class _DispatchClient:
    def __init__(self, captured: list, *, evaluation_text: str = _EVALUATION_RESPONSE) -> None:
        self.messages = _DispatchMessages(captured, evaluation_text=evaluation_text)


@pytest.fixture
def pinned_investigation_id(monkeypatch) -> str:
    class _FixedUUID:
        hex = _FIXED_INVESTIGATION_ID + "ffffffffffffffffffff"  # >12 chars, [:12] slice used by caller

    monkeypatch.setattr("research.investigation.uuid.uuid4", lambda: _FixedUUID())
    return _FIXED_INVESTIGATION_ID


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def test_full_investigation_persists_hypotheses_and_synthesis(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    captured: list = []
    client = _DispatchClient(captured)
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])

    assert investigation.investigation_id == pinned_investigation_id
    assert len(investigation.hypotheses) == 2
    assert [h.hypothesis_id for h in investigation.hypotheses] == [_H1, _H2]
    assert len(investigation.evaluations) == 2
    assert investigation.failed_hypothesis_ids == []
    assert investigation.synthesis is not None
    assert investigation.synthesis.ranked_hypothesis_ids == [_H1, _H2]

    row = get_investigation(company_conn, investigation.investigation_id)
    assert row is not None
    assert row["question"] == "Why did margins decline?"
    assert row["strongest_explanation"] == "Input cost inflation is the most likely driver."

    hyp_rows = list_investigation_hypotheses(company_conn, investigation.investigation_id)
    assert len(hyp_rows) == 2
    assert all(r["verdict"] == "SUPPORTED" for r in hyp_rows)
    assert {r["hypothesis_id"]: r["synthesis_rank"] for r in hyp_rows} == {_H1: 1, _H2: 2}

    evidence_rows = list_investigation_hypothesis_evidence(company_conn, _H1)
    assert any(r["stance"] == "missing" for r in evidence_rows)


def test_single_hypothesis_evaluation_failure_does_not_fail_the_investigation(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    captured: list = []
    client = _DispatchClient(captured)
    call_count = {"evaluations": 0}
    original_create = client.messages.create

    def create(**kwargs):
        system = kwargs.get("system", "")
        if system.startswith(_EVALUATOR_PREFIX):
            call_count["evaluations"] += 1
            if call_count["evaluations"] == 1:
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn")
        return original_create(**kwargs)

    client.messages.create = create
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])

    assert investigation.failed_hypothesis_ids == [_H1]
    assert list(investigation.evaluations.keys()) == [_H2]
    assert investigation.synthesis is not None

    hyp_rows = list_investigation_hypotheses(company_conn, investigation.investigation_id)
    failed_row = next(r for r in hyp_rows if r["hypothesis_id"] == _H1)
    assert failed_row["verdict"] is None
    assert failed_row["synthesis_rank"] is None


def test_hypothesis_generation_failure_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    client = _DispatchClient([])

    def create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn")

    client.messages.create = create
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    with pytest.raises(InvestigationError, match="could not generate hypotheses"):
        run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])


def test_all_evaluations_failing_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    client = _DispatchClient([])

    def create(**kwargs):
        system = kwargs.get("system", "")
        if system.startswith(_GENERATOR_PREFIX):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=_HYPOTHESES_RESPONSE)], stop_reason="end_turn")
        if system.startswith(_EVALUATOR_PREFIX):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn")
        raise AssertionError("synthesis should never be reached")

    client.messages.create = create
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    with pytest.raises(InvestigationError, match="nothing to synthesize"):
        run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])
