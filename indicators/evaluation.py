"""The evaluation engine: resolve each rule's effective configuration for
one company and one user, run the enabled rules, and append an audit row
per newly-triggered/changed indicator.

    Facts -> Rules -> User Configuration -> Evaluation -> Presentation

Deliberately independent of ingestion, LLM insight generation, hypothesis
generation and investigation (spec section 8): nothing here writes facts,
nothing here calls a model, and ingestion never calls into this module.
Evaluation consumes already-normalized facts through the injected
`FactStore` seam (storage/fact_store.py), the same way research/ and
context/ do — this module never imports storage.repositories directly.

**Persistence policy.** One audit row per *triggered* indicator, appended
only when its `result_hash` differs from that rule's most recent row for
the same (company, user). Rationale: the auditable event is "this
indicator fired, on these facts, under this configuration", and a page
refresh that reproduces the identical result is not a new event — but a
changed threshold, a changed classification, or a new quarter of data is,
and each of those changes the hash. Rules that *didn't* trigger are not
recorded: with every rule evaluated on every company page view that would
be almost entirely rows saying "nothing happened", and the rule
registry plus the stored effective configuration already make a non-trigger
reproducible. Nothing here is ever UPDATEd or DELETEd.

An LLM never participates in this path, and cannot: no model client is
imported, and classification comes only from the frozen system rule plus
the user's own stored configuration.

Extension point (spec section 11, deliberately NOT built in this
increment): a future Agree | Disagree | Not Sure feedback loop would hang a
`indicator_feedback(evaluation_id, user_id, verdict, created_at)` table off
the `evaluation_id` each TriggeredIndicator already carries, and would
*suggest* a change to the user's indicator_rule_config layer for approval —
never rewrite a fact, never modify a system rule, never silently change a
classification.
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

from indicators.config import RuleOverride, override_from_row, resolve_effective_config
from indicators.framework import (
    CLASSIFICATIONS,
    EffectiveConfig,
    IndicatorRule,
    TriggeredIndicator,
    facts_json,
    list_rules,
    result_hash,
)
from storage.db_types import DBConnection
from storage.fact_store import FactStore, default_fact_store
from storage.indicator_repository import (
    insert_indicator_evaluation,
    select_indicator_configs_for_user,
    select_latest_indicator_result_hashes,
)

logger = logging.getLogger(__name__)


def load_user_overrides(conn: DBConnection, user_id: int | None) -> list[RuleOverride]:
    """A signed-out visitor has no configuration layer at all — they see the
    system defaults, which is the correct "no user identity, no user
    settings" behaviour rather than borrowing someone else's."""
    if user_id is None:
        return []
    return [override_from_row(row) for row in select_indicator_configs_for_user(conn, user_id)]


def company_sector(company_row) -> str | None:
    """Which company column counts as "the sector" for sector-scoped
    overrides. `companies.sector` is the vocabulary the Settings sector
    dropdown is populated from (storage/repositories.py's `sectors` table),
    so it's the primary; NSE's `basic_industry`/`macro_economic_sector` are
    fallbacks for a company registered before `sector` was being filled in,
    matching storage/company_repository.py's own peer-lookup COALESCE."""
    if company_row is None:
        return None
    for key in ("sector", "basic_industry", "macro_economic_sector"):
        try:
            value = company_row[key]
        except (KeyError, IndexError, TypeError):
            continue
        if value:
            return str(value)
    return None


def effective_configs_for_company(
    conn: DBConnection,
    company_id: str,
    *,
    user_id: int | None = None,
    fact_store: FactStore | None = None,
    overrides: Sequence[RuleOverride] | None = None,
) -> dict[str, EffectiveConfig]:
    """rule_id -> the effective configuration this company/user resolves to.
    Exposed separately from `evaluate_company_indicators` so the Settings UI
    can show "what would apply here" without running any rule."""
    fs = fact_store or default_fact_store()
    resolved_overrides = list(overrides) if overrides is not None else load_user_overrides(conn, user_id)
    sector = company_sector(fs.get_company(conn, company_id))
    return {
        rule.rule_id: resolve_effective_config(
            rule, resolved_overrides, sector=sector, company_id=company_id
        )
        for rule in list_rules()
    }


def _threshold_summary(rule: IndicatorRule, config: EffectiveConfig) -> str:
    """"Threshold/comparison" for the company-page card — the effective
    numbers, labelled, e.g. "Minimum decline: 1pp"."""
    parts = []
    for spec in rule.thresholds:
        value = config.thresholds.get(spec.key, spec.default)
        parts.append(f"{spec.label}: {value:g}{spec.unit}")
    return " · ".join(parts)


def evaluate_company_indicators(
    conn: DBConnection,
    company_id: str,
    *,
    user_id: int | None = None,
    fact_store: FactStore | None = None,
    persist: bool = True,
    now: str | None = None,
) -> list[TriggeredIndicator]:
    """Every currently-triggered indicator for this company under this
    user's configuration, most severe first. A rule that raises is logged
    and skipped, never allowed to blank the whole section — same
    one-failure-can't-block-its-siblings rule ingestion/event_bus.py applies
    to workers."""
    from storage.database import utcnow_iso  # local: keeps the clock out of the pure paths

    fs = fact_store or default_fact_store()
    evaluated_at = now or utcnow_iso()
    overrides = load_user_overrides(conn, user_id)
    sector = company_sector(fs.get_company(conn, company_id))

    triggered: list[TriggeredIndicator] = []
    for rule in list_rules():
        config = resolve_effective_config(rule, overrides, sector=sector, company_id=company_id)
        if not config.enabled:
            continue
        try:
            outcome = rule.evaluate(conn, company_id, thresholds=config.thresholds, fact_store=fs)
        except Exception:  # noqa: BLE001 — one bad rule must not blank the section
            logger.exception("Indicator rule %s failed for %s", rule.rule_id, company_id)
            continue
        if outcome is None:
            continue
        classification = config.classification
        if classification not in CLASSIFICATIONS:  # defensive: a corrupt stored value
            classification = rule.default_classification
        triggered.append(
            TriggeredIndicator(
                rule_id=rule.rule_id,
                rule_name=rule.name,
                rule_version=rule.version,
                family=rule.family,
                classification=classification,
                severity=outcome.severity or rule.default_severity,
                explanation=outcome.explanation,
                facts=outcome.facts,
                effective_config=config,
                scope_applied=config.most_specific_scope,
                evaluated_at=evaluated_at,
                period_label=outcome.period_label,
                provenance=outcome.provenance,
                threshold_summary=_threshold_summary(rule, config),
            )
        )

    triggered.sort(key=_presentation_sort_key)
    if persist:
        triggered = _append_audit_rows(conn, company_id, user_id, triggered)
    return triggered


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _presentation_sort_key(indicator: TriggeredIndicator) -> tuple:
    return (_SEVERITY_RANK.get(indicator.severity, 3), indicator.rule_id)


def _append_audit_rows(
    conn: DBConnection, company_id: str, user_id: int | None, triggered: list[TriggeredIndicator]
) -> list[TriggeredIndicator]:
    """Append-only, deduped against each rule's most recent stored result.
    Returns the same indicators with `evaluation_id` set where a row was
    actually written (None means "identical result already on record")."""
    import dataclasses

    known = select_latest_indicator_result_hashes(conn, company_id, user_id)
    out: list[TriggeredIndicator] = []
    for indicator in triggered:
        digest = result_hash(
            rule_id=indicator.rule_id,
            rule_version=indicator.rule_version,
            facts=indicator.facts,
            effective_config=indicator.effective_config,
            classification=indicator.classification,
            severity=indicator.severity,
        )
        if known.get(indicator.rule_id) == digest:
            out.append(indicator)
            continue
        evaluation_id = insert_indicator_evaluation(
            conn,
            company_id=company_id,
            user_id=user_id,
            rule_id=indicator.rule_id,
            rule_version=indicator.rule_version,
            classification=indicator.classification,
            severity=indicator.severity,
            explanation=indicator.explanation,
            facts_json=facts_json(indicator.facts),
            effective_config_json=json.dumps(indicator.effective_config.as_dict(), sort_keys=True),
            scope_applied=indicator.scope_applied,
            period_label=indicator.period_label,
            provenance=indicator.provenance,
            result_hash=digest,
            evaluated_at=indicator.evaluated_at,
        )
        out.append(dataclasses.replace(indicator, evaluation_id=evaluation_id))
    return out


def group_by_classification(
    indicators: Sequence[TriggeredIndicator],
) -> dict[str, list[TriggeredIndicator]]:
    """The company page's three columns. Always returns all three keys, in
    Positive / Observation / Warning order, so an empty column still renders
    its (collapsed) heading rather than disappearing."""
    grouped: dict[str, list[TriggeredIndicator]] = {c: [] for c in CLASSIFICATIONS}
    for indicator in indicators:
        grouped.setdefault(indicator.classification, []).append(indicator)
    return grouped
