"""Pulls a company's uploaded documents (Docs tab / `documents` table) and
extracts their text as MANAGEMENT_STATEMENT evidence, for company-specific
investigations (research/assistant.py, research/signals_report.py).

Lean version of README step 7 ("Investor Relations + document pipeline") —
no chunking/FTS5/hybrid_search yet, just direct extraction into the prompt.
Today's per-company doc volume is a handful of files (added one at a time via
the Docs tab's Add form), not a corpus that needs a search/ranking layer.

Documents contribute evidence two ways: an uploaded PDF (raw_file_path /
storage_object_key) is read through the active DocumentStore
(storage/document_store.py — local disk or S3, whichever
config.settings.DOCUMENT_STORE_BACKEND selects); a link-only row (source_url,
no uploaded file) is fetched over HTTP if the URL looks like a PDF.
Today's real Docs-tab usage is almost entirely link-only (pasted
BSE/company-site URLs), so upload-only support would ground nothing for
those companies. Non-PDF documents (recordings, plain announcement links)
have no text to extract, so they're silently skipped rather than erroring.

Fixed a P0 perf bug (architecture review, document/raw-content storage
investigation): get_document_evidence() is called live, uncached, on every
single-company investigation (research/investigation_planner.py), so every
question used to re-open/re-fetch and re-parse every one of a company's
documents from scratch. _document_bytes() below caches each document's raw
bytes (the expensive disk read or HTTP fetch) in a per-process dict keyed by
(document_id, file_hash) — good enough for this app's "a handful of gunicorn
workers, not a distributed fleet" deployment shape; no cross-process/
cross-request-boundary invalidation is attempted, and a document that's
re-uploaded (new file_hash) simply gets a fresh cache entry rather than
evicting the old one, since a handful of small PDFs per company never grows
large enough for that to matter.
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

from pypdf.errors import DependencyError, PyPdfError

from research.evidence import Evidence
from research.temporal import date_visible
from storage.document_store import DocumentStoreError, default_document_store
from storage.fact_store import FactStore, default_fact_store

# Bounds a single link-only document fetch — avoids hanging on a slow host or
# pulling down an unexpectedly huge file just because its URL ends in .pdf.
REQUEST_TIMEOUT_SECONDS = 15
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# BSE (a common Docs-tab source per the module docstring above) 403s a
# fetch with no User-Agent/requests' default one — confirmed by hand
# against a real bseindia.com corpfiling link. Without this, that fetch
# fails silently (requests.HTTPError caught below, same as any other
# unreachable link) and the document just never has extractable text —
# no error, no log line pointing at why.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

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
    """Path-based PDF text extraction — kept as a standalone helper (used
    directly by tests/test_documents.py against arbitrary filesystem paths,
    not necessarily anything under DOCUMENTS_DIR/BASE_DIR) separate from
    _extract_pdf_text_from_bytes(), which is what document_text()/
    document_pages() actually use once a document's bytes have been
    resolved through the active DocumentStore or an HTTP fetch."""
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


def _extract_pdf_text_from_bytes(data: bytes) -> str | None:
    try:
        return _text_from_reader(PdfReader(BytesIO(data)))
    except (PyPdfError, DependencyError, OSError):
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
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, stream=True, headers=_FETCH_HEADERS)
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


#: Per-process cache of a document's raw bytes, keyed by (document_id,
def _fetch_document_bytes(row: Row) -> bytes | None:
    """Source resolution shared by document_text()/document_pages(): an
    uploaded file (raw_file_path / storage_object_key) is read through the
    active DocumentStore (storage/document_store.py); a link-only row
    (source_url) is fetched over HTTP if it looks like a PDF. None under the
    same conditions the old direct-disk/direct-HTTP code returned None
    (non-PDF, missing/unfetchable file or link)."""
    key = row["storage_object_key"] or row["raw_file_path"]
    if key:
        if Path(key).suffix.lower() != ".pdf":
            return None
        store = default_document_store()
        if not store.exists(key):
            return None
        try:
            return store.retrieve(key)
        except DocumentStoreError:
            return None
    if row["source_url"] and _looks_like_pdf_url(row["source_url"]):
        return _fetch_url_bytes(row["source_url"])
    return None


#: Per-process cache of document_text()'s result, keyed by (document_id,
#: file_hash, pointer) — see this module's docstring for why this exists
#: (the P0 uncached-refetch-and-reparse-per-question bug the architecture
#: review flagged) and why a plain dict is enough (a handful of gunicorn
#: workers, not a distributed fleet; a handful of small documents per
#: company, not a corpus). Only document_text() is cached, not
#: document_pages() — the latter is exercised once per document by the
#: ingestion/chunking pipeline (research/document_chunker.py), never
#: repeatedly per question, so caching it would only add staleness risk
#: (e.g. a document reprocessed in place, same path, no file_hash change
#: yet) for zero benefit. `pointer` (the row's raw_file_path/
#: storage_object_key/source_url) is included alongside file_hash because
#: file_hash is only refreshed by ingestion/coordinator.py's
#: process_documents() AFTER a reprocessed document is re-extracted — a
#: document whose pointer changed is never served stale cached text even
#: when file_hash hasn't caught up yet. Deliberately never evicted — a
#: document reprocessed in place (same pointer AND file_hash) is the one
#: case this cache can go stale for; accepted as a known tradeoff for a
#: simple per-process dict, per the architecture review's "don't
#: over-engineer this" guidance.
_DOCUMENT_TEXT_CACHE: dict[tuple[int, str | None, str | None], str | None] = {}


def document_text(row: Row) -> str | None:
    pointer = row["storage_object_key"] or row["raw_file_path"] or row["source_url"]
    cache_key = (row["document_id"], row["file_hash"], pointer)
    if cache_key in _DOCUMENT_TEXT_CACHE:
        return _DOCUMENT_TEXT_CACHE[cache_key]
    data = _fetch_document_bytes(row)
    text = None if data is None else _extract_pdf_text_from_bytes(data)
    _DOCUMENT_TEXT_CACHE[cache_key] = text
    return text


def document_pages(row: Row) -> list[str] | None:
    """Same source resolution as document_text() (uploaded file vs. a
    fetched PDF-looking link), but preserving page boundaries —
    research/document_chunker.py (Step 2D) uses this to attach a real
    page_number to each chunk, which the single flattened string
    document_text() returns can't do. Returns None under the exact same
    conditions document_text() would (non-PDF, unfetchable link) rather
    than a list of one flattened page. Not cached — see
    _DOCUMENT_TEXT_CACHE's docstring for why."""
    data = _fetch_document_bytes(row)
    if data is None:
        return None
    try:
        return _pages_from_reader(PdfReader(BytesIO(data)))
    except (PyPdfError, DependencyError, OSError):
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


#: How many hybrid-retrieved passages get_document_passage_evidence() adds
#: to a Q&A answer's evidence block — a handful of the MOST relevant
#: passages, not a second copy of every document (get_document_evidence()
#: above already contributes each whole document up to
#: MAX_CHARS_PER_DOCUMENT; this is the additive, targeted complement to it,
#: not a replacement).
MAX_PASSAGE_EVIDENCE = 5


def get_document_passage_evidence(
    conn: DBConnection, company_id: str, question: str, *, fact_store: FactStore | None = None,
    as_of: str | None = None, limit: int = MAX_PASSAGE_EVIDENCE,
) -> list[Evidence]:
    """MANAGEMENT_STATEMENT evidence from the hybrid (FTS5 + semantic)
    document retriever (retrieval/hybrid_search.py) — the specific,
    question-relevant passages a question is actually asking about, as an
    ADDITIVE complement to get_document_evidence()'s whole-document text
    above (feature spec section 9: "question -> hybrid retrieval -> top-K
    relevant passages -> LLM", added alongside the existing full-document
    path, not replacing it — a workflow that genuinely needs the complete
    source still gets it from get_document_evidence()).

    Finds evidence get_document_evidence() cannot: that function only reads
    documents this company has directly on file via the Docs tab and returns
    each one's OWN opening ~12,000 characters regardless of where in a long
    filing the relevant passage actually sits; this function's semantic leg
    also matches on paraphrased wording FTS5 alone would miss, and its
    result is the exact page/passage — not just "somewhere in this
    document." Company-specific only, same constraint as
    get_document_evidence() (research/documents.py has no per-company
    attribution story for a multi-company comparison yet).

    No LLM call anywhere in this path (retrieval/hybrid_search.py). Same
    `as_of` convention as get_document_evidence() (research/temporal.py) —
    threaded straight through to the hybrid retriever's own as_of handling,
    not re-implemented here."""
    from retrieval.hybrid_search import hybrid_search_documents

    passages = hybrid_search_documents(
        conn, question, company_id=company_id, limit=limit, as_of=as_of, fact_store=fact_store,
    )
    evidence = []
    for passage in passages:
        label = _DOCUMENT_TYPE_LABELS.get(passage.document_type, passage.document_type or "Document")
        period = f"{passage.quarter} {passage.fiscal_year}" if passage.quarter else (passage.fiscal_year or "period unknown")
        page = f", page {passage.page_number}" if passage.page_number else ""
        evidence.append(Evidence(
            kind="MANAGEMENT_STATEMENT",
            company_id=company_id,
            label=f"{label} ({period}){page} — relevant passage",
            value=passage.text,
            citation=f"{label}, {period}{page}, matched via {passage.retrieval_source} retrieval",
        ))
    return evidence
