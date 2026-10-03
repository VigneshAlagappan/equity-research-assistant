"""Pure validation/normalization for causal graph writes. No I/O: everything
the service accepts passes through here first, so an LLM-shaped payload can
never put an unknown relationship type, an out-of-range confidence or an
unattributed edge into the graph."""

from __future__ import annotations

import hashlib
import json
import re

from config import causal_graph as cg


class CausalGraphError(ValueError):
    """Base class: a rejected write. Subclasses name the reason."""


class ValidationError(CausalGraphError):
    pass


class DuplicateNodeError(CausalGraphError):
    def __init__(self, node_id: str):
        super().__init__(f"node already exists: {node_id}")
        self.node_id = node_id


class DuplicateRelationshipError(CausalGraphError):
    def __init__(self, edge_id: str):
        super().__init__(f"equivalent relationship already exists: {edge_id}")
        self.edge_id = edge_id


class NotFoundError(CausalGraphError):
    pass


class TransitionError(CausalGraphError):
    pass


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")


def node_id_for(family: str, canonical_name: str) -> str:
    if family == "Company":
        return canonical_name  # a company's id IS companies.company_id
    return f"{re.sub(r'(?<!^)(?=[A-Z])', '_', family).lower()}:{canonical_name}"


def edge_id_for(source_id: str, rel_type: str, target_id: str) -> str:
    digest = hashlib.sha1(f"{source_id}|{rel_type}|{target_id}".encode()).hexdigest()[:16]
    return f"e_{digest}"


# --- nodes ---------------------------------------------------------------------

def validate_node(family: str, display_name: str, *, canonical_name: str | None = None,
                  description: str | None = None, references: dict | None = None) -> dict:
    if family not in cg.NODE_FAMILIES:
        raise ValidationError(f"unknown node family {family!r}; allowed: {', '.join(cg.NODE_FAMILIES)}")
    display_name = (display_name or "").strip()
    if not display_name:
        raise ValidationError("display_name is required")
    canonical = canonical_name.strip() if (family == "Company" and canonical_name) else slugify(canonical_name or display_name)
    if not canonical:
        raise ValidationError("canonical_name is empty after normalization")
    refs = references or {}
    allowed = cg.NODE_REFERENCE_FIELDS[family]
    unknown = set(refs) - set(allowed)
    if unknown:
        raise ValidationError(f"{family} does not take reference field(s) {sorted(unknown)}; allowed: {list(allowed)}")
    for key, value in refs.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"reference {key} must be a non-empty string")
    return {
        "id": node_id_for(family, canonical), "family": family, "canonical_name": canonical,
        "display_name": display_name, "description": (description or "").strip() or None,
        "references": {k: v.strip() for k, v in refs.items()},
    }


# --- relationship type ---------------------------------------------------------

def normalize_relationship_type(raw: str) -> str:
    key = re.sub(r"[\s-]+", "_", (raw or "").strip()).upper()
    if key in cg.RELATIONSHIP_TYPES:
        return key
    mapped = cg.RELATIONSHIP_ALIASES.get(key)
    if mapped:
        return mapped
    raise ValidationError(f"unknown relationship type {raw!r}; canonical types: {', '.join(cg.RELATIONSHIP_TYPES)}")


# --- edge knowledge ------------------------------------------------------------

def validate_confidence(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("confidence must be a number between 0 and 1")
    if not 0.0 <= float(value) <= 1.0:
        raise ValidationError(f"confidence {value} is outside 0..1")
    return round(float(value), 4)


def validate_effect_strength(value) -> str:
    v = (value or "").strip().upper() if isinstance(value, str) else ""
    if v not in cg.EFFECT_STRENGTHS:
        raise ValidationError(f"effect_strength must be one of {cg.EFFECT_STRENGTHS}, got {value!r}")
    return v


def validate_lag(lag: dict | None) -> dict:
    """{"min": 1, "max": 2, "unit": "quarters"} or None (unknown lag)."""
    if not lag:
        return {"lag_min": None, "lag_max": None, "lag_unit": None}
    unit = lag.get("unit")
    lo, hi = lag.get("min"), lag.get("max")
    if unit not in cg.LAG_UNITS:
        raise ValidationError(f"lag unit must be one of {cg.LAG_UNITS}, got {unit!r}")
    for name, v in (("min", lo), ("max", hi)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValidationError(f"lag {name} must be a number >= 0, got {v!r}")
    if lo > hi:
        raise ValidationError(f"lag min {lo} exceeds max {hi}")
    return {"lag_min": float(lo), "lag_max": float(hi), "lag_unit": unit}


def validate_scope(scope: dict | None) -> dict:
    if scope is None:
        return {}
    if not isinstance(scope, dict):
        raise ValidationError("scope must be an object")
    unknown = set(scope) - set(cg.SCOPE_KEYS)
    if unknown:
        raise ValidationError(f"unknown scope key(s) {sorted(unknown)}; allowed: {list(cg.SCOPE_KEYS)}")
    clean = {}
    for key, value in scope.items():
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ValidationError(f"scope {key} must be a string")
        clean[key] = value.strip()
    return clean


def validate_direction(rel_type: str, direction: str | None) -> str:
    if rel_type in cg.STRUCTURAL_TYPES:
        if direction not in (None, cg.STRUCTURAL_DIRECTION):
            raise ValidationError(f"{rel_type} is structural; direction must be omitted")
        return cg.STRUCTURAL_DIRECTION
    d = (direction or "").strip().upper() if isinstance(direction, str) else ""
    if d not in cg.DIRECTIONS:
        raise ValidationError(f"direction must be one of {cg.DIRECTIONS}, got {direction!r}")
    implied = cg.IMPLIED_DIRECTION.get(rel_type)
    if implied and d != implied:
        raise ValidationError(f"{rel_type} implies direction {implied}, got {d}")
    return d


def validate_provenance(provenance: dict | None) -> dict:
    if not provenance:
        raise ValidationError("provenance is required")
    ptype, ref = provenance.get("type"), (provenance.get("ref") or "").strip()
    if ptype not in cg.PROVENANCE_TYPES:
        raise ValidationError(f"provenance type must be one of {cg.PROVENANCE_TYPES}, got {ptype!r}")
    if not ref:
        raise ValidationError("provenance ref is required (a seed id, investigation id, source key, ...)")
    out = {"type": ptype, "ref": ref}
    note = (provenance.get("note") or "").strip()
    if note:
        out["note"] = note[: cg.MAX_NOTE_CHARS]
    return out


def validate_endpoints(rel_type: str, source_family: str, target_family: str) -> None:
    sources, targets = cg.ENDPOINTS[rel_type]
    if source_family not in sources:
        raise ValidationError(f"{rel_type} cannot start at a {source_family}; allowed: {sorted(sources)}")
    if target_family not in targets:
        raise ValidationError(f"{rel_type} cannot end at a {target_family}; allowed: {sorted(targets)}")


def validate_actor(actor_kind: str) -> str:
    if actor_kind not in cg.ACTORS:
        raise ValidationError(f"actor kind must be one of {cg.ACTORS}, got {actor_kind!r}")
    return actor_kind


def validate_edge_fields(rel_type: str, *, direction, mechanism, confidence, effect_strength, lag, scope,
                         provenance) -> dict:
    """Everything an edge carries, validated. Structural edges (BELONGS_TO)
    take defaults for the causal-only fields."""
    structural = rel_type in cg.STRUCTURAL_TYPES
    mech = (mechanism or "").strip()
    if not structural and not mech:
        raise ValidationError("mechanism is required: say HOW the source affects the target")
    return {
        "direction": validate_direction(rel_type, direction),
        "mechanism": mech or None,
        "confidence": 1.0 if structural and confidence is None else validate_confidence(confidence),
        "effect_strength": None if structural and effect_strength is None else validate_effect_strength(effect_strength),
        **validate_lag(lag),
        "scope": validate_scope(scope),
        "provenance": validate_provenance(provenance),
    }


def scope_json(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True)
