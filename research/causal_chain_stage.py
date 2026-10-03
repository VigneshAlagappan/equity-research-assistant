"""The dynamic causal-chain stage of the L5 investigation pipeline.

Runs inside research/investigation.py for Level-5 questions only (L1-L4 never
reach it). It reads the persistent causal graph through CausalKnowledgeService,
tests candidate paths against stored data, and returns a plain-data result that
the investigation persists beside its hypotheses. It never raises: any failure
(graph unreachable, empty graph, no target) becomes a result with a status, so
the existing investigation proceeds unchanged.

  status "ok"          a chain was built (it may still hold no retained path)
  status "skipped"     disabled by settings
  status "unavailable" the causal graph could not be reached or read
  status "no_target"   nothing in the graph matches the question
  status "error"       unexpected failure (logged)

No model is called here; narrative contradiction search uses the existing
hybrid retriever with the fixed cue-word baseline.
"""

from __future__ import annotations

import logging

from config import settings
from research.dynamic_chain import build_dynamic_chain

logger = logging.getLogger(__name__)


def _geography(conn, company_id: str | None) -> str | None:
    if not company_id:
        return None
    from companies.registry import get_company

    company = get_company(conn, company_id)
    return None if company is None else ("US" if (company["currency"] or "INR") == "USD" else "IN")


def _narrative(conn, fact_store, as_of):
    from retrieval.hybrid_search import hybrid_search_documents

    def search(query: str, company_id: str | None) -> list[dict]:
        try:
            passages = hybrid_search_documents(conn, query, company_id=company_id, limit=5, fact_store=fact_store, as_of=as_of)
        except Exception:  # noqa: BLE001 -- retrieval trouble means no narrative evidence, not a failed stage
            logger.info("narrative search unavailable for %r", query, exc_info=True)
            return []
        return [{"chunk_id": p.chunk_id, "text": p.text} for p in passages]

    return search


def _open_service(conn):
    from causal_graph.history import SqlHistory
    from causal_graph.neo4j_store import Neo4jGraphStore
    from causal_graph.service import CausalKnowledgeService
    from companies.registry import get_company

    return CausalKnowledgeService(
        Neo4jGraphStore(), SqlHistory(conn),
        company_lookup=lambda cid: (lambda row: row["display_name"] if row else None)(get_company(conn, cid)),
    )


def run_causal_chain_stage(conn, question: str, company_ids: list[str], investigation_id: str, *,
                           statement_type: str = "consolidated", fact_store=None, as_of: str | None = None,
                           service=None) -> dict:
    if not settings.CAUSAL_CHAIN_ENABLED:
        return {"status": "skipped", "reason": "CAUSAL_CHAIN_ENABLED is off"}
    company_id = company_ids[0] if company_ids else None
    try:
        service = service or _open_service(conn)
        result = build_dynamic_chain(
            service, question, company_id=company_id, geography=_geography(conn, company_id), conn=conn,
            fact_store=fact_store, statement_type=statement_type,
            narrative=_narrative(conn, fact_store, as_of) if company_id else None,
            investigation_id=investigation_id, attach_references=settings.CAUSAL_CHAIN_ATTACH_EVIDENCE,
        )
    except Exception as exc:  # noqa: BLE001 -- additive stage: never fail the investigation
        logger.warning("Causal chain stage failed for %s", investigation_id, exc_info=True)
        status = "unavailable" if type(exc).__module__.split(".")[0] in ("neo4j", "psycopg2") or "Neo4j" in type(exc).__name__ else "error"
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"status": status, "reason": f"{type(exc).__name__}: {exc}"[:300]}
    result["status"] = "ok" if result["target"] else "no_target"
    return result
