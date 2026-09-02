"""The seeded system rules — V1 ships two families (spec section 10's list
has ten; these two prove the shape, and a third is a new
`register_rule(...)` call in this file, not a framework change).

* **shareholding** — promoter-holding movement between the two most recent
  quarters on file, read from `shareholding_observations` via
  FactStore.list_shareholding_history (the same data the Shareholding tab's
  feed already uses, web/shareholding_feed.py).
* **financial_trajectory** — a year-over-year move in one of
  financials/report.py's TREND_METRICS, computed by
  financials/calculations.py::yoy_growth_for_metric over
  `canonical_financials`.

On the relationship with analytics/patterns.py: `detect_yoy_spikes()` is
the Tools tab's *cross-company scan* — every company, no per-user
configuration, no persistence. `_evaluate_yoy_move` below is the same
pattern expressed *per company, per user configuration, with an audit
trail*. Both sit on the one shared YoY computation
(`yoy_growth_for_metric`) and share one default threshold
(`analytics.patterns.DEFAULT_YOY_THRESHOLD_PERCENT`), so there is exactly
one definition of "a significant YoY move" in the codebase even though the
two surfaces render it very differently. Rewiring the Tools tab to go
through the rule engine would mean giving a cross-company scan a
company-scoped user configuration it has no UI for, so it was deliberately
left as-is.

Explanation templates are strictly factual: what the value was, what it
became, what the threshold was. No speculative language — an indicator is a
computed fact, never an inference about *why*.

Every rule's `evaluate` is a plain function of (conn, company_id,
thresholds, fact_store). It never sees the user, never sees its own
classification, and never calls an LLM.
"""

from __future__ import annotations

from typing import Any, Mapping

from analytics.patterns import DEFAULT_YOY_THRESHOLD_PERCENT
from financials.calculations import CalculationError, yoy_growth_for_metric
from indicators.framework import (
    OBSERVATION,
    POSITIVE,
    WARNING,
    IndicatorRule,
    RuleOutcome,
    ThresholdSpec,
    register_rule,
)
from storage.db_types import DBConnection
from storage.fact_store import FactStore

_SHAREHOLDING_PROVENANCE = "shareholding_observations (NSE SHP filing)"
_FINANCIALS_PROVENANCE = "canonical_financials (reconciled)"


def _severity_for(magnitude: float, threshold: float, default: str) -> str:
    """A move far past its threshold is more severe than one that just
    cleared it. Deterministic and threshold-relative, so a user who raises
    the threshold also raises the bar for "high" — no separate severity
    configuration to keep in sync."""
    if threshold <= 0:
        return default
    ratio = magnitude / threshold
    if ratio >= 3.0:
        return "high"
    if ratio >= 1.75:
        return "medium"
    return default


# ------------------------------------------------------------------
# Family: shareholding
# ------------------------------------------------------------------


def _latest_two_promoter_quarters(conn: DBConnection, company_id: str, fact_store: FactStore):
    """The two most recent quarters that actually disclose a promoter
    percentage, newest first. Quarters with a NULL promoter percentage are
    skipped rather than treated as zero — absence isn't an error and
    certainly isn't a 100pp decline (same "absence isn't an error" rule
    analytics/patterns.py already follows)."""
    history = fact_store.list_shareholding_history(conn, company_id)  # oldest first
    disclosed = [q for q in history if q.get("promoter_holding_percent") is not None]
    if len(disclosed) < 2:
        return None
    return disclosed[-1], disclosed[-2]


def _promoter_facts(latest: Mapping[str, Any], previous: Mapping[str, Any], delta_pp: float) -> dict[str, Any]:
    return {
        "latest_period": f"{latest['quarter']} {latest['fiscal_year']}",
        "previous_period": f"{previous['quarter']} {previous['fiscal_year']}",
        "latest_promoter_percent": float(latest["promoter_holding_percent"]),
        "previous_promoter_percent": float(previous["promoter_holding_percent"]),
        "change_pp": round(delta_pp, 4),
        "source_url": latest.get("source_url"),
    }


def _evaluate_promoter_decline(
    conn: DBConnection, company_id: str, *, thresholds: Mapping[str, float], fact_store: FactStore
) -> RuleOutcome | None:
    pair = _latest_two_promoter_quarters(conn, company_id, fact_store)
    if pair is None:
        return None
    latest, previous = pair
    threshold = float(thresholds["decline_pp"])
    delta = float(latest["promoter_holding_percent"]) - float(previous["promoter_holding_percent"])
    if -delta <= threshold:
        return None
    facts = _promoter_facts(latest, previous, delta)
    return RuleOutcome(
        explanation=(
            f"Promoter holding declined from {facts['previous_promoter_percent']:.2f}% "
            f"({facts['previous_period']}) to {facts['latest_promoter_percent']:.2f}% "
            f"({facts['latest_period']}), a decline of {-delta:.2f} percentage points, "
            f"exceeding the {threshold:g}pp threshold."
        ),
        facts=facts,
        period_label=facts["latest_period"],
        provenance=latest.get("source_url") or _SHAREHOLDING_PROVENANCE,
        severity=_severity_for(-delta, threshold, "medium"),
    )


def _evaluate_promoter_increase(
    conn: DBConnection, company_id: str, *, thresholds: Mapping[str, float], fact_store: FactStore
) -> RuleOutcome | None:
    pair = _latest_two_promoter_quarters(conn, company_id, fact_store)
    if pair is None:
        return None
    latest, previous = pair
    threshold = float(thresholds["increase_pp"])
    delta = float(latest["promoter_holding_percent"]) - float(previous["promoter_holding_percent"])
    if delta <= threshold:
        return None
    facts = _promoter_facts(latest, previous, delta)
    return RuleOutcome(
        explanation=(
            f"Promoter holding rose from {facts['previous_promoter_percent']:.2f}% "
            f"({facts['previous_period']}) to {facts['latest_promoter_percent']:.2f}% "
            f"({facts['latest_period']}), an increase of {delta:.2f} percentage points, "
            f"exceeding the {threshold:g}pp threshold."
        ),
        facts=facts,
        period_label=facts["latest_period"],
        provenance=latest.get("source_url") or _SHAREHOLDING_PROVENANCE,
        severity=_severity_for(delta, threshold, "low"),
    )


register_rule(
    IndicatorRule(
        rule_id="shareholding.promoter_holding_decline",
        name="Promoter holding declined",
        family="shareholding",
        description=(
            "Promoter holding fell by more than the configured number of percentage points "
            "between the two most recent quarters on file."
        ),
        version="1.0.0",
        required_facts=("shareholding_observations.promoter_holding_percent",),
        default_classification=WARNING,
        default_severity="medium",
        thresholds=(
            ThresholdSpec(
                key="decline_pp", label="Minimum decline", default=1.0, unit="pp",
                minimum=0.01, maximum=100.0,
            ),
        ),
        evaluate=_evaluate_promoter_decline,
    )
)

register_rule(
    IndicatorRule(
        rule_id="shareholding.promoter_holding_increase",
        name="Promoter holding increased",
        family="shareholding",
        description=(
            "Promoter holding rose by more than the configured number of percentage points "
            "between the two most recent quarters on file."
        ),
        version="1.0.0",
        required_facts=("shareholding_observations.promoter_holding_percent",),
        default_classification=POSITIVE,
        default_severity="low",
        thresholds=(
            ThresholdSpec(
                key="increase_pp", label="Minimum increase", default=1.0, unit="pp",
                minimum=0.01, maximum=100.0,
            ),
        ),
        evaluate=_evaluate_promoter_increase,
    )
)


# ------------------------------------------------------------------
# Family: financial_trajectory
#
# One parameterized evaluator, registered once per metric — adding
# "advances" or "deposits" as its own configurable indicator is a
# `_register_yoy_move_rule(...)` line, not new framework code. That is the
# extensibility contract spec section 10 asks for, demonstrated rather than
# asserted.
# ------------------------------------------------------------------


def _make_yoy_evaluator(metric_key: str, metric_label: str):
    def _evaluate(
        conn: DBConnection, company_id: str, *, thresholds: Mapping[str, float], fact_store: FactStore
    ) -> RuleOutcome | None:
        series = fact_store.get_canonical_series(conn, company_id, metric_key, "annual", "consolidated")
        if not series:
            return None
        latest_fiscal_year = series[-1]["fiscal_year"]
        try:
            result = yoy_growth_for_metric(
                conn, company_id, metric_key, latest_fiscal_year, statement_type="consolidated"
            )
        except CalculationError:
            # No prior-year value, or a zero denominator — not an error, just
            # nothing to compare against (analytics/patterns.py does the same).
            return None
        threshold = float(thresholds["move_percent"])
        if abs(result.value) < threshold:
            return None
        previous_fiscal_year = f"FY{int(latest_fiscal_year[2:]) - 1}"
        direction = "rose" if result.value > 0 else "fell"
        facts = {
            "metric_key": metric_key,
            "metric_label": metric_label,
            "fiscal_year": latest_fiscal_year,
            "previous_fiscal_year": previous_fiscal_year,
            "yoy_percent": round(result.value, 4),
        }
        return RuleOutcome(
            explanation=(
                f"{metric_label} {direction} {abs(result.value):.1f}% year over year "
                f"({previous_fiscal_year} to {latest_fiscal_year}, consolidated), "
                f"a move of at least the {threshold:g}% threshold."
            ),
            facts=facts,
            period_label=latest_fiscal_year,
            provenance=_FINANCIALS_PROVENANCE,
            severity=_severity_for(abs(result.value), threshold, "low"),
        )

    return _evaluate


def _make_yoy_growth_evaluator(metric_key: str, metric_label: str):
    """Growth-only variant of `_make_yoy_growth_evaluator`'s sibling above —
    fires only when the metric rose at least the threshold, never on a
    decline of any size. Direction is part of the trigger condition, so it
    lives in code (a separate rule), not a config knob on the direction-
    agnostic `_yoy_move` rule — same split analytics/patterns.py's
    promoter_holding_decline/increase pair already establishes."""

    def _evaluate(
        conn: DBConnection, company_id: str, *, thresholds: Mapping[str, float], fact_store: FactStore
    ) -> RuleOutcome | None:
        series = fact_store.get_canonical_series(conn, company_id, metric_key, "annual", "consolidated")
        if not series:
            return None
        latest_fiscal_year = series[-1]["fiscal_year"]
        try:
            result = yoy_growth_for_metric(
                conn, company_id, metric_key, latest_fiscal_year, statement_type="consolidated"
            )
        except CalculationError:
            return None
        threshold = float(thresholds["growth_percent"])
        if result.value < threshold:
            return None  # a decline, or growth below threshold — never fires on a decline of any size
        previous_fiscal_year = f"FY{int(latest_fiscal_year[2:]) - 1}"
        facts = {
            "metric_key": metric_key,
            "metric_label": metric_label,
            "fiscal_year": latest_fiscal_year,
            "previous_fiscal_year": previous_fiscal_year,
            "yoy_percent": round(result.value, 4),
        }
        return RuleOutcome(
            explanation=(
                f"{metric_label} grew {result.value:.1f}% year over year "
                f"({previous_fiscal_year} to {latest_fiscal_year}, consolidated), "
                f"at or above the {threshold:g}% growth threshold."
            ),
            facts=facts,
            period_label=latest_fiscal_year,
            provenance=_FINANCIALS_PROVENANCE,
            severity=_severity_for(result.value, threshold, "low"),
        )

    return _evaluate


def _register_yoy_growth_rule(metric_key: str, metric_label: str) -> IndicatorRule:
    return register_rule(
        IndicatorRule(
            rule_id=f"financial_trajectory.{metric_key}_growth",
            name=f"{metric_label} grew year over year",
            family="financial_trajectory",
            description=(
                f"{metric_label} increased by at least the configured percentage between the two "
                "most recent annual periods on file, consolidated. Growth-only — never fires on a decline."
            ),
            version="1.0.0",
            required_facts=(f"canonical_financials.{metric_key} (annual, consolidated)",),
            default_classification=POSITIVE,
            default_severity="low",
            thresholds=(
                ThresholdSpec(
                    key="growth_percent", label="Minimum YoY growth", default=DEFAULT_YOY_THRESHOLD_PERCENT,
                    unit="%", minimum=0.1, maximum=1000.0,
                ),
            ),
            evaluate=_make_yoy_growth_evaluator(metric_key, metric_label),
        )
    )


_register_yoy_growth_rule("net_profit", "Net Profit")


def _register_yoy_move_rule(metric_key: str, metric_label: str) -> IndicatorRule:
    return register_rule(
        IndicatorRule(
            rule_id=f"financial_trajectory.{metric_key}_yoy_move",
            name=f"{metric_label} moved sharply year over year",
            family="financial_trajectory",
            description=(
                f"{metric_label} moved (in either direction) by at least the configured percentage "
                "between the two most recent annual periods on file, consolidated."
            ),
            version="1.0.0",
            required_facts=(f"canonical_financials.{metric_key} (annual, consolidated)",),
            # Observation, not Positive/Warning: the rule is direction-agnostic
            # by design (a large move either way is worth noticing), and calling
            # a rise "positive" would be an inference this framework must not make.
            default_classification=OBSERVATION,
            default_severity="low",
            thresholds=(
                ThresholdSpec(
                    key="move_percent", label="Minimum absolute YoY move",
                    default=DEFAULT_YOY_THRESHOLD_PERCENT, unit="%", minimum=0.1, maximum=1000.0,
                ),
            ),
            evaluate=_make_yoy_evaluator(metric_key, metric_label),
        )
    )


_register_yoy_move_rule("net_profit", "Net Profit")
_register_yoy_move_rule("total_assets", "Total Assets")
