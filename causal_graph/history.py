"""History/reference store for the causal graph service: what changed and the
evidence / validation / feedback references. A SQL adapter over
storage/causal_graph_repository (Postgres or SQLite per backend) and an
in-memory one for tests. Rows come back as plain dicts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol


class History(Protocol):
    def insert_event(self, ev: dict) -> int: ...
    def list_events(self, *, edge_id: str | None = None, node_id: str | None = None, limit: int = 200) -> list[dict]: ...
    def insert_evidence_ref(self, ref: dict) -> int: ...
    def list_evidence_refs(self, edge_id: str) -> list[dict]: ...
    def insert_validation_ref(self, ref: dict) -> int: ...
    def list_validation_refs(self, edge_id: str) -> list[dict]: ...
    def insert_feedback_ref(self, ref: dict) -> int: ...
    def list_feedback_refs(self, *, edge_id=None, target_kind=None, target_ref=None) -> list[dict]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlHistory:
    def __init__(self, conn):
        self._conn = conn

    @property
    def _repo(self):
        from storage import causal_graph_repository  # swapped for the _pg twin by backend_bootstrap

        return causal_graph_repository

    def insert_event(self, ev):
        return self._repo.insert_event(self._conn, ev)

    def list_events(self, *, edge_id=None, node_id=None, limit=200):
        return [dict(r) for r in self._repo.list_events(self._conn, edge_id=edge_id, node_id=node_id, limit=limit)]

    def insert_evidence_ref(self, ref):
        return self._repo.insert_evidence_ref(self._conn, ref)

    def list_evidence_refs(self, edge_id):
        return [dict(r) for r in self._repo.list_evidence_refs(self._conn, edge_id)]

    def insert_validation_ref(self, ref):
        return self._repo.insert_validation_ref(self._conn, ref)

    def list_validation_refs(self, edge_id):
        return [dict(r) for r in self._repo.list_validation_refs(self._conn, edge_id)]

    def insert_feedback_ref(self, ref):
        return self._repo.insert_feedback_ref(self._conn, ref)

    def list_feedback_refs(self, *, edge_id=None, target_kind=None, target_ref=None):
        return [dict(r) for r in self._repo.list_feedback_refs(
            self._conn, edge_id=edge_id, target_kind=target_kind, target_ref=target_ref)]


class InMemoryHistory:
    def __init__(self):
        self.events, self.evidence, self.validations, self.feedback = [], [], [], []

    def insert_event(self, ev):
        row = {**ev, "event_id": len(self.events) + 1, "changes_json": json.dumps(ev.get("changes") or {}, sort_keys=True),
               "created_at": _now()}
        self.events.append(row)
        return row["event_id"]

    def list_events(self, *, edge_id=None, node_id=None, limit=200):
        return [e for e in self.events
                if (edge_id is None or e.get("edge_id") == edge_id) and (node_id is None or e.get("node_id") == node_id)][:limit]

    def insert_evidence_ref(self, ref):
        self.evidence.append({**ref, "ref_id": len(self.evidence) + 1, "created_at": _now()})
        return self.evidence[-1]["ref_id"]

    def list_evidence_refs(self, edge_id):
        return [r for r in self.evidence if r["edge_id"] == edge_id]

    def insert_validation_ref(self, ref):
        self.validations.append({**ref, "validation_id": len(self.validations) + 1, "created_at": _now()})
        return self.validations[-1]["validation_id"]

    def list_validation_refs(self, edge_id):
        return [r for r in self.validations if r["edge_id"] == edge_id]

    def insert_feedback_ref(self, ref):
        self.feedback.append({**ref, "id": len(self.feedback) + 1, "created_at": _now()})
        return self.feedback[-1]["id"]

    def list_feedback_refs(self, *, edge_id=None, target_kind=None, target_ref=None):
        return [r for r in self.feedback
                if (edge_id is None or r.get("edge_id") == edge_id)
                and (target_kind is None or r["target_kind"] == target_kind)
                and (target_ref is None or r["target_ref"] == target_ref)]
