"""Scope and override resolution: System Rule + Applicable Configuration ->
Effective Rule (spec sections 4 and 5).

    Company Override > Sector Override > Global/User Default > System Default

`resolve_effective_config()` is a pure function — it takes the rule, a list
of already-loaded overrides, and the company's sector/id, and returns an
`EffectiveConfig`. It touches no database and no clock, which is what makes
"same facts + same rule version + same configuration -> same result"
testable in isolation.

Two deliberate design decisions:

1. **Per-field resolution.** An override row carries NULLs for the fields it
   doesn't touch, and each field is resolved independently. So a user who
   sets *classification* to Observation for one company still inherits the
   *threshold* they configured globally, instead of that company override
   silently freezing today's global threshold. `sources` records which scope
   won each field, which is exactly what the Settings UI shows as
   "overridden" and what the audit trail stores as `scope_applied`.
2. **Thresholds merge key-by-key too.** A rule with two thresholds where the
   user overrode one keeps the system default for the other.

An override never modifies or duplicates the system rule — the rule object
is frozen and shared; everything here is applied on top of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from indicators.framework import (
    CLASSIFICATIONS,
    SCOPE_TYPES,
    SYSTEM_SCOPE,
    EffectiveConfig,
    IndicatorRule,
    InvalidIndicatorConfigError,
)


@dataclass(frozen=True)
class RuleOverride:
    """One `indicator_rule_config` row, decoded. A None field means
    "inherit", never "off" — the difference matters: `enabled=False` is a
    user disabling a rule, `enabled=None` is a user who only changed the
    threshold."""

    rule_id: str
    scope_type: str  # global | sector | company
    scope_value: str  # "" for global
    enabled: bool | None = None
    classification: str | None = None
    thresholds: Mapping[str, float] | None = None

    @property
    def scope_label(self) -> str:
        return self.scope_type if self.scope_type == "global" else f"{self.scope_type}:{self.scope_value}"


def override_from_row(row: Mapping[str, Any]) -> RuleOverride:
    """Decode one `indicator_rule_config` row (thresholds_json is TEXT).
    A malformed thresholds_json is treated as "no threshold override"
    rather than raising — a corrupt settings row should degrade to the
    system default, not break every company page for that user."""
    raw = row["thresholds_json"]
    thresholds: dict[str, float] | None = None
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                thresholds = {str(k): float(v) for k, v in decoded.items()}
        except (ValueError, TypeError):
            thresholds = None
    return RuleOverride(
        rule_id=row["rule_id"],
        scope_type=row["scope_type"],
        scope_value=row["scope_value"] or "",
        enabled=None if row["enabled"] is None else bool(row["enabled"]),
        classification=row["classification"],
        thresholds=thresholds,
    )


def validate_override(rule: IndicatorRule, override: RuleOverride) -> None:
    """Gate for anything arriving from a form post. Everything here is a
    fixed vocabulary or a rule-declared threshold key — a user can never
    introduce a new threshold, a new scope type, or a classification the
    framework doesn't know."""
    if override.scope_type not in SCOPE_TYPES:
        raise InvalidIndicatorConfigError(f"scope_type must be one of {SCOPE_TYPES}, got {override.scope_type!r}")
    if override.scope_type not in rule.applicable_scopes:
        raise InvalidIndicatorConfigError(f"{rule.rule_id} cannot be configured at scope {override.scope_type!r}")
    if override.scope_type == "global" and override.scope_value:
        raise InvalidIndicatorConfigError("A global override must not carry a scope value")
    if override.scope_type != "global" and not override.scope_value:
        raise InvalidIndicatorConfigError(f"A {override.scope_type} override needs a scope value")
    if override.classification is not None and override.classification not in CLASSIFICATIONS:
        raise InvalidIndicatorConfigError(
            f"classification must be one of {CLASSIFICATIONS}, got {override.classification!r}"
        )
    for key, value in (override.thresholds or {}).items():
        spec = rule.threshold_spec(key)
        if spec is None:
            raise InvalidIndicatorConfigError(f"{rule.rule_id} has no threshold named {key!r}")
        if not (spec.minimum <= value <= spec.maximum):
            raise InvalidIndicatorConfigError(
                f"{rule.rule_id}.{key} must be between {spec.minimum} and {spec.maximum}, got {value}"
            )


def applicable_overrides(
    overrides: Iterable[RuleOverride], *, rule_id: str, sector: str | None, company_id: str | None
) -> list[RuleOverride]:
    """The subset of a user's overrides that applies to this rule in this
    company's context, ordered least- to most-specific. Sector matching is
    exact on the company's sector name (see indicators/evaluation.py's
    `company_sector()` for which company column that is)."""
    matched: list[RuleOverride] = []
    for scope_type in SCOPE_TYPES:  # global, sector, company — precedence order
        for override in overrides:
            if override.rule_id != rule_id or override.scope_type != scope_type:
                continue
            if scope_type == "global":
                matched.append(override)
            elif scope_type == "sector" and sector is not None and override.scope_value == sector:
                matched.append(override)
            elif scope_type == "company" and company_id is not None and override.scope_value == company_id:
                matched.append(override)
    return matched


def resolve_effective_config(
    rule: IndicatorRule,
    overrides: Sequence[RuleOverride],
    *,
    sector: str | None = None,
    company_id: str | None = None,
) -> EffectiveConfig:
    """Pure: rule defaults, then each applicable override in
    least-to-most-specific order, field by field. `overrides` may be the
    user's whole set — irrelevant rules/scopes are filtered here."""
    enabled = rule.enabled_by_default
    classification = rule.default_classification
    thresholds = rule.default_thresholds()
    sources: dict[str, str] = {"enabled": SYSTEM_SCOPE, "classification": SYSTEM_SCOPE}
    for key in thresholds:
        sources[f"threshold:{key}"] = SYSTEM_SCOPE

    for override in applicable_overrides(
        overrides, rule_id=rule.rule_id, sector=sector, company_id=company_id
    ):
        label = override.scope_label
        if override.enabled is not None:
            enabled, sources["enabled"] = override.enabled, label
        if override.classification is not None:
            classification, sources["classification"] = override.classification, label
        for key, value in (override.thresholds or {}).items():
            if key in thresholds:  # unknown keys are ignored, never invented
                thresholds[key], sources[f"threshold:{key}"] = value, label

    return EffectiveConfig(
        rule_id=rule.rule_id,
        enabled=enabled,
        classification=classification,
        thresholds=thresholds,
        sources=sources,
    )
