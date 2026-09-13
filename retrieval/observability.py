"""Per-call observability for the Hybrid Retriever (section 13) — the
retrieval-layer counterpart to llm/observability.py's per-call logging for
the Model Router. Every retrieval/hybrid_search.py call gets one structured
log line (console + logs/app.log) and one retrieval_diagnostics row, so
retrieval quality/latency/degradation is inspectable instead of invisible.

Deliberately lightweight, not a copy of llm/observability.py's cost
accounting — there is no LLM call in this path (retrieval never calls the
LLM), so there is nothing to price. What's worth recording instead is
exactly what a hybrid-ranking bug report needs to reconstruct: how many
candidates each method found, how long each step took, whether the vector
layer degraded, and — per returned passage — which method(s) found it and
at what rank/score, without ever persisting the passage's actual text
(schemas/sqlite_schema.sql's retrieval_diagnostics table comment: "never
raw passage text")."""

from __future__ import annotations

import json
import logging
import sqlite3

from retrieval.hybrid_search import HybridRetrievalDiagnostics
from storage.database import init_db, utcnow_iso
from storage.db_types import DBConnection
from storage.repositories import insert_retrieval_diagnostic

logger = logging.getLogger(__name__)

_QUERY_EXCERPT_MAX_CHARS = 200


def record(conn: DBConnection, diagnostics: HybridRetrievalDiagnostics) -> None:
    """retrieval_diagnostics is one of the tables storage/backend_
    bootstrap.py's docstring documents as SQLite-only forever (same as
    batch_job_runs/llm_call_log/etc.) -- hybrid_search_documents() (this
    function's only caller) passes whatever backend-appropriate connection
    the research pipeline is already using for the real document search,
    which is a psycopg2 connection under DATABASE_BACKEND=postgres. Opens a
    dedicated SQLite connection for the log write specifically when `conn`
    isn't already one, same fix/reasoning as llm/observability.py's
    record()/ingestion/batch_log.py's BatchRun -- found the hard way: every
    single production retrieval call was silently failing to record its
    diagnostics (`AttributeError: 'psycopg2.extensions.connection' object
    has no attribute 'execute'`, caught by hybrid_search.py's own
    try/except so it never broke an actual answer, but retrieval_
    diagnostics was permanently empty in production)."""
    query_excerpt = diagnostics.query[:_QUERY_EXCERPT_MAX_CHARS]

    logger.info(
        "hybrid_retrieval query=%r company=%s as_of=%s keyword_candidates=%d semantic_candidates=%d "
        "returned=%d embedding_latency_ms=%s vector_latency_ms=%s keyword_latency_ms=%s degraded=%s reason=%s",
        query_excerpt, diagnostics.company_id, diagnostics.as_of, diagnostics.keyword_candidate_count,
        diagnostics.semantic_candidate_count, diagnostics.returned_count, diagnostics.embedding_latency_ms,
        diagnostics.vector_store_latency_ms, diagnostics.keyword_latency_ms, diagnostics.degraded,
        diagnostics.degradation_reason,
    )

    logs_conn = conn if isinstance(conn, sqlite3.Connection) else init_db()
    try:
        insert_retrieval_diagnostic(
            logs_conn,
            created_at=utcnow_iso(),
            query_excerpt=query_excerpt,
            company_id=diagnostics.company_id,
            as_of=diagnostics.as_of,
            keyword_candidate_count=diagnostics.keyword_candidate_count,
            semantic_candidate_count=diagnostics.semantic_candidate_count,
            returned_count=diagnostics.returned_count,
            embedding_latency_ms=diagnostics.embedding_latency_ms,
            vector_store_latency_ms=diagnostics.vector_store_latency_ms,
            keyword_latency_ms=diagnostics.keyword_latency_ms,
            degraded=diagnostics.degraded,
            degradation_reason=diagnostics.degradation_reason,
            passages_json=json.dumps(diagnostics.passages),
        )
    finally:
        if logs_conn is not conn:
            logs_conn.close()
