"""SQLite side of the persistent causal graph's sidecar tables (history/audit
and evidence / validation / feedback references; see schemas/sqlite_schema.sql
and config/causal_graph.py). The Postgres twin is causal_graph_repository_pg.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_event(conn: sqlite3.Connection, ev: dict) -> int:
    cur = conn.execute(
        "INSERT INTO causal_graph_events (event_type, node_id, edge_id, version, changes_json, actor_kind, actor_id, "
        "source, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ev["event_type"], ev.get("node_id"), ev.get("edge_id"), ev.get("version"),
         json.dumps(ev.get("changes") or {}, sort_keys=True), ev["actor_kind"], ev.get("actor_id"),
         ev.get("source"), ev.get("reason"), _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_events(conn: sqlite3.Connection, *, edge_id: str | None = None, node_id: str | None = None,
                limit: int = 200) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM causal_graph_events WHERE 1=1", []
    if edge_id is not None:
        sql += " AND edge_id = ?"
        params.append(edge_id)
    if node_id is not None:
        sql += " AND node_id = ?"
        params.append(node_id)
    return conn.execute(sql + " ORDER BY event_id LIMIT ?", (*params, limit)).fetchall()


def insert_evidence_ref(conn: sqlite3.Connection, ref: dict) -> int:
    cur = conn.execute(
        "INSERT INTO causal_graph_evidence_refs (edge_id, ref_type, locator, stance, note, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref["edge_id"], ref["ref_type"], ref["locator"], ref["stance"], ref.get("note"), ref.get("added_by"), _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_evidence_refs(conn: sqlite3.Connection, edge_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM causal_graph_evidence_refs WHERE edge_id = ? ORDER BY ref_id", (edge_id,)
    ).fetchall()


def insert_validation_ref(conn: sqlite3.Connection, ref: dict) -> int:
    cur = conn.execute(
        "INSERT INTO causal_graph_validation_refs (edge_id, result, method, ref, note, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref["edge_id"], ref["result"], ref["method"], ref.get("ref"), ref.get("note"), ref.get("added_by"), _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_validation_refs(conn: sqlite3.Connection, edge_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM causal_graph_validation_refs WHERE edge_id = ? ORDER BY validation_id", (edge_id,)
    ).fetchall()


def insert_feedback_ref(conn: sqlite3.Connection, ref: dict) -> int:
    cur = conn.execute(
        "INSERT INTO causal_graph_feedback_refs (edge_id, target_kind, target_ref, feedback_type, feedback_id, "
        "added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref.get("edge_id"), ref["target_kind"], ref["target_ref"], ref["feedback_type"], ref.get("feedback_id"),
         ref.get("added_by"), _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_feedback_refs(conn: sqlite3.Connection, *, edge_id: str | None = None, target_kind: str | None = None,
                       target_ref: str | None = None) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM causal_graph_feedback_refs WHERE 1=1", []
    for col, val in (("edge_id", edge_id), ("target_kind", target_kind), ("target_ref", target_ref)):
        if val is not None:
            sql += f" AND {col} = ?"
            params.append(val)
    return conn.execute(sql + " ORDER BY id", params).fetchall()
