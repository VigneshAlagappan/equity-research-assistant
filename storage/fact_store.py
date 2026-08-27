"""FactStore — the "Fact/Data Access" capability interface (architecture
guardrail #3: "access structured factual data through a repository/
data-access interface... business logic, Planner logic, or reasoning logic
must never depend directly on SQLite-specific behavior"). Every field is a
plain callable matching a real storage/repositories.py or
companies/registry.py function's signature exactly — those functions already
satisfy this dataclass structurally, no wrapper classes needed, same pattern
research/capabilities.py's PlannerCapabilities already established.

default_fact_store() is the only place that imports the concrete SQLite-
backed functions directly. Everywhere else (research/investigation.py and
everything it calls, context/reuse.py, context/graph.py, context/graph_neo4j.py,
context/knowledge_graph.py, retrieval/*.py) takes an optional `fact_store`
parameter defaulting to it — so swapping SQLite for Postgres tomorrow means
supplying a different FactStore, not editing every call site."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FactStore:
    # Financials / structured facts (retrieval/structured_search.py)
    get_canonical_series: Callable[..., list[sqlite3.Row]]

    # Knowledge graph — entities/claims/relationships/evidence
    # (research/investigation_planner.py, research/hypothesis_generator.py,
    # context/knowledge_graph.py, context/graph_neo4j.py)
    list_knowledge_entities_for_companies: Callable[..., list[sqlite3.Row]]
    find_knowledge_claims_about_entity: Callable[..., list[sqlite3.Row]]
    list_knowledge_evidence_for_claim: Callable[..., list[sqlite3.Row]]
    list_knowledge_relationships_for_claim: Callable[..., list[sqlite3.Row]]
    list_all_knowledge_entities: Callable[..., list[sqlite3.Row]]
    list_all_knowledge_claims: Callable[..., list[sqlite3.Row]]
    list_all_knowledge_relationships: Callable[..., list[sqlite3.Row]]
    list_all_knowledge_evidence: Callable[..., list[sqlite3.Row]]

    # Documents (research/documents.py)
    list_company_documents: Callable[..., list[sqlite3.Row]]

    # Document search (retrieval/document_search.py)
    search_document_chunks: Callable[..., list[sqlite3.Row]]

    # Macro (research/macro_evidence.py)
    get_macro_series: Callable[..., list[sqlite3.Row]]
    list_macro_series_summary: Callable[..., list[sqlite3.Row]]

    # Generated reports / investigation memory (context/reuse.py,
    # context/graph.py, context/graph_neo4j.py)
    list_generated_reports: Callable[..., list[sqlite3.Row]]
    list_report_evidence: Callable[..., list[sqlite3.Row]]
    list_report_followups: Callable[..., list[sqlite3.Row]]
    get_latest_data_timestamp: Callable[..., str | None]

    # Companies (research/hypothesis_generator.py, context/graph.py,
    # context/graph_neo4j.py)
    get_company: Callable[..., sqlite3.Row | None]
    list_companies_by_sector_field: Callable[..., list[sqlite3.Row]]
    list_companies_with_sector: Callable[..., list[sqlite3.Row]]

    # Investigations (2E-2H persistence writes, research/investigation.py)
    save_investigation: Callable[..., None]
    save_investigation_hypothesis: Callable[..., None]
    save_investigation_hypothesis_evidence: Callable[..., None]


def default_fact_store() -> FactStore:
    """The only place that imports the real SQLite-backed implementations
    directly. Everywhere else routes through an injected/default FactStore."""
    from companies.registry import get_company, list_companies_by_sector_field, list_companies_with_sector
    from storage.repositories import (
        find_knowledge_claims_about_entity,
        get_canonical_series,
        get_latest_data_timestamp,
        get_macro_series,
        list_all_knowledge_claims,
        list_all_knowledge_entities,
        list_all_knowledge_evidence,
        list_all_knowledge_relationships,
        list_company_documents,
        list_generated_reports,
        list_knowledge_entities_for_companies,
        list_knowledge_evidence_for_claim,
        list_knowledge_relationships_for_claim,
        list_macro_series_summary,
        list_report_evidence,
        list_report_followups,
        save_investigation,
        save_investigation_hypothesis,
        save_investigation_hypothesis_evidence,
        search_document_chunks,
    )

    return FactStore(
        get_canonical_series=get_canonical_series,
        list_knowledge_entities_for_companies=list_knowledge_entities_for_companies,
        find_knowledge_claims_about_entity=find_knowledge_claims_about_entity,
        list_knowledge_evidence_for_claim=list_knowledge_evidence_for_claim,
        list_knowledge_relationships_for_claim=list_knowledge_relationships_for_claim,
        list_all_knowledge_entities=list_all_knowledge_entities,
        list_all_knowledge_claims=list_all_knowledge_claims,
        list_all_knowledge_relationships=list_all_knowledge_relationships,
        list_all_knowledge_evidence=list_all_knowledge_evidence,
        list_company_documents=list_company_documents,
        search_document_chunks=search_document_chunks,
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
    )
