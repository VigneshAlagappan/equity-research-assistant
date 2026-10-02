"""Deterministic per-investigation metrics (docs/L5_MVP_TASK_PLAN.md, M7,
definitions version "mvp-1" in config/versions.py). Computed only from the
persisted graph and evidence rows -- no LLM, no database access in the pure
functions here.

Honest limits, repeated in every artifact's metrics block:
  - edge evidence counts only include items the evaluator tagged to a link;
    `tagging_rate` says how much that is. A low rate means edge-level numbers
    understate support.
  - every presented edge is treated as material (there is no materiality
    signal yet), recorded as materiality_basis = "none".
"""

from __future__ import annotations

from config.versions import GRAPH_VERSION  # noqa: F401  (re-exported for callers)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compute_graph_metrics(graph, evaluations: dict, hypotheses_total: int) -> dict:
    """Metrics derivable from the graph + evaluations alone."""
    edges = graph.edges
    presented = [e for e in edges if e["presented"]]
    supported = [e for e in presented if e["supporting_count"] > 0]

    all_items = []
    for evaluation in evaluations.values():
        all_items.extend(evaluation.supporting_evidence)
        all_items.extend(evaluation.contradicting_evidence)
    tagged = [i for i in all_items if getattr(i, "chain_step", None) is not None]
    contradicting_items = sum(len(ev.contradicting_evidence) for ev in evaluations.values())

    return {
        "hypotheses_total": hypotheses_total,
        "hypotheses_evaluated": len(evaluations),
        "nodes_explored": len(graph.nodes),
        "edges_explored": len(edges),
        "edges_presented": len(presented),
        "supported_edges": len(supported),
        "unsupported_edges": len(presented) - len(supported),
        "contradicting_evidence_items": contradicting_items,
        "evidence_coverage": _ratio(len(supported), len(presented)),
        "unsupported_edge_rate": _ratio(len(presented) - len(supported), len(presented)),
        "investigation_efficiency": _ratio(len(supported), len(edges)),
        "tagging_rate": _ratio(len(tagged), len(all_items)),
        "cross_sector_edges": None,  # no sector scope on nodes in the MVP
    }
