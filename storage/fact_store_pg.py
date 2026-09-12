"""Postgres (Neon) port of `storage/fact_store.py`.

Checkpoint port: `storage/fact_store.py` has exactly one function --
`default_fact_store()` -- which wires up a `FactStore` dataclass instance by
importing the concrete SQLite-backed implementations from
`companies/registry.py` and `storage/repositories.py`. This file ports that
one function, `default_fact_store()`, to instead wire up a `FactStore`
targeting a `psycopg2` connection: every field it needs already has a proven
Postgres port in `storage/repositories_pg.py` (verified last checkpoint),
except three -- `get_company`, `list_companies_by_sector_field`,
`list_companies_with_sector` -- which in the original module come from
`companies/registry.py`, not `storage/repositories.py` directly.

`companies/registry.py`'s versions of those three are themselves thin,
backend-agnostic wrappers (`get_company()` is just
`repo.select_company(conn, normalize_company_id(company_id))`, etc.) around
`storage/company_repository.py`, hardcoded to that SQLite-backed module via
`from storage import company_repository as repo`. Rather than editing
`companies/registry.py` (out of scope -- not one of the 3 files this
checkpoint ports, and not to be modified per the task), this file re-derives
those same three thin wrappers locally, against the already-verified
`storage/company_repository_pg.py` port instead. `normalize_company_id()`
itself is pure Python string normalization with no SQL in it at all, so it's
imported and reused unchanged -- same function, same behavior, either
backend.

The `FactStore` dataclass type itself is NOT re-declared here -- it is
backend-agnostic (every field is typed as a plain `Callable`, structurally
satisfied by any function with a matching signature regardless of what kind
of `conn` object it closes over), so this module imports and reuses the one
declared in `storage/fact_store.py` rather than duplicating it.

Purely additive, like every other `*_pg.py` file this checkpoint --
NOT wired into any caller.
"""

from __future__ import annotations

import re

from storage.db_types import DBConnection, Row

from storage.fact_store import FactStore

# Same tokenization `storage/repositories.py::_sanitize_fts_query()` uses for
# FTS5 -- kept identical here so a query behaves the same way against either
# backend: alphanumeric tokens only, OR-joined (not AND) so a long multi-word
# blob (e.g. a hypothesis statement + mechanism, per research/investigation_
# planner.py Step 2F) still matches chunks sharing SOME of the terms, with
# ranking (ts_rank here, bm25/rank in FTS5) rewarding chunks that share MORE.
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _sanitize_fts_query_pg(query: str) -> str:
    """Builds a Postgres `to_tsquery` expression equivalent to the SQLite
    FTS5 version's OR-joined literal tokens. Returns "" for a query with no
    usable tokens (caller must treat that as "no results", not an error)."""
    tokens = _FTS_TOKEN_RE.findall(query)
    return " | ".join(tokens)


def get_company(conn: DBConnection, company_id: str) -> Row | None:
    from normalization.companies import normalize_company_id
    from storage import company_repository_pg as repo

    return repo.select_company(conn, normalize_company_id(company_id))


_SECTOR_PEER_FIELDS = ("basic_industry", "macro_economic_sector")


def list_companies_by_sector_field(conn: DBConnection, field: str, value: str, exclude_company_id: str) -> list[Row]:
    from storage import company_repository_pg as repo

    if field not in _SECTOR_PEER_FIELDS:
        raise ValueError(f"field must be one of {_SECTOR_PEER_FIELDS}, got {field!r}")
    return repo.select_companies_by_sector_column(conn, field, value, exclude_company_id)


def list_companies_with_sector(conn: DBConnection) -> list[Row]:
    from storage import company_repository_pg as repo

    return repo.select_companies_with_sector_column(conn)


def search_document_chunks(
    conn: DBConnection, query: str, *, company_id: str | None = None, limit: int = 10
) -> list[Row]:
    """Postgres tsvector/GIN counterpart of
    `storage/repositories.py::search_document_chunks()`'s FTS5 MATCH query --
    same return shape (chunk_id, document_id, company_id, page_number,
    chunk_index, text, document_type, fiscal_year, quarter, source,
    published_at, retrieved_at), ranked (`ts_rank` here vs FTS5's bm25
    `rank`), same `company_id` filter and `limit`, and the same "[] for a
    query with no usable tokens" contract. Requires `document_chunks.
    search_vector` (a `tsvector` column, GIN-indexed, backfilled via
    `to_tsvector('english', text)`) to exist -- see schemas/postgres_schema.sql.
    """
    tsquery = _sanitize_fts_query_pg(query)
    if not tsquery:
        return []
    sql = (
        "SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text, "
        "       d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at "
        "FROM document_chunks dc "
        "JOIN documents d ON d.document_id = dc.document_id "
        "WHERE dc.search_vector @@ to_tsquery('english', %(q)s)"
    )
    params: dict[str, object] = {"q": tsquery, "limit": limit}
    if company_id is not None:
        sql += " AND dc.company_id = %(company_id)s"
        params["company_id"] = company_id
    sql += " ORDER BY ts_rank(dc.search_vector, to_tsquery('english', %(q)s)) DESC LIMIT %(limit)s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def default_fact_store() -> FactStore:
    """The Postgres counterpart of `storage/fact_store.py::default_fact_store()`
    -- the only place in this file that imports the concrete Postgres-backed
    implementations directly."""
    from storage.repositories_pg import (
        find_knowledge_claims_about_entity,
        find_knowledge_claims_for_entity_ids,
        get_canonical_series,
        get_document_chunks_by_ids,
        get_latest_data_timestamp,
        get_macro_series,
        list_all_knowledge_claims,
        list_all_knowledge_entities,
        list_all_knowledge_evidence,
        list_all_knowledge_relationships,
        list_canonical_financials_for_companies,
        list_company_documents,
        list_company_ids_with_financial_data,
        list_document_chunks,
        list_entity_neighbors,
        list_generated_reports,
        list_knowledge_entities_for_companies,
        list_knowledge_entity_ids_by_type_and_name,
        list_knowledge_evidence_for_claim,
        list_knowledge_relationships_for_claim,
        list_macro_series_summary,
        list_recent_high_confidence_claims,
        list_report_evidence,
        list_report_followups,
        list_shareholding_history,
        save_investigation,
        save_investigation_hypothesis,
        save_investigation_hypothesis_evidence,
        save_system_insight,
        set_document_chunks_embedding_status,
    )

    return FactStore(
        get_canonical_series=get_canonical_series,
        list_canonical_financials_for_companies=list_canonical_financials_for_companies,
        list_knowledge_entities_for_companies=list_knowledge_entities_for_companies,
        find_knowledge_claims_about_entity=find_knowledge_claims_about_entity,
        list_knowledge_evidence_for_claim=list_knowledge_evidence_for_claim,
        list_knowledge_relationships_for_claim=list_knowledge_relationships_for_claim,
        list_all_knowledge_entities=list_all_knowledge_entities,
        list_all_knowledge_claims=list_all_knowledge_claims,
        list_all_knowledge_relationships=list_all_knowledge_relationships,
        list_all_knowledge_evidence=list_all_knowledge_evidence,
        list_knowledge_entity_ids_by_type_and_name=list_knowledge_entity_ids_by_type_and_name,
        list_entity_neighbors=list_entity_neighbors,
        find_knowledge_claims_for_entity_ids=find_knowledge_claims_for_entity_ids,
        list_company_documents=list_company_documents,
        search_document_chunks=search_document_chunks,
        list_document_chunks=list_document_chunks,
        get_document_chunks_by_ids=get_document_chunks_by_ids,
        set_document_chunks_embedding_status=set_document_chunks_embedding_status,
        get_macro_series=get_macro_series,
        list_macro_series_summary=list_macro_series_summary,
        list_generated_reports=list_generated_reports,
        list_report_evidence=list_report_evidence,
        list_report_followups=list_report_followups,
        get_latest_data_timestamp=get_latest_data_timestamp,
        get_company=get_company,
        list_companies_by_sector_field=list_companies_by_sector_field,
        list_companies_with_sector=list_companies_with_sector,
        save_investigation=save_investigation,
        save_investigation_hypothesis=save_investigation_hypothesis,
        save_investigation_hypothesis_evidence=save_investigation_hypothesis_evidence,
        list_recent_high_confidence_claims=list_recent_high_confidence_claims,
        save_system_insight=save_system_insight,
        list_company_ids_with_financial_data=list_company_ids_with_financial_data,
        list_shareholding_history=list_shareholding_history,
    )
