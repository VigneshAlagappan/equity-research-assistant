"""research/knowledge_builder.py (Step 2A) — the Anthropic client is mocked
throughout, same pattern as tests/test_assistant.py. Text extraction is
exercised against a real minimal PDF, same fixture tests/test_documents.py
already builds for that purpose."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from companies.registry import seed_companies
from research.knowledge_builder import KnowledgeExtractionError, extract_document_knowledge
from storage.repositories import (
    list_knowledge_claims_for_company,
    list_knowledge_claims_for_document,
    list_knowledge_relationships_for_claim,
    save_company_document,
)
from tests.test_documents import _make_minimal_pdf


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


_VALID_RESPONSE = """{
  "entities": [
    {"type": "Product", "name": "Widget Pro"},
    {"type": "Risk", "name": "Input cost inflation"}
  ],
  "claims": [
    {
      "text": "Revenue grew 12% year over year.",
      "claim_type": "FACT",
      "category": "fact",
      "speaker": null,
      "confidence": 0.95,
      "quote": "Revenue for the quarter grew 12% year over year.",
      "relationships": []
    },
    {
      "text": "Management believes Widget Pro will drive future growth.",
      "claim_type": "MANAGEMENT_OPINION",
      "category": "strategy",
      "speaker": "CEO",
      "confidence": 0.7,
      "quote": "We believe Widget Pro is central to our growth strategy.",
      "relationships": [
        {"relationship_type": "OFFERS", "source_entity": "COMPANY", "target_entity": "Widget Pro"},
        {"relationship_type": "EXPOSED_TO", "source_entity": "COMPANY", "target_entity": "Input cost inflation"}
      ]
    }
  ]
}"""


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _add_pdf_document(conn: sqlite3.Connection, tmp_path: Path, company_id: str = "HDFCBANK", text: str = "Some report text") -> sqlite3.Row:
    pdf_path = tmp_path / "report.pdf"
    _make_minimal_pdf(pdf_path, text)
    return save_company_document(
        conn, company_id, document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", raw_file_path=str(pdf_path),
    )


def test_extraction_persists_entities_claims_relationships_and_evidence(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    _install_fake_client(monkeypatch, _VALID_RESPONSE)

    result = extract_document_knowledge(company_conn, doc)

    assert result.claims_created == 2
    assert result.entities_created == 2
    assert result.relationships_created == 2

    claims = list_knowledge_claims_for_document(company_conn, doc["document_id"])
    assert len(claims) == 2
    fact_claim = next(c for c in claims if c["claim_type"] == "FACT")
    assert fact_claim["claim_text"] == "Revenue grew 12% year over year."
    assert fact_claim["company_id"] == "HDFCBANK"
    assert fact_claim["fiscal_year"] == "FY2024"
    assert fact_claim["extraction_confidence"] == 0.95

    opinion_claim = next(c for c in claims if c["claim_type"] == "MANAGEMENT_OPINION")
    assert opinion_claim["speaker"] == "CEO"
    relationships = list_knowledge_relationships_for_claim(company_conn, opinion_claim["claim_id"])
    assert {r["relationship_type"] for r in relationships} == {"OFFERS", "EXPOSED_TO"}
    assert {r["source_name"] for r in relationships} == {"HDFCBANK"}  # COMPANY resolved to the real company
    assert {r["target_name"] for r in relationships} == {"Widget Pro", "Input cost inflation"}


def test_claims_are_scoped_to_the_company(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(company_conn, doc)

    assert len(list_knowledge_claims_for_company(company_conn, "HDFCBANK")) == 2
    assert list_knowledge_claims_for_company(company_conn, "ICICIBANK") == []


def test_a_second_document_adds_claims_without_touching_the_first(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """Every extraction is additive — never an UPDATE to a prior document's
    claims, same "never overwrite" discipline financial_observations follows."""
    doc1 = _add_pdf_document(company_conn, tmp_path, text="first report")
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(company_conn, doc1)

    doc2_pdf = tmp_path / "report2.pdf"
    _make_minimal_pdf(doc2_pdf, "second report")
    doc2 = save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", raw_file_path=str(doc2_pdf),
    )
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(company_conn, doc2)

    all_claims = list_knowledge_claims_for_company(company_conn, "HDFCBANK")
    assert len(all_claims) == 4  # 2 from each document, first document's rows untouched
    assert len(list_knowledge_claims_for_document(company_conn, doc1["document_id"])) == 2
    assert len(list_knowledge_claims_for_document(company_conn, doc2["document_id"])) == 2


def test_entities_are_deduped_across_documents(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    doc1 = _add_pdf_document(company_conn, tmp_path, text="first")
    _install_fake_client(monkeypatch, _VALID_RESPONSE)
    extract_document_knowledge(company_conn, doc1)

    doc2_pdf = tmp_path / "report2.pdf"
    _make_minimal_pdf(doc2_pdf, "second")
    doc2 = save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", raw_file_path=str(doc2_pdf),
    )
    _install_fake_client(monkeypatch, _VALID_RESPONSE)  # same entities named again
    extract_document_knowledge(company_conn, doc2)

    entities = company_conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_entities WHERE name = 'Widget Pro'"
    ).fetchone()
    assert entities["n"] == 1  # not duplicated on the second document


def test_hallucinated_claim_type_is_dropped_not_stored(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    bad_response = """{"entities": [], "claims": [
        {"text": "Made up claim", "claim_type": "NOT_A_REAL_TYPE", "quote": "x", "relationships": []}
    ]}"""
    _install_fake_client(monkeypatch, bad_response)

    result = extract_document_knowledge(company_conn, doc)
    assert result.claims_created == 0
    assert list_knowledge_claims_for_document(company_conn, doc["document_id"]) == []


def test_relationship_naming_an_unknown_entity_is_skipped_not_stored(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    response = """{"entities": [], "claims": [
        {"text": "x", "claim_type": "FACT", "quote": "x",
         "relationships": [{"relationship_type": "OFFERS", "source_entity": "COMPANY", "target_entity": "Never Declared"}]}
    ]}"""
    _install_fake_client(monkeypatch, response)

    result = extract_document_knowledge(company_conn, doc)
    assert result.claims_created == 1
    assert result.relationships_created == 0
    assert len(result.skipped_relationships) == 1


def test_no_extractable_text_skips_the_llm_call_entirely(company_conn: sqlite3.Connection, monkeypatch) -> None:
    """A link-only document whose URL doesn't look like a PDF has nothing to
    extract — same "absence isn't an error" rule get_document_evidence()
    already follows; no LLM call should even be attempted."""
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="announcement", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/press-release",
    )
    called = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: called.append(1) or _FakeClient(_VALID_RESPONSE, "end_turn", []),
    )

    result = extract_document_knowledge(company_conn, doc)
    assert result.claims_created == 0
    assert called == []


def test_all_providers_unavailable_raises_extraction_error(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    from llm.router import AllProvidersUnavailableError, Attempt

    doc = _add_pdf_document(company_conn, tmp_path)
    monkeypatch.setattr(
        "research.knowledge_builder.route",
        lambda **kw: (_ for _ in ()).throw(AllProvidersUnavailableError([Attempt("x", "anthropic", "unavailable")])),
    )

    with pytest.raises(KnowledgeExtractionError):
        extract_document_knowledge(company_conn, doc)


def test_unparseable_response_raises_extraction_error(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    _install_fake_client(monkeypatch, "I'm not going to respond in JSON.")

    with pytest.raises(KnowledgeExtractionError):
        extract_document_knowledge(company_conn, doc)


def test_refusal_raises_extraction_error(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    doc = _add_pdf_document(company_conn, tmp_path)
    _install_fake_client(monkeypatch, "won't answer", stop_reason="refusal")

    with pytest.raises(KnowledgeExtractionError):
        extract_document_knowledge(company_conn, doc)


def test_truncated_response_raises_a_clear_extraction_error(company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    """A response cut off mid-JSON (stop_reason="max_tokens") gets its own
    diagnosis, not a confusing raw JSONDecodeError — a real failure mode
    hit against an actual dense document, not a hypothetical."""
    doc = _add_pdf_document(company_conn, tmp_path)
    _install_fake_client(monkeypatch, '{"entities": [], "claims": [{"text": "cut off mid', stop_reason="max_tokens")

    with pytest.raises(KnowledgeExtractionError, match="truncated"):
        extract_document_knowledge(company_conn, doc)
