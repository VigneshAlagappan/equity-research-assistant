"""L5 dynamic causal-chain traversal.

Given a question and a context (company, sector, geography, period), builds the
smallest set of material, evidence-tested causal paths from the PERSISTENT causal
graph, reading only through CausalKnowledgeService:

  identify target -> context -> bounded candidate paths (ranked) -> test each
  path as a hypothesis against current data -> look for contradictions ->
  weigh alternatives -> drop weak/irrelevant paths -> final chain + explanation

The graph says what Signal knows; the result says which part of it explains
THIS question. Persistent edge confidence is never modified here: evidence found
during the run is attached only as references (Postgres), and the runtime
ranking score lives in the result, not on any edge. No LLM is called in this
module; narrative contradiction search goes through an injected retrieval
callable. The result is plain JSON-able data with stable ids (investigation,
hypothesis, path, edge) so structured feedback can later point at any of them.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from causal_graph.validation import NotFoundError
from research.causal_chain_paths import CandidatePath, ChainLimits, Trace, generate_candidate_paths
from research.causal_chain_ranking import Context, parse_period
from research.link_evidence import (
    LEVEL_THRESHOLD, MIN_POINTS, Loader, match_concept, realized_direction, stated_direction,
)

logger = logging.getLogger(__name__)

SUPPORTED, PLAUSIBLE, WEAK, CONTRADICTED, UNRESOLVED = "SUPPORTED", "PLAUSIBLE", "WEAK", "CONTRADICTED", "UNRESOLVED"
_STATUS_RANK = {SUPPORTED: 0, PLAUSIBLE: 1, UNRESOLVED: 2, WEAK: 3, CONTRADICTED: 4}
SUPPORTED_MIN_FINDINGS = 2
CONTRADICTION_CUES = re.compile(
    r"hedg|locked[- ]in|fixed[- ]price|long[- ]term contract|pass(ed)?[- ]through|offset by|fully offset|unchanged|flat", re.I
)

#: question wording -> a node display name looked up in the graph (the graph decides what exists).
TARGET_SYNONYMS: dict[str, tuple[str, ...]] = {
    "operating margin": ("margin", "ebitda margin", "operating profit margin", "profitability", "operating profit"),
    "revenue": ("revenue", "sales", "top line", "turnover"),
}


# --- observations -------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    node_id: str
    direction: int          # +1 up, -1 down, 0 flat over the window
    label: str
    ref_type: str           # EVIDENCE_REF_TYPES
    locator: str
    detail: str


class Observer(Protocol):
    def can_observe(self, node: dict) -> bool: ...
    def observe(self, node: dict, ctx: Context) -> Observation | None: ...


def _window_values(rows: list[tuple[str, float]], period: tuple[int, int] | None) -> list[float]:
    """Series values inside the fiscal window (Indian FY: Apr of first-1 .. Mar of last); all values without a window."""
    if period is None:
        return [v for _, v in rows]
    lo, hi = (period[0] - 1) * 12 + 3, period[1] * 12 + 2  # months since year 0, Apr(first-1) .. Mar(last)
    out = []
    for p, v in rows:
        parts = str(p).split("-")
        try:
            year, month = int(parts[0]), int(parts[1]) if len(parts) > 1 else 6
        except ValueError:
            continue
        if lo <= year * 12 + month - 1 <= hi:
            out.append(v)
    return out


class MacroSeriesObserver:
    """A node that references a macro series (node.references.series_key)."""

    def __init__(self, conn):
        self._conn = conn

    def can_observe(self, node: dict) -> bool:
        return bool((node.get("references") or {}).get("series_key"))

    def observe(self, node: dict, ctx: Context) -> Observation | None:
        from storage.repositories import get_macro_series

        key = (node.get("references") or {}).get("series_key")
        if not key:
            return None
        rows = [(r["period"], float(r["value"])) for r in get_macro_series(self._conn, key)]
        values = _window_values(rows, ctx.period)
        if len(values) < MIN_POINTS:
            return None
        first, last = values[0], values[-1]
        moved = first != 0 and abs(last / first - 1.0) >= LEVEL_THRESHOLD
        direction = (1 if last > first else -1) if moved else 0
        return Observation(
            node["id"], direction, f"{node['display_name']} ({key}): {first:,.2f} -> {last:,.2f}",
            "NEON_OBSERVATION", f"macro_observations:{key}", f"{len(values)} observations in window",
        )


class FinancialObserver:
    """A node whose name maps to a metric we compute from canonical financials."""

    def __init__(self, loader: Loader, company_id: str):
        self._loader, self._company = loader, company_id

    def can_observe(self, node: dict) -> bool:
        return node["family"] in ("EconomicDriver", "BusinessDriver", "FinancialMetric") and match_concept(node["display_name"]) is not None

    def observe(self, node: dict, ctx: Context) -> Observation | None:
        concept = match_concept(node["display_name"])
        if concept is None:
            return None
        try:
            series = concept.compute(self._loader)
        except Exception:  # noqa: BLE001 -- a missing metric is "no observation", not an error
            return None
        if ctx.period:
            series = {y: v for y, v in series.items() if ctx.period[0] <= y <= ctx.period[1]}
        real = realized_direction(series, concept.kind)
        if real is None:
            return None
        direction, y0, y1, first, last = real
        unit = "%" if concept.kind == "ratio" else ""
        proxy = " (proxy)" if concept.proxy else ""
        return Observation(
            node["id"], direction, f"{self._company} {concept.label}{proxy}: {first:,.1f}{unit} -> {last:,.1f}{unit} (FY{y0}-FY{y1})",
            "NEON_OBSERVATION", f"canonical_financials:{self._company}:{concept.key}:FY{y0}-FY{y1}", "annual canonical series",
        )


class CompositeObserver:
    def __init__(self, observers: list[Observer]):
        self._observers = observers

    def can_observe(self, node: dict) -> bool:
        return any(o.can_observe(node) for o in self._observers)

    def observe(self, node: dict, ctx: Context) -> Observation | None:
        for o in self._observers:
            if o.can_observe(node):
                found = o.observe(node, ctx)
                if found is not None:
                    return found
        return None


def default_observers(conn, company_id: str | None, statement_type: str, fact_store) -> list[Observer]:
    from research.link_evidence import make_loader

    observers: list[Observer] = [MacroSeriesObserver(conn)]
    if company_id:
        loader = make_loader(conn, company_id, statement_type, fact_store)
        if loader is not None:
            observers.append(FinancialObserver(loader, company_id))
    return observers


NarrativeSearch = Callable[[str, "str | None"], "list[dict]"]  # (query, company_id) -> [{"chunk_id", "text"}]


# --- target and context ---------------------------------------------------------------

def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(r"\b" + re.escape(phrase.lower()) + r"\b", text) is not None


def identify_target(service, question: str, explicit: str | None = None) -> dict | None:
    """The graph node the question is about: an explicit id, else a metric/driver
    whose name (or a known synonym) appears in the question."""
    if explicit:
        try:
            return service.get_node(explicit)
        except NotFoundError:
            return None
    text = (question or "").lower()
    candidates = service.find_node(family="FinancialMetric", limit=100) + service.find_node(family="BusinessDriver", limit=100)
    for node in candidates:  # exact name first
        if _has_phrase(text, node["display_name"]):
            return node
    for node in candidates:
        if any(_has_phrase(text, w) for w in TARGET_SYNONYMS.get(node["display_name"].lower(), ())):
            return node
    return None


def build_context(service, company_id: str | None, geography: str | None, period, question: str) -> Context:
    sectors: list[str] = []
    exposure_ids: set[str] = set()
    if company_id:
        try:
            exposures = service.get_company_exposures(company_id)
            sectors = [service.get_node(s)["display_name"] for s in exposures["sectors"]]
            exposure_ids = {e["target_id"] for e in exposures["exposures"]}
        except NotFoundError:
            pass  # a company not in the graph still gets an investigation; it just has no sector context
    if isinstance(period, str):
        period = parse_period(period)
    return Context(company_id=company_id, sector_names=tuple(sectors), geography=geography,
                   period=period or parse_period(question), exposure_node_ids=frozenset(exposure_ids))


# --- testing one path ------------------------------------------------------------------

_SIGN = {"POSITIVE": 1, "NEGATIVE": -1}


def _arrow(d: int) -> str:
    return {1: "up", -1: "down", 0: "flat"}[d]


class _Run:
    """Per-run caches and budgets, kept off the result."""

    def __init__(self, observer: Observer | None, narrative: NarrativeSearch | None, limits: ChainLimits, ctx: Context):
        self.observer, self.narrative, self.limits, self.ctx = observer, narrative, limits, ctx
        self._obs: dict[str, Observation | None] = {}
        self.observer_calls = self.narrative_queries = 0
        self.evidence_retrieved = self.contradictions_found = 0

    def observe(self, node: dict) -> Observation | None:
        if node["id"] not in self._obs:
            self.observer_calls += 1
            self._obs[node["id"]] = self.observer.observe(node, self.ctx) if self.observer and self.observer.can_observe(node) else None
        return self._obs[node["id"]]


def _finding(stance: str, kind: str, label: str, ref_type: str, locator: str, edge_id: str | None, detail: str = "") -> dict:
    return {"stance": stance, "kind": kind, "label": label, "ref_type": ref_type, "locator": locator,
            "edge_id": edge_id, "detail": detail}


def evaluate_path(path: CandidatePath, nodes: dict[str, dict], run: _Run, target_dir: int | None) -> dict:
    """Test one candidate path as a hypothesis and classify it."""
    edges, ids = path.edges, path.node_ids
    expected: dict[str, int | None] = {ids[-1]: target_dir}
    for edge in reversed(edges):  # propagate the required movement from the target back to each cause
        effect_dir, sign = expected.get(edge["effect_id"]), _SIGN.get(edge["direction"])
        expected[edge["cause_id"]] = effect_dir * sign if effect_dir and sign else None
    edge_for_cause = {e["cause_id"]: e["edge_id"] for e in edges}

    findings: list[dict] = []
    unmeasured: list[str] = []
    observed: dict[str, Observation | None] = {}
    for node_id in ids:
        obs = observed[node_id] = run.observe(nodes[node_id])
        if obs is None:
            unmeasured.append(node_id)
            continue
        run.evidence_retrieved += 1
        want = expected.get(node_id)
        if want is None or node_id == ids[-1]:
            continue  # no stated direction, or the target itself: its movement is the question's premise, not evidence for one path
        if obs.direction == want:
            findings.append(_finding("SUPPORTS", "NODE_MOVEMENT", f"{obs.label}: moved {_arrow(obs.direction)} as hypothesised",
                                     obs.ref_type, obs.locator, edge_for_cause[node_id], obs.detail))
        else:
            why = "stayed flat" if obs.direction == 0 else f"moved {_arrow(obs.direction)}, not {_arrow(want)}"
            findings.append(_finding("CONTRADICTS", "NODE_MOVEMENT", f"{obs.label}: {why}", obs.ref_type, obs.locator,
                                     edge_for_cause[node_id], obs.detail))
    for edge in edges:  # does the effect move the way the cause implies?
        c, e, sign = observed.get(edge["cause_id"]), observed.get(edge["effect_id"]), _SIGN.get(edge["direction"])
        if c is None or e is None or sign is None or c.direction == 0 or e.direction == 0:
            continue
        ok = e.direction == c.direction * sign
        findings.append(_finding(
            "SUPPORTS" if ok else "CONTRADICTS", "EDGE_CONSISTENCY",
            f"{nodes[edge['cause_id']]['display_name']} {_arrow(c.direction)}, {nodes[edge['effect_id']]['display_name']} "
            f"{_arrow(e.direction)}: {'consistent with' if ok else 'against'} a {edge['direction'].lower()} link",
            e.ref_type, f"{c.locator}|{e.locator}", edge["edge_id"]))

    narrative_hits = 0
    if run.narrative and run.ctx.company_id:
        for node_id in ids[:-1][:2]:  # the root and the next step: where hedging/pass-through would sit
            if run.narrative_queries >= run.limits.max_narrative_queries:
                break
            run.narrative_queries += 1
            query = f"{nodes[node_id]['display_name']} hedging pass-through offset"
            for passage in run.narrative(query, run.ctx.company_id)[:3]:
                text = passage.get("text") or ""
                if passage.get("chunk_id") and CONTRADICTION_CUES.search(text):
                    narrative_hits += 1
                    findings.append(_finding("CONTRADICTS", "NARRATIVE", f"Filing text mentions a mitigating factor for {nodes[node_id]['display_name']}",
                                             "QDRANT_CHUNK", str(passage["chunk_id"]), edge_for_cause[node_id], text[:200]))
    supports = [f for f in findings if f["stance"] == "SUPPORTS"]
    contradicts = [f for f in findings if f["stance"] == "CONTRADICTS"]
    run.contradictions_found += len(contradicts)
    status = classify(len(supports), len(contradicts), path.score, run.limits.min_path_score, len(findings))
    return {
        "status": status, "supporting": supports, "contradicting": contradicts, "unmeasured_nodes": unmeasured,
        "expected_direction": {n: d for n, d in expected.items() if d is not None},
    }


def classify(supports: int, contradicts: int, score: float, min_score: float, findings: int) -> str:
    if contradicts and contradicts >= supports:
        return CONTRADICTED
    if supports and contradicts:
        return WEAK  # mixed: more support than contradiction, but not a clean story
    if supports >= SUPPORTED_MIN_FINDINGS:
        return SUPPORTED
    if supports:
        return PLAUSIBLE
    return WEAK if score < min_score else UNRESOLVED  # nothing testable either way


# --- assembly ---------------------------------------------------------------------------

def _edge_view(edge: dict, inv: str, path_id: str, pos: int) -> dict:
    lag = None if edge.get("lag_min") is None else {"min": edge["lag_min"], "max": edge["lag_max"], "unit": edge["lag_unit"]}
    return {
        "edge_id": edge["edge_id"], "edge_ref": f"{path_id}:e{pos}", "cause_id": edge["cause_id"],
        "effect_id": edge["effect_id"], "type": edge["type"], "direction": edge["direction"], "mechanism": edge["mechanism"],
        "confidence": edge["confidence"], "effect_strength": edge["effect_strength"], "lag": lag, "scope": edge["scope"],
        "status": edge["status"], "version": edge["version"], "contextual_relevance": edge["relevance"],
        "materiality": edge["materiality"], "runtime_score": edge["score"], "score_components": edge["components"],
    }


def _why_selected(h: dict) -> str:
    best = max(h["edges"], key=lambda e: e["runtime_score"])
    name = dict(zip(h["node_ids"], h["labels"]))
    return (f"Ranked {h['rank']} of {h['candidates_total']} candidates (path score {h['path_score']}); "
            f"{len(h['supporting'])} supporting vs {len(h['contradicting'])} contradicting finding(s); "
            f"strongest link {name[best['cause_id']]} -> {name[best['effect_id']]} (confidence {best['confidence']}, "
            f"strength {best['effect_strength']}, relevance {best['contextual_relevance']}).")


def _reject_reason(h: dict) -> str:
    if h["status"] == CONTRADICTED:
        return "contradicted: " + "; ".join(f["label"] for f in h["contradicting"][:2])
    if h["status"] == WEAK:
        return "weak: " + ("mixed evidence" if h["supporting"] and h["contradicting"] else f"path score {h['path_score']} below threshold")
    return "no testable evidence for this path in the available data"


def _attach(service, h: dict, inv: str, trace_out: dict) -> None:
    """References only: evidence found for an edge in this run, attached once.
    Never touches confidence, version or status."""
    seen: dict[str, set] = {}
    for f in h["supporting"] + h["contradicting"]:
        eid = f["edge_id"]
        if eid not in seen:
            seen[eid] = {(r["ref_type"], r["locator"], r["stance"]) for r in service.get_relationship(eid)["evidence_refs"]}
        stance = "SUPPORTS" if f["stance"] == "SUPPORTS" else "CONTRADICTS"
        key = (f["ref_type"], f["locator"], stance)
        if key in seen[eid]:
            continue
        try:
            service.attach_evidence(eid, f["ref_type"], f["locator"], stance, note=f["label"], actor_kind="system", source=inv)
            seen[eid].add(key)
            trace_out["evidence_attached"] += 1
        except Exception:  # noqa: BLE001 -- recording a reference must never fail an investigation
            logger.warning("could not attach evidence to %s", eid, exc_info=True)


def build_dynamic_chain(service, question: str, *, company_id: str | None = None, geography: str | None = None,
                        period=None, target_node_id: str | None = None, conn=None, fact_store=None,
                        statement_type: str = "consolidated", observers: list[Observer] | None = None,
                        narrative: NarrativeSearch | None = None, limits: ChainLimits | None = None,
                        investigation_id: str | None = None, attach_references: bool = True) -> dict:
    started = time.monotonic()
    limits = limits or ChainLimits.from_settings()
    inv = investigation_id or f"dc{uuid.uuid4().hex[:10]}"
    ctx_target = identify_target(service, question, target_node_id)
    ctx = build_context(service, company_id, geography, period, question)
    result: dict = {
        "investigation_id": inv, "question": question, "context": {
            "company_id": ctx.company_id, "sectors": list(ctx.sector_names), "geography": ctx.geography,
            "period": list(ctx.period) if ctx.period else None},
        "limits": limits.__dict__.copy(), "target": None, "hypotheses": [], "retained_paths": [], "rejected_paths": [],
        "alternative_explanations": [], "graph": {"nodes": [], "edges": []}, "warnings": [],
    }
    if ctx_target is None:
        result["warnings"].append("no target metric/driver in the graph matches the question")
        result["trace"] = {"starting_target": None, "runtime_ms": round((time.monotonic() - started) * 1000, 1)}
        return result
    result["target"] = {"node_id": ctx_target["id"], "display_name": ctx_target["display_name"], "family": ctx_target["family"]}

    if observers is None and conn is not None:
        observers = default_observers(conn, company_id, statement_type, fact_store)
    observer = CompositeObserver(observers) if observers else None
    run = _Run(observer, narrative, limits, ctx)
    trace = Trace()
    measurable = (lambda c, e: bool(observer and c and e and observer.can_observe(c) and observer.can_observe(e)))
    nodes, candidates = generate_candidate_paths(service, ctx_target["id"], ctx, limits, trace, measurable)

    target_obs = run.observe(ctx_target)
    target_dir = stated_direction(question)
    premise = {"stated_direction": target_dir, "observed_direction": target_obs.direction if target_obs else None}
    if target_dir is None and target_obs and target_obs.direction:
        target_dir = target_obs.direction
        premise["note"] = "no direction in the question; used the observed movement of the target"
    if target_dir is None:
        result["warnings"].append("the question states no direction and the target is not measurable; paths cannot be tested")
    elif target_obs and target_obs.direction != 0 and premise["stated_direction"] and target_obs.direction != target_dir:
        result["warnings"].append(f"premise not supported by data: the question implies {ctx_target['display_name']} moved "
                                  f"{_arrow(target_dir)}, the data shows {_arrow(target_obs.direction)}")
    result["premise"] = premise

    ranked = [(i + 1, p) for i, p in enumerate(candidates)]
    tested: list[dict] = []
    pending = list(ranked)
    iterations = 0
    for iterations in range(1, limits.max_iterations + 1):
        batch, pending = pending[: limits.paths_per_iteration], pending[limits.paths_per_iteration:]
        if not batch:
            iterations -= 1
            break
        for rank, path in batch:
            path_id, hyp_id = f"{inv}:p{rank}", f"{inv}:h{rank}"
            outcome = evaluate_path(path, nodes, run, target_dir)
            h = {
                "hypothesis_id": hyp_id, "path_id": path_id, "rank": rank, "candidates_total": len(candidates),
                "node_ids": path.node_ids, "labels": [nodes[n]["display_name"] for n in path.node_ids],
                "edges": [_edge_view(e, inv, path_id, i) for i, e in enumerate(path.edges)], "path_score": path.score,
                "contextual_relevance": round(sum(e["relevance"] for e in path.edges) / len(path.edges), 4),
                "cross_sector_nodes": path.cross_sector_nodes,
                "cumulative_lag_months": None if path.cumulative_lag_months is None else list(path.cumulative_lag_months),
                "status": outcome["status"], "supporting": outcome["supporting"], "contradicting": outcome["contradicting"],
                "unmeasured_nodes": outcome["unmeasured_nodes"], "expected_direction": outcome["expected_direction"],
            }
            tested.append(h)
        if any(h["status"] == SUPPORTED for h in tested) and len(tested) >= 2:
            break  # a supported explanation, and at least one alternative weighed against it

    retained = sorted((h for h in tested if h["status"] in (SUPPORTED, PLAUSIBLE)),
                      key=lambda h: (_STATUS_RANK[h["status"]], -(len(h["supporting"]) - len(h["contradicting"])), -h["path_score"], h["path_id"]))[: limits.max_retained_paths]
    if not retained:  # nothing testable held up: keep the best unresolved leads, flagged as such
        retained = sorted((h for h in tested if h["status"] == UNRESOLVED), key=lambda h: (-h["path_score"], h["path_id"]))[:2]
    kept_ids = {h["path_id"] for h in retained}
    for h in tested:
        h["retained"] = h["path_id"] in kept_ids
        if h["retained"]:
            h["why_selected"] = _why_selected(h)
        else:
            h["rejection_reason"] = _reject_reason(h)
    for h in retained:
        h["alternatives"] = [{"path_id": o["path_id"], "status": o["status"], "retained": o["retained"],
                              "reason": o.get("rejection_reason") or "also retained as a competing explanation"}
                             for o in tested if o["path_id"] != h["path_id"]]

    for rank, path in pending:  # candidates the iteration budget never reached
        result["rejected_paths"].append({"path_id": f"{inv}:p{rank}", "node_ids": path.node_ids, "path_score": path.score,
                                         "reason": "not tested: iteration budget reached"})
    for item in trace.rejected_paths:
        result["rejected_paths"].append({"node_ids": item["path"], "reason": item["reason"]})
    for h in tested:
        if not h["retained"]:
            result["rejected_paths"].append({"path_id": h["path_id"], "hypothesis_id": h["hypothesis_id"], "node_ids": h["node_ids"],
                                             "path_score": h["path_score"], "status": h["status"], "reason": h["rejection_reason"]})
    result["alternative_explanations"] = [h for h in tested if not h["retained"] and h["status"] in (CONTRADICTED, WEAK)]
    result["hypotheses"] = tested
    result["retained_paths"] = [h["path_id"] for h in retained]

    graph_nodes: dict[str, dict] = {}
    graph_edges: dict[str, dict] = {}
    for h in retained:
        for n in h["node_ids"]:
            node = graph_nodes.setdefault(n, {"node_id": n, "family": nodes[n]["family"], "display_name": nodes[n]["display_name"], "path_ids": []})
            node["path_ids"].append(h["path_id"])
        for e in h["edges"]:
            edge = graph_edges.setdefault(e["edge_id"], {**e, "path_ids": []})
            edge["path_ids"].append(h["path_id"])
    result["graph"] = {"nodes": list(graph_nodes.values()), "edges": list(graph_edges.values())}

    attached = {"evidence_attached": 0}
    if attach_references:
        for h in tested:
            _attach(service, h, inv, attached)

    result["trace"] = {
        "starting_target": ctx_target["id"], "nodes_expanded": trace.nodes_expanded, "edges_considered": trace.edges_considered,
        "edges_rejected": trace.rejected_edges, "candidate_paths": len(candidates),
        "cross_sector_hops_max": trace.cross_sector_hops_seen, "paths_tested": len(tested),
        "evidence_retrieved": run.evidence_retrieved, "contradictions_found": run.contradictions_found,
        "iterations": iterations, "final_paths": result["retained_paths"], "evidence_attached": attached["evidence_attached"],
        "tool_calls": {"graph_service": trace.service_calls, "observers": run.observer_calls, "narrative_search": run.narrative_queries},
        "model_calls": 0, "estimated_cost_usd": 0.0,
        "runtime_ms": round((time.monotonic() - started) * 1000, 1),
    }
    logger.info("dynamic_chain %s target=%s candidates=%d tested=%d retained=%d iterations=%d", inv,
                ctx_target["id"], len(candidates), len(tested), len(retained), iterations)
    return result


def render_explanation(result: dict) -> str:
    """Deterministic plain-text explanation of the final chain."""
    if not result["target"]:
        return "No explanation: " + "; ".join(result["warnings"])
    lines = [f"Why: {result['question']}", f"Target: {result['target']['display_name']}"]
    lines += [f"Warning: {w}" for w in result["warnings"]]
    by_id = {h["path_id"]: h for h in result["hypotheses"]}
    for pid in result["retained_paths"]:
        h = by_id[pid]
        lines.append(f"\n[{h['status']}] " + " -> ".join(h["labels"]))
        lines.append(f"  selected: {h['why_selected']}")
        lines += [f"  supports: {f['label']}" for f in h["supporting"]] or ["  supports: none found"]
        lines += [f"  contradicts: {f['label']}" for f in h["contradicting"]] or ["  contradicts: none found"]
        lines += [f"  vs {a['path_id']} [{a['status']}]: {a['reason']}" for a in h["alternatives"]]
    if not result["retained_paths"]:
        lines.append("\nNo causal path held up against the available data.")
    return "\n".join(lines)
