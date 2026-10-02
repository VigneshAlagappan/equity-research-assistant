"""Repository for the L5 causal investigation tables (docs/L5_MVP_TASK_PLAN.md,
M4): the per-investigation graph, per-investigation metrics, structured
feedback, and golden-eval results. SQLite implementation; the Postgres twin is
storage/causal_repository_pg.py (swapped in wholesale by
storage/backend_bootstrap.py, same as every other repository module).

Everything here is investigation-level structure -- never durable causal
knowledge, and nothing in this module touches `knowledge_relationships`.
Writers commit; readers return plain rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Graph
# ------------------------------------------------------------------

def replace_investigation_graph(conn: sqlite3.Connection, investigation_id: str, nodes: list[dict], edges: list[dict]) -> None:
    """Idempotent per investigation: re-persisting replaces that investigation's
    graph wholesale. Evidence rows keep their edge tags (set separately)."""
    conn.execute("DELETE FROM investigation_graph_edges WHERE investigation_id = ?", (investigation_id,))
    conn.execute("DELETE FROM investigation_graph_nodes WHERE investigation_id = ?", (investigation_id,))
    conn.executemany(
        "INSERT INTO investigation_graph_nodes (node_id, investigation_id, hypothesis_id, position, label, "
        "normalized_label, node_type, ontology_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(n["node_id"], investigation_id, n["hypothesis_id"], n["position"], n["label"], n["normalized_label"],
          n.get("node_type", "step"), n.get("ontology_ref")) for n in nodes],
    )
    conn.executemany(
        "INSERT INTO investigation_graph_edges (edge_id, investigation_id, hypothesis_id, position, source_node_id, "
        "target_node_id, edge_key, supporting_count, contradicting_count, presented, hypothesis_verdict, ontology_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(e["edge_id"], investigation_id, e["hypothesis_id"], e["position"], e["source_node_id"], e["target_node_id"],
          e["edge_key"], e.get("supporting_count", 0), e.get("contradicting_count", 0), int(bool(e.get("presented"))),
          e.get("hypothesis_verdict"), e.get("ontology_ref")) for e in edges],
    )
    conn.commit()


def list_graph_nodes(conn: sqlite3.Connection, investigation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_graph_nodes WHERE investigation_id = ? ORDER BY hypothesis_id, position",
        (investigation_id,),
    ).fetchall()


def list_graph_edges(conn: sqlite3.Connection, investigation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_graph_edges WHERE investigation_id = ? ORDER BY hypothesis_id, position",
        (investigation_id,),
    ).fetchall()


# ------------------------------------------------------------------
# Version stamps on the investigations row
# ------------------------------------------------------------------

def update_investigation_versions(conn: sqlite3.Connection, investigation_id: str, stamp: dict) -> None:
    conn.execute(
        "UPDATE investigations SET engine_version = ?, prompt_version = ?, config_hash = ?, "
        "metrics_definition_version = ? WHERE investigation_id = ?",
        (stamp.get("engine_version"), stamp.get("prompt_version"), stamp.get("config_hash"),
         stamp.get("metrics_definition_version"), investigation_id),
    )
    conn.commit()


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------

_METRIC_COLUMNS = (
    "investigation_version", "engine_version", "prompt_version", "config_hash", "metrics_definition_version",
    "models_used", "hypotheses_total", "hypotheses_evaluated", "nodes_explored", "edges_explored", "edges_presented",
    "supported_edges", "unsupported_edges", "contradicting_evidence_items", "evidence_coverage",
    "unsupported_edge_rate", "investigation_efficiency", "tagging_rate", "cross_sector_edges", "model_calls",
    "input_tokens", "output_tokens", "estimated_cost_usd", "iterations", "runtime_ms",
)


def save_investigation_metrics(conn: sqlite3.Connection, investigation_id: str, metrics: dict) -> None:
    conn.execute("DELETE FROM l5_investigation_metrics WHERE investigation_id = ?", (investigation_id,))
    values = [metrics.get(c) for c in _METRIC_COLUMNS]
    if isinstance(metrics.get("models_used"), (list, tuple)):
        values[_METRIC_COLUMNS.index("models_used")] = json.dumps(list(metrics["models_used"]))
    if values[0] is None:
        values[0] = 1
    conn.execute(
        f"INSERT INTO l5_investigation_metrics (investigation_id, {', '.join(_METRIC_COLUMNS)}, created_at) "
        f"VALUES (?, {', '.join('?' for _ in _METRIC_COLUMNS)}, ?)",
        (investigation_id, *values, _now()),
    )
    conn.commit()


def get_investigation_metrics(conn: sqlite3.Connection, investigation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM l5_investigation_metrics WHERE investigation_id = ?", (investigation_id,)
    ).fetchone()


def sum_llm_usage_for_investigation(conn: sqlite3.Connection, investigation_id: str) -> dict:
    """Calls, tokens, cost and models from llm_call_log (already keyed by
    investigation_id)."""
    rows = conn.execute(
        "SELECT model_used, input_tokens, output_tokens, estimated_cost_usd FROM llm_call_log WHERE investigation_id = ?",
        (investigation_id,),
    ).fetchall()
    return {
        "model_calls": len(rows),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "estimated_cost_usd": sum(r["estimated_cost_usd"] or 0 for r in rows),
        "models_used": sorted({r["model_used"] for r in rows if r["model_used"]}),
    }


# ------------------------------------------------------------------
# Feedback
# ------------------------------------------------------------------

def insert_feedback(conn: sqlite3.Connection, fb: dict) -> int:
    """Append one feedback row; any earlier ACTIVE vote by the same user on the
    same target (hypothesis/path/edge triple) is marked superseded by it."""
    cur = conn.execute(
        "INSERT INTO causal_feedback (investigation_id, investigation_version, hypothesis_id, path_id, edge_id, "
        "feedback_type, company_id, sector, geography, period, regime_tag, question_type, user_id, user_class, "
        "comment, engine_version, graph_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fb["investigation_id"], fb.get("investigation_version") or 1, fb.get("hypothesis_id"), fb.get("path_id"),
         fb.get("edge_id"), fb["feedback_type"], fb.get("company_id"), fb.get("sector"), fb.get("geography"),
         fb.get("period"), fb.get("regime_tag"), fb.get("question_type"), fb.get("user_id"),
         fb.get("user_class") or "ordinary", fb.get("comment"), fb.get("engine_version"), fb.get("graph_version"),
         _now()),
    )
    new_id = cur.lastrowid
    conn.execute(
        "UPDATE causal_feedback SET superseded_by = ? WHERE feedback_id != ? AND superseded_by IS NULL "
        "AND investigation_id = ? AND user_id IS ? AND hypothesis_id IS ? AND path_id IS ? AND edge_id IS ?",
        (new_id, new_id, fb["investigation_id"], fb.get("user_id"), fb.get("hypothesis_id"), fb.get("path_id"),
         fb.get("edge_id")),
    )
    conn.commit()
    return new_id


def list_feedback(conn: sqlite3.Connection, investigation_id: str, user_id: int | None = None,
                  include_superseded: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM causal_feedback WHERE investigation_id = ?"
    params: list = [investigation_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if not include_superseded:
        sql += " AND superseded_by IS NULL"
    return conn.execute(sql + " ORDER BY feedback_id", params).fetchall()


# ------------------------------------------------------------------
# Golden evals
# ------------------------------------------------------------------

_RUN_COLUMNS = (
    "benchmark_version", "engine_version", "prompt_version", "config_hash", "metrics_definition_version",
    "cases_total", "cases_completed", "golden_recall", "golden_precision_lower_bound", "evidence_coverage",
    "unsupported_edge_rate", "estimated_cost_usd", "runtime_ms", "s3_key",
)


def insert_eval_run(conn: sqlite3.Connection, eval_run_id: str, run: dict) -> None:
    conn.execute(
        f"INSERT INTO causal_eval_runs (eval_run_id, {', '.join(_RUN_COLUMNS)}, created_at) "
        f"VALUES (?, {', '.join('?' for _ in _RUN_COLUMNS)}, ?)",
        (eval_run_id, *[run.get(c) for c in _RUN_COLUMNS], _now()),
    )
    conn.commit()


def insert_eval_case_result(conn: sqlite3.Connection, eval_run_id: str, result: dict) -> None:
    unmatched = result.get("unmatched_presented")
    conn.execute(
        "INSERT INTO causal_eval_case_results (eval_run_id, case_id, investigation_id, status, expected_essential, "
        "matched_essential, presented_edges, matched_presented, unmatched_presented_json, error_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eval_run_id, result["case_id"], result.get("investigation_id"), result["status"],
         result.get("expected_essential"), result.get("matched_essential"), result.get("presented_edges"),
         result.get("matched_presented"), json.dumps(unmatched) if unmatched is not None else None,
         result.get("error_detail")),
    )
    conn.commit()


def list_eval_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM causal_eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


def list_eval_case_results(conn: sqlite3.Connection, eval_run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM causal_eval_case_results WHERE eval_run_id = ? ORDER BY case_id", (eval_run_id,)
    ).fetchall()
