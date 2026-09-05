"""research/documents.py tests — PDF text extraction is exercised against a
real minimal PDF built at test time; period-hint parsing and evidence
assembly are tested directly, matching this repo's mocking style for the
external-library boundary (see tests/test_assistant.py's Anthropic mocking)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import requests

from companies.registry import seed_companies
from research.documents import (
    _extract_period_hint,
    _extract_pdf_text,
    get_document_evidence,
)
from storage.repositories import save_company_document


def _make_minimal_pdf_bytes(text: str) -> bytes:
    """A hand-built single-page PDF with one text run — enough for pypdf to
    parse and extract `text` back out, without pulling in a PDF-authoring
    dependency just for tests."""
    stream = f"BT /F1 24 Tf 10 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj".encode() + obj + b"endobj\n"
    xref_start = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += b"trailer<</Size " + str(len(objects) + 1).encode() + b"/Root 1 0 R>>\n"
    buf += b"startxref\n" + str(xref_start).encode() + b"\n%%EOF"
    return bytes(buf)


def _make_minimal_pdf(path: Path, text: str) -> None:
    path.write_bytes(_make_minimal_pdf_bytes(text))


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self._content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


def test_extract_pdf_text_reads_a_real_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _make_minimal_pdf(pdf_path, "Hello from the concall transcript")

    text = _extract_pdf_text(str(pdf_path))

    assert text is not None
    assert "Hello from the concall transcript" in text


def test_extract_pdf_text_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert _extract_pdf_text(str(tmp_path / "does-not-exist.pdf")) is None


def test_extract_pdf_text_returns_none_for_a_corrupt_file(tmp_path: Path) -> None:
    bad_path = tmp_path / "corrupt.pdf"
    bad_path.write_bytes(b"not a real pdf")

    assert _extract_pdf_text(str(bad_path)) is None


def test_extract_pdf_text_returns_none_for_an_encrypted_pdf_missing_crypto_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    """An AES-encrypted PDF pypdf can't open without the optional
    `cryptography` package raises pypdf.errors.DependencyError — a
    PyPdfError sibling of PdfReadError, not a subclass of it. Must degrade
    to None like every other unreadable-PDF case, not crash the whole
    ingestion batch (real incident: killed a running bulk-ingestion worker)."""
    import pypdf.errors

    def _raise(*args, **kwargs):
        raise pypdf.errors.DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr("research.documents.PdfReader", _raise)
    path = tmp_path / "encrypted.pdf"
    path.write_bytes(b"%PDF-1.7 fake encrypted content")

    assert _extract_pdf_text(str(path)) is None


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What did management say in Q1 FY2025?", ("FY2025", "Q1")),
        ("How did results look in FY24?", ("FY2024", None)),
        ("What's the outlook for FY 2025?", ("FY2025", None)),
        ("How is the company doing?", (None, None)),
    ],
)
def test_extract_period_hint(question: str, expected: tuple[str | None, str | None]) -> None:
    assert _extract_period_hint(question) == expected


@pytest.fixture
def company_conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def test_get_document_evidence_extracts_text_from_uploaded_pdfs(
    company_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    pdf_path = tmp_path / "transcript.pdf"
    _make_minimal_pdf(pdf_path, "Guidance raised for the year")
    save_company_document(
        company_conn,
        "HDFCBANK",
        document_type="transcript",
        fiscal_year="FY2025",
        quarter="Q1",
        added_by_user="tester",
        raw_file_path=str(pdf_path),
    )

    evidence = get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?")

    assert len(evidence) == 1
    assert evidence[0].kind == "MANAGEMENT_STATEMENT"
    assert evidence[0].company_id == "HDFCBANK"
    assert "Guidance raised for the year" in evidence[0].value
    assert "Concall Transcript" in evidence[0].label
    assert "Q1 FY2025" in evidence[0].citation


def test_get_document_evidence_skips_link_only_documents(company_conn: sqlite3.Connection) -> None:
    save_company_document(
        company_conn,
        "HDFCBANK",
        document_type="announcement",
        fiscal_year="FY2025",
        quarter=None,
        added_by_user="tester",
        source_url="https://example.com/announcement",
    )

    assert get_document_evidence(company_conn, "HDFCBANK", "Anything new?") == []


def test_get_document_evidence_filters_to_the_period_mentioned_in_the_question(
    company_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    old_pdf = tmp_path / "old.pdf"
    new_pdf = tmp_path / "new.pdf"
    _make_minimal_pdf(old_pdf, "Old quarter commentary")
    _make_minimal_pdf(new_pdf, "New quarter commentary")
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2024", quarter="Q4",
        added_by_user="tester", raw_file_path=str(old_pdf),
    )
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", raw_file_path=str(new_pdf),
    )

    evidence = get_document_evidence(company_conn, "HDFCBANK", "What happened in Q1 FY2025?")

    assert len(evidence) == 1
    assert "New quarter commentary" in evidence[0].value


def test_get_document_evidence_with_no_documents_returns_empty(company_conn: sqlite3.Connection) -> None:
    assert get_document_evidence(company_conn, "HDFCBANK", "How is it doing?") == []


def test_fetch_url_bytes_sends_a_browser_user_agent(monkeypatch) -> None:
    """BSE (a common Docs-tab link source) 403s a fetch with no
    User-Agent/requests' bare default — confirmed by hand against a real
    bseindia.com corpfiling link, which is what document_id=2 (a real
    ingested document) silently failed against before this fix. Without a
    User-Agent, that 403 is caught by _fetch_url_bytes' broad
    requests.RequestException handler and looks identical to "link is
    genuinely dead" — no error, no chunks, no evidence, and nothing in the
    logs pointing at why."""
    import research.documents as documents_module

    captured_headers: list[dict | None] = []

    def fake_get(url, timeout=None, stream=None, headers=None):
        captured_headers.append(headers)
        return _FakeResponse(b"%PDF-1.4 not a real pdf")

    monkeypatch.setattr("research.documents.requests.get", fake_get)

    documents_module._fetch_url_bytes("https://www.bseindia.com/xml-data/corpfiling/example.pdf")

    assert captured_headers == [documents_module._FETCH_HEADERS]
    assert "User-Agent" in documents_module._FETCH_HEADERS


def test_get_document_evidence_fetches_a_pdf_from_a_link_only_document(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    pdf_bytes = _make_minimal_pdf_bytes("Management commentary from the filed transcript")
    captured_urls: list[str] = []

    def fake_get(url, timeout=None, stream=None, headers=None):
        captured_urls.append(url)
        return _FakeResponse(pdf_bytes)

    monkeypatch.setattr("research.documents.requests.get", fake_get)
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", source_url="https://example.com/filings/transcript.pdf",
    )

    evidence = get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?")

    assert captured_urls == ["https://example.com/filings/transcript.pdf"]
    assert len(evidence) == 1
    assert "Management commentary from the filed transcript" in evidence[0].value


def test_get_document_evidence_skips_non_pdf_links(company_conn: sqlite3.Connection, monkeypatch) -> None:
    def fake_get(*args, **kwargs):
        raise AssertionError("should never fetch a non-PDF URL")

    monkeypatch.setattr("research.documents.requests.get", fake_get)
    save_company_document(
        company_conn, "HDFCBANK", document_type="concall_recording", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", source_url="https://example.com/filings/recording.mp3",
    )

    assert get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?") == []


def test_get_document_evidence_skips_a_link_on_fetch_failure(company_conn: sqlite3.Connection, monkeypatch) -> None:
    def fake_get(*args, **kwargs):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr("research.documents.requests.get", fake_get)
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", source_url="https://example.com/filings/transcript.pdf",
    )

    assert get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?") == []


def test_get_document_evidence_skips_a_download_over_the_size_cap(
    company_conn: sqlite3.Connection, monkeypatch
) -> None:
    import research.documents as documents_module

    monkeypatch.setattr(documents_module, "MAX_DOWNLOAD_BYTES", 10)

    def fake_get(url, timeout=None, stream=None, headers=None):
        return _FakeResponse(b"x" * 100)

    monkeypatch.setattr("research.documents.requests.get", fake_get)
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", source_url="https://example.com/filings/huge.pdf",
    )

    assert get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?") == []


def test_get_document_evidence_prefers_uploaded_file_over_source_url(
    company_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    def fake_get(*args, **kwargs):
        raise AssertionError("should not fetch a URL when raw_file_path is set")

    monkeypatch.setattr("research.documents.requests.get", fake_get)
    pdf_path = tmp_path / "uploaded.pdf"
    _make_minimal_pdf(pdf_path, "Uploaded file wins")
    save_company_document(
        company_conn, "HDFCBANK", document_type="transcript", fiscal_year="FY2025", quarter="Q1",
        added_by_user="tester", raw_file_path=str(pdf_path), source_url="https://example.com/also-here.pdf",
    )

    evidence = get_document_evidence(company_conn, "HDFCBANK", "How did Q1 FY2025 go?")

    assert len(evidence) == 1
    assert "Uploaded file wins" in evidence[0].value
