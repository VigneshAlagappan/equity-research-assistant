"""Graph storage seam for the causal graph: a small interface, an in-memory
implementation (tests and local runs without Neo4j) and, in
causal_graph/neo4j_store.py, the Neo4j one. The service holds all the rules;
a store only persists and fetches.

Node dict:  id, family, canonical_name, display_name, description, version,
            created_at, updated_at, references {field: value}
Edge dict:  edge_id, source_id, target_id, type, direction, mechanism,
            confidence, effect_strength, lag_min, lag_max, lag_unit, scope,
            status, version, provenance, created_at, updated_at
            (evidence / validation / feedback references and the change history
            live in Postgres only, never on the graph edge)
"""

from __future__ import annotations

import copy
from typing import Protocol

class GraphStore(Protocol):
    def ensure_schema(self) -> list[str]: ...
    def put_node(self, node: dict) -> dict: ...
    def get_node(self, node_id: str) -> dict | None: ...
    def find_nodes(self, *, family: str | None = None, name_contains: str | None = None, limit: int = 50) -> list[dict]: ...
    def put_edge(self, edge: dict) -> dict: ...
    def patch_edge(self, edge_id: str, props: dict) -> dict | None: ...
    def get_edge(self, edge_id: str) -> dict | None: ...
    def find_edge(self, source_id: str, rel_type: str, target_id: str) -> dict | None: ...
    def incident_edges(self, node_ids: list[str], *, limit: int) -> list[dict]: ...


class InMemoryGraphStore:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}

    def ensure_schema(self) -> list[str]:
        return []

    def put_node(self, node: dict) -> dict:
        existing = self.nodes.get(node["id"])
        stored = copy.deepcopy(node)
        if existing:
            stored["created_at"] = existing["created_at"]
        self.nodes[node["id"]] = stored
        return copy.deepcopy(stored)

    def get_node(self, node_id: str) -> dict | None:
        node = self.nodes.get(node_id)
        return copy.deepcopy(node) if node else None

    def find_nodes(self, *, family=None, name_contains=None, limit=50) -> list[dict]:
        needle = (name_contains or "").lower()
        rows = [
            n for n in self.nodes.values()
            if (family is None or n["family"] == family)
            and (not needle or needle in n["canonical_name"].lower() or needle in n["display_name"].lower())
        ]
        return [copy.deepcopy(n) for n in sorted(rows, key=lambda n: n["id"])[:limit]]

    def put_edge(self, edge: dict) -> dict:
        existing = self.edges.get(edge["edge_id"])
        stored = copy.deepcopy(edge)
        if existing:
            stored["created_at"] = existing["created_at"]
        self.edges[edge["edge_id"]] = stored
        return copy.deepcopy(stored)

    def patch_edge(self, edge_id: str, props: dict) -> dict | None:
        edge = self.edges.get(edge_id)
        if edge is None:
            return None
        edge.update(copy.deepcopy(props))
        return copy.deepcopy(edge)

    def get_edge(self, edge_id: str) -> dict | None:
        edge = self.edges.get(edge_id)
        return copy.deepcopy(edge) if edge else None

    def find_edge(self, source_id: str, rel_type: str, target_id: str) -> dict | None:
        for edge in self.edges.values():
            if edge["source_id"] == source_id and edge["type"] == rel_type and edge["target_id"] == target_id:
                return copy.deepcopy(edge)
        return None

    def incident_edges(self, node_ids: list[str], *, limit: int) -> list[dict]:
        ids = set(node_ids)
        rows = [e for e in self.edges.values() if e["source_id"] in ids or e["target_id"] in ids]
        return [copy.deepcopy(e) for e in sorted(rows, key=lambda e: e["edge_id"])[:limit]]
