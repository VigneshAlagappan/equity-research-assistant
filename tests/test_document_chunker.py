"""research/document_chunker.py + retrieval/document_search.py (Step 2D) —
FTS5 chunking and keyword search, exercised against a real minimal PDF
(same fixture tests/test_documents.py builds) and real SQLite FTS5, no
mocking needed since there's no external service involved."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from research.document_chunker import CHUNK_OVERLAP, CHUNK_SIZE, _split_into_chunks, chunk_and_index_document
from retrieval.document_search import search_documents
from storage.repositories import save_company_document
from tests.test_documents import _make_minimal_pdf


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def _add_pdf_document(conn: sqlite3.Connection, tmp_path: Path, text: str, filename: str = "report.pdf") -> sqlite3.Row:
    pdf_path = tmp_path / filename
    _make_minimal_pdf(pdf_path, text)
    return save_company_document(
        conn, "HDFCBANK", document_type="annual_report", fiscal_year="FY2024", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path),
    )


def test_split_into_chunks_short_text_is_one_chunk() -> None:
    assert _split_into_chunks("short text") == ["short text"]


def test_split_into_chunks_long_text_overlaps() -> None:
    text = "x" * (CHUNK_SIZE * 2)
    pieces = _split_into_chunks(text)
    assert len(pieces) > 1
    assert all(len(p) <= CHUNK_SIZE for p in pieces)


def test_split_into_chunks_blank_text_is_empty() -> None:
    assert _split_into_chunks("   ") == []


def test_split_into_chunks_never_splits_a_word_across_a_boundary() -> None:
    """Regression: the old character-offset chunker split real words across
    chunk boundaries — a real Infosys annual report chunk ended
    '...necessitate building en' with the next chunk starting 'ndeavor can
    be...', splitting "endeavor" in half. Sentence-aware packing must never
    let a chunk start or end mid-word."""
    filler = "This is filler content padding out each sentence to a reasonable length for testing. "
    text = (filler * 20) + "This endeavor can be multi tiered and complex in scope."

    pieces = _split_into_chunks(text)

    assert len(pieces) > 1
    assert any("endeavor" in p for p in pieces)  # the word itself survives whole, in some chunk

    normalized = " ".join(text.split())
    for piece in pieces:
        start = normalized.index(piece)
        end = start + len(piece)
        assert start == 0 or normalized[start - 1] == " ", f"chunk starts mid-word: {piece[:30]!r}"
        assert end == len(normalized) or normalized[end] == " ", f"chunk ends mid-word: {piece[-30:]!r}"


def test_split_into_chunks_oversized_single_sentence_falls_back_to_raw_slicing() -> None:
    """A single unpunctuated run longer than CHUNK_SIZE (e.g. a table/list
    block _split_into_sentences() has no boundary to break on) must still be
    bounded, not produce one unbounded chunk — same fallback the old
    character-slicing behavior always used, now scoped to just this one
    oversized piece instead of the whole document."""
    text = "x" * (CHUNK_SIZE * 2)  # no . ! ? anywhere -- one giant "sentence"

    pieces = _split_into_chunks(text)

    assert len(pieces) == 2
    assert all(len(p) == CHUNK_SIZE for p in pieces)


def test_chunk_and_index_document_writes_chunks_with_page_number(
    company_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    doc = _add_pdf_document(company_conn, tmp_path, "Revenue grew twelve percent this quarter")

    count = chunk_and_index_document(company_conn, doc)

    assert count >= 1
    rows = company_conn.execute(
        "SELECT page_number, chunk_index, company_id, document_id FROM document_chunks WHERE document_id = ?",
        (doc["document_id"],),
    ).fetchall()
    assert len(rows) == count
    assert rows[0]["page_number"] == 1
    assert rows[0]["company_id"] == "HDFCBANK"
    assert rows[0]["document_id"] == doc["document_id"]


def test_chunk_and_index_document_no_text_indexes_zero_chunks(company_conn: sqlite3.Connection) -> None:
    doc = save_company_document(
        company_conn, "HDFCBANK", document_type="announcement", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", source_url="https://example.com/not-a-pdf-link",
    )
    assert chunk_and_index_document(company_conn, doc) == 0


def test_reindexing_a_document_replaces_old_chunks_not_accumulates(
    company_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    doc = _add_pdf_document(company_conn, tmp_path, "Original report text")
    first_count = chunk_and_index_document(company_conn, doc)

    second_count = chunk_and_index_document(company_conn, doc)

    rows = company_conn.execute(
        "SELECT COUNT(*) AS n FROM document_chunks WHERE document_id = ?", (doc["document_id"],)
    ).fetchone()
    assert rows["n"] == second_count == first_count  # not doubled


def test_search_documents_finds_indexed_text(company_conn: sqlite3.Connection, tmp_path: Path) -> None:
    doc = _add_pdf_document(company_conn, tmp_path, "Revenue grew twelve percent this quarter driven by exports")
    chunk_and_index_document(company_conn, doc)

    results = search_documents(company_conn, "revenue exports")

    assert len(results) == 1
    passage = results[0]
    assert passage.document_id == doc["document_id"]
    assert passage.company_id == "HDFCBANK"
    assert passage.fiscal_year == "FY2024"
    assert passage.quarter == "Q1"
    assert passage.document_type == "annual_report"
    assert "revenue" in passage.text.lower() or "Revenue" in passage.text


def test_search_documents_scoped_to_company(company_conn: sqlite3.Connection, tmp_path: Path) -> None:
    hdfc_doc = _add_pdf_document(company_conn, tmp_path, "Widget sales grew strongly", filename="hdfc.pdf")
    chunk_and_index_document(company_conn, hdfc_doc)

    icici_pdf = tmp_path / "icici.pdf"
    _make_minimal_pdf(icici_pdf, "Widget sales grew strongly")
    icici_doc = save_company_document(
        company_conn, "ICICIBANK", document_type="annual_report", fiscal_year="FY2024", quarter=None,
        added_by_user="tester", raw_file_path=str(icici_pdf),
    )
    chunk_and_index_document(company_conn, icici_doc)

    results = search_documents(company_conn, "widget sales", company_id="HDFCBANK")

    assert len(results) == 1
    assert results[0].company_id == "HDFCBANK"


def test_search_documents_returns_empty_for_no_match(company_conn: sqlite3.Connection, tmp_path: Path) -> None:
    doc = _add_pdf_document(company_conn, tmp_path, "Revenue grew twelve percent")
    chunk_and_index_document(company_conn, doc)

    assert search_documents(company_conn, "completely unrelated topic xyz") == []


def test_search_documents_handles_special_characters_without_raising(company_conn: sqlite3.Connection, tmp_path: Path) -> None:
    doc = _add_pdf_document(company_conn, tmp_path, "Cost-of-funds declined")
    chunk_and_index_document(company_conn, doc)

    # Hyphens/quotes/colons are FTS5 query operators — must not raise a syntax error.
    results = search_documents(company_conn, 'cost-of-funds: "declined"?')
    assert isinstance(results, list)


def test_search_documents_empty_query_returns_empty(company_conn: sqlite3.Connection) -> None:
    assert search_documents(company_conn, "   ") == []
