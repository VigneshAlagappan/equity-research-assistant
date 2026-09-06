"""Deterministic cross-company aggregation for a resolved company group
(retrieval/tag_resolver.py) -- "Nifty 50 net profit CAGR over the last 3
years" needs to (1) sum a metric across every group member per period,
then (2) run CAGR/growth on the summed series, neither of which is a
reasoning task an LLM should be doing arithmetic for (a real, observed
failure mode: handing 50 companies' worth of individual evidence to an
LLM produced an illegible 50-line comparison chart and a non-answer, not
a number -- the wrong tool for what is actually a sum-then-CAGR problem).

The one piece that DOES need an LLM is figuring out which metric, which
operation, and which time range the question is actually asking for --
"total profit," "combined earnings," "how much did they make together"
are all the same intent phrased differently, and metric_key names
(net_profit, total_revenue, ...) are a closed, known vocabulary an LLM
maps free text onto exactly the way research/macro_evidence.py's
_plan_retrieval already does for macro series -- the one existing
precedent in this app for "spend an LLM call mapping text to a known
vocabulary" (see retrieval/tag_resolver.py's own docstring for why a tag
NAME doesn't need this treatment but an operation/metric DOES: a tag name
is a proper noun someone types close to verbatim, "how much did they make
together" has no verbatim anchor to regex-match against at all).

extract_aggregate_intent() is the only LLM call in this module, QUICK
tier, and fails soft (returns is_aggregate=False) on any error so a
caller never needs its own try/except just to fall through to the normal
Ask/Investigation path. compute_group_aggregate()/format_aggregate_answer()
are pure deterministic Python -- no LLM, no network, given a company list
and an already-extracted intent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from config.settings import ANTHROPIC_MODEL
from financials.calculations import CalculationError
from financials.calculations import cagr as _cagr
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from storage.db_types import DBConnection
from storage.repositories import get_canonical_series, list_all_metrics

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_OPERATIONS = ("sum", "average", "cagr", "growth")
_INTENT_MAX_TOKENS = 256


@dataclass(frozen=True)
class AggregateIntent:
    is_aggregate: bool
    metric_key: str | None = None
    operation: str | None = None  # sum | average | cagr | growth
    num_years: int | None = None


@dataclass(frozen=True)
class AggregatePeriodValue:
    fiscal_year: str
    value: float
    companies_reporting: int
    companies_missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AggregateResult:
    metric_key: str
    metric_label: str
    operation: str
    per_period: list[AggregatePeriodValue]
    cagr_percent: float | None
    unit: str | None


class AggregateQueryError(Exception):
    pass


def _build_intent_system_prompt(conn: DBConnection) -> str:
    metrics = list_all_metrics(conn)
    metric_lines = "\n".join(f"- {key}: {label}" for key, label in metrics)
    return (
        "You classify one financial-research question about a GROUP of companies "
        "(e.g. an index, sector, or industry). Determine whether it's asking for an "
        "AGGREGATE calculation -- sum, average, CAGR, or growth of ONE metric, combined "
        "across every company in the group -- as opposed to a per-company comparison, a "
        "causal/explanatory question, or a single-company lookup.\n\n"
        "Known metric keys (choose exactly one that matches, or null if none clearly applies):\n"
        + metric_lines + "\n\n"
        "num_years: how many of the most recent fiscal years the question wants (an integer), "
        "or null if unspecified/not applicable.\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown fences:\n"
        '{"is_aggregate": true|false, "metric_key": "<key>"|null, '
        '"operation": "sum"|"average"|"cagr"|"growth"|null, "num_years": <int>|null}'
    )


def extract_aggregate_intent(conn: DBConnection, question: str) -> AggregateIntent:
    """One LLM call, STANDARD tier -- NOT QUICK, despite this being a small
    classification/extraction task that would otherwise fit QUICK's own
    description. Measured, not assumed: QUICK's preferred model is the
    local Gemma 4 (config.settings.TIER_PREFERRED_MODEL), and against this
    exact prompt+question it returned a blank response 3 times out of 5
    real calls (the other 2 were correct) -- a coin-flip failure rate that
    would silently and unpredictably fall an aggregate question through to
    the old broken per-company flow half the time. A wrong classification
    here is worse than an expensive one: this decision determines whether
    the user gets a clean, correct number or 50 companies' worth of
    illegible chart, so correctness outranks the marginal cost of Haiku
    over a free local call for this one call site.

    Returns AggregateIntent(is_aggregate=False) on ANY failure (provider
    unavailable, unparseable response, hallucinated metric_key) rather than
    raising -- a caller checks .is_aggregate and falls through to the
    normal Ask/Investigation path, same "absence isn't an error" contract
    this app's source adapters already follow."""
    hardness = fixed(Tier.STANDARD, "aggregate-intent classification")
    try:
        result = route(
            system=_build_intent_system_prompt(conn), user_message=question,
            hardness=hardness, max_tokens=_INTENT_MAX_TOKENS, pinned_model=ANTHROPIC_MODEL,
        )
    except AllProvidersUnavailableError:
        return AggregateIntent(is_aggregate=False)

    observability.record(conn, task_name="aggregate_intent", company_ids=[], question=question, result=result)

    text = result.response.text or ""
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return AggregateIntent(is_aggregate=False)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return AggregateIntent(is_aggregate=False)

    known_metric_keys = {key for key, _label in list_all_metrics(conn)}
    metric_key = data.get("metric_key")
    if not data.get("is_aggregate") or metric_key not in known_metric_keys:
        return AggregateIntent(is_aggregate=False)

    operation = data.get("operation")
    if operation not in _OPERATIONS:
        operation = "cagr"
    num_years = data.get("num_years")
    return AggregateIntent(
        is_aggregate=True, metric_key=metric_key, operation=operation,
        num_years=num_years if isinstance(num_years, int) and num_years > 0 else None,
    )


def compute_group_aggregate(
    conn: DBConnection, company_ids: list[str], intent: AggregateIntent, *,
    period_type: str = "annual", statement_type: str | None = "consolidated",
) -> AggregateResult:
    """Deterministic -- no LLM involved. Sums intent.metric_key across every
    company_id per fiscal year, counting only companies that actually
    reported that year (a company with a gap is recorded in
    companies_missing, never estimated or silently dropped from the total
    without a trace). Runs CAGR across the full available span when
    intent.operation is "cagr"/"growth" and at least two periods exist."""
    if intent.metric_key is None:
        raise AggregateQueryError("intent.metric_key is required to compute an aggregate")

    per_company_series = {
        company_id: {
            row["fiscal_year"]: row
            for row in get_canonical_series(conn, company_id, intent.metric_key, period_type, statement_type)
        }
        for company_id in company_ids
    }
    all_years = sorted({fy for series in per_company_series.values() for fy in series})
    if intent.num_years:
        all_years = all_years[-intent.num_years:]

    unit = None
    per_period: list[AggregatePeriodValue] = []
    for fy in all_years:
        total = 0.0
        reporting = 0
        missing: list[str] = []
        for company_id, series in per_company_series.items():
            row = series.get(fy)
            if row is None:
                missing.append(company_id)
                continue
            total += row["canonical_value"]
            reporting += 1
            unit = unit or row["unit"]
        per_period.append(
            AggregatePeriodValue(fiscal_year=fy, value=total, companies_reporting=reporting, companies_missing=missing)
        )

    cagr_percent = None
    if intent.operation in ("cagr", "growth") and len(per_period) >= 2:
        begin, end = per_period[0], per_period[-1]
        try:
            cagr_percent = _cagr(begin.value, end.value, len(per_period) - 1)
        except CalculationError:
            cagr_percent = None

    label = dict(list_all_metrics(conn)).get(intent.metric_key, intent.metric_key)
    return AggregateResult(
        metric_key=intent.metric_key, metric_label=label, operation=intent.operation,
        per_period=per_period, cagr_percent=cagr_percent, unit=unit,
    )


def format_aggregate_answer(result: AggregateResult, group_size: int) -> str:
    """Plain deterministic markdown -- no LLM needed to phrase this; the
    numbers already are the answer. Every period explicitly notes how many
    of the group's companies actually had data that year, so a partial
    total is never mistaken for a complete one."""
    unit_label = f" {result.unit}" if result.unit else ""
    lines = [f"**{result.metric_label}, summed across {group_size} companies:**", ""]
    for p in result.per_period:
        note = f" (only {p.companies_reporting}/{group_size} companies reporting)" if p.companies_missing else ""
        lines.append(f"- {p.fiscal_year}: {p.value:,.1f}{unit_label}{note}")
    if result.cagr_percent is not None:
        lines.append("")
        lines.append(f"**CAGR ({result.per_period[0].fiscal_year} → {result.per_period[-1].fiscal_year}): {result.cagr_percent:.1f}%**")
    return "\n".join(lines)
