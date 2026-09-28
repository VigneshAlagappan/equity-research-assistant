"""Audit trail for research/routing_policy.py — one row per routed question,
persisted to signals_routing_log (schemas/sqlite_schema.sql /
schemas/postgres_schema.sql), per docs/ADR/023 section 5 ("every execution
must create an audit record so that evals can be added and run later").

Deliberately separate from llm/observability.py's llm_call_log (see that
table's own header comment in the schema file): this module logs the
ROUTING decision and its end-to-end outcome, not an individual model call —
routing_policy.py still calls llm/observability.record() for each model call
it makes along the way (tagged task_name="signals_level3"/"signals_level4"/
"jev_complexity_classifier"), so per-call token/cost accounting keeps
working exactly as it does for every other LLM call site in this app.

No private chain-of-thought is stored here — only observable actions,
decisions, evidence identifiers, and outputs, matching the policy's own
"Do not store private chain-of-thought" instruction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from storage.db_types import DBConnection

from storage.repositories import insert_signals_routing_log


@dataclass
class RoutingAudit:
    run_id: str
    question: str
    company_ids: list[str]
    jev_level: int
    jev_confidence: float | None
    jev_reason: str | None
    jev_source: str
    model_selected: str | None = None
    fallback_model_used: str | None = None
    data_sources: list[str] = field(default_factory=list)
    neo4j_used: bool = False
    planner_used: bool = False
    tools_executed: list[str] = field(default_factory=list)
    calculations_performed: list[str] = field(default_factory=list)
    evidence_identifiers: list[str] = field(default_factory=list)
    missing_data_issues: list[str] = field(default_factory=list)
    final_confidence: str | None = None
    execution_status: str = "answered"  # answered | insufficient_data | error
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    answer_reference: str | None = None


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def persist(conn: DBConnection, audit: RoutingAudit) -> None:
    insert_signals_routing_log(
        conn,
        run_id=audit.run_id,
        question=audit.question,
        company_ids=",".join(audit.company_ids),
        jev_level=audit.jev_level,
        jev_confidence=audit.jev_confidence,
        jev_reason=audit.jev_reason,
        jev_source=audit.jev_source,
        model_selected=audit.model_selected,
        fallback_model_used=audit.fallback_model_used,
        data_sources_json=json.dumps(audit.data_sources),
        neo4j_used=audit.neo4j_used,
        planner_used=audit.planner_used,
        tools_executed_json=json.dumps(audit.tools_executed),
        calculations_performed_json=json.dumps(audit.calculations_performed),
        evidence_identifiers_json=json.dumps(audit.evidence_identifiers),
        missing_data_issues_json=json.dumps(audit.missing_data_issues),
        final_confidence=audit.final_confidence,
        execution_status=audit.execution_status,
        latency_ms=audit.latency_ms,
        input_tokens=audit.input_tokens,
        output_tokens=audit.output_tokens,
        estimated_cost_usd=audit.estimated_cost_usd,
        answer_reference=audit.answer_reference,
    )
