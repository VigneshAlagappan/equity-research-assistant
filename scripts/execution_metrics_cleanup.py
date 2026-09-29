"""Execution Analytics retention (Admin > Settings > Execution Analytics) --
deletes execution_metrics detail rows older than
config.settings.EXECUTION_METRICS_RETENTION_DAYS (default 90), after first
rolling each expiring day up into one execution_metrics_daily row per
(day, task_name, complexity_level) -- so Complexity Level 1-5 trend history
survives past the detail window even though the per-run rows don't.

Runs only through the existing background scheduler/job mechanism
(scheduling/jobs.py's "Maintenance" category -- CLI, the Schedule panel's
"Run now" button, or the cron-triggered -async route), never in the user
request path, per this feature's own boundary: retention must never cost a
real Signal request any latency.

Usage: python -m scripts.execution_metrics_cleanup
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from config.settings import EXECUTION_METRICS_RETENTION_DAYS
from ingestion.batch_log import BatchRun
from llm.execution_metrics import percentile
from storage.backend_bootstrap import open_db
from storage.repositories import (
    delete_execution_metrics_before,
    list_execution_metrics,
    upsert_execution_metrics_daily,
)

JOB_NAME = "execution_metrics_cleanup"


def _day_key(created_at: str) -> str:
    """execution_metrics.created_at is an ISO-8601 UTC timestamp (utcnow_iso/
    _utcnow_iso, same convention as every other audit table in this app) --
    the leading 10 characters are always its date, regardless of whether the
    full string carries fractional seconds."""
    return created_at[:10]


def aggregate_execution_metrics_rows(rows: list[dict]) -> list[dict]:
    """Groups raw execution_metrics rows by (day, task_name, complexity_
    level) and reduces each group to one execution_metrics_daily row --
    factored out of run_execution_metrics_cleanup() so it's directly
    testable against a plain list of dicts, no DB connection needed."""
    groups: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    for row in rows:
        level = row["complexity_level"] if row["complexity_level"] is not None else 0
        groups[(_day_key(row["created_at"]), row["task_name"], level)].append(row)

    daily_rows = []
    for (day, task_name, level), group in groups.items():
        durations = sorted(r["total_ms"] for r in group if r["total_ms"] is not None)
        daily_rows.append({
            "day": day,
            "task_name": task_name,
            "complexity_level": level,
            "total_runs": len(group),
            "success_runs": sum(1 for r in group if r["status"] in ("success", "reused")),
            "error_runs": sum(1 for r in group if r["status"] == "error"),
            "avg_total_ms": (sum(durations) / len(durations)) if durations else None,
            "p50_total_ms": percentile(durations, 50),
            "p95_total_ms": percentile(durations, 95),
            "max_total_ms": durations[-1] if durations else None,
            "total_input_tokens": sum(r["input_tokens"] or 0 for r in group),
            "total_output_tokens": sum(r["output_tokens"] or 0 for r in group),
            "total_estimated_cost_usd": sum(r["estimated_cost_usd"] or 0 for r in group),
        })
    return daily_rows


def run_execution_metrics_cleanup(conn=None, *, retention_days: int | None = None) -> int:
    """Rolls up and deletes every execution_metrics row older than
    retention_days (default: config.settings.EXECUTION_METRICS_RETENTION_
    DAYS). Wrapped in a BatchRun, same "one capability, two triggers" shape
    every other scheduling/jobs.py job uses, so a run shows up in
    Audit Log > Job Runs like any other scheduled job. Returns the
    BatchRun's run_id."""
    retention_days = retention_days if retention_days is not None else EXECUTION_METRICS_RETENTION_DAYS
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

    owns_conn = conn is None
    if conn is None:
        conn = open_db()
    try:
        with BatchRun(conn, JOB_NAME, scope_label=f"retention={retention_days}d, cutoff={cutoff_iso}") as run:
            with run.item(None) as item:
                # Capped at 200k rows per run -- generous relative to this
                # feature's own volume (one row per Signal request, not per
                # LLM call), and a run that hits the cap simply rolls up/
                # deletes what it can this time; the next scheduled run
                # picks up whatever's left, same "resume next time" shape
                # scheduling/jobs.py's price-history backfills already use
                # for their own time-budget caps.
                expiring_rows = list_execution_metrics(conn, until_iso=cutoff_iso, limit=200_000)
                daily_rows = aggregate_execution_metrics_rows(expiring_rows)
                if daily_rows:
                    upsert_execution_metrics_daily(conn, daily_rows)
                deleted = delete_execution_metrics_before(conn, cutoff_iso)
                item.detail = (
                    f"rolled up {len(daily_rows)} day/task/level bucket(s) from {len(expiring_rows)} row(s), "
                    f"deleted {deleted} row(s) older than {cutoff_iso}"
                )
        print(f"Execution metrics cleanup done. {item.detail}", flush=True)
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    run_execution_metrics_cleanup()


if __name__ == "__main__":
    main()
