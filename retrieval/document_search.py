"""Document passage search (Step 2D) — keyword (FTS5) retrieval over chunks
research/document_chunker.py has indexed. Retrieval never calls the LLM
(README: Retrieval Architecture) — this module only reads
document_chunks_fts via storage/repositories.py, returning typed
DocumentPassage results, same "typed result, not a raw row" shape
retrieval/structured_search.py already uses for financial Evidence.

Answers "where was something similar discussed?" — a keyword-relevance
search over document text, not "what does the evidence say" the way
Evidence/get_document_evidence() does. Deliberately not wired into
research/assistant.py's or research/signals_report.py's evidence gathering
— it's a standalone retrieval capability, not a replacement for the
existing structured-SQL-first evidence discipline (README: "Do not replace
structured SQL retrieval with vector search").
"""

from __future__ import annotations

from storage.db_types import DBConnection
from dataclasses import dataclass

from storage.fact_store import FactStore, default_fact_store


@dataclass
class DocumentPassage:
    """One matched chunk, with everything Step 2D asks a passage to retain:
    document, company, fiscal period, page, and source."""

    chunk_id: int
    document_id: int
    company_id: str | None
    text: str
    page_number: int | None
    document_type: str | None
    fiscal_year: str | None
    quarter: str | None
    source: str | None
    published_at: str | None


def search_documents(
    conn: DBConnection, query: str, *, company_id: str | None = None, limit: int = 10,
    fact_store: FactStore | None = None,
) -> list[DocumentPassage]:
    """Keyword search over every indexed document chunk (optionally scoped
    to one company), ranked by FTS5 relevance. Returns [] for a query with
    no usable search tokens or no match — never raises over "nothing found."
    """
    fs = fact_store or default_fact_store()
    rows = fs.search_document_chunks(conn, query, company_id=company_id, limit=limit)
    return [
        DocumentPassage(
            chunk_id=row["chunk_id"], document_id=row["document_id"], company_id=row["company_id"],
            text=row["text"], page_number=row["page_number"], document_type=row["document_type"],
            fiscal_year=row["fiscal_year"], quarter=row["quarter"], source=row["source"],
            published_at=row["published_at"],
        )
        for row in rows
    ]
