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

from research.temporal import date_visible
from storage.fact_store import FactStore, default_fact_store


@dataclass
class DocumentPassage:
    """One matched chunk, with everything Step 2D asks a passage to retain:
    document, company, fiscal period, page, and source.

    The last four fields are retrieval/hybrid_search.py's contribution
    (section 7) — every passage still round-trips through this exact type
    whether it came from FTS5, semantic search, or both, so nothing
    downstream (research/investigation_planner.py's plan.passages) needs to
    know which retrieval method produced it. Defaulted so this dataclass
    stays backward compatible with every existing keyword-only
    construction/test (retrieval/document_search.py's own search_documents(),
    tests/test_document_chunker.py) that never sets them:

      retrieval_source  "keyword" | "semantic" | "both" — "both" (a
                         confidence boost, section 7) means the same chunk
                         was found independently by FTS5 AND vector search.
      fts_rank           1-based BM25 rank if found by keyword search, else None.
      semantic_score      cosine similarity if found by semantic search, else None.
      hybrid_score        the final fused ranking score (retrieval/hybrid_search.py's
                         Reciprocal Rank Fusion) — None for a passage returned
                         by search_documents()/search_documents_semantic() alone,
                         outside the hybrid retriever.
    """

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
    retrieval_source: str = "keyword"
    fts_rank: int | None = None
    semantic_score: float | None = None
    hybrid_score: float | None = None


def search_documents(
    conn: DBConnection, query: str, *, company_id: str | None = None, limit: int = 10,
    fact_store: FactStore | None = None, as_of: str | None = None,
) -> list[DocumentPassage]:
    """Keyword search over every indexed document chunk (optionally scoped
    to one company), ranked by FTS5 relevance. Returns [] for a query with
    no usable search tokens or no match — never raises over "nothing found."

    `as_of` (ISO date) drops passages from documents published after the
    cutoff — research/temporal.py. Applied after ranking rather than inside
    the FTS query so relevance ordering is unchanged; the cost is that a
    cutoff can return fewer than `limit` passages, which is the honest
    outcome (there were fewer available at the time).
    """
    fs = fact_store or default_fact_store()
    rows = fs.search_document_chunks(conn, query, company_id=company_id, limit=limit)
    if as_of:
        rows = [r for r in rows if date_visible(r["published_at"], as_of)]
    return [
        DocumentPassage(
            chunk_id=row["chunk_id"], document_id=row["document_id"], company_id=row["company_id"],
            text=row["text"], page_number=row["page_number"], document_type=row["document_type"],
            fiscal_year=row["fiscal_year"], quarter=row["quarter"], source=row["source"],
            published_at=row["published_at"], retrieval_source="keyword", fts_rank=rank,
        )
        for rank, row in enumerate(rows, start=1)
    ]
