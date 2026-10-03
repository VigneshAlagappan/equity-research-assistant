"""Investigation graph (docs/L5_MVP_TASK_PLAN.md, M6): turns each hypothesis's
causal chain (`chain_steps`, cause -> observed effect) into nodes and
consecutive-pair edges, attaches evidence counts per edge from the evaluator's
per-item `chain_step` tags, and marks which edges are "presented".

Pure functions, no database or LLM: persistence is storage/causal_repository.py,
orchestration is research/investigation.py::_persist.

This is INVESTIGATION-LEVEL structure only -- the graph one question activated,
not durable causal knowledge. Nodes are per hypothesis (no cross-hypothesis
merging yet: that needs an ontology), so `edge_key` -- a normalized
"source->target" string -- is what makes edges comparable across
investigations later.

Definitions (metrics_definition_version "mvp-1"):
  - presented edge: an edge of a hypothesis whose verdict is SUPPORTED or
    PARTIALLY_SUPPORTED (what the synthesis can rely on).
  - an edge's supporting/contradicting count counts only evidence TAGGED to
    that link; untagged evidence stays hypothesis-level and is never spread
    across edges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PRESENTED_VERDICTS = frozenset({"SUPPORTED", "PARTIALLY_SUPPORTED"})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_label(label: str) -> str:
    """Lowercase, punctuation -> single spaces, trimmed."""
    return _NON_ALNUM.sub(" ", (label or "").lower()).strip()


@dataclass
class InvestigationGraph:
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

    def edge_id_for(self, hypothesis_id: str, link: int | None) -> str | None:
        if link is None:
            return None
        for edge in self.edges:
            if edge["hypothesis_id"] == hypothesis_id and edge["position"] == link:
                return edge["edge_id"]
        return None


def build_graph(investigation_id: str, hypotheses: list, evaluations: dict) -> InvestigationGraph:
    """`hypotheses`: objects with hypothesis_id and chain_steps. `evaluations`:
    {hypothesis_id: evaluation} where an evaluation has verdict and
    supporting_evidence/contradicting_evidence items carrying `chain_step`.
    A hypothesis with fewer than two usable steps contributes no edges (the UI
    already falls back to its prose mechanism)."""
    graph = InvestigationGraph()
    for hypothesis in hypotheses:
        steps = [s for s in (getattr(hypothesis, "chain_steps", None) or []) if isinstance(s, str) and s.strip()]
        if len(steps) < 2:
            continue
        hid = hypothesis.hypothesis_id
        evaluation = evaluations.get(hid)
        verdict = evaluation.verdict if evaluation is not None else None
        presented = verdict in PRESENTED_VERDICTS
        node_ids = []
        for position, label in enumerate(steps):
            node_id = f"{investigation_id}:{hid}:n{position}"
            node_ids.append(node_id)
            graph.nodes.append({
                "node_id": node_id, "hypothesis_id": hid, "position": position, "label": label.strip(),
                "normalized_label": normalize_label(label), "node_type": "step",
            })
        for position in range(len(steps) - 1):
            supporting = contradicting = 0
            if evaluation is not None:
                supporting = sum(1 for e in evaluation.supporting_evidence if getattr(e, "chain_step", None) == position)
                contradicting = sum(1 for e in evaluation.contradicting_evidence if getattr(e, "chain_step", None) == position)
            graph.edges.append({
                "edge_id": f"{investigation_id}:{hid}:e{position}", "hypothesis_id": hid, "position": position,
                "source_node_id": node_ids[position], "target_node_id": node_ids[position + 1],
                "edge_key": f"{normalize_label(steps[position])}->{normalize_label(steps[position + 1])}",
                "supporting_count": supporting, "contradicting_count": contradicting,
                "presented": presented, "hypothesis_verdict": verdict,
            })
    return graph
