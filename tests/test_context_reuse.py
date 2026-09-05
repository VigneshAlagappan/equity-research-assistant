"""context/reuse.py — reuse-before-recompute against generated_reports, and
the freshness check that stops a stale report from being reused."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from companies.registry import seed_companies
from context.reuse import find_reusable_report
from ingestion.pipeline import ingest_file
from retrieval.embedding_provider import EmbeddingProviderUnavailable
from storage.repositories import save_generated_report, save_report_evidence
from tests.conftest import FakeEmbeddingProvider
from tests.test_screener_adapter import _make_screener_workbook


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def _save_report(
    conn: sqlite3.Connection, thread_id: str, question: str, company_ids: list[str], *,
    embedding_provider: FakeEmbeddingProvider | None = None,
) -> None:
    embedding = embedding_provider.embed_text(question) if embedding_provider else None
    model_id = embedding_provider.model_id if embedding_provider else None
    save_generated_report(
        conn, thread_id, question, company_ids, "consolidated", f"# Report\n{question}",
        question_embedding=embedding, question_embedding_model=model_id,
    )
    save_report_evidence(conn, thread_id, [
        {"kind": "FACT", "company_id": company_ids[0], "label": "Net Profit FY2024", "value": "100", "citation": "c"},
    ])


def test_fresh_report_on_same_question_is_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "consolidated")

    assert candidate is not None
    assert candidate.thread_id == "t1"
    assert candidate.evidence


def test_near_duplicate_wording_is_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change over time?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change over time", ["HDFCBANK"], "consolidated")

    assert candidate is not None


def test_unrelated_question_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "What is the CASA ratio outlook?", ["HDFCBANK"], "consolidated")

    assert candidate is None


def test_different_company_scope_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK", "ICICIBANK"], "consolidated")

    assert candidate is None


def test_different_statement_type_is_not_reused(ingested_conn: sqlite3.Connection) -> None:
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "standalone")

    assert candidate is None


def test_stale_report_is_not_reused_after_new_data_is_ingested(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A report generated before fresh data was ingested must not be handed
    back as if it reflects that new data (README §17)."""
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"])

    time.sleep(0.01)  # ensure a strictly later created_at timestamp
    file_path = tmp_path / "ICICIBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(ingested_conn, file_path, company_id="ICICIBANK", source_id="screener")
    # Re-ingesting HDFCBANK itself is what actually invalidates an HDFCBANK report;
    # simulate that by ingesting fresh HDFCBANK data again.
    hdfc_file = tmp_path / "HDFCBANK_v2.xlsx"
    _make_screener_workbook(hdfc_file)
    ingest_file(ingested_conn, hdfc_file, company_id="HDFCBANK", source_id="screener")

    candidate = find_reusable_report(ingested_conn, "How did net profit change?", ["HDFCBANK"], "consolidated")

    assert candidate is None


def test_injected_fact_store_is_used_instead_of_the_real_tables(db_conn: sqlite3.Connection) -> None:
    """Proves the FactStore seam (storage/fact_store.py, architecture
    guardrail #3's 'access structured facts through a repository interface')
    is actually wired into find_reusable_report — an injected fake FactStore
    surfaces a candidate even though db_conn here has no real
    generated_reports row at all."""
    from dataclasses import replace

    from storage.fact_store import default_fact_store

    fake_report = {
        "thread_id": "fake-t1", "question": "How did net profit change?", "company_ids": ["HDFCBANK"],
        "statement_type": "consolidated", "report_markdown": "# fake", "generated_at": "2099-01-01T00:00:00Z",
    }
    fs = replace(
        default_fact_store(),
        list_generated_reports=lambda conn: [fake_report],
        list_report_evidence=lambda conn, thread_id: [],
        list_report_followups=lambda conn, thread_id: [],
        get_latest_data_timestamp=lambda conn, company_ids: None,
    )

    candidate = find_reusable_report(
        db_conn, "How did net profit change?", ["HDFCBANK"], "consolidated", fact_store=fs
    )

    assert candidate is not None
    assert candidate.thread_id == "fake-t1"


def test_semantic_paraphrase_is_reused_when_jaccard_alone_would_miss_it(
    ingested_conn: sqlite3.Connection,
) -> None:
    """The whole point of the semantic layer: a real paraphrase using
    different words for the same concept ("profit" -> "earnings",
    FakeEmbeddingProvider's synonym folding) scores well below
    SIMILARITY_THRESHOLD on Jaccard but clears SEMANTIC_SIMILARITY_THRESHOLD
    on cosine similarity, and gets reused."""
    provider = FakeEmbeddingProvider()
    _save_report(ingested_conn, "t1", "How did net profit change?", ["HDFCBANK"], embedding_provider=provider)

    candidate = find_reusable_report(
        ingested_conn, "How did net earnings change?", ["HDFCBANK"], "consolidated", embedding_provider=provider
    )

    assert candidate is not None
    assert candidate.thread_id == "t1"
    assert candidate.match_kind == "semantic"


def test_period_hint_conflict_blocks_reuse_even_with_high_semantic_similarity(
    ingested_conn: sqlite3.Connection,
) -> None:
    """The exact real risk that motivates the period-hint gate: two
    questions overwhelmingly similar in wording (high cosine similarity) but
    naming different fiscal years must never reuse across them — this is
    load-bearing, not a formality, so the test constructs a case where the
    semantic signal alone WOULD have cleared its threshold, to prove the
    gate is what actually stops it."""
    provider = FakeEmbeddingProvider()
    long_question_fy24 = (
        "What was the reported total net profit figure for the full fiscal year FY2024?"
    )
    long_question_fy25 = (
        "What was the reported total net profit figure for the full fiscal year FY2025?"
    )
    _save_report(ingested_conn, "t1", long_question_fy24, ["HDFCBANK"], embedding_provider=provider)

    # Sanity check the premise: semantic similarity alone really does clear
    # the bar here (so the gate, not a coincidentally-low score, is what
    # blocks reuse below).
    from context.reuse import SEMANTIC_SIMILARITY_THRESHOLD, _cosine_similarity

    semantic_similarity = _cosine_similarity(
        provider.embed_text(long_question_fy24), provider.embed_text(long_question_fy25)
    )
    assert semantic_similarity >= SEMANTIC_SIMILARITY_THRESHOLD

    candidate = find_reusable_report(
        ingested_conn, long_question_fy25, ["HDFCBANK"], "consolidated", embedding_provider=provider
    )

    assert candidate is None


def test_reuse_falls_back_to_word_overlap_when_embedding_provider_is_unavailable(
    ingested_conn: sqlite3.Connection,
) -> None:
    """A down/misconfigured embedding provider must degrade this call, never
    fail it — same graceful-degradation shape as retrieval/hybrid_search.py.
    Near-identical wording (still catchable by Jaccard alone) is still
    reused; genuine cross-wording paraphrases are not, since the layer that
    would catch them isn't available."""

    class _UnavailableProvider:
        model_id = "unavailable"
        dimension = 1

        def embed_text(self, text: str) -> list[float]:
            raise EmbeddingProviderUnavailable("simulated outage")

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingProviderUnavailable("simulated outage")

    _save_report(ingested_conn, "t1", "How did net profit change over time?", ["HDFCBANK"])

    candidate = find_reusable_report(
        ingested_conn, "How did net profit change over time", ["HDFCBANK"], "consolidated",
        embedding_provider=_UnavailableProvider(),
    )

    assert candidate is not None
    assert candidate.match_kind == "jaccard"


def test_period_hint_conflicts_only_blocks_when_both_questions_name_a_period() -> None:
    from context.reuse import _period_hint_conflicts

    assert _period_hint_conflicts("Net profit in FY2024?", "Net profit in FY2025?") is True
    assert _period_hint_conflicts("Net profit in Q1 FY2024?", "Net profit in Q2 FY2024?") is True
    assert _period_hint_conflicts("Net profit in FY2024?", "Net profit in FY2024?") is False
    # One or both questions mention no period at all -- never blocked on that basis.
    assert _period_hint_conflicts("How did net profit change?", "Net profit in FY2024?") is False
    assert _period_hint_conflicts("How did net profit change?", "How did net profit change?") is False
