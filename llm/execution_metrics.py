"""Execution Analytics (Admin > Settings > Execution Analytics) --
per-request timing + outcome for the three Signal pipelines (Ask AI,
"Generate full report", Deep Dive investigation), so an operator can see
how each Complexity Level (llm/hardness.py) actually performs over time,
and whether a level is being run sync or async (research/case_runner.py's
research_cases-backed path).

Deliberately a cross-cutting, contextvar-based timer rather than a value
threaded through every function signature down the call stack -- gather_
evidence(), plan_and_gather(), and llm/router.py::route() already sit many
frames below the request handler that knows the run_id, and changing all of
their signatures just to carry a timer object would touch far more of the
codebase than this feature's own boundary should (task: "measure first,
don't change routing/planner/agent behavior"). A contextvar naturally
follows one logical run: the same thread executes it start-to-finish
whether that thread is a Flask request thread (the synchronous Ask AI/
Signals report/investigation routes) or a research/case_runner.py
background thread (the async Ask AI/investigation routes) -- run_case_in_
background's compute(conn) calls straight into answer_question()/
run_investigation() on ITS OWN thread, so start_run() inside those
functions sees a fresh, correctly-scoped contextvar either way. This would
NOT work if start_run() were called in the Flask request thread and the
actual pipeline ran on a different thread started without
contextvars.copy_context() -- it deliberately isn't; see research/
assistant.py::answer_question, research/signals_report.py::
generate_signals_report, and research/investigation.py::run_investigation
for the three start_run() call sites.

Every write to execution_metrics goes through finish_run()'s own try/except
-- this module must never raise into, or change the outcome of, the request/
case it's timing (the same "observability must not affect behavior" rule
llm/observability.py's own record() follows for llm_call_log).
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from storage.db_types import DBConnection
from storage.repositories import insert_execution_metrics

logger = logging.getLogger(__name__)

#: Exception class names (matched by name, not isinstance -- see module
#: docstring on avoiding a research.assistant/research.investigation import
#: here, which would be circular: those modules import this one) that mean
#: something other than a genuine technical failure.
_STATUS_BY_EXCEPTION_NAME = {
    "InsufficientEvidenceError": "insufficient_data",
    "CaseCancelledError": "cancelled",
}


@dataclass
class RunTimer:
    run_id: str
    task_name: str
    execution_mode: str
    complexity_level: int | None = None
    complexity_tier: str | None = None
    model_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    status: str = "success"
    error_detail: str | None = None
    _durations_ms: dict = field(default_factory=dict)
    _start: float = field(default_factory=time.perf_counter)
    # True once complexity_level holds Jev's real, per-request classification
    # (research/routing_policy.py's ComplexityLevel, 1-5, passed into
    # start_run()'s jev_complexity_level) rather than llm/hardness.py's
    # model-tier heuristic (Tier.QUICK/STANDARD/DEEP, only ever 2/3/5) --
    # attach_llm_result() below must never clobber an authoritative Jev
    # level with the unrelated hardness-tier number a route() call happens
    # to carry. Was previously always False, silently mislabeling every row
    # with the wrong classifier's number under the right-looking field name.
    _complexity_level_is_jev: bool = False

    def add_phase_ms(self, bucket: str, ms: float) -> None:
        self._durations_ms[bucket] = self._durations_ms.get(bucket, 0.0) + ms

    def attach_llm_result(
        self, *, complexity_level: int, complexity_tier: str, model_used: str,
        input_tokens: int, output_tokens: int, estimated_cost_usd: float,
    ) -> None:
        """Called by llm/observability.py::record() for every route() call
        made while this run is active -- an investigation makes several
        (hypothesis generation, one evaluation per hypothesis, synthesis),
        so tokens/cost accumulate across all of them while complexity_level/
        tier/model_used just take the latest call's values (good enough for
        observability; an investigation is fixed-DEEP throughout today
        anyway -- see research/investigation.py's own comment). The
        complexity_level/tier this receives is llm/hardness.py's model-tier
        number, not Jev's -- skipped when start_run() already set Jev's real
        level (_complexity_level_is_jev), so the correct value survives."""
        if not self._complexity_level_is_jev:
            self.complexity_level = complexity_level
            self.complexity_tier = complexity_tier
        self.model_used = model_used
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.estimated_cost_usd += estimated_cost_usd


_current: contextvars.ContextVar[RunTimer | None] = contextvars.ContextVar(
    "execution_metrics_current_run", default=None
)


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over an already-sorted list -- shared by the
    Execution Analytics panel (web/execution_analytics.py) and the retention
    job's daily rollup (scripts/execution_metrics_cleanup.py) so P50/P95 mean
    the same thing in both places. Plain Python rather than a backend SQL
    function (Postgres has percentile_cont, SQLite doesn't) so the number is
    identical regardless of DATABASE_BACKEND."""
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[index]


def current_run() -> RunTimer | None:
    return _current.get()


@contextmanager
def phase(bucket: str):
    """Times one named phase ('db', 'calc', 'llm', 'neo4j', 'planner') of
    whichever run is currently active, accumulating into that bucket (a
    phase entered more than once in one run, e.g. 'llm' across an
    investigation's several LLM calls, sums rather than overwrites). A
    silent no-op with no active run (start_run) -- e.g. a script or test
    calling gather_evidence()/plan_and_gather() directly -- same fail-open
    spirit as research/assistant.py's own sentry_span()."""
    timer = _current.get()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        if timer is not None:
            timer.add_phase_ms(bucket, (time.perf_counter() - t0) * 1000)


@contextmanager
def start_run(
    conn: DBConnection, run_id: str, task_name: str, *,
    execution_mode: str = "sync", jev_complexity_level: int | None = None,
):
    """Starts timing one Signal request/run and persists exactly one
    execution_metrics row when the `with` block exits, however it exits.
    `execution_mode` should be "async" when this run is executing inside
    research/case_runner.py's background thread (i.e. case_id was passed
    down to this pipeline call), "sync" otherwise -- see the three call
    sites' own reasoning for how they derive it.

    `jev_complexity_level` is Jev's real per-request classification
    (research/routing_policy.py's ComplexityLevel, 1-5) when the caller
    already has one (research/assistant.py::answer_question(), passed the
    level web/app.py's classify_and_log() computed) -- takes priority over
    whatever llm/hardness.py-derived number a route() call inside this run
    would otherwise report via attach_llm_result(). Omitted (None) for
    callers with no Jev classification available (main.py's CLI path,
    research/signals_report.py, research/investigation.py) -- those rows
    keep today's hardness-tier-derived number rather than a fabricated one.

    An exception raised inside the block is recorded (InsufficientEvidence
    Error/CaseCancelledError by name -> their own status, anything else ->
    "error") and then re-raised unchanged -- this context manager only
    observes the run, it never swallows or changes its outcome."""
    timer = RunTimer(run_id=run_id, task_name=task_name, execution_mode=execution_mode)
    if jev_complexity_level is not None:
        timer.complexity_level = jev_complexity_level
        timer._complexity_level_is_jev = True
    token = _current.set(timer)
    try:
        yield timer
    except BaseException as exc:
        timer.status = _STATUS_BY_EXCEPTION_NAME.get(type(exc).__name__, "error")
        timer.error_detail = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _current.reset(token)
        total_ms = (time.perf_counter() - timer._start) * 1000
        try:
            insert_execution_metrics(
                conn,
                run_id=timer.run_id, task_name=timer.task_name, execution_mode=timer.execution_mode,
                complexity_level=timer.complexity_level, complexity_tier=timer.complexity_tier,
                total_ms=total_ms,
                db_ms=timer._durations_ms.get("db"), calc_ms=timer._durations_ms.get("calc"),
                llm_ms=timer._durations_ms.get("llm"), neo4j_ms=timer._durations_ms.get("neo4j"),
                planner_ms=timer._durations_ms.get("planner"),
                model_used=timer.model_used, input_tokens=timer.input_tokens,
                output_tokens=timer.output_tokens, estimated_cost_usd=timer.estimated_cost_usd,
                status=timer.status, error_detail=timer.error_detail,
            )
        except Exception:  # noqa: BLE001 -- observability must never mask the real outcome above
            logger.warning(
                "execution_metrics insert failed for run_id=%s task_name=%s", timer.run_id, timer.task_name,
                exc_info=True,
            )
