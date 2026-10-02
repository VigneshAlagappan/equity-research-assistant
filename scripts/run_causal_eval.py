"""Golden causal evaluation runner (docs/L5_MVP_TASK_PLAN.md, M9).

Runs each golden case (research/golden/<version>/*.json) through the real
L5 investigation pipeline, scores the persisted investigation graph against the
case's expected edges (research/causal_eval.py), stores one row per run and per
case, and writes a run artifact to the object store.

REAL LLM SPEND: every case is a full Level-5 investigation (several model
calls, minutes of wall-clock). Not scheduled -- run on demand, and expect the
first run's cost to be reported before anyone decides on a cadence.

Usage (as a module, from the repo root):
  python -m scripts.run_causal_eval --list
  python -m scripts.run_causal_eval --cases l5_maruti_margin_movement
  python -m scripts.run_causal_eval                      # all cases in v1
  python -m scripts.run_causal_eval --score-existing INV_ID:CASE_ID   # no LLM: score an investigation already on file
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from config.versions import version_stamp  # noqa: E402
from research.causal_eval import (  # noqa: E402
    DEFAULT_BENCHMARK_VERSION, GoldenCase, aggregate, format_report, load_cases, score_case,
)
from storage.backend_bootstrap import open_db  # noqa: E402
from storage.causal_repository import (  # noqa: E402
    get_investigation_metrics, insert_eval_case_result, insert_eval_run, list_graph_edges, list_graph_nodes,
)
from storage.document_store import default_document_store  # noqa: E402


def score_investigation(conn, case: GoldenCase, investigation_id: str) -> dict:
    """Score one persisted investigation against one case. No LLM."""
    labels = {n["node_id"]: n["label"] for n in list_graph_nodes(conn, investigation_id)}
    edges = [
        {"source_label": labels.get(e["source_node_id"], ""), "target_label": labels.get(e["target_node_id"], ""),
         "presented": bool(e["presented"]), "edge_key": e["edge_key"]}
        for e in list_graph_edges(conn, investigation_id)
    ]
    result = score_case(case, edges)
    metrics = get_investigation_metrics(conn, investigation_id)
    result.update({
        "case_id": case.case_id, "investigation_id": investigation_id, "status": "ok",
        "evidence_coverage": metrics["evidence_coverage"] if metrics else None,
        "unsupported_edge_rate": metrics["unsupported_edge_rate"] if metrics else None,
        "estimated_cost_usd": metrics["estimated_cost_usd"] if metrics else None,
        "runtime_ms": metrics["runtime_ms"] if metrics else None,
    })
    return result


def run_causal_eval(conn, case_ids: list[str] | None = None, benchmark_version: str = DEFAULT_BENCHMARK_VERSION,
                    investigate=None) -> str:
    """`investigate(conn, question, company_ids, as_of=...) -> Investigation`
    defaults to the real pipeline; tests pass a stub."""
    if investigate is None:
        from research.investigation import run_investigation as investigate

    cases = [c for c in load_cases(benchmark_version) if not case_ids or c.case_id in case_ids]
    if not cases:
        raise SystemExit(f"no golden cases matched in benchmark {benchmark_version}")
    eval_run_id = f"ceval-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    results: list[dict] = []
    for case in cases:
        print(f"[{len(results) + 1}/{len(cases)}] {case.case_id} ...", flush=True)
        try:
            investigation = investigate(conn, case.question, case.company_ids, as_of=case.as_of)
            results.append(score_investigation(conn, case, investigation.investigation_id))
        except Exception as exc:  # noqa: BLE001 -- a failed case is a result, not a crash
            results.append({"case_id": case.case_id, "status": "failed", "error_detail": f"{type(exc).__name__}: {exc}"[:500]})
    agg = aggregate(results)
    stamp = version_stamp()

    report = format_report(eval_run_id, benchmark_version, results, agg, {c.case_id: c for c in cases})
    now = datetime.now(timezone.utc)
    s3_key = f"causal-evals/runs/{now:%Y}/{now:%m}/{eval_run_id}.json"
    try:
        default_document_store().store(s3_key, json.dumps(
            {"eval_run_id": eval_run_id, "benchmark_version": benchmark_version, "versions": stamp,
             "aggregate": agg, "results": results, "report": report}, indent=2, default=str).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not store run artifact: {exc}", flush=True)
        s3_key = None

    insert_eval_run(conn, eval_run_id, {**agg, **stamp, "benchmark_version": benchmark_version, "s3_key": s3_key,
                                        "runtime_ms": (time.monotonic() - started) * 1000})
    for r in results:
        insert_eval_case_result(conn, eval_run_id, r)
    print("\n" + report, flush=True)
    return eval_run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK_VERSION)
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--list", action="store_true", help="list cases and exit (no LLM, no database)")
    parser.add_argument("--score-existing", metavar="INVESTIGATION_ID:CASE_ID",
                        help="score an investigation already on file against a case (no LLM)")
    args = parser.parse_args()

    if args.list:
        for c in load_cases(args.benchmark):
            print(f"{c.case_id:45s} {','.join(c.company_ids):22s} edges={len(c.expected_edges)} "
                  f"{'reviewed' if c.reviewed else 'DRAFT'}")
        return
    conn = open_db()
    if args.score_existing:
        inv_id, case_id = args.score_existing.split(":", 1)
        case = next(c for c in load_cases(args.benchmark) if c.case_id == case_id)
        result = score_investigation(conn, case, inv_id)
        print(json.dumps(result, indent=2, default=str))
        return
    run_causal_eval(conn, [c.strip() for c in args.cases.split(",")] if args.cases else None, args.benchmark)


if __name__ == "__main__":
    main()
