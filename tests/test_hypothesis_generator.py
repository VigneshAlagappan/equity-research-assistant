"""research/hypothesis_generator.py (Step 2E) — Anthropic client mocked,
same pattern as tests/test_assistant.py."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from research.hypothesis_generator import HypothesisGenerationError, generate_hypotheses


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
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


_VALID_RESPONSE = """[
  {
    "statement": "Input costs increased, compressing margins.",
    "mechanism": "Higher raw material prices raised COGS faster than revenue grew.",
    "category": "financial",
    "rationale": "A common driver of margin decline.",
    "known_relationships": ["Repo rate cuts affect cost of funds"],
    "unknowns": ["Whether input cost data is actually available"]
  },
  {
    "statement": "Competitive pricing pressure reduced margins.",
    "mechanism": "A competitor's price cuts forced matching price reductions.",
    "category": "competitive",
    "rationale": "Plausible given the sector.",
    "known_relationships": [],
    "unknowns": ["Which competitor, and by how much"]
  }
]"""


def test_generates_hypotheses_with_expected_fields(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    hypotheses = generate_hypotheses(company_conn, "inv1", "Why did margins decline?", ["HDFCBANK"])

    assert len(hypotheses) == 2
    assert hypotheses[0].hypothesis_id == "inv1-h1"
    assert hypotheses[0].category == "financial"
    assert hypotheses[0].companies == ["HDFCBANK"]
    assert hypotheses[0].generation_order == 0
    assert hypotheses[1].hypothesis_id == "inv1-h2"
    assert hypotheses[1].category == "competitive"


def test_hallucinated_category_is_dropped(company_conn: sqlite3.Connection, monkeypatch) -> None:
    response = '[{"statement": "x", "category": "not_a_real_category", "mechanism": "m", "rationale": "r"}, ' \
               '{"statement": "valid one", "category": "financial", "mechanism": "m", "rationale": "r"}]'
    _install_fake_client(monkeypatch, response)

    hypotheses = generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])

    assert len(hypotheses) == 1
    assert hypotheses[0].statement == "valid one"


def test_empty_statement_is_dropped(company_conn: sqlite3.Connection, monkeypatch) -> None:
    response = '[{"statement": "", "category": "financial", "mechanism": "m", "rationale": "r"}]'
    _install_fake_client(monkeypatch, response)

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_no_usable_hypotheses_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "[]")

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_unparseable_response_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "not json at all")

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_refusal_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, "won't answer", stop_reason="refusal")

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_truncated_response_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    _install_fake_client(monkeypatch, '[{"statement": "cut off', stop_reason="max_tokens")

    with pytest.raises(HypothesisGenerationError, match="truncated"):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_all_providers_unavailable_raises(company_conn: sqlite3.Connection, monkeypatch) -> None:
    from llm.router import AllProvidersUnavailableError, Attempt

    monkeypatch.setattr(
        "research.hypothesis_generator.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "question", ["HDFCBANK"])


def test_company_context_includes_sector_and_known_entities(company_conn: sqlite3.Connection, monkeypatch) -> None:
    company_conn.execute(
        "INSERT INTO knowledge_entities (entity_type, name, company_id, created_at) "
        "VALUES ('Risk', 'Input cost inflation', 'HDFCBANK', '2024-01-01')"
    )
    company_conn.commit()
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)

    generate_hypotheses(company_conn, "inv1", "Why did margins decline?", ["HDFCBANK"])

    user_message = captured[0]["messages"][0]["content"]
    assert "HDFCBANK" in user_message
    assert "Financial Services" in user_message  # HDFCBANK's seeded sector
    assert "Input cost inflation" in user_message


def test_a_raw_newline_inside_a_string_field_still_parses(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    """A real golden-loop run failed an entire investigation on "Invalid
    control character at line 20 column 361" — the model had emitted an
    unescaped newline inside one "mechanism" field. Structurally the JSON was
    fine, so `strict=False` accepts it (the same fix
    research/knowledge_builder.py already applies). Generation is the one
    step run_investigation() cannot degrade past, so this must not be fatal."""
    response = (
        '[{"statement": "Funding costs rose.",'
        ' "mechanism": "Deposit repricing lagged,\nso spreads compressed.",'
        ' "category": "financial", "rationale": "r",'
        ' "known_relationships": [], "unknowns": []}]'
    )
    _install_fake_client(monkeypatch, response)

    hypotheses = generate_hypotheses(company_conn, "inv1", "Why?", ["HDFCBANK"])

    assert len(hypotheses) == 1
    assert "so spreads compressed" in hypotheses[0].mechanism


def test_structural_validation_survives_the_lenient_parse(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    """strict=False relaxes control characters only — a hallucinated category
    is still dropped, so a lenient parse cannot smuggle in an invalid
    hypothesis."""
    response = (
        '[{"statement": "s", "mechanism": "m\nm", "category": "vibes",'
        ' "rationale": "r", "known_relationships": [], "unknowns": []}]'
    )
    _install_fake_client(monkeypatch, response)

    with pytest.raises(HypothesisGenerationError):
        generate_hypotheses(company_conn, "inv1", "Why?", ["HDFCBANK"])


def test_token_budget_has_headroom_for_the_full_hypothesis_set() -> None:
    """Real runs produced 2155-3495 output tokens for one generation call;
    the old 3072 cap truncated a multi-clause question outright. Guards
    against a future edit quietly lowering it back under that observed range."""
    from research.hypothesis_generator import MAX_TOKENS

    assert MAX_TOKENS >= 4096


def test_triggered_indicators_reach_the_generation_prompt(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    """The spec's flow starts at "Trusted Facts / Indicators / Question" — a
    deterministic rule that has already fired is context Step 2E should see,
    supplied through the capability seam rather than a direct import."""
    from research.capabilities import PlannerCapabilities
    from research.evidence import Evidence

    caps = PlannerCapabilities(
        financial_evidence=lambda conn, company_id: [],
        document_evidence=lambda conn, company_id, question: [],
        macro_evidence=lambda conn, question: [],
        document_search=lambda conn, query, *, company_id, limit: [],
        knowledge_graph=lambda conn, entity_type, entity_name: [],
        indicator_evidence=lambda conn, company_id: [
            Evidence(
                kind="CALCULATION", company_id=company_id,
                label="Indicator: Promoter holding declined [Q2 FY2025]",
                value="Promoter holding declined from 26.00% to 23.50%.", citation="rule v1",
            )
        ],
    )
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)

    generate_hypotheses(company_conn, "inv1", "Why?", ["HDFCBANK"], capabilities=caps)

    user_message = captured[0]["messages"][0]["content"]
    assert "Triggered indicator — Indicator: Promoter holding declined" in user_message


def test_generation_without_a_capability_bundle_is_unchanged(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    """Indicators are additive context, never a dependency — generation must
    not require an indicator to have fired."""
    captured = _install_fake_client(monkeypatch, _VALID_RESPONSE)
    assert generate_hypotheses(company_conn, "inv1", "Why?", ["HDFCBANK"])
    assert "Triggered indicator" not in captured[0]["messages"][0]["content"]
