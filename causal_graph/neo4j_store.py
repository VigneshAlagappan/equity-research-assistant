"""Neo4j implementation of causal_graph.store.GraphStore.

Reuses context/graph_neo4j.get_driver() (one driver per process, same
settings). Every node gets the :CausalNode marker label and every edge
layer='causal', and every query filters on them, so the causal layer never
reads the semantic layer's AFFECTS/DRIVES/EXPOSED_TO edges that share names.
Relationship types and labels cannot be Cypher parameters; they are
interpolated only after a whitelist check against config/causal_graph.py.
"""

from __future__ import annotations

import json

from config import causal_graph as cg

_EDGE_SCALARS = (
    "edge_id", "direction", "mechanism", "confidence", "effect_strength", "lag_min", "lag_max", "lag_unit",
    "status", "version", "created_at", "updated_at",
)
_NODE_SCALARS = ("id", "family", "canonical_name", "display_name", "description", "version", "created_at", "updated_at")
_ALL_REFERENCE_FIELDS = sorted({f for fields in cg.NODE_REFERENCE_FIELDS.values() for f in fields})


def _family(family: str) -> str:
    if family not in cg.NODE_FAMILIES:
        raise ValueError(f"unknown node family {family!r}")
    return family


def _rel_type(rel_type: str) -> str:
    if rel_type not in cg.RELATIONSHIP_TYPES:
        raise ValueError(f"unknown relationship type {rel_type!r}")
    return rel_type


def _node_from_props(props: dict) -> dict:
    node = {k: props.get(k) for k in _NODE_SCALARS}
    node["references"] = {f: props[f] for f in _ALL_REFERENCE_FIELDS if props.get(f)}
    return node


def _edge_to_props(edge: dict) -> dict:
    props = {k: edge.get(k) for k in _EDGE_SCALARS if k not in ("edge_id",)}
    props["layer"] = cg.CAUSAL_LAYER
    props["scope_json"] = json.dumps(edge.get("scope") or {}, sort_keys=True)
    for key in ("geography", "sector"):
        props[f"scope_{key}"] = (edge.get("scope") or {}).get(key)
    prov = edge.get("provenance") or {}
    props["provenance_type"] = prov.get("type")
    props["provenance_ref"] = prov.get("ref")
    props["provenance_json"] = json.dumps(prov, sort_keys=True)
    return props


def _edge_from_record(rec) -> dict:
    p = dict(rec["props"])
    edge = {k: p.get(k) for k in _EDGE_SCALARS}
    edge["source_id"], edge["target_id"], edge["type"] = rec["source_id"], rec["target_id"], rec["type"]
    edge["scope"] = json.loads(p.get("scope_json") or "{}")
    edge["provenance"] = json.loads(p.get("provenance_json") or "{}")
    return edge


_EDGE_RETURN = "RETURN a.id AS source_id, b.id AS target_id, type(r) AS type, properties(r) AS props"


class Neo4jGraphStore:
    def __init__(self, driver=None):
        if driver is None:
            from context.graph_neo4j import get_driver

            driver = get_driver()
        self._driver = driver

    # -- schema -----------------------------------------------------------------

    def ensure_schema(self) -> list[str]:
        """Idempotent. Uniqueness on each family's id, and an index on each
        causal relationship type's edge_id. Returns the statements run."""
        statements = [
            f"CREATE CONSTRAINT causal_{family.lower()}_id IF NOT EXISTS "
            f"FOR (n:{family}) REQUIRE n.id IS UNIQUE" for family in cg.NODE_FAMILIES
        ] + [
            f"CREATE INDEX causal_edge_{t.lower()}_id IF NOT EXISTS FOR ()-[r:{t}]-() ON (r.edge_id)"
            for t in cg.RELATIONSHIP_TYPES
        ]
        with self._driver.session() as session:
            for statement in statements:
                session.run(statement)
        return statements

    # -- nodes ------------------------------------------------------------------

    def put_node(self, node: dict) -> dict:
        label = _family(node["family"])
        props = {k: node.get(k) for k in _NODE_SCALARS if k not in ("id", "created_at")}
        props.update(node.get("references") or {})
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:{label} {{id: $id}}) "
                f"ON CREATE SET n.created_at = $created_at "
                f"SET n:{cg.CAUSAL_NODE_LABEL}, n += $props",
                id=node["id"], created_at=node["created_at"], props=props,
            )
        return self.get_node(node["id"]) or node

    def get_node(self, node_id: str) -> dict | None:
        with self._driver.session() as session:
            rec = session.run(
                f"MATCH (n:{cg.CAUSAL_NODE_LABEL} {{id: $id}}) RETURN properties(n) AS props", id=node_id
            ).single()
        return _node_from_props(dict(rec["props"])) if rec else None

    def find_nodes(self, *, family=None, name_contains=None, limit=50) -> list[dict]:
        label = f":{_family(family)}" if family else ""
        with self._driver.session() as session:
            rows = session.run(
                f"MATCH (n:{cg.CAUSAL_NODE_LABEL}{label}) "
                "WHERE $needle = '' OR toLower(n.canonical_name) CONTAINS $needle "
                "OR toLower(n.display_name) CONTAINS $needle "
                "RETURN properties(n) AS props ORDER BY n.id LIMIT $limit",
                needle=(name_contains or "").lower(), limit=limit,
            )
            return [_node_from_props(dict(r["props"])) for r in rows]

    # -- edges ------------------------------------------------------------------

    def put_edge(self, edge: dict) -> dict:
        rel = _rel_type(edge["type"])
        with self._driver.session() as session:
            session.run(
                f"MATCH (a:{cg.CAUSAL_NODE_LABEL} {{id: $source}}), (b:{cg.CAUSAL_NODE_LABEL} {{id: $target}}) "
                f"MERGE (a)-[r:{rel} {{edge_id: $edge_id}}]->(b) "
                "ON CREATE SET r.created_at = $created_at "
                "SET r += $props",
                source=edge["source_id"], target=edge["target_id"], edge_id=edge["edge_id"],
                created_at=edge["created_at"], props={k: v for k, v in _edge_to_props(edge).items() if k != "created_at"},
            )
        return self.get_edge(edge["edge_id"]) or edge

    def patch_edge(self, edge_id: str, props: dict) -> dict | None:
        merged = self.get_edge(edge_id)
        if merged is None:
            return None
        merged.update(props)
        update = {k: v for k, v in _edge_to_props(merged).items() if k != "created_at"}
        with self._driver.session() as session:
            session.run(
                "MATCH (:CausalNode)-[r]->(:CausalNode) WHERE r.layer = $layer AND r.edge_id = $edge_id "
                "SET r += $props",
                layer=cg.CAUSAL_LAYER, edge_id=edge_id, props=update,
            )
        return self.get_edge(edge_id)

    def get_edge(self, edge_id: str) -> dict | None:
        with self._driver.session() as session:
            rec = session.run(
                f"MATCH (a:{cg.CAUSAL_NODE_LABEL})-[r]->(b:{cg.CAUSAL_NODE_LABEL}) "
                f"WHERE r.layer = $layer AND r.edge_id = $edge_id {_EDGE_RETURN}",
                layer=cg.CAUSAL_LAYER, edge_id=edge_id,
            ).single()
        return _edge_from_record(rec) if rec else None

    def find_edge(self, source_id: str, rel_type: str, target_id: str) -> dict | None:
        rel = _rel_type(rel_type)
        with self._driver.session() as session:
            rec = session.run(
                f"MATCH (a:{cg.CAUSAL_NODE_LABEL} {{id: $s}})-[r:{rel}]->(b:{cg.CAUSAL_NODE_LABEL} {{id: $t}}) "
                f"WHERE r.layer = $layer {_EDGE_RETURN} LIMIT 1",
                s=source_id, t=target_id, layer=cg.CAUSAL_LAYER,
            ).single()
        return _edge_from_record(rec) if rec else None

    def incident_edges(self, node_ids: list[str], *, limit: int) -> list[dict]:
        if not node_ids:
            return []
        with self._driver.session() as session:
            rows = session.run(
                f"MATCH (a:{cg.CAUSAL_NODE_LABEL})-[r]->(b:{cg.CAUSAL_NODE_LABEL}) "
                "WHERE r.layer = $layer AND (a.id IN $ids OR b.id IN $ids) "
                f"{_EDGE_RETURN} ORDER BY r.edge_id LIMIT $limit",
                layer=cg.CAUSAL_LAYER, ids=list(node_ids), limit=limit,
            )
            return [_edge_from_record(r) for r in rows]
