"""Deterministic per-investigation metrics (docs/L5_MVP_TASK_PLAN.md, M7,
definitions version "mvp-1" in config/versions.py). Computed only from the
persisted graph and evidence rows -- no LLM, no database access in the pure
functions here.

Definitions version "mvp-2" splits "unsupported" so metrics stop blaming the model
for gaps in the data. Over PRESENTED edges (research/investigation_graph.py):
  supported     supporting evidence tagged to the edge, none against
  contested     supporting AND contradicting evidence tagged to it
  contradicted  contradicting evidence only
  untested      no evidence tagged to it either way (a coverage gap: the data
                may not exist, may not have been retrieved, or may be untagged --
                this metric cannot tell which, and says so)
`unsupported_edges` / `unsupported_edge_rate` keep their mvp-1 meaning
(supporting count = 0, i.e. contradicted + untested) for continuity;
`untested_edge_rate` and `contradicted_edge_rate` are the new honest split.
Evidence added by the data-first link checks (research/link_evidence.py) and the
gap-fill pass (research/link_gap_fill.py) is counted separately by origin.

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
    untested = [e for e in presented if e["supporting_count"] == 0 and e["contradicting_count"] == 0]
    contradicted = [e for e in presented if e["supporting_count"] == 0 and e["contradicting_count"] > 0]
    contested = [e for e in presented if e["supporting_count"] > 0 and e["contradicting_count"] > 0]
    by_origin = {"CALCULATED": 0, "RETRIEVED": 0}
    for item in all_items:
        origin = getattr(item, "source_tier", None)
        if origin in by_origin:
            by_origin[origin] += 1

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
        "edges_untested": len(untested),
        "edges_contradicted": len(contradicted),
        "edges_contested": len(contested),
        "untested_edge_rate": _ratio(len(untested), len(presented)),
        "contradicted_edge_rate": _ratio(len(contradicted), len(presented)),
        "link_items_calculated": by_origin["CALCULATED"],
        "link_items_gapfill": by_origin["RETRIEVED"],
        "cross_sector_edges": None,  # no sector scope on nodes in the MVP
    }
