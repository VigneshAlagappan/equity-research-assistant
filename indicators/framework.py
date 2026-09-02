"""The Configurable Indicator Framework's core shapes and rule registry.

An *indicator* is a deterministic, factual pattern ("promoter holding fell
2.1pp last quarter") — not an insight, conclusion, prediction or
recommendation. It sits alongside Evidence in this app's
Fact -> Evidence -> Inference -> Hypothesis -> Conclusion separation: an
indicator is a computed fact, so every explanation template here stays
strictly factual and cites the values it fired on. No LLM call exists
anywhere on this path, by construction — a rule is Python, its thresholds
and classification come from the user's own configuration, and nothing
else can change either.

Three pieces live here:

* `IndicatorRule` — the *system rule*: stable id, version, description,
  required facts, default classification/severity/thresholds, scope
  applicability, and an `evaluate` callable. Rules are code, not database
  rows (their trigger logic is real Python; making it interpretable from
  JSON would be a rule-authoring DSL, which V1 explicitly doesn't need).
  Only *thresholds, classification and enabled/disabled* are configurable,
  and those live in `indicator_rule_config` — a user's configuration never
  modifies or duplicates the rule itself.
* `RULE_REGISTRY` — a plain `dict[rule_id, IndicatorRule]`, same spirit as
  ingestion/detector.py's ADAPTER_CLASSES and ingestion/event_bus.py's
  worker registry: a registry of named, versioned, pluggable things.
  Adding an indicator family means registering more rules, not touching
  the engine.
* `TriggeredIndicator` — one fired indicator, carrying everything the
  audit trail and the UI need (rule id/version, input facts, effective
  configuration, scope applied, classification, severity, period,
  provenance, timestamp).

`evaluate` returns a `RuleOutcome | None` — None means "this rule found no
pattern", which is not an error and not something the UI shows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from storage.db_types import DBConnection
from storage.fact_store import FactStore

# ------------------------------------------------------------------
# Vocabularies
# ------------------------------------------------------------------

#: The three V1 classifications. RED is deliberately unused — reserved for a
#: future "critical" classification (spec section 2), so nothing here has to
#: be renamed when that lands. The text label always ships alongside the
#: color in the UI (accessibility: color is never the only signal).
POSITIVE = "positive"
OBSERVATION = "observation"
WARNING = "warning"
CLASSIFICATIONS: tuple[str, ...] = (POSITIVE, OBSERVATION, WARNING)

CLASSIFICATION_LABELS = {POSITIVE: "Positive", OBSERVATION: "Observation", WARNING: "Warning"}

SEVERITIES: tuple[str, ...] = ("low", "medium", "high")

#: Global -> Sector -> Company, least to most specific. The order of this
#: tuple IS the precedence order used by indicators/config.py — resolution
#: walks it and the last applicable value wins.
SCOPE_TYPES: tuple[str, ...] = ("global", "sector", "company")

#: The system default, the level below every user scope. Recorded in the
#: audit trail as the scope that supplied a field nobody overrode.
SYSTEM_SCOPE = "system"


class InvalidIndicatorConfigError(ValueError):
    """Raised when a caller supplies a classification/scope/threshold that
    isn't part of this framework's fixed vocabulary."""


# ------------------------------------------------------------------
# Rule definition
# ------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdSpec:
    """One configurable number on a rule. `key` is what the user's override
    JSON is keyed by, `default` is the system default, and min/max bound
    what the Settings form will accept — a rule declares its own sane range
    rather than the form guessing one."""

    key: str
    label: str
    default: float
    unit: str  # "pp" (percentage points), "%", "x", ...
    minimum: float = 0.0
    maximum: float = 1000.0


@dataclass(frozen=True)
class RuleOutcome:
    """What a rule's `evaluate` returns when it finds its pattern. The rule
    supplies the *facts* and the *severity*; the engine supplies the
    classification (from the effective configuration) and the audit
    metadata — a rule never decides its own final classification, so a
    user's override can never be silently ignored by a rule."""

    explanation: str
    facts: Mapping[str, Any]
    period_label: str | None = None
    provenance: str | None = None
    severity: str | None = None  # None -> the rule's default_severity


class RuleEvaluator(Protocol):
    def __call__(
        self,
        conn: DBConnection,
        company_id: str,
        *,
        thresholds: Mapping[str, float],
        fact_store: FactStore,
    ) -> RuleOutcome | None: ...


@dataclass(frozen=True)
class IndicatorRule:
    """A system rule. Immutable at runtime: user configuration lives in a
    separate table and is applied on top by indicators/config.py, so
    nothing a user does mutates or duplicates this object.

    `version` is bumped by hand in indicators/rules.py whenever trigger
    logic changes — the audit trail stores it per triggered indicator, so
    "same facts + same rule version + same configuration -> same result"
    stays checkable after a logic change (same reasoning as
    ingestion/event_bus.py's WORKER_VERSION).
    """

    rule_id: str
    name: str
    family: str  # "shareholding", "financial_trajectory", ... (spec section 10)
    description: str
    version: str
    required_facts: tuple[str, ...]
    default_classification: str
    default_severity: str
    thresholds: tuple[ThresholdSpec, ...]
    evaluate: RuleEvaluator
    #: Scopes this rule may be configured at. Every V1 rule is company-
    #: evaluated and configurable at all three levels; the field exists so a
    #: future sector-only or global-only rule doesn't need a framework change.
    applicable_scopes: tuple[str, ...] = SCOPE_TYPES
    enabled_by_default: bool = True

    def default_thresholds(self) -> dict[str, float]:
        return {spec.key: spec.default for spec in self.thresholds}

    def threshold_spec(self, key: str) -> ThresholdSpec | None:
        return next((spec for spec in self.thresholds if spec.key == key), None)


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

RULE_REGISTRY: dict[str, IndicatorRule] = {}


def register_rule(rule: IndicatorRule) -> IndicatorRule:
    """Register a system rule. Duplicate ids are a programming error, not a
    silent last-one-wins — two rules sharing an id would make every audit
    row ambiguous about which logic produced it."""
    if rule.default_classification not in CLASSIFICATIONS:
        raise InvalidIndicatorConfigError(
            f"{rule.rule_id}: default_classification must be one of {CLASSIFICATIONS}"
        )
    if rule.default_severity not in SEVERITIES:
        raise InvalidIndicatorConfigError(f"{rule.rule_id}: default_severity must be one of {SEVERITIES}")
    if rule.rule_id in RULE_REGISTRY:
        raise InvalidIndicatorConfigError(f"A rule is already registered with rule_id={rule.rule_id!r}")
    RULE_REGISTRY[rule.rule_id] = rule
    return rule


def get_rule(rule_id: str) -> IndicatorRule | None:
    _ensure_rules_loaded()
    return RULE_REGISTRY.get(rule_id)


def list_rules(*, family: str | None = None, search: str | None = None) -> list[IndicatorRule]:
    """Every registered rule, ordered by family then name — the Settings
    page's browse/search list. `search` is a plain case-insensitive
    substring match over id/name/description (no ranking; the catalog is
    small enough that anything cleverer would be noise)."""
    _ensure_rules_loaded()
    rules = sorted(RULE_REGISTRY.values(), key=lambda r: (r.family, r.name))
    if family:
        rules = [r for r in rules if r.family == family]
    if search:
        needle = search.strip().lower()
        rules = [
            r for r in rules
            if needle in r.rule_id.lower() or needle in r.name.lower() or needle in r.description.lower()
        ]
    return rules


def list_families() -> list[str]:
    _ensure_rules_loaded()
    return sorted({rule.family for rule in RULE_REGISTRY.values()})


def _ensure_rules_loaded() -> None:
    """Importing indicators.rules is what populates the registry. Doing it
    lazily here (rather than from this module's top level) keeps
    framework.py importable by rules.py without a circular import, and means
    a caller only ever needs `from indicators.framework import list_rules`."""
    if not RULE_REGISTRY:
        import indicators.rules  # noqa: F401  (import side effect: registration)


# ------------------------------------------------------------------
# Effective configuration + triggered indicator
# ------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveConfig:
    """System rule + applicable configuration -> effective rule (spec
    section 4). `sources` records which scope supplied each field — that's
    what makes "overridden" visible in Settings and reproducible in the
    audit trail, without duplicating the rule to represent an override.
    """

    rule_id: str
    enabled: bool
    classification: str
    thresholds: Mapping[str, float]
    #: field name -> scope label ("system", "global", "sector:Banks",
    #: "company:HDFCBANK"). Threshold fields are keyed "threshold:<key>".
    sources: Mapping[str, str]

    @property
    def most_specific_scope(self) -> str:
        """The most specific scope that contributed *anything*, for the audit
        row's `scope_applied`. Plain system defaults report "system"."""
        ranked = {SYSTEM_SCOPE: 0, "global": 1, "sector": 2, "company": 3}
        best, best_rank = SYSTEM_SCOPE, 0
        for label in self.sources.values():
            rank = ranked.get(label.split(":", 1)[0], 0)
            if rank > best_rank:
                best, best_rank = label, rank
        return best

    @property
    def is_overridden(self) -> bool:
        return any(label != SYSTEM_SCOPE for label in self.sources.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "enabled": self.enabled,
            "classification": self.classification,
            "thresholds": dict(self.thresholds),
            "sources": dict(self.sources),
        }


@dataclass(frozen=True)
class TriggeredIndicator:
    """One fired indicator, fully self-describing: what happened
    (explanation + facts), what rule fired (rule_id/name/version), and why
    it carries this classification (effective config + scope applied)."""

    rule_id: str
    rule_name: str
    rule_version: str
    family: str
    classification: str
    severity: str
    explanation: str
    facts: Mapping[str, Any]
    effective_config: EffectiveConfig
    scope_applied: str
    evaluated_at: str
    period_label: str | None = None
    provenance: str | None = None
    #: Set by the evaluation engine once the audit row is written; None when
    #: the identical result was already on record (nothing new to append).
    evaluation_id: int | None = None
    threshold_summary: str = ""

    @property
    def classification_label(self) -> str:
        return CLASSIFICATION_LABELS[self.classification]

    def result_hash(self) -> str:
        return result_hash(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            facts=self.facts,
            effective_config=self.effective_config,
            classification=self.classification,
            severity=self.severity,
        )


def result_hash(
    *, rule_id: str, rule_version: str, facts: Mapping[str, Any],
    effective_config: EffectiveConfig, classification: str, severity: str,
) -> str:
    """Deterministic fingerprint of "this exact indicator, on these exact
    facts, under this exact configuration". Same inputs -> same hash, which
    is what lets the audit trail stay append-only without re-recording an
    unchanged result on every page view."""
    payload = json.dumps(
        {
            "rule_id": rule_id,
            "rule_version": rule_version,
            "facts": _jsonable(facts),
            "config": effective_config.as_dict(),
            "classification": classification,
            "severity": severity,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonable(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Floats are rounded to 6dp before hashing/persisting so a value that
    round-trips through SQLite with a different last binary digit doesn't
    read as a changed indicator."""
    out: dict[str, Any] = {}
    for key, value in facts.items():
        out[key] = round(value, 6) if isinstance(value, float) else value
    return out


def facts_json(facts: Mapping[str, Any]) -> str:
    return json.dumps(_jsonable(facts), sort_keys=True, default=str)
