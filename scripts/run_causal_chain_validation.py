"""Controlled real-data validation of the L5 dynamic causal-chain stage.

Runs a small fixed set of questions through research.causal_chain_stage (the
exact function the L5 pipeline calls) against the REAL Neon and Aura in the
environment, with evidence attachment off, and writes every run's full result,
baseline metrics and the narrative passages the contradiction search saw. It
never writes to the graph and never calls a model. Failures are recorded, not
repaired: nothing here adds an edge to make a case pass.

  python -m scripts.run_causal_chain_validation --out /tmp/chain_validation.json
"""

from __future__ import annotations

import argparse
import json
import time

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from research import causal_chain_stage  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402
from storage.fact_store import default_fact_store  # noqa: E402

#: id, sector theme, company, question, expected target node, high-level expected explanation
CASES = [
    ("auto_margin", "Auto", "MARUTI", "Why did Maruti's operating margin decline between FY2023 and FY2026?",
     "financial_metric:operating_margin", "input (material) cost and mix/pricing pressure; possibly offset by volume"),
    ("auto_volume", "Auto", "MARUTI", "Why did Maruti's vehicle volume change between FY2023 and FY2026?",
     "business_driver:vehicle_volume", "financing cost and rates -> demand -> volume"),
    ("steel_margin", "Metals", "TATASTEEL", "Why did Tata Steel's operating margin decline between FY2023 and FY2026?",
     "financial_metric:operating_margin", "iron ore / coking coal cost, steel price spreads, demand"),
    ("bank_nim", "Banking", "HDFCBANK", "Why did HDFC Bank's net interest margin change between FY2023 and FY2026?",
     None, "policy rate -> deposit/lending rates -> funding cost -> NIM"),
    ("bank_revenue_hdfc", "Banking", "HDFCBANK", "Why did HDFC Bank's revenue grow between FY2023 and FY2026?",
     "financial_metric:revenue", "credit demand -> loan growth -> revenue; merger effect"),
    ("bank_revenue_idfc", "Banking", "IDFCFIRSTB", "Why did IDFC First Bank's revenue grow between FY2023 and FY2026?",
     "financial_metric:revenue", "retail loan growth; lending rates"),
    ("macro_banking", "Macro->Sector", None, "How did RBI repo rate changes affect banking sector revenue between FY2023 and FY2026?",
     "financial_metric:revenue", "repo rate -> lending rate -> credit demand -> loan growth -> revenue"),
    ("us_bank", "US", "JPM", "Why did JPMorgan's revenue grow between FY2022 and FY2025?",
     "financial_metric:revenue", "fed funds -> lending rate -> credit/loan growth -> revenue"),
]


class RecordingNarrative:
    """Wraps the stage's narrative search to keep what the cue-word baseline saw."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def __call__(self, query, company_id):
        passages = self.inner(query, company_id)
        self.calls.append({"query": query, "company_id": company_id,
                           "passages": [{"chunk_id": p["chunk_id"], "text": p["text"][:400]} for p in passages]})
        return passages


def metrics(result: dict) -> dict:
    t = result.get("trace") or {}
    hyps = result.get("hypotheses") or []
    retained = [h for h in hyps if h["retained"]]
    graph_edges = (result.get("graph") or {}).get("edges", [])
    useful_edge_ids = {e["edge_id"] for h in retained if h["status"] in ("SUPPORTED", "PLAUSIBLE") for e in h["edges"]}
    with_finding = {f["edge_id"] for h in retained for f in h["supporting"] + h["contradicting"]}
    with_support = {f["edge_id"] for h in retained for f in h["supporting"]}
    explored = t.get("edges_considered", 0)
    cross = {e["edge_id"] for h in retained for e in h["edges"]} if False else set()
    nodes_family = {n["node_id"]: n["family"] for n in (result.get("graph") or {}).get("nodes", [])}
    for h in retained:
        if h["cross_sector_nodes"]:
            cross.update(e["edge_id"] for e in h["edges"])
    n = len(graph_edges)
    return {
        "nodes_explored": len(t.get("nodes_expanded", [])), "edges_explored": explored, "edges_retained": n,
        "evidence_coverage": round(len(with_finding & {e["edge_id"] for e in graph_edges}) / n, 3) if n else None,
        "unsupported_edge_rate": round(1 - len(with_support & {e["edge_id"] for e in graph_edges}) / n, 3) if n else None,
        "investigation_efficiency": round(len(useful_edge_ids) / explored, 3) if explored else None,
        "cross_sector_edges_retained": len(cross), "contradictions_found": t.get("contradictions_found"),
        "runtime_ms": t.get("runtime_ms"), "model_calls": t.get("model_calls"), "llm_cost_usd": t.get("estimated_cost_usd"),
        "candidate_paths": t.get("candidate_paths"), "paths_tested": t.get("paths_tested"), "paths_retained": len(retained),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    conn = open_db()
    fs = default_fact_store()
    out = []
    real_narrative = causal_chain_stage._narrative
    for case_id, theme, company, question, expected_target, expected in CASES:
        recorder: list[RecordingNarrative] = []

        def patched(conn_, fs_, as_of, _r=recorder):
            wrapped = RecordingNarrative(real_narrative(conn_, fs_, as_of))
            _r.append(wrapped)
            return wrapped

        causal_chain_stage._narrative = patched
        started = time.monotonic()
        result = causal_chain_stage.run_causal_chain_stage(
            conn, question, [company] if company else [], f"val-{case_id}", fact_store=fs)
        causal_chain_stage._narrative = real_narrative
        out.append({
            "case": case_id, "theme": theme, "company": company, "question": question, "expected_target": expected_target,
            "expected_explanation": expected, "wall_s": round(time.monotonic() - started, 2), "result": result,
            "metrics": metrics(result) if result.get("status") == "ok" else None,
            "narrative_calls": recorder[0].calls if recorder else [],
        })
        t = result.get("target")
        print(f"{case_id}: status={result['status']} target={t['node_id'] if t else None} "
              f"candidates={(result.get('trace') or {}).get('candidate_paths')} retained={len(result.get('retained_paths', []))}")
    json.dump(out, open(args.out, "w"), indent=1, default=str)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
