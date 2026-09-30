"""Aggregation for Admin > Settings > Execution Analytics -- turns raw
execution_metrics rows (storage.repositories.list_execution_metrics) into
the summary stats, per-Complexity-Level time-bucketed series, and scatter
points the panel renders. Same "aggregate in Python, cheap at this app's
scale, no per-backend SQL" approach the Audit Log panel's raw_objects
rollup already uses (web/app.py's _audit_panel_context) -- avoids relying
on Postgres-only percentile_cont, which SQLite has no equivalent for.

Purely a read/render helper -- never touches routing, planning, or model
selection (this feature's own boundary): it only shapes data llm/
execution_metrics.py already wrote for display.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config.settings import EXECUTION_METRICS_RETENTION_DAYS
from llm.execution_metrics import percentile
from storage.repositories import list_execution_metrics

#: The five levels llm/hardness.py's TIER_LEVEL vocabulary defines --
#: complexity_level=0 (context/reuse.py hits, llm/observability.py's
#: record_reuse) is real data but isn't a "complexity level" in the sense
#: this panel filters by, so it's counted in totals but never its own line-
#: chart series.
COMPLEXITY_LEVELS = (1, 2, 3, 4, 5)

PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
DEFAULT_PERIOD = "30d"

GRANULARITIES = ("daily", "weekly", "monthly")
DEFAULT_GRANULARITY = "daily"

#: Caps how many raw points the scatter view renders -- an SVG with tens of
#: thousands of points is both illegible and slow to paint; the most recent
#: N within the already-period-filtered window is what an operator hunting
#: for a recent slow outlier actually wants, not a downsampled average.
MAX_SCATTER_POINTS = 3000

TASK_LABELS = {
    "assistant_qa": "Ask AI",
    "signals_report": "Expanded investigation",
    "investigation": "Investigation (L5)",
    "signals_fast_path": "Fast path (Level 1/2, no LLM)",
}


def _format_ms(ms: float | None) -> str:
    """1.2s below 60s (this app's requests are seconds, not minutes, on the
    fast end), Xm Ys above -- never bare milliseconds, which nobody reading
    a latency dashboard actually wants to mentally divide by 1000."""
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {int(remainder)}s"


def _parse_created_at(value: str) -> datetime:
    # execution_metrics.created_at is written by utcnow_iso()/_utcnow_iso()
    # (storage/database.py / storage/repositories_pg.py) -- always ISO-8601
    # UTC, same convention as every other audit table in this app.
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bucket_start(dt: datetime, granularity: str) -> str:
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "weekly":
        day = day - timedelta(days=day.weekday())  # Monday
    elif granularity == "monthly":
        day = day.replace(day=1)
    return day.date().isoformat()


def build_execution_analytics_context(conn, *, period: str, granularity: str, level_filter: str) -> dict:
    """Everything the Execution Analytics panel needs -- filter state for
    the form controls, the summary stat row, and one JSON blob
    (`execution_analytics_data_json`) the panel embeds for web/static/js/
    execution_analytics.js to render both the line chart and the scatter
    toggle from, with no separate fetch needed (same "hand-rolled SVG from
    embedded data" approach web/static/js/charts_overlay.js already uses)."""
    period = period if period in PERIOD_DAYS else DEFAULT_PERIOD
    granularity = granularity if granularity in GRANULARITIES else DEFAULT_GRANULARITY
    level = int(level_filter) if level_filter in ("1", "2", "3", "4", "5") else None

    since_iso = (datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])).isoformat()
    rows = list_execution_metrics(conn, since_iso=since_iso, complexity_level=level, limit=20000)

    durations = sorted(r["total_ms"] for r in rows if r["total_ms"] is not None)
    success_count = sum(1 for r in rows if r["status"] in ("success", "reused"))
    avg_ms = (sum(durations) / len(durations)) if durations else None
    p50_ms = percentile(durations, 50)
    p95_ms = percentile(durations, 95)
    success_rate_pct = round(success_count / len(rows) * 100, 1) if rows else None
    summary = {
        "total_runs": len(rows),
        "avg_latency_display": _format_ms(avg_ms),
        "p50_latency_display": _format_ms(p50_ms),
        "p95_latency_display": _format_ms(p95_ms),
        "success_rate_display": f"{success_rate_pct}%" if success_rate_pct is not None else "—",
    }

    # Line chart: one series per Complexity Level present in the current
    # (period + level-filter) window -- when a single level is selected via
    # `level`, `rows` already only contains that level, so this naturally
    # renders one series; "All" renders every level that had any runs.
    buckets_by_level: dict[int, dict[str, list[float]]] = {}
    for row in rows:
        lvl = row["complexity_level"]
        if lvl not in COMPLEXITY_LEVELS or row["total_ms"] is None:
            continue
        bucket = _bucket_start(_parse_created_at(row["created_at"]), granularity)
        buckets_by_level.setdefault(lvl, {}).setdefault(bucket, []).append(row["total_ms"])

    line_series = [
        {
            "level": lvl,
            "points": [
                {"bucket": bucket, "avg_ms": round(sum(values) / len(values), 1), "count": len(values)}
                for bucket, values in sorted(buckets_by_level[lvl].items())
            ],
        }
        for lvl in COMPLEXITY_LEVELS if lvl in buckets_by_level
    ]

    scatter = [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "level": row["complexity_level"],
            "task": TASK_LABELS.get(row["task_name"], row["task_name"]),
            "model": row["model_used"],
            "duration_ms": row["total_ms"],
            "status": row["status"],
            "mode": row["execution_mode"],
        }
        for row in rows[-MAX_SCATTER_POINTS:] if row["total_ms"] is not None
    ]

    # Async vs. sync split, by Complexity Level -- directly serves the
    # feature's stated purpose ("determine which complexity levels actually
    # require asynchronous execution and state management"): a level that's
    # already running mostly async, or one that's slow but still all sync,
    # is exactly the signal this panel exists to surface.
    mode_by_level: dict[int, dict[str, int]] = {}
    for row in rows:
        lvl = row["complexity_level"]
        if lvl not in COMPLEXITY_LEVELS:
            continue
        bucket = mode_by_level.setdefault(lvl, {"sync": 0, "async": 0})
        bucket[row["execution_mode"]] = bucket.get(row["execution_mode"], 0) + 1
    execution_mode_by_level = [
        {"level": lvl, **mode_by_level[lvl]} for lvl in COMPLEXITY_LEVELS if lvl in mode_by_level
    ]

    return {
        # "exa_*", not "ea_*" -- disambiguated from the sibling Eval
        # Analytics panel (_eval_analytics_panel_context, web/app.py), which
        # independently landed the same "ea" abbreviation for its own,
        # unrelated Jinja context vars and query params.
        "exa_retention_days": EXECUTION_METRICS_RETENTION_DAYS,
        "exa_period": period,
        "exa_granularity": granularity,
        "exa_level": level_filter if level_filter in ("1", "2", "3", "4", "5") else "all",
        "exa_period_options": [
            {"value": "7d", "label": "Last 7 days"}, {"value": "30d", "label": "Last 30 days"},
            {"value": "90d", "label": "Last 90 days"}, {"value": "365d", "label": "Last year"},
        ],
        "exa_granularity_options": [
            {"value": "daily", "label": "Daily"}, {"value": "weekly", "label": "Weekly"},
            {"value": "monthly", "label": "Monthly"},
        ],
        "exa_summary": summary,
        "exa_execution_mode_by_level": execution_mode_by_level,
        "execution_analytics_data_json": json.dumps({
            "line_series": line_series,
            "scatter": scatter,
            "granularity": granularity,
        }),
    }
