"""Signals Eval Runner — periodically re-runs the golden eval set
(research/signals_eval_cases.py) through the live Jev classifier and
routing policy (research/routing_policy.py::route_question(), ADR-023),
and records whether each question's classified complexity level matched
what it's expected to be.

This is the "future eval runner" ADR-023's audit design was built for:
`signals_routing_log` already has, per question, everything needed to ask
"did Signals follow the expected behavior for its complexity level" —
this script is what actually asks that question, on a schedule, instead
of only being answerable by hand-querying the table.

A classification MISMATCH is not a crash — it's the actual signal this
job exists to surface, and is recorded as a normal (if unwelcome)
`batch_job_items` failure via ingestion/batch_log.py's established
per-item pass/fail convention (same mechanism every other batch job in
this app already uses for its own audit trail — see that module's
docstring), so "how has Jev's accuracy trended" is answerable from
Audit Log -> Job Runs the same way "how has the NSE fetch's success rate
trended" already is, no bespoke reporting surface needed.

Real LLM cost, not free to run on every commit: 11 cases, 3 of which
(Level 5) run the full hypothesis-driven investigation pipeline — several
real LLM calls and minutes of wall-clock time each. See
research/signals_eval_cases.py's own docstring before widening this set,
and confirm the configured cadence (scheduling/jobs.py) is the level of
recurring spend actually wanted before enabling it to run unattended —
same judgment call scripts/batch_generate_insights.py's docstring makes
for its own monthly LLM job.

Usage: python -m scripts.run_signals_eval
(a plain `python scripts/run_signals_eval.py` fails on the `research`/
`storage` imports below — run as a module so the repo root, not scripts/,
lands on sys.path, same as every other script here.)
"""

from __future__ import annotations

import time

from config.settings import ANTHROPIC_API_KEY_SET
from ingestion.batch_log import BatchRun
from research.routing_policy import route_question
from research.signals_eval_cases import EVAL_CASES
from storage.database import init_db

JOB_NAME = "signals_eval"


class EvalMismatchError(Exception):
    """Raised when Jev's classified level doesn't match an eval case's
    expected level — caught by BatchRun.item() the same way any other
    per-item failure is, recording the mismatch as this item's
    batch_job_items detail rather than aborting the run."""


def run_signals_eval(conn=None) -> int:
    """The actual eval loop, factored out of main() so the Settings > Data
    Operations > Schedule panel's "Run now" button (web/app.py) can trigger
    the identical run on demand — same one-capability-two-triggers shape
    every other job in this directory's sibling scripts uses.

    Returns the BatchRun's run_id. Raises RuntimeError up front (before
    opening a BatchRun or touching any eval case) if no Anthropic API key
    is configured — same precondition check scripts/batch_generate_insights.py
    makes for the same reason: better to fail the whole run loudly up front
    than to record 11 confusing "temporarily unavailable" mismatches."""
    if not ANTHROPIC_API_KEY_SET:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server — the assistant can't run.")

    owns_conn = conn is None
    if conn is None:
        conn = init_db()

    try:
        total = len(EVAL_CASES)
        matched = 0
        by_level: dict[int, list[bool]] = {}
        print(f"{total} eval cases (research/signals_eval_cases.py)", flush=True)

        with BatchRun(conn, JOB_NAME, scope_label=f"golden eval set ({total} cases)") as run:
            for i, case in enumerate(EVAL_CASES, 1):
                with run.item(case.name) as item:
                    start = time.monotonic()
                    result = route_question(conn, case.question, case.company_ids)
                    elapsed_ms = (time.monotonic() - start) * 1000

                    actual_level = int(result.classification.level)
                    expected_level = int(case.expected_level)
                    is_match = actual_level == expected_level
                    by_level.setdefault(expected_level, []).append(is_match)

                    detail = (
                        f"expected=L{expected_level} actual=L{actual_level} "
                        f"confidence={result.classification.confidence:.2f} "
                        f"status={result.audit.execution_status} latency_ms={elapsed_ms:.0f}"
                    )
                    print(
                        f"[{i}/{total}] {case.name:40s} {'MATCH' if is_match else 'MISMATCH':8s} {detail}",
                        flush=True,
                    )

                    if not is_match:
                        raise EvalMismatchError(f"{detail} reason={result.classification.reason!r}")

                    matched += 1
                    item.detail = detail

        accuracy_pct = (matched / total * 100) if total else 0.0
        print(f"\nDone. {matched}/{total} matched ({accuracy_pct:.0f}%)", flush=True)
        for level in sorted(by_level):
            outcomes = by_level[level]
            print(f"  Level {level}: {sum(outcomes)}/{len(outcomes)} matched", flush=True)
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_signals_eval()


if __name__ == "__main__":
    main()
