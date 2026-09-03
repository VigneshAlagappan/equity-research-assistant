"""What the Settings page's "Indicator Rules" section needs: a read model
(every rule, its system default, the user's currently-effective
configuration, and each override they've made) and the two write
operations behind its form (save an override, reset one back to inherited).

Kept out of web/app.py so the route stays thin, and out of
indicators/config.py so that module stays pure/DB-free. Every SQL statement
lives in storage/indicator_repository.py; nothing here calls
`conn.execute(...)`.

A user's configuration is per-user by construction (`user_id` is part of
every key), and writing one never touches the system rule — the rules are
frozen dataclasses in indicators/rules.py that this module only reads.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from indicators.config import (
    RuleOverride,
    override_from_row,
    resolve_effective_config,
    validate_override,
)
from indicators.framework import (
    CLASSIFICATION_LABELS,
    SCOPE_TYPES,
    IndicatorRule,
    InvalidIndicatorConfigError,
    get_rule,
    list_rules,
)
from storage.db_types import DBConnection
from storage.indicator_repository import (
    delete_indicator_config,
    select_indicator_configs_for_user,
    upsert_indicator_config,
)


def _threshold_rows(rule: IndicatorRule, config) -> list[dict[str, Any]]:
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "unit": spec.unit,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "default": spec.default,
            "effective": config.thresholds.get(spec.key, spec.default),
            "source": config.sources.get(f"threshold:{spec.key}", "system"),
            "overridden": config.sources.get(f"threshold:{spec.key}", "system") != "system",
        }
        for spec in rule.thresholds
    ]


def build_rules_settings(
    conn: DBConnection, user_id: int, *, search: str | None = None
) -> list[dict[str, Any]]:
    """One entry per registered rule, in browse order. `effective` is the
    user's *global* effective configuration — the baseline that applies to
    a company with no sector/company override — and `overrides` lists every
    sector/company override they've made for that rule, each with its own
    resolved values so "what this actually does for Banks" is visible
    without opening a company page."""
    overrides = [override_from_row(row) for row in select_indicator_configs_for_user(conn, user_id)]
    entries: list[dict[str, Any]] = []
    for rule in list_rules(search=search):
        rule_overrides = [o for o in overrides if o.rule_id == rule.rule_id]
        global_config = resolve_effective_config(rule, rule_overrides, sector=None, company_id=None)
        scoped: list[dict[str, Any]] = []
        for override in sorted(rule_overrides, key=lambda o: (SCOPE_TYPES.index(o.scope_type), o.scope_value)):
            if override.scope_type == "global":
                continue
            resolved = resolve_effective_config(
                rule,
                rule_overrides,
                sector=override.scope_value if override.scope_type == "sector" else None,
                company_id=override.scope_value if override.scope_type == "company" else None,
            )
            scoped.append(
                {
                    "scope_type": override.scope_type,
                    "scope_value": override.scope_value,
                    "scope_label": override.scope_label,
                    "enabled": resolved.enabled,
                    "classification": resolved.classification,
                    "classification_label": CLASSIFICATION_LABELS[resolved.classification],
                    "thresholds": _threshold_rows(rule, resolved),
                }
            )
        has_global_override = any(o.scope_type == "global" for o in rule_overrides)
        entries.append(
            {
                "rule": rule,
                "rule_id": rule.rule_id,
                "name": rule.name,
                "family": rule.family,
                "description": rule.description,
                "version": rule.version,
                "required_facts": list(rule.required_facts),
                "system": {
                    "enabled": rule.enabled_by_default,
                    "classification": rule.default_classification,
                    "classification_label": CLASSIFICATION_LABELS[rule.default_classification],
                    "severity": rule.default_severity,
                    "thresholds": [
                        {"key": s.key, "label": s.label, "default": s.default, "unit": s.unit}
                        for s in rule.thresholds
                    ],
                },
                "effective": {
                    "enabled": global_config.enabled,
                    "classification": global_config.classification,
                    "classification_label": CLASSIFICATION_LABELS[global_config.classification],
                    "thresholds": _threshold_rows(rule, global_config),
                    "overridden": has_global_override and global_config.is_overridden,
                },
                "overrides": scoped,
            }
        )
    return entries


def save_rule_override(
    conn: DBConnection,
    *,
    user_id: int,
    rule_id: str,
    scope_type: str,
    scope_value: str,
    enabled: bool | None,
    classification: str | None,
    thresholds: Mapping[str, float] | None,
    now: str | None = None,
) -> None:
    """Validate against the system rule's own vocabulary/ranges, then write
    one override row. Every field is optional: passing None means "inherit
    this field", which is how the UI's "(inherit)" option and a
    threshold-only change are represented."""
    from storage.database import utcnow_iso

    rule = get_rule(rule_id)
    if rule is None:
        raise InvalidIndicatorConfigError(f"No indicator rule registered with rule_id={rule_id!r}")
    override = RuleOverride(
        rule_id=rule_id,
        scope_type=scope_type,
        scope_value=scope_value,
        enabled=enabled,
        classification=classification,
        thresholds=dict(thresholds) if thresholds else None,
    )
    validate_override(rule, override)
    upsert_indicator_config(
        conn,
        user_id=user_id,
        rule_id=rule_id,
        scope_type=scope_type,
        scope_value=scope_value,
        enabled=None if enabled is None else int(enabled),
        classification=classification,
        thresholds_json=json.dumps(override.thresholds, sort_keys=True) if override.thresholds else None,
        now=now or utcnow_iso(),
    )


def reset_rule_override(
    conn: DBConnection, *, user_id: int, rule_id: str, scope_type: str, scope_value: str
) -> bool:
    """Delete the override row so the rule falls back to whatever it
    inherits (the next-less-specific scope, ultimately the system default).
    Returns whether a row was actually removed."""
    if scope_type not in SCOPE_TYPES:
        raise InvalidIndicatorConfigError(f"scope_type must be one of {SCOPE_TYPES}, got {scope_type!r}")
    return delete_indicator_config(
        conn, user_id=user_id, rule_id=rule_id, scope_type=scope_type, scope_value=scope_value
    ) > 0


def parse_override_form(rule: IndicatorRule, form: Mapping[str, str]) -> dict[str, Any]:
    """Decode one Settings form post into save_rule_override()'s keyword
    arguments. Blank/"inherit" values become None (inherit) rather than a
    stored default — see storage/indicator_repository.py's
    delete_indicator_config docstring for why storing today's default would
    be wrong."""
    scope_type = (form.get("scope_type") or "global").strip()
    scope_value = (form.get("scope_value") or "").strip()
    if scope_type == "global":
        scope_value = ""

    raw_enabled = (form.get("enabled") or "").strip()
    enabled = None if raw_enabled in ("", "inherit") else raw_enabled in ("1", "true", "on", "yes")

    raw_classification = (form.get("classification") or "").strip()
    classification = raw_classification or None

    thresholds: dict[str, float] = {}
    for spec in rule.thresholds:
        raw = (form.get(f"threshold__{spec.key}") or "").strip()
        if raw == "":
            continue
        try:
            thresholds[spec.key] = float(raw)
        except ValueError:
            raise InvalidIndicatorConfigError(f"{spec.label} must be a number, got {raw!r}") from None

    return {
        "rule_id": rule.rule_id,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "enabled": enabled,
        "classification": classification,
        "thresholds": thresholds or None,
    }
