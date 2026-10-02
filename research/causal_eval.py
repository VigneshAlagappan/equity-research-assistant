"""Golden Investigation evaluation for L5 causal investigations
(docs/L5_MVP_TASK_PLAN.md, M9).

A golden case states a question, the companies, and the causal edges an
expert would expect a good explanation to contain -- each as source/target
concept ALIASES, not phrases. An investigation edge matches an expected edge
when its source label contains one of the source aliases AND its target label
contains one of the target aliases (case-insensitive, word-boundary). Matching
is deterministic only in the MVP (no LLM judge); presented edges that match
nothing are listed for human review rather than silently counted wrong.

Scores (per case, then aggregated):
  golden recall       matched ESSENTIAL expected edges / ESSENTIAL expected edges
                      (against edges the investigation PRESENTED)
  golden precision    presented edges matching ANY expected edge / presented
                      edges. A LOWER BOUND: a golden set is never exhaustive, so
                      a correct edge nobody wrote down counts against it.
Evidence coverage and unsupported-edge rate come from the investigation's own
metrics row (research/investigation_metrics.py).

Case files live in research/golden/<benchmark_version>/*.json. Changing an
existing case's expectations means a new benchmark version, never an edit in
place. A case with no `reviewed_by` is a DRAFT and is reported as such.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
DEFAULT_BENCHMARK_VERSION = "v1"


@dataclass
class GoldenCase:
    case_id: str
    benchmark_version: str
    question: str
    company_ids: list[str]
    as_of: str | None
    expected_edges: list[dict]
    known_weak_explanations: list[str] = field(default_factory=list)
    reviewed_by: str | None = None
    reviewed_on: str | None = None

    @property
    def reviewed(self) -> bool:
        return bool(self.reviewed_by and self.reviewed_on)


def load_cases(benchmark_version: str = DEFAULT_BENCHMARK_VERSION, directory: Path | None = None) -> list[GoldenCase]:
    base = (directory or GOLDEN_DIR) / benchmark_version
    cases = []
    for path in sorted(base.glob("*.json")):
        raw = json.loads(path.read_text())
        cases.append(GoldenCase(
            case_id=raw["case_id"], benchmark_version=raw["benchmark_version"], question=raw["question"],
            company_ids=list(raw["company_ids"]), as_of=raw.get("as_of"), expected_edges=list(raw["expected_edges"]),
            known_weak_explanations=list(raw.get("known_weak_explanations") or []),
            reviewed_by=raw.get("reviewed_by"), reviewed_on=raw.get("reviewed_on"),
        ))
    return cases


def _contains_alias(label: str, aliases: list[str]) -> bool:
    text = (label or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(a.lower())}(?![a-z0-9])", text) for a in aliases)


def edge_matches(source_label: str, target_label: str, expected: dict) -> bool:
    return _contains_alias(source_label, expected["source_aliases"]) and _contains_alias(target_label, expected["target_aliases"])


def score_case(case: GoldenCase, edges: list[dict]) -> dict:
    """`edges`: dicts with source_label, target_label, presented (bool), edge_key.
    Returns per-case counts and the unmatched presented edges."""
    presented = [e for e in edges if e["presented"]]
    essential = [x for x in case.expected_edges if x.get("importance", "essential") == "essential"]

    def found(expected: dict, pool: list[dict]) -> bool:
        return any(edge_matches(e["source_label"], e["target_label"], expected) for e in pool)

    matched_essential = [x["edge_id"] for x in essential if found(x, presented)]
    matched_presented = [
        e for e in presented if any(edge_matches(e["source_label"], e["target_label"], x) for x in case.expected_edges)
    ]
    unmatched = [e["edge_key"] for e in presented if e not in matched_presented]
    return {
        "expected_essential": len(essential),
        "matched_essential": len(matched_essential),
        "matched_essential_ids": matched_essential,
        "missed_essential_ids": [x["edge_id"] for x in essential if x["edge_id"] not in matched_essential],
        "matched_essential_explored": sum(1 for x in essential if found(x, edges)),
        "presented_edges": len(presented),
        "matched_presented": len(matched_presented),
        "unmatched_presented": unmatched,
    }


def aggregate(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    expected = sum(r["expected_essential"] for r in ok)
    presented = sum(r["presented_edges"] for r in ok)
    covs = [r["evidence_coverage"] for r in ok if r.get("evidence_coverage") is not None]
    unsup = [r["unsupported_edge_rate"] for r in ok if r.get("unsupported_edge_rate") is not None]
    return {
        "cases_total": len(results),
        "cases_completed": len(ok),
        "golden_recall": (sum(r["matched_essential"] for r in ok) / expected) if expected else None,
        "golden_precision_lower_bound": (sum(r["matched_presented"] for r in ok) / presented) if presented else None,
        "evidence_coverage": (sum(covs) / len(covs)) if covs else None,
        "unsupported_edge_rate": (sum(unsup) / len(unsup)) if unsup else None,
        "estimated_cost_usd": sum(r.get("estimated_cost_usd") or 0 for r in ok),
        "runtime_ms": sum(r.get("runtime_ms") or 0 for r in ok),
    }


def format_report(run_id: str, benchmark_version: str, results: list[dict], agg: dict, cases: dict[str, GoldenCase]) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.0f}%"

    lines = [f"Causal golden eval {run_id} (benchmark {benchmark_version})", ""]
    for r in results:
        case = cases.get(r["case_id"])
        draft = "" if case is not None and case.reviewed else "  [DRAFT case: not reviewed]"
        if r["status"] != "ok":
            lines.append(f"- {r['case_id']}: FAILED ({r.get('error_detail')}){draft}")
            continue
        lines.append(
            f"- {r['case_id']}: recall {r['matched_essential']}/{r['expected_essential']} essential"
            f" (explored: {r['matched_essential_explored']}), presented {r['matched_presented']}/{r['presented_edges']} matched,"
            f" coverage {pct(r.get('evidence_coverage'))}, unsupported {pct(r.get('unsupported_edge_rate'))}{draft}"
        )
        if r["missed_essential_ids"]:
            lines.append(f"    missed essential: {', '.join(r['missed_essential_ids'])}")
        if r["unmatched_presented"]:
            lines.append(f"    presented but unmatched (review): {'; '.join(r['unmatched_presented'][:8])}")
    lines += [
        "", f"Recall {pct(agg['golden_recall'])} | precision (lower bound) {pct(agg['golden_precision_lower_bound'])}"
        f" | coverage {pct(agg['evidence_coverage'])} | unsupported {pct(agg['unsupported_edge_rate'])}",
        f"Cases {agg['cases_completed']}/{agg['cases_total']} | cost ${agg['estimated_cost_usd']:.2f}"
        f" | runtime {agg['runtime_ms'] / 1000:.0f}s",
    ]
    return "\n".join(lines)
