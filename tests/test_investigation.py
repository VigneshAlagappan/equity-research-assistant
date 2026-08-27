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

_INSUFFICIENT_RESPONSE = """{
  "verdict": "INSUFFICIENT_EVIDENCE",
  "confidence_basis": "Not enough evidence yet.",
  "supporting_evidence": [],
  "contradicting_evidence": [],
  "missing_evidence": ["quarterly cost breakdown"]
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


def test_insufficient_evidence_triggers_a_retry_that_then_succeeds(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    """Step 2G's INSUFFICIENT_EVIDENCE verdict must trigger exactly one more
    Step 2F retrieval pass + re-evaluation (the Orchestrator's loop, guardrail
    #7) — not zero (ignoring the signal) and not more than needed once a
    later verdict resolves it. Injected capabilities always surface fresh
    evidence on retry so the no-new-evidence control can't be what's
    (accidentally) making this pass — company_conn has no ingested data, so
    the default capabilities would return an empty plan every time."""
    from research.capabilities import PlannerCapabilities
    from research.evidence import Evidence

    counters = {"financial": 0}

    def ever_new_evidence(conn, company_id):
        counters["financial"] += 1
        return [Evidence(kind="FACT", company_id=company_id, label="metric", value=str(counters["financial"]), citation="t")]

    caps = PlannerCapabilities(
        financial_evidence=ever_new_evidence,
        document_evidence=lambda conn, company_id, question: [],
        macro_evidence=lambda conn, question: [],
        document_search=lambda conn, query, *, company_id, limit: [],
        knowledge_graph=lambda conn, entity_type, entity_name: [],
    )

    captured: list = []
    client = _DispatchClient(captured)
    call_count = {"evaluations": 0}
    original_create = client.messages.create

    def create(**kwargs):
        system = kwargs.get("system", "")
        if system.startswith(_EVALUATOR_PREFIX):
            call_count["evaluations"] += 1
            if call_count["evaluations"] == 1:
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=_INSUFFICIENT_RESPONSE)], stop_reason="end_turn"
                )
        return original_create(**kwargs)

    client.messages.create = create
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"], capabilities=caps)

    assert investigation.evaluations[_H1].verdict == "SUPPORTED"
    # H1: INSUFFICIENT then SUPPORTED (2 calls) + H2: SUPPORTED first try (1 call) = 3
    assert call_count["evaluations"] == 3


def test_persistent_insufficient_evidence_stops_at_max_iterations(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    """Even if the evidence gap never closes, the loop must not run forever —
    MAX_EVIDENCE_ITERATIONS bounds it. Uses injected capabilities that always
    surface fresh (never-before-seen) evidence, so the no-new-evidence
    control can't be what stops it — only the max-iterations control can."""
    from research.investigation import MAX_EVIDENCE_ITERATIONS
    from research.capabilities import PlannerCapabilities
    from research.evidence import Evidence

    counters = {"financial": 0}

    def ever_new_evidence(conn, company_id):
        counters["financial"] += 1
        return [Evidence(kind="FACT", company_id=company_id, label="metric", value=str(counters["financial"]), citation="t")]

    caps = PlannerCapabilities(
        financial_evidence=ever_new_evidence,
        document_evidence=lambda conn, company_id, question: [],
        macro_evidence=lambda conn, question: [],
        document_search=lambda conn, query, *, company_id, limit: [],
        knowledge_graph=lambda conn, entity_type, entity_name: [],
    )

    captured: list = []
    client = _DispatchClient(captured, evaluation_text=_INSUFFICIENT_RESPONSE)
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"], capabilities=caps)

    eval_calls = [c for c in captured if c.get("system", "").startswith(_EVALUATOR_PREFIX)]
    assert len(eval_calls) == MAX_EVIDENCE_ITERATIONS * 2  # 2 hypotheses, each hits the cap
    assert investigation.evaluations[_H1].verdict == "INSUFFICIENT_EVIDENCE"
    assert investigation.evaluations[_H2].verdict == "INSUFFICIENT_EVIDENCE"


def test_no_new_evidence_stops_the_loop_early(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    """A retry that surfaces nothing beyond the prior pass must stop the loop
    immediately, without paying for another (identical) evaluation call —
    company_conn here has no ingested data, so every plan_and_gather() pass
    is naturally empty every time."""
    captured: list = []
    client = _DispatchClient(captured, evaluation_text=_INSUFFICIENT_RESPONSE)
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])

    eval_calls = [c for c in captured if c.get("system", "").startswith(_EVALUATOR_PREFIX)]
    assert len(eval_calls) == 2  # one per hypothesis, no retries
    assert investigation.evaluations[_H1].verdict == "INSUFFICIENT_EVIDENCE"


def test_injected_fact_store_is_used_for_persistence(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    """Proves the FactStore seam (storage/fact_store.py) is wired into
    run_investigation's write path — an injected fake FactStore's
    save_investigation* fields get called instead of the real ones, so
    nothing lands in the real investigations table."""
    from dataclasses import replace

    from storage.fact_store import default_fact_store

    calls = {"save_investigation": 0, "save_investigation_hypothesis": 0, "save_investigation_hypothesis_evidence": 0}

    def fake_save_investigation(conn, **kwargs):
        calls["save_investigation"] += 1

    def fake_save_investigation_hypothesis(conn, **kwargs):
        calls["save_investigation_hypothesis"] += 1

    def fake_save_investigation_hypothesis_evidence(conn, hypothesis_id, evidence):
        calls["save_investigation_hypothesis_evidence"] += 1

    fs = replace(
        default_fact_store(),
        save_investigation=fake_save_investigation,
        save_investigation_hypothesis=fake_save_investigation_hypothesis,
        save_investigation_hypothesis_evidence=fake_save_investigation_hypothesis_evidence,
    )

    captured: list = []
    client = _DispatchClient(captured)
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"], fact_store=fs)

    assert calls["save_investigation"] == 1
    assert calls["save_investigation_hypothesis"] == 2  # one per hypothesis
    from storage.repositories import get_investigation as real_get_investigation

    assert real_get_investigation(company_conn, pinned_investigation_id) is None  # nothing really persisted


def test_timeout_stops_the_loop_without_a_retry(
    company_conn: sqlite3.Connection, pinned_investigation_id: str, monkeypatch
) -> None:
    """A zero timeout budget means the deadline has already passed by the
    time the first evaluation returns — no retry should be attempted even
    though the verdict is INSUFFICIENT_EVIDENCE."""
    monkeypatch.setattr("research.investigation.INVESTIGATION_TIMEOUT_SECONDS", 0)
    captured: list = []
    client = _DispatchClient(captured, evaluation_text=_INSUFFICIENT_RESPONSE)
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: client)

    investigation = run_investigation(company_conn, "Why did margins decline?", ["HDFCBANK"])

    eval_calls = [c for c in captured if c.get("system", "").startswith(_EVALUATOR_PREFIX)]
    assert len(eval_calls) == 2  # one per hypothesis, no retries
    assert investigation.evaluations[_H1].verdict == "INSUFFICIENT_EVIDENCE"
