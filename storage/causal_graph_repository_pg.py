"""Postgres (Neon) port of storage/causal_graph_repository.py -- same function
names and signatures, psycopg2 connection (RealDictCursor rows)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from storage.db_types import DBConnection, Row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(conn: DBConnection, sql: str, params: tuple, returning: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"{sql} RETURNING {returning}", params)
        new_id = cur.fetchone()[returning]
    conn.commit()
    return new_id


def _select(conn: DBConnection, sql: str, params: list | tuple) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def insert_event(conn: DBConnection, ev: dict) -> int:
    return _insert(
        conn,
        "INSERT INTO causal_graph_events (event_type, node_id, edge_id, version, changes_json, actor_kind, actor_id, "
        "source, reason, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (ev["event_type"], ev.get("node_id"), ev.get("edge_id"), ev.get("version"),
         json.dumps(ev.get("changes") or {}, sort_keys=True), ev["actor_kind"], ev.get("actor_id"),
         ev.get("source"), ev.get("reason"), _now()),
        "event_id",
    )


def list_events(conn: DBConnection, *, edge_id: str | None = None, node_id: str | None = None,
                limit: int = 200) -> list[Row]:
    sql, params = "SELECT * FROM causal_graph_events WHERE TRUE", []
    if edge_id is not None:
        sql += " AND edge_id = %s"
        params.append(edge_id)
    if node_id is not None:
        sql += " AND node_id = %s"
        params.append(node_id)
    return _select(conn, sql + " ORDER BY event_id LIMIT %s", (*params, limit))


def insert_evidence_ref(conn: DBConnection, ref: dict) -> int:
    return _insert(
        conn,
        "INSERT INTO causal_graph_evidence_refs (edge_id, ref_type, locator, stance, note, added_by, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ref["edge_id"], ref["ref_type"], ref["locator"], ref["stance"], ref.get("note"), ref.get("added_by"), _now()),
        "ref_id",
    )


def list_evidence_refs(conn: DBConnection, edge_id: str) -> list[Row]:
    return _select(conn, "SELECT * FROM causal_graph_evidence_refs WHERE edge_id = %s ORDER BY ref_id", (edge_id,))


def insert_validation_ref(conn: DBConnection, ref: dict) -> int:
    return _insert(
        conn,
        "INSERT INTO causal_graph_validation_refs (edge_id, result, method, ref, note, added_by, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ref["edge_id"], ref["result"], ref["method"], ref.get("ref"), ref.get("note"), ref.get("added_by"), _now()),
        "validation_id",
    )


def list_validation_refs(conn: DBConnection, edge_id: str) -> list[Row]:
    return _select(
        conn, "SELECT * FROM causal_graph_validation_refs WHERE edge_id = %s ORDER BY validation_id", (edge_id,)
    )


def insert_feedback_ref(conn: DBConnection, ref: dict) -> int:
    return _insert(
        conn,
        "INSERT INTO causal_graph_feedback_refs (edge_id, target_kind, target_ref, feedback_type, feedback_id, "
        "added_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ref.get("edge_id"), ref["target_kind"], ref["target_ref"], ref["feedback_type"], ref.get("feedback_id"),
         ref.get("added_by"), _now()),
        "id",
    )


def list_feedback_refs(conn: DBConnection, *, edge_id: str | None = None, target_kind: str | None = None,
                       target_ref: str | None = None) -> list[Row]:
    sql, params = "SELECT * FROM causal_graph_feedback_refs WHERE TRUE", []
    for col, val in (("edge_id", edge_id), ("target_kind", target_kind), ("target_ref", target_ref)):
        if val is not None:
            sql += f" AND {col} = %s"
            params.append(val)
    return _select(conn, sql + " ORDER BY id", params)
