"""Bounded candidate-path generation over the persistent causal graph.

Reads only through CausalKnowledgeService (get_drivers: one depth-1 query per
expanded node), never Neo4j directly. Each candidate edge is scored for this
investigation (research/causal_chain_ranking.py) or rejected with a recorded
reason; at most `max_branches_per_node` causes survive per node, and the whole
walk stops at max_depth / max_nodes / max_edges. Candidate paths are then
enumerated root-cause -> target over the surviving edges, with a cap on how many
foreign sectors a path may pass through.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from config import settings
from research.causal_chain_ranking import Context, path_lag_months, path_score, score_edge, _norm

ROOT_FAMILIES = frozenset({"MacroIndicator", "Commodity"})
MAX_CANDIDATE_PATHS = 200


@dataclass(frozen=True)
class ChainLimits:
    max_depth: int = 5
    max_branches_per_node: int = 4
    max_nodes: int = 40
    max_edges: int = 60
    max_cross_sector_hops: int = 2
    max_iterations: int = 2
    paths_per_iteration: int = 4
    max_retained_paths: int = 4
    min_edge_confidence: float = 0.3
    min_path_score: float = 0.35
    cross_sector_min_materiality: float = 0.3
    max_narrative_queries: int = 6

    @classmethod
    def from_settings(cls, **overrides) -> "ChainLimits":
        base = dict(
            max_depth=settings.CHAIN_MAX_DEPTH, max_branches_per_node=settings.CHAIN_MAX_BRANCHES_PER_NODE,
            max_nodes=settings.CHAIN_MAX_NODES, max_edges=settings.CHAIN_MAX_EDGES,
            max_cross_sector_hops=settings.CHAIN_MAX_CROSS_SECTOR_HOPS, max_iterations=settings.CHAIN_MAX_ITERATIONS,
            paths_per_iteration=settings.CHAIN_PATHS_PER_ITERATION, max_retained_paths=settings.CHAIN_MAX_RETAINED_PATHS,
            min_edge_confidence=settings.CHAIN_MIN_EDGE_CONFIDENCE, min_path_score=settings.CHAIN_MIN_PATH_SCORE,
            cross_sector_min_materiality=settings.CHAIN_CROSS_SECTOR_MIN_MATERIALITY,
            max_narrative_queries=settings.CHAIN_MAX_NARRATIVE_QUERIES,
        )
        base.update(overrides)
        for key in ("max_depth", "max_branches_per_node", "max_nodes", "max_edges", "max_iterations",
                    "paths_per_iteration", "max_retained_paths"):
            base[key] = max(1, int(base[key]))
        base["max_cross_sector_hops"] = max(0, int(base["max_cross_sector_hops"]))
        return cls(**base)


@dataclass
class Trace:
    """Everything needed to reconstruct what the traversal did."""
    service_calls: int = 0
    edges_considered: int = 0
    nodes_expanded: list[str] = field(default_factory=list)
    rejected_edges: list[dict] = field(default_factory=list)
    rejected_paths: list[dict] = field(default_factory=list)
    cross_sector_hops_seen: int = 0

    def reject_edge(self, edge: dict, reason: str) -> None:
        self.rejected_edges.append({"edge_id": edge["edge_id"], "cause_id": edge["cause_id"],
                                    "effect_id": edge["effect_id"], "type": edge["type"], "reason": reason})


@dataclass
class CandidatePath:
    node_ids: list[str]            # root cause ... target
    edges: list[dict]              # same order, each carrying score components
    score: float
    cross_sector_nodes: list[str]
    cumulative_lag_months: tuple[float, float] | None

    @property
    def key(self) -> str:
        return ">".join(self.node_ids)


def _foreign_sector(node: dict | None, ctx: Context) -> bool:
    return bool(node) and node["family"] == "Sector" and _norm(node["display_name"]) not in {_norm(s) for s in ctx.sector_names}


def sector_fit(ordered_edges: list[dict], node_ids: list[str], nodes: dict[str, dict], ctx: Context) -> str | None:
    """None if the path suits the company's sector, else the reason it does not.

    Applies only when the company's sectors are known and the path passes through some other
    sector. Such a path must be anchored to the company: it passes through the company's own
    sector, or one of its edges is scoped to that sector. And it must not leave the company's
    sector for one downstream of it (a steelmaker's margin is not explained through the autos
    it sells to)."""
    if not ctx.sector_names:
        return None
    own = {_norm(s) for s in ctx.sector_names}
    sector_pos = [(i, _norm(nodes[n]["display_name"])) for i, n in enumerate(node_ids) if nodes[n]["family"] == "Sector"]
    foreign = [i for i, name in sector_pos if name not in own]
    if not foreign:
        return None
    own_pos = [i for i, name in sector_pos if name in own]
    scoped = any(_norm((e.get("scope") or {}).get("sector", "")) in own for e in ordered_edges)
    if not own_pos and not scoped:
        return "foreign_sector_without_company_link"
    if own_pos and max(foreign) > min(own_pos):
        return "leaves_company_sector_downstream"
    return None


def generate_candidate_paths(service, target_id: str, ctx: Context, limits: ChainLimits, trace: Trace,
                             measurable) -> tuple[dict[str, dict], list[CandidatePath]]:
    """Returns (nodes by id, candidate paths best-first). `measurable(cause_node, effect_node)` says
    whether both end nodes of an edge can be tested against stored data."""
    target = service.get_node(target_id)
    nodes: dict[str, dict] = {target_id: target}
    causes_of: dict[str, list[dict]] = {}
    depth_of, queue, edge_count = {target_id: 0}, deque([target_id]), 0

    while queue:
        node_id = queue.popleft()
        if depth_of[node_id] >= limits.max_depth:
            continue
        trace.service_calls += 1
        trace.nodes_expanded.append(node_id)
        result = service.get_drivers(node_id, include_deprecated=True)
        usable: list[tuple[dict, object]] = []
        for edge in result["edges"]:
            trace.edges_considered += 1
            cause_node = result["nodes"].get(edge["cause_id"])
            if edge["status"] == "DEPRECATED":
                trace.reject_edge(edge, "deprecated"); continue
            if edge["confidence"] < limits.min_edge_confidence:
                trace.reject_edge(edge, "below_min_confidence"); continue
            scored = score_edge(edge, ctx, measurable=measurable(cause_node, result["nodes"].get(edge["effect_id"])))
            if isinstance(scored, str):
                trace.reject_edge(edge, scored); continue
            if _foreign_sector(cause_node, ctx) and scored.materiality < limits.cross_sector_min_materiality:
                trace.reject_edge(edge, "cross_sector_low_materiality"); continue
            usable.append((edge, scored))
        usable.sort(key=lambda p: (-p[1].score, p[0]["edge_id"]))
        for edge, scored in usable[limits.max_branches_per_node:]:
            trace.reject_edge(edge, "branch_limit")
        for edge, scored in usable[: limits.max_branches_per_node]:
            cause = edge["cause_id"]
            if edge_count >= limits.max_edges:
                trace.reject_edge(edge, "edge_budget"); continue
            if cause not in nodes:
                if len(nodes) >= limits.max_nodes:
                    trace.reject_edge(edge, "node_budget"); continue
                nodes[cause] = result["nodes"][cause]
                depth_of[cause] = depth_of[node_id] + 1
                queue.append(cause)
            causes_of.setdefault(node_id, []).append({
                **edge, "score": scored.score, "components": scored.components,
                "materiality": scored.materiality, "relevance": scored.relevance,
            })
            edge_count += 1

    paths: list[CandidatePath] = []
    window = ctx.window_months

    def walk(node_id: str, chain: list[dict], visited: set[str], foreign: list[str]) -> None:
        if len(paths) >= MAX_CANDIDATE_PATHS:
            return
        causes = causes_of.get(node_id, [])
        if chain and (not causes or nodes[node_id]["family"] in ROOT_FAMILIES):
            ordered = list(reversed(chain))
            lag = path_lag_months(ordered)
            ids = [ordered[0]["cause_id"]] + [e["effect_id"] for e in ordered]
            misfit = sector_fit(ordered, ids, nodes, ctx)
            if window is not None and lag and lag[0] > window:
                trace.rejected_paths.append({"path": ids, "reason": "cumulative_lag_exceeds_window"})
            elif misfit:
                trace.rejected_paths.append({"path": ids, "reason": misfit})
            else:
                paths.append(CandidatePath(ids, ordered, path_score([e["score"] for e in ordered]), list(foreign), lag))
            if not causes or nodes[node_id]["family"] in ROOT_FAMILIES:
                return
        for edge in causes:
            cause = edge["cause_id"]
            if cause in visited:
                trace.rejected_paths.append({"path": [cause, node_id], "reason": "cycle"}); continue
            next_foreign = foreign + [cause] if _foreign_sector(nodes[cause], ctx) else foreign
            if len(next_foreign) > limits.max_cross_sector_hops:
                trace.rejected_paths.append({"path": [cause, node_id], "reason": "cross_sector_hop_limit"}); continue
            walk(cause, chain + [edge], visited | {cause}, next_foreign)

    walk(target_id, [], {target_id}, [])
    paths.sort(key=lambda p: (-p.score, p.key))
    trace.cross_sector_hops_seen = max((len(p.cross_sector_nodes) for p in paths), default=0)
    return nodes, paths
