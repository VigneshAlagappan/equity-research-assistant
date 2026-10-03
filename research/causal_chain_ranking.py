"""Runtime ranking of candidate causal edges and paths for ONE investigation.

Nothing here is written back to the graph. The persistent edge keeps its own
confidence, effect strength, lag and scope as separate facts; this module only
turns them, together with the investigation's context, into a score used to
decide which candidate paths are worth testing. The components stay visible in
the output so a reader (and later feedback) can see why a path ranked where it did:

  confidence   global credibility of the relationship            (persistent)
  relevance    does it apply to THIS company/sector/geography/period (runtime)
  strength     how much the source can move the target           (persistent)
  temporal     can the usual lag show up inside the question's window (runtime)
  evidence     prior supporting evidence, or data we can test it against
  validation   prior validation results on the edge
  feedback     prior structured feedback (a light nudge; it never edits the edge)
  materiality  strength x relevance: is this link big enough to matter here
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

STRENGTH_VALUE = {"LOW": 0.4, "MEDIUM": 0.7, "HIGH": 1.0}
WEIGHTS = {
    "confidence": 0.25, "relevance": 0.20, "strength": 0.15, "temporal": 0.10,
    "evidence": 0.15, "validation": 0.10, "feedback": 0.05,
}
LENGTH_PENALTY = 0.95  # per extra edge: prefer the shorter explanation when scores are close
GLOBAL_RELEVANCE = 0.6  # an edge with no scope is credible everywhere, but not specifically for here
EXPOSURE_BONUS = 0.1  # the edge touches something the company is directly exposed to
NEGATIVE_FEEDBACK = frozenset({"WRONG_RELATIONSHIP", "NOT_RELEVANT", "OVERSTATED"})
POSITIVE_FEEDBACK = frozenset({"CORRECT"})
_MONTHS_PER = {"days": 1 / 30.0, "weeks": 1 / 4.345, "months": 1.0, "quarters": 3.0, "years": 12.0}
_FY_RE = re.compile(r"FY\s?(\d{4})", re.IGNORECASE)


@dataclass(frozen=True)
class Context:
    """What the investigation is about. Everything optional; a missing piece is
    neutral, never a mismatch."""
    company_id: str | None = None
    sector_names: tuple[str, ...] = ()       # display names of the company's sectors
    geography: str | None = None
    period: tuple[int, int] | None = None    # (first FY, last FY)
    exposure_node_ids: frozenset[str] = frozenset()

    @property
    def window_months(self) -> float | None:
        return None if self.period is None else (self.period[1] - self.period[0] + 1) * 12.0


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def lag_months(edge: dict) -> tuple[float, float] | None:
    if edge.get("lag_unit") not in _MONTHS_PER or edge.get("lag_min") is None:
        return None
    k = _MONTHS_PER[edge["lag_unit"]]
    return edge["lag_min"] * k, edge["lag_max"] * k


def _period_overlap(value: str, period: tuple[int, int] | None) -> bool | None:
    years = [int(y) for y in _FY_RE.findall(value)]
    if not years or period is None:
        return None  # unparseable or no window: neutral
    lo, hi = min(years), max(years)
    return lo <= period[1] and hi >= period[0]


def contextual_relevance(edge: dict, ctx: Context) -> float | None:
    """0..1, or None when the edge's scope rules it out for this context."""
    scope = edge.get("scope") or {}
    matched = 0
    for key, value in scope.items():
        if key == "geography":
            if ctx.geography is None:
                continue
            if _norm(value) != _norm(ctx.geography):
                return None
            matched += 1
        elif key in ("sector", "sub_sector"):
            if not ctx.sector_names:
                continue
            if _norm(value) not in {_norm(s) for s in ctx.sector_names}:
                return None
            matched += 1
        elif key == "company_id":
            if ctx.company_id is None:
                continue
            if value != ctx.company_id:
                return None
            matched += 1
        elif key == "period":
            overlap = _period_overlap(value, ctx.period)
            if overlap is False:
                return None
            matched += int(bool(overlap))
        # regime: not testable yet -- compatible, but earns no match credit
    relevance = GLOBAL_RELEVANCE + (1 - GLOBAL_RELEVANCE) * (matched / len(scope)) if scope else GLOBAL_RELEVANCE
    if edge.get("cause_id") in ctx.exposure_node_ids or edge.get("effect_id") in ctx.exposure_node_ids:
        relevance += EXPOSURE_BONUS
    return min(1.0, relevance)


def temporal_fit(edge: dict, ctx: Context) -> float | None:
    """1 when the lag fits inside the window, 0.6 when only part of it does,
    0.8 when the lag or the window is unknown, None when the cause cannot
    have shown up in the window at all."""
    lag, window = lag_months(edge), ctx.window_months
    if lag is None or window is None:
        return 0.8
    if lag[0] > window:
        return None
    return 1.0 if lag[1] <= window else 0.6


def validation_score(edge: dict) -> float:
    results = [r["result"] for r in edge.get("validation_refs") or []]
    if not results:
        return 0.5
    good = results.count("SUPPORT") + 0.5 * results.count("PARTIAL")
    bad = results.count("CONTRADICT")
    return good / (good + bad) if (good + bad) else 0.5


def feedback_score(edge: dict) -> float:
    kinds = [r["feedback_type"] for r in edge.get("feedback_refs") or []]
    pos, neg = sum(k in POSITIVE_FEEDBACK for k in kinds), sum(k in NEGATIVE_FEEDBACK for k in kinds)
    return 0.5 + 0.5 * (pos - neg) / (pos + neg) if (pos + neg) else 0.5


def evidence_availability(edge: dict, measurable: bool) -> float:
    if any(r["stance"] == "SUPPORTS" for r in edge.get("evidence_refs") or []):
        return 1.0
    return 0.6 if measurable else 0.3


@dataclass
class EdgeScore:
    components: dict[str, float]
    score: float
    materiality: float
    relevance: float


def score_edge(edge: dict, ctx: Context, *, measurable: bool) -> EdgeScore | str:
    """EdgeScore, or the reason string the edge cannot be used here."""
    relevance = contextual_relevance(edge, ctx)
    if relevance is None:
        return "scope_mismatch"
    temporal = temporal_fit(edge, ctx)
    if temporal is None:
        return "lag_exceeds_window"
    strength = STRENGTH_VALUE.get(edge.get("effect_strength"), 0.5)
    components = {
        "confidence": edge["confidence"], "relevance": relevance, "strength": strength, "temporal": temporal,
        "evidence": evidence_availability(edge, measurable), "validation": validation_score(edge),
        "feedback": feedback_score(edge),
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items())
    return EdgeScore({k: round(v, 4) for k, v in components.items()}, round(score, 4),
                     round(strength * relevance, 4), round(relevance, 4))


def path_score(edge_scores: list[float]) -> float:
    """Geometric mean (one weak link drags the path down) with a mild preference for shorter paths."""
    if not edge_scores:
        return 0.0
    gm = math.exp(sum(math.log(max(s, 1e-6)) for s in edge_scores) / len(edge_scores))
    return round(gm * LENGTH_PENALTY ** (len(edge_scores) - 1), 4)


def path_lag_months(edges: list[dict]) -> tuple[float, float] | None:
    """Cumulative (min, max) lag along a path, None if no edge states one."""
    lags = [lag_months(e) for e in edges]
    known = [l for l in lags if l]
    if not known:
        return None
    return sum(l[0] for l in known), sum(l[1] for l in known)


def parse_period(text: str | None) -> tuple[int, int] | None:
    years = [int(y) for y in _FY_RE.findall(text or "")]
    return (min(years), max(years)) if years else None
