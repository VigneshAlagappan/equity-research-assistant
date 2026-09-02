"""Capability interfaces the Investigation Planner (Step 2F,
research/investigation_planner.py) depends on — `Protocol` contracts, not
concrete module imports, so a capability could later be swapped for a
remote service/different backend without touching plan_and_gather()'s
routing logic (architecture guardrail: "Planner -> Capability Interface ->
Current In-Process Implementation").

Every Protocol's __call__ signature matches the real in-process function it
fronts exactly, so those functions already satisfy it structurally — no
wrapper classes needed. Same minimal pattern llm/providers/base.py's
Provider(Protocol) already uses elsewhere in this codebase for swappable
model providers.
"""

from __future__ import annotations

from storage.db_types import DBConnection
from dataclasses import dataclass
from typing import Protocol

from context.graph import GraphCandidate
from context.knowledge_graph import KnowledgeClaimView
from context.reuse import ReuseCandidate
from research.evidence import Evidence
from retrieval.document_search import DocumentPassage
from storage.fact_store import FactStore, default_fact_store


class FinancialEvidenceCapability(Protocol):
    def __call__(self, conn: DBConnection, company_id: str) -> list[Evidence]: ...


class DocumentEvidenceCapability(Protocol):
    def __call__(self, conn: DBConnection, company_id: str, question: str) -> list[Evidence]: ...


class MacroEvidenceCapability(Protocol):
    def __call__(self, conn: DBConnection, question: str) -> list[Evidence]: ...


class DocumentSearchCapability(Protocol):
    def __call__(
        self, conn: DBConnection, query: str, *, company_id: str | None, limit: int
    ) -> list[DocumentPassage]: ...


class KnowledgeGraphCapability(Protocol):
    def __call__(self, conn: DBConnection, entity_type: str, entity_name: str) -> list[KnowledgeClaimView]: ...


@dataclass(frozen=True)
class PlannerCapabilities:
    """Bundles the Planner's five capability dependencies behind one seam.
    default_capabilities() below binds the real in-process implementations —
    swap one field to point at a remote/alternate backend without changing
    plan_and_gather()'s routing logic."""

    financial_evidence: FinancialEvidenceCapability
    document_evidence: DocumentEvidenceCapability
    macro_evidence: MacroEvidenceCapability
    document_search: DocumentSearchCapability
    knowledge_graph: KnowledgeGraphCapability


def default_capabilities(*, fact_store: FactStore | None = None) -> PlannerCapabilities:
    """The only place that imports the 5 concrete implementations directly —
    everywhere else routes through the PlannerCapabilities seam instead.
    `fact_store` (storage/fact_store.py — a separate, lower-level seam) is
    threaded into each binding via a thin wrapper, so a single injected
    FactStore reaches every one of the 5 capabilities without changing
    PlannerCapabilities' own outward signatures."""
    from context.knowledge_graph import find_claims_about_entity
    from research.documents import get_document_evidence
    from research.macro_evidence import get_macro_evidence
    from retrieval.document_search import search_documents
    from retrieval.structured_search import get_company_evidence

    fs = fact_store or default_fact_store()
    return PlannerCapabilities(
        financial_evidence=lambda conn, company_id: get_company_evidence(conn, company_id, fact_store=fs),
        document_evidence=lambda conn, company_id, question: get_document_evidence(
            conn, company_id, question, fact_store=fs
        ),
        macro_evidence=lambda conn, question: get_macro_evidence(conn, question, fact_store=fs),
        document_search=lambda conn, query, *, company_id, limit: search_documents(
            conn, query, company_id=company_id, limit=limit, fact_store=fs
        ),
        knowledge_graph=lambda conn, entity_type, entity_name: find_claims_about_entity(
            conn, entity_type, entity_name, fact_store=fs
        ),
    )


class ReusableReportCapability(Protocol):
    def __call__(
        self, conn: DBConnection, question: str, company_ids: list[str], statement_type: str | None
    ) -> ReuseCandidate | None: ...


class RelatedInvestigationsCapability(Protocol):
    def __call__(self, conn: DBConnection, question: str, company_ids: list[str]) -> list[GraphCandidate]: ...


@dataclass(frozen=True)
class InvestigationMemoryCapabilities:
    """Bundles the Investigation Memory capability's two dependencies —
    reuse-before-recompute (context/reuse.py) and sector-peer prior-
    investigation surfacing (context/graph.py) — behind one seam, consumed by
    research/assistant.py and research/signals_report.py. Same
    'Planner -> Capability Interface -> Implementation' shape as
    PlannerCapabilities, applied to a different capability."""

    reusable_report: ReusableReportCapability
    related_investigations: RelatedInvestigationsCapability


def default_investigation_memory(*, fact_store: FactStore | None = None) -> InvestigationMemoryCapabilities:
    from context.graph import find_related_investigations
    from context.reuse import find_reusable_report

    fs = fact_store or default_fact_store()
    return InvestigationMemoryCapabilities(
        reusable_report=lambda conn, question, company_ids, statement_type: find_reusable_report(
            conn, question, company_ids, statement_type, fact_store=fs
        ),
        related_investigations=lambda conn, question, company_ids: find_related_investigations(
            conn, question, company_ids, fact_store=fs
        ),
    )
