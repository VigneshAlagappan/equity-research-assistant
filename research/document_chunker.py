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

Chunking is purely mechanical (fixed-size, page-scoped, no LLM call) — a
document with no extractable pages (non-PDF, unfetchable link) is indexed
with zero chunks, same "absence isn't an error" rule the rest of the
Knowledge Builder pipeline already follows.
"""

from __future__ import annotations

from storage.db_types import DBConnection, Row

from research.documents import document_pages
from storage.repositories import replace_document_chunks

#: Character-based, not token-based — simple and good enough for keyword
#: search (unlike research/knowledge_builder.py's MAX_CHARS_FOR_EXTRACTION,
#: nothing here is paying per-token for an LLM call). Overlap keeps a
#: sentence that straddles a chunk boundary findable from either chunk.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 150


def _split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    pieces: list[str] = []
    start = 0
    step = chunk_size - overlap
    while start < len(text):
        piece = text[start:start + chunk_size].strip()
        if piece:
            pieces.append(piece)
        start += step
    return pieces


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
