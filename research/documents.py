"""Pulls a company's uploaded documents (Docs tab / `documents` table) and
extracts their text as MANAGEMENT_STATEMENT evidence, for company-specific
investigations (research/assistant.py, research/signals_report.py).

Lean version of README step 7 ("Investor Relations + document pipeline") —
no chunking/FTS5/hybrid_search yet, just direct extraction into the prompt.
Today's per-company doc volume is a handful of files (added one at a time via
the Docs tab's Add form), not a corpus that needs a search/ranking layer.

Documents contribute evidence two ways: an uploaded PDF (raw_file_path) is
read straight off disk; a link-only row (source_url, no uploaded file) is
fetched over HTTP if the URL looks like a PDF. Today's real Docs-tab usage is
almost entirely link-only (pasted BSE/company-site URLs), so upload-only
support would ground nothing for those companies. Fetches happen fresh on
every call — no caching of downloaded bytes yet, a known tradeoff for a
handful of small requests per question rather than a bigger cache-invalidation
design. Non-PDF documents (recordings, plain announcement links) have no text
to extract, so they're silently skipped rather than erroring.
"""

from __future__ import annotations

import re
from storage.db_types import DBConnection, Row
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from config.settings import from_repo_relative
from pypdf.errors import DependencyError, PyPdfError

from research.evidence import Evidence
from research.temporal import date_visible
from storage.fact_store import FactStore, default_fact_store

# Bounds a single link-only document fetch — avoids hanging on a slow host or
# pulling down an unexpectedly huge file just because its URL ends in .pdf.
REQUEST_TIMEOUT_SECONDS = 15
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Keeps one long filing from crowding out the financial FACT/CALCULATION
# evidence in the prompt — a rough cap, not a real chunking/relevance pass.
MAX_CHARS_PER_DOCUMENT = 12_000

_DOCUMENT_TYPE_LABELS = {
    "annual_report": "Annual Report",
    "investor_presentation": "Investor Presentation",
    "transcript": "Concall Transcript",
    "financial_result": "Quarterly Result",
    "concall_recording": "Concall Recording",
    "ai_summary": "AI Summary",
    "announcement": "Announcement",
    "xbrl": "XBRL Filing",
}

_QUESTION_FY_RE = re.compile(r"\bFY\s?(\d{2}|\d{4})\b", re.IGNORECASE)
_QUESTION_QUARTER_RE = re.compile(r"\bQ([1-4])\b", re.IGNORECASE)


def _extract_period_hint(question: str) -> tuple[str | None, str | None]:
    """Best-effort (fiscal_year, quarter) mentioned in a free-text question,
    e.g. "What did management say in Q1 FY2025?" -> ("FY2025", "Q1"). A bare
    "Q1" with no fiscal year is too ambiguous to filter on, so quarter is only
    returned alongside a fiscal year match."""
    fy_match = _QUESTION_FY_RE.search(question)
    if fy_match is None:
        return None, None
    digits = fy_match.group(1)
    fiscal_year = f"FY{2000 + int(digits) if len(digits) == 2 else int(digits)}"
    quarter_match = _QUESTION_QUARTER_RE.search(question)
    quarter = f"Q{quarter_match.group(1)}" if quarter_match else None
    return fiscal_year, quarter


def _select_documents(docs: list[Row], question: str) -> list[Row]:
    """Filter to the fiscal year/quarter mentioned in the question, if any —
    falls back to every document on file when nothing is mentioned, or when
    the mentioned period matches nothing (an unfiltered answer beats a
    silently empty one)."""
    fiscal_year, quarter = _extract_period_hint(question)
    if fiscal_year is None:
        return docs
    matching = [
        d for d in docs
        if d["fiscal_year"] == fiscal_year and (quarter is None or d["quarter"] in (quarter, None))
    ]
    return matching or docs


def _pages_from_reader(reader: PdfReader) -> list[str]:
    return [page.extract_text() or "" for page in reader.pages]


def _text_from_reader(reader: PdfReader) -> str | None:
    text = "\n".join(_pages_from_reader(reader))
    text = text.strip()
    return text or None


def _extract_pdf_text(path: str) -> str | None:
    try:
        return _text_from_reader(PdfReader(path))
    except (PyPdfError, DependencyError, OSError):
        # DependencyError (e.g. an AES-encrypted PDF needing the optional
        # `cryptography` package) is a direct Exception subclass, not a
        # PyPdfError — needs its own arm here, not just a broader PyPdfError
        # catch. Missing it used to crash the whole ingestion batch instead
        # of just this one unreadable document, same "absence isn't an
        # error" rule this function already follows for every other
        # unreadable-PDF case.
        return None


def _looks_like_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _fetch_url_bytes(url: str) -> bytes | None:
    # requests' `timeout=` with stream=True only bounds each individual
    # socket read, not the download as a whole — a host trickling bytes just
    # under that interval (throttled, or a huge slow filing) never trips it
    # and can hang far past REQUEST_TIMEOUT_SECONDS. This wall-clock deadline
    # is what actually caps total time spent on one link, so one slow host
    # can't stall an entire batch ingestion run.
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS * 4
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, stream=True)
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65_536):
            content += chunk
            if len(content) > MAX_DOWNLOAD_BYTES:
                return None
            if time.monotonic() > deadline:
                return None
        return bytes(content)
    except requests.RequestException:
        return None


def _extract_pdf_text_from_url(url: str) -> str | None:
    data = _fetch_url_bytes(url)
    if data is None:
        return None
    try:
        return _text_from_reader(PdfReader(BytesIO(data)))
    except (PyPdfError, DependencyError):
        return None


def document_text(row: Row) -> str | None:
    if row["raw_file_path"]:
        if Path(row["raw_file_path"]).suffix.lower() != ".pdf":
            return None
        return _extract_pdf_text(str(from_repo_relative(row["raw_file_path"])))
    if row["source_url"] and _looks_like_pdf_url(row["source_url"]):
        return _extract_pdf_text_from_url(row["source_url"])
    return None


def document_pages(row: Row) -> list[str] | None:
    """Same source resolution as document_text() (uploaded file vs. a
    fetched PDF-looking link), but preserving page boundaries —
    research/document_chunker.py (Step 2D) uses this to attach a real
    page_number to each chunk, which the single flattened string
    document_text() returns can't do. Returns None under the exact same
    conditions document_text() would (non-PDF, unfetchable link) rather
    than a list of one flattened page."""
    if row["raw_file_path"]:
        if Path(row["raw_file_path"]).suffix.lower() != ".pdf":
            return None
        try:
            return _pages_from_reader(PdfReader(str(from_repo_relative(row["raw_file_path"]))))
        except (PyPdfError, DependencyError, OSError):
            return None
    if row["source_url"] and _looks_like_pdf_url(row["source_url"]):
        data = _fetch_url_bytes(row["source_url"])
        if data is None:
            return None
        try:
            return _pages_from_reader(PdfReader(BytesIO(data)))
        except (PyPdfError, DependencyError):
            return None
    return None


def get_document_evidence(
    conn: DBConnection, company_id: str, question: str, *, fact_store: FactStore | None = None,
    as_of: str | None = None,
) -> list[Evidence]:
    """MANAGEMENT_STATEMENT evidence extracted from this company's Docs-tab
    documents — uploaded files and fetched links alike (README: Evidence &
    Citations). Company-specific only — there's
    no per-company attribution story yet for a multi-company comparison, so
    callers should only use this for single-company investigations.

    `as_of` (ISO date) keeps only documents published on or before the cutoff
    — research/temporal.py, which fails closed: a document with no
    published_at at all is dropped under a cutoff rather than assumed old
    enough."""
    fs = fact_store or default_fact_store()
    docs = list(fs.list_company_documents(conn, company_id))
    if as_of:
        docs = [d for d in docs if date_visible(d["published_at"], as_of)]
    docs = _select_documents(docs, question)

    evidence = []
    for row in docs:
        text = document_text(row)
        if text is None:
            continue
        label = _DOCUMENT_TYPE_LABELS.get(row["document_type"], row["document_type"] or "Document")
        period = f"{row['quarter']} {row['fiscal_year']}" if row["quarter"] else (row["fiscal_year"] or "period unknown")
        evidence.append(Evidence(
            kind="MANAGEMENT_STATEMENT",
            company_id=company_id,
            label=f"{label} ({period})",
            value=text[:MAX_CHARS_PER_DOCUMENT],
            citation=f"{label}, {period}, added {row['retrieved_at']}",
        ))
    return evidence
