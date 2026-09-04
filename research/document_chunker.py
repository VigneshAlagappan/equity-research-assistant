"""Document chunking + FTS5 indexing (Step 2D) — splits a processed
document's text into page-scoped chunks and indexes them for keyword
search (see retrieval/document_search.py), answering "where was something
similar discussed?" without replacing structured SQL retrieval
(financials/, research/macro_evidence.py) or the Knowledge Builder's own
extraction (Step 2A) for anything else.

FTS5, not embeddings/vector search — schemas/sqlite_schema.sql's
document_chunks_fts table has sat unpopulated since it was first written
("FTS5-ready, not yet populated" — architecture.md's Known Gaps), and the
spec allows either "FTS/vector representation." A real semantic embedding
layer would need a new dependency (a local embedding model, or a paid
embeddings API — Anthropic doesn't offer one) and a vector index SQLite has
no native support for; FTS5 needs neither, is already sitting in the schema
waiting to be used, and matches this app's "local-first, no external
services beyond the LLM API" principle. document_chunks.embedding stays
NULL on every row — a real semantic layer, if ever added, is additive on
top of this, not a rewrite.

Every chunk keeps its provenance: document, company, fiscal period, page,
and source — chunk_and_index_document() reads document/company/fiscal_year/
quarter/source from the same `documents` row research/documents.py's
document_pages() already resolves against; nothing here invents a chunk's
context.

Chunking is purely mechanical (no LLM call), and sentence-aware, not blind
character-offset slicing (architecture.md's Known Gaps: "a chunk boundary
can land mid-paragraph or mid-table"). `_split_into_chunks()` (via
`_split_into_sentences()` + `_pack_sentences()`) packs whole sentences up to
CHUNK_SIZE, so a boundary lands between sentences, not mid-word — the
concrete bug this fixes: a real Infosys annual report chunk under the old
character-slice approach ended "...necessitate building en" with the next
chunk starting "ndeavor can be...", splitting "endeavor" in half; verified
fixed against that same real document (the word now appears whole, as the
sentence carried by chunk overlap). Sentence detection is regex-based and
intentionally loose — "the obvious cases," not a real NLP tokenizer. A
single sentence longer than CHUNK_SIZE (an unpunctuated table/list run
_split_into_sentences() can't break up further) falls back to a raw
character slice for just that one oversized piece, same as the old
universal behavior, rather than producing one unbounded chunk.

Section/heading-aware chunking was also attempted (line-level detection of
short, unpunctuated lines as headings, to finally populate
document_chunks.section_heading — still NULL on every row, architecture.md's
Known Gaps) and reverted after checking it against this app's own real
ingested PDFs: pypdf's extract_text() inserts a newline at every VISUAL line
wrap of justified body text, not at paragraph/heading boundaries, so most
ordinary body lines are short and end without punctuation — indistinguishable
from a real heading by that heuristic. It fragmented one real page into 24
garbage "sections," most of them mid-sentence line-wrap fragments mislabeled
as headings, which is worse than the character-slicing bug it was meant to
fix. A real fix needs either a layout-aware extraction library (pdfplumber,
unstructured.io-style block extraction) or a much better heuristic than
"short line, no trailing punctuation" — not attempted here.

A document with no extractable pages (non-PDF, unfetchable link) is indexed
with zero chunks, same "absence isn't an error" rule the rest of the
Knowledge Builder pipeline already follows.
"""

from __future__ import annotations

import re
from storage.db_types import DBConnection, Row

from research.documents import document_pages
from storage.repositories import replace_document_chunks

#: Character-based, not token-based — simple and good enough for keyword
#: search (unlike research/knowledge_builder.py's MAX_CHARS_FOR_EXTRACTION,
#: nothing here is paying per-token for an LLM call). Overlap keeps a
#: sentence that straddles a chunk boundary findable from either chunk.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 150

#: Splits after a sentence-ending .!? followed by whitespace and what looks
#: like the start of the next sentence (capital letter, digit, opening
#: quote/paren) — a lightweight heuristic tokenizer, not a real one.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"‘’(])")
_WHITESPACE_RE = re.compile(r"\s+")


def _split_into_sentences(text: str) -> list[str]:
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _pack_sentences(sentences: list[str], *, chunk_size: int, overlap: int) -> list[str]:
    """Greedily packs whole sentences up to ~chunk_size — a chunk boundary
    lands between sentences, never mid-sentence/mid-word. `overlap` carries
    the trailing sentence(s) of one chunk into the next, by whole sentences
    (at least one, even if that alone exceeds `overlap`) rather than a raw
    character count, so the carried continuity is never itself a
    mid-sentence fragment."""
    if not sentences:
        return []
    pieces: list[str] = []
    current: list[str] = []

    def current_text() -> str:
        return " ".join(current)

    for sentence in sentences:
        if len(sentence) > chunk_size:
            # A single "sentence" (an unpunctuated table/list run
            # _split_into_sentences() had nothing to split on) longer than a
            # whole chunk — fall back to a raw slice for just this one
            # oversized piece rather than producing one unbounded chunk.
            if current:
                pieces.append(current_text())
                current = []
            pieces.extend(sentence[start : start + chunk_size] for start in range(0, len(sentence), chunk_size))
            continue
        if current and len(current_text()) + 1 + len(sentence) > chunk_size:
            pieces.append(current_text())
            carried: list[str] = []
            carried_len = 0
            for s in reversed(current):
                if carried and carried_len + len(s) > overlap:
                    break
                carried.insert(0, s)
                carried_len += len(s)
            current = carried
        current.append(sentence)
    if current:
        pieces.append(current_text())
    return pieces


def _split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    return _pack_sentences(_split_into_sentences(text), chunk_size=chunk_size, overlap=overlap)


def chunk_and_index_document(conn: DBConnection, document_row: Row) -> int:
    """Chunk one document's text (page by page, so each chunk carries a
    real page_number) and (re)index it for search. Returns the number of
    chunks written — 0 if the document has no extractable text, not an
    error. Safe to call again on the same document (e.g. reprocessing after
    a content change) — replace_document_chunks() replaces the prior set
    rather than accumulating stale duplicates."""
    pages = document_pages(document_row)
    if not pages:
        return 0

    chunks: list[dict] = []
    chunk_index = 0
    for page_number, page_text in enumerate(pages, start=1):
        for piece in _split_into_chunks(page_text):
            chunks.append({
                "document_id": document_row["document_id"],
                "company_id": document_row["company_id"],
                "page_number": page_number,
                "chunk_index": chunk_index,
                "text": piece,
            })
            chunk_index += 1

    replace_document_chunks(conn, document_row["document_id"], chunks)
    return len(chunks)
