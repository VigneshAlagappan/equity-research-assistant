"""CausalKnowledgeService -- the only sanctioned write path into the causal
graph, and its bounded read API.

Rules enforced here, never left to a caller or an LLM:
  * only canonical node families and relationship types; aliases are mapped,
    unknown names rejected (validation.py)
  * confidence, effect strength, lag, scope and provenance validated on every
    write; an equivalent edge (source, type, target) is never duplicated
  * every change to an edge's knowledge bumps its version and writes an audit
    event with old and new values
  * status moves only along config.causal_graph.STATUS_TRANSITIONS, gated on
    the references that justify it; an LLM can never reach EVIDENCE_BACKED,
    VALIDATED or PROMOTED, and only a human can promote
  * feedback, evidence and validation are stored as REFERENCES, in Postgres
    only (counts are derived on read). None of them changes confidence -- confidence moves only through
    update_relationship, which an LLM cannot call.

Reads are bounded (depth, nodes, edges); nothing here explores on its own.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from causal_graph import validation as v
from causal_graph.history import History
from causal_graph.store import GraphStore
from causal_graph.validation import (
    DuplicateNodeError, DuplicateRelationshipError, NotFoundError, TransitionError, ValidationError,
)
from config import causal_graph as cg
from config.causal_feedback import FEEDBACK_TYPES, TYPES_BY_LEVEL

logger = logging.getLogger(__name__)

#: Fields whose change is "important relationship knowledge": versioned.
TRACKED_FIELDS = (
    "direction", "mechanism", "confidence", "effect_strength", "lag_min", "lag_max", "lag_unit", "scope", "status",
)
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CausalKnowledgeService:
    def __init__(self, store: GraphStore, history: History, *, company_lookup=None, series_exists=None,
                 metric_exists=None):
        self.store, self.history = store, history
        self._company_lookup = company_lookup      # company_id -> display name | None
        self._series_exists = series_exists        # macro series_key -> bool
        self._metric_exists = metric_exists        # metric_key -> bool

    # -- audit ------------------------------------------------------------------

    def _emit(self, event_type: str, *, actor_kind: str, actor_id=None, source=None, reason=None, node_id=None,
              edge_id=None, version=None, changes=None) -> None:
        self.history.insert_event({
            "event_type": event_type, "node_id": node_id, "edge_id": edge_id, "version": version,
            "changes": changes, "actor_kind": actor_kind, "actor_id": actor_id, "source": source, "reason": reason,
        })
        # Ids and field names only -- never document text or payloads.
        logger.info("%s node=%s edge=%s version=%s actor=%s", event_type, node_id, edge_id, version, actor_kind)

    # -- nodes ------------------------------------------------------------------

    def create_node(self, family: str, display_name: str, *, canonical_name: str | None = None,
                    description: str | None = None, references: dict | None = None, actor_kind: str = "system",
                    actor_id: str | None = None, source: str | None = None) -> dict:
        node, created = self.ensure_node(
            family, display_name, canonical_name=canonical_name, description=description, references=references,
            actor_kind=actor_kind, actor_id=actor_id, source=source,
        )
        if not created:
            raise DuplicateNodeError(node["id"])
        return node

    def ensure_node(self, family: str, display_name: str, *, canonical_name: str | None = None,
                    description: str | None = None, references: dict | None = None, actor_kind: str = "system",
                    actor_id: str | None = None, source: str | None = None) -> tuple[dict, bool]:
        """Idempotent create: returns (node, created). An existing node is left
        exactly as it is."""
        v.validate_actor(actor_kind)
        if family == "Company" and self._company_lookup is not None:
            company_id = (canonical_name or display_name).strip()
            registered = self._company_lookup(company_id)
            if not registered:
                raise ValidationError(f"company {company_id!r} is not in the company registry")
            display_name, canonical_name = registered, company_id
        spec = v.validate_node(family, display_name, canonical_name=canonical_name, description=description,
                               references=references)
        refs = spec["references"]
        if refs.get("series_key") and self._series_exists and not self._series_exists(refs["series_key"]):
            raise ValidationError(f"macro series {refs['series_key']!r} not found in macro_observations")
        if refs.get("metric_key") and self._metric_exists and not self._metric_exists(refs["metric_key"]):
            raise ValidationError(f"financial metric {refs['metric_key']!r} not found")
        existing = self.store.get_node(spec["id"])
        if existing:
            return existing, False
        now = _now()
        node = self.store.put_node({**spec, "version": 1, "created_at": now, "updated_at": now})
        self._emit("causal_node_created", actor_kind=actor_kind, actor_id=actor_id, source=source, node_id=node["id"])
        return node, True

    def get_node(self, node_id: str) -> dict:
        node = self.store.get_node(node_id)
        if node is None:
            raise NotFoundError(f"node not found: {node_id}")
        return node

    def find_node(self, *, family: str | None = None, name: str | None = None, limit: int = 20) -> list[dict]:
        if family is not None and family not in cg.NODE_FAMILIES:
            raise ValidationError(f"unknown node family {family!r}")
        return self.store.find_nodes(family=family, name_contains=name, limit=max(1, min(limit, 100)))

    # -- relationships ----------------------------------------------------------

    def create_relationship(self, source_id: str, rel_type: str, target_id: str, *, direction=None, mechanism=None,
                            confidence=None, effect_strength=None, lag=None, scope=None, provenance=None,
                            status: str = "CANDIDATE", actor_kind: str = "system", actor_id=None, source=None,
                            reason=None) -> dict:
        v.validate_actor(actor_kind)
        rel = v.normalize_relationship_type(rel_type)
        src, tgt = self.store.get_node(source_id), self.store.get_node(target_id)
        if src is None:
            raise NotFoundError(f"source node not found: {source_id}")
        if tgt is None:
            raise NotFoundError(f"target node not found: {target_id}")
        if source_id == target_id:
            raise ValidationError("a relationship cannot start and end at the same node")
        v.validate_endpoints(rel, src["family"], tgt["family"])
        fields = v.validate_edge_fields(
            rel, direction=direction, mechanism=mechanism, confidence=confidence, effect_strength=effect_strength,
            lag=lag, scope=scope, provenance=provenance,
        )
        if status not in cg.CREATE_STATUSES:
            raise TransitionError(f"a new relationship starts as one of {sorted(cg.CREATE_STATUSES)}, not {status}")
        if status not in cg.ACTOR_ALLOWED_TARGETS[actor_kind]:
            raise TransitionError(f"actor {actor_kind!r} may not create a relationship at {status}")
        existing = self.store.find_edge(source_id, rel, target_id)
        if existing:
            raise DuplicateRelationshipError(existing["edge_id"])
        now = _now()
        edge = self.store.put_edge({
            "edge_id": v.edge_id_for(source_id, rel, target_id), "source_id": source_id, "target_id": target_id,
            "type": rel, **fields, "status": status, "version": 1, "created_at": now, "updated_at": now,
        })
        self._emit(
            "causal_relationship_created", actor_kind=actor_kind, actor_id=actor_id, source=source or fields["provenance"]["ref"],
            reason=reason, edge_id=edge["edge_id"], version=1,
            changes={k: {"old": None, "new": edge[k]} for k in TRACKED_FIELDS},
        )
        return edge

    def get_relationship(self, edge_id: str, *, include_references: bool = True) -> dict:
        edge = self.store.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"relationship not found: {edge_id}")
        return self._hydrate(edge) if include_references else edge

    def _hydrate(self, edge: dict) -> dict:
        """The edge plus its references. References, counts and history are read
        from Postgres (the system of record); the graph edge holds only the
        relationship knowledge."""
        eid = edge["edge_id"]
        evidence = self.history.list_evidence_refs(eid)
        validations = self.history.list_validation_refs(eid)
        feedback = self.history.list_feedback_refs(edge_id=eid)
        return {
            **edge,
            "evidence_refs": evidence, "validation_refs": validations, "feedback_refs": feedback,
            "evidence_support_count": sum(r["stance"] == "SUPPORTS" for r in evidence),
            "evidence_contradict_count": sum(r["stance"] == "CONTRADICTS" for r in evidence),
            "validation_count": len(validations),
            "last_validation_result": validations[-1]["result"] if validations else None,
            "feedback_count": len(feedback),
        }

    def update_relationship(self, edge_id: str, *, direction=_UNSET, mechanism=_UNSET, confidence=_UNSET,
                            effect_strength=_UNSET, lag=_UNSET, scope=_UNSET, actor_kind: str, actor_id=None,
                            source: str, reason: str) -> dict:
        """Change relationship knowledge. Needs a source and a reason, bumps the
        version and records old/new values. Status is not changed here
        (transition_status); an LLM cannot call this at all."""
        v.validate_actor(actor_kind)
        if actor_kind == "llm":
            raise TransitionError("an LLM cannot modify relationship knowledge; propose a new CANDIDATE instead")
        if not (source or "").strip() or not (reason or "").strip():
            raise ValidationError("source and reason are required to change relationship knowledge")
        edge = self.store.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"relationship not found: {edge_id}")
        new = dict(edge)
        if direction is not _UNSET:
            new["direction"] = v.validate_direction(edge["type"], direction)
        if mechanism is not _UNSET:
            if not (mechanism or "").strip() and edge["type"] not in cg.STRUCTURAL_TYPES:
                raise ValidationError("mechanism cannot be emptied")
            new["mechanism"] = (mechanism or "").strip() or None
        if confidence is not _UNSET:
            new["confidence"] = v.validate_confidence(confidence)
        if effect_strength is not _UNSET:
            new["effect_strength"] = v.validate_effect_strength(effect_strength)
        if lag is not _UNSET:
            new.update(v.validate_lag(lag))
        if scope is not _UNSET:
            new["scope"] = v.validate_scope(scope)
        if cg.IMPLIED_DIRECTION.get(edge["type"]) and new["direction"] != cg.IMPLIED_DIRECTION[edge["type"]]:
            raise ValidationError(f"{edge['type']} implies direction {cg.IMPLIED_DIRECTION[edge['type']]}")
        changes = {k: {"old": edge[k], "new": new[k]} for k in TRACKED_FIELDS if edge[k] != new[k]}
        if not changes:
            return edge
        new["version"], new["updated_at"] = edge["version"] + 1, _now()
        saved = self.store.put_edge(new)
        self._emit("causal_relationship_updated", actor_kind=actor_kind, actor_id=actor_id, source=source,
                   reason=reason, edge_id=edge_id, version=saved["version"], changes=changes)
        return saved

    # -- lifecycle --------------------------------------------------------------

    def transition_status(self, edge_id: str, new_status: str, *, actor_kind: str, actor_id=None, source=None,
                          reason: str) -> dict:
        v.validate_actor(actor_kind)
        if new_status not in cg.STATUSES:
            raise ValidationError(f"unknown status {new_status!r}")
        if not (reason or "").strip():
            raise ValidationError("a reason is required for a status change")
        edge = self.store.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"relationship not found: {edge_id}")
        old = edge["status"]
        if new_status not in cg.STATUS_TRANSITIONS[old]:
            raise TransitionError(f"{old} -> {new_status} is not an allowed transition")
        if new_status not in cg.ACTOR_ALLOWED_TARGETS[actor_kind]:
            raise TransitionError(f"actor {actor_kind!r} may not move a relationship to {new_status}")
        if new_status == "PROMOTED" and actor_kind not in cg.PROMOTION_ACTORS:
            raise TransitionError("only a human can promote a relationship")
        if new_status == "EVIDENCE_BACKED" and not any(
            r["stance"] == "SUPPORTS" for r in self.history.list_evidence_refs(edge_id)
        ):
            raise TransitionError("EVIDENCE_BACKED needs at least one supporting evidence reference")
        if new_status == "VALIDATED":
            results = [r["result"] for r in self.history.list_validation_refs(edge_id)]
            if "SUPPORT" not in results or results[-1] not in ("SUPPORT", "PARTIAL"):
                raise TransitionError("VALIDATED needs a SUPPORT validation and a latest validation of SUPPORT or PARTIAL")
        updated = self.store.put_edge({**edge, "status": new_status, "version": edge["version"] + 1, "updated_at": _now()})
        self._emit("causal_relationship_status_changed", actor_kind=actor_kind, actor_id=actor_id, source=source,
                   reason=reason, edge_id=edge_id, version=updated["version"],
                   changes={"status": {"old": old, "new": new_status}})
        return updated

    # -- references -------------------------------------------------------------

    def _require_edge(self, edge_id: str) -> dict:
        edge = self.store.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"relationship not found: {edge_id}")
        return edge

    def attach_evidence(self, edge_id: str, ref_type: str, locator: str, stance: str, *, note: str | None = None,
                        actor_kind: str = "system", actor_id=None, source=None) -> int:
        """Store a pointer to evidence (S3 key, Neon row, Qdrant chunk id,
        investigation id) -- never the evidence. Stored in Postgres only;
        confidence is untouched."""
        v.validate_actor(actor_kind)
        edge = self._require_edge(edge_id)
        if ref_type not in cg.EVIDENCE_REF_TYPES:
            raise ValidationError(f"evidence ref_type must be one of {cg.EVIDENCE_REF_TYPES}")
        if stance not in cg.EVIDENCE_STANCES:
            raise ValidationError(f"stance must be one of {cg.EVIDENCE_STANCES}")
        if not (locator or "").strip():
            raise ValidationError("evidence locator is required")
        ref_id = self.history.insert_evidence_ref({
            "edge_id": edge_id, "ref_type": ref_type, "locator": locator.strip(), "stance": stance,
            "note": (note or "")[: cg.MAX_NOTE_CHARS] or None, "added_by": actor_id or actor_kind,
        })
        self._emit("causal_evidence_attached", actor_kind=actor_kind, actor_id=actor_id, source=source,
                   edge_id=edge_id, version=edge["version"], changes={"ref_type": ref_type, "stance": stance})
        return ref_id

    def attach_validation(self, edge_id: str, result: str, method: str, *, ref: str | None = None,
                          note: str | None = None, actor_kind: str = "validation", actor_id=None) -> int:
        """Record that a validation ran and what it concluded. The full result
        stays wherever `ref` points (a pilot run, a report key)."""
        v.validate_actor(actor_kind)
        edge = self._require_edge(edge_id)
        if result not in cg.VALIDATION_RESULTS:
            raise ValidationError(f"validation result must be one of {cg.VALIDATION_RESULTS}")
        if not (method or "").strip():
            raise ValidationError("validation method is required")
        validation_id = self.history.insert_validation_ref({
            "edge_id": edge_id, "result": result, "method": method.strip(), "ref": ref,
            "note": (note or "")[: cg.MAX_NOTE_CHARS] or None, "added_by": actor_id or actor_kind,
        })
        self._emit("causal_validation_attached", actor_kind=actor_kind, actor_id=actor_id, source=method,
                   edge_id=edge_id, version=edge["version"], changes={"result": result})
        return validation_id

    def attach_feedback(self, target_kind: str, target_ref: str, feedback_type: str, *, edge_id: str | None = None,
                        feedback_id: int | None = None, actor_kind: str = "human", actor_id=None) -> int:
        """Link structured feedback to an edge / path / hypothesis /
        investigation. Reference only: feedback never changes confidence."""
        v.validate_actor(actor_kind)
        if target_kind not in cg.FEEDBACK_TARGET_KINDS:
            raise ValidationError(f"target_kind must be one of {cg.FEEDBACK_TARGET_KINDS}")
        if feedback_type not in FEEDBACK_TYPES:
            raise ValidationError(f"unknown feedback type {feedback_type!r}")
        if feedback_type not in TYPES_BY_LEVEL[target_kind]:
            raise ValidationError(f"feedback type {feedback_type} does not apply at level {target_kind}")
        if not (target_ref or "").strip():
            raise ValidationError("target_ref is required")
        if target_kind == "edge":
            edge_id = target_ref
        edge = self._require_edge(edge_id) if edge_id else None
        row_id = self.history.insert_feedback_ref({
            "edge_id": edge_id, "target_kind": target_kind, "target_ref": target_ref.strip(),
            "feedback_type": feedback_type, "feedback_id": feedback_id, "added_by": actor_id or actor_kind,
        })
        self._emit("causal_feedback_attached", actor_kind=actor_kind, actor_id=actor_id, edge_id=edge_id,
                   version=edge["version"] if edge else None, changes={"target_kind": target_kind, "feedback_type": feedback_type})
        return row_id

    def get_relationship_history(self, edge_id: str) -> list[dict]:
        self._require_edge(edge_id)
        out = []
        for ev in self.history.list_events(edge_id=edge_id):
            ev = dict(ev)
            ev["changes"] = json.loads(ev.get("changes_json") or "{}")
            out.append(ev)
        return out

    # -- bounded traversal ------------------------------------------------------

    def _expand(self, node_id: str, direction: str, *, max_depth, max_nodes, max_edges, min_confidence,
                include_deprecated, include_references) -> dict:
        """BFS over causal (non-structural) edges. direction 'causes' walks
        influence upstream, 'effects' downstream; REVERSE-flow edges
        (DEPENDS_ON, CONSUMES, EXPOSED_TO) are oriented by who influences whom."""
        max_depth = max(1, min(int(max_depth), cg.MAX_TRAVERSAL_DEPTH))
        max_nodes = max(1, min(int(max_nodes), cg.MAX_TRAVERSAL_NODES))
        max_edges = max(1, min(int(max_edges), cg.MAX_TRAVERSAL_EDGES))
        root = self.get_node(node_id)
        nodes, edges, seen_edges = {node_id: root}, [], set()
        frontier, truncated = [node_id], False
        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            next_frontier: list[str] = []
            for edge in self.store.incident_edges(frontier, limit=max_edges + 1):
                flow = cg.FLOW.get(edge["type"], "NONE")
                if flow == "NONE" or edge["edge_id"] in seen_edges:
                    continue
                if edge["status"] == "DEPRECATED" and not include_deprecated:
                    continue
                if min_confidence is not None and edge["confidence"] < min_confidence:
                    continue
                cause, effect = (edge["source_id"], edge["target_id"]) if flow == "FORWARD" else (edge["target_id"], edge["source_id"])
                here, there = (effect, cause) if direction == "causes" else (cause, effect)
                if here not in frontier:
                    continue
                if len(edges) >= max_edges or (there not in nodes and len(nodes) >= max_nodes):
                    truncated = True
                    continue
                seen_edges.add(edge["edge_id"])
                if there not in nodes:
                    nodes[there] = self.store.get_node(there)
                    next_frontier.append(there)
                edges.append({**(self._hydrate(edge) if include_references else edge), "depth": depth,
                              "cause_id": cause, "effect_id": effect})
            frontier = next_frontier
        return {"root": node_id, "direction": direction, "nodes": nodes, "edges": edges, "truncated": truncated}

    def expand_causes(self, node_id: str, *, max_depth: int = 3, max_nodes: int = 50, max_edges: int = 100,
                      min_confidence: float | None = None, include_deprecated: bool = False,
                      include_references: bool = True) -> dict:
        return self._expand(node_id, "causes", max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges,
                            min_confidence=min_confidence, include_deprecated=include_deprecated,
                            include_references=include_references)

    def expand_effects(self, node_id: str, *, max_depth: int = 3, max_nodes: int = 50, max_edges: int = 100,
                       min_confidence: float | None = None, include_deprecated: bool = False,
                       include_references: bool = True) -> dict:
        return self._expand(node_id, "effects", max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges,
                            min_confidence=min_confidence, include_deprecated=include_deprecated,
                            include_references=include_references)

    def get_drivers(self, node_id: str, **kw) -> dict:
        """Direct causes of the node (depth 1)."""
        return self.expand_causes(node_id, max_depth=1, **kw)

    def get_dependents(self, node_id: str, **kw) -> dict:
        """Direct effects of the node (depth 1): what depends on it."""
        return self.expand_effects(node_id, max_depth=1, **kw)

    def get_sector_mechanisms(self, sector_id: str, *, max_depth: int = 2, max_nodes: int = 50,
                              max_edges: int = 100) -> dict:
        """Causal edges around a sector, both directions, bounded."""
        if self.get_node(sector_id)["family"] != "Sector":
            raise ValidationError(f"{sector_id} is not a Sector")
        up = self.expand_causes(sector_id, max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges)
        down = self.expand_effects(sector_id, max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges)
        edges = {e["edge_id"]: e for e in up["edges"] + down["edges"]}
        return {"sector": sector_id, "nodes": {**up["nodes"], **down["nodes"]}, "edges": list(edges.values()),
                "truncated": up["truncated"] or down["truncated"]}

    def get_company_exposures(self, company_id: str, *, max_edges: int = 100) -> dict:
        """What the company is directly exposed to, the sectors it belongs to,
        and those sectors' mechanisms (inherited, not copied per company)."""
        if self.get_node(company_id)["family"] != "Company":
            raise ValidationError(f"{company_id} is not a Company")
        exposures, sectors = [], []
        for edge in self.store.incident_edges([company_id], limit=max_edges):
            if edge["source_id"] != company_id:
                continue
            if edge["type"] == "BELONGS_TO":
                sectors.append(edge["target_id"])
            elif edge["type"] == "EXPOSED_TO":
                exposures.append(self._hydrate(edge))
        return {
            "company": company_id, "exposures": exposures, "sectors": sectors,
            "sector_mechanisms": {s: self.get_sector_mechanisms(s, max_depth=1) for s in sectors},
        }

    def get_cross_sector_dependencies(self, sector_id: str, *, max_depth: int = 4, max_nodes: int = 100,
                                      max_edges: int = 200) -> dict:
        """Other sectors reachable from this one through causal edges, upstream
        and downstream, with the edges on the way."""
        if self.get_node(sector_id)["family"] != "Sector":
            raise ValidationError(f"{sector_id} is not a Sector")
        up = self.expand_causes(sector_id, max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges)
        down = self.expand_effects(sector_id, max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges)
        def sectors(res):
            return sorted(n for n, node in res["nodes"].items() if node and node["family"] == "Sector" and n != sector_id)
        return {"sector": sector_id, "upstream_sectors": sectors(up), "downstream_sectors": sectors(down),
                "upstream": up, "downstream": down}
