"""Hardness Evaluator — how difficult is the remaining reasoning task, before
a model is chosen for it. Shared by all three LLM call sites
(research/assistant.py, research/insights.py, research/signals_report.py) so
model-tier logic lives in one inspectable place instead of being reinvented,
or skipped, per module.

Deterministic and keyword-based, not an LLM classifier call — a classifier
round-trip would cost more than the tokens it's trying to save. Generalizes
the peer-comparison / deep-analysis / quick-lookup heuristic that used to
live only inside research/assistant.py's private _select_model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from config.settings import TIER_MIN_REASONING_STRENGTH


class Tier(Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


# Tier -> complexity level, for observability (a stand-in for the prompt's
# LEVEL 0-5 vocabulary — this app only ever needs three practical buckets).
TIER_LEVEL = {Tier.QUICK: 2, Tier.STANDARD: 3, Tier.DEEP: 5}

# TIER_MIN_REASONING_STRENGTH (imported above, config/settings.py): tier ->
# minimum ModelSpec.reasoning_strength a fallback candidate must have to be
# offered this task at all (llm/router.py) — never blindly push a DEEP task
# onto a model too weak for it, even as a last resort. String-keyed there
# (routing policy lives in one settings file, not scattered enum-keyed
# dicts) — HardnessResult.min_reasoning_strength below looks it up by
# `self.tier.value`.

# Signals a straight factual lookup ("what was X", "how much did Y") that
# doesn't need cross-referencing or judgment.
_QUICK_LOOKUP_RE = re.compile(
    r"\b(what (is|was|are|were)|how much|current|latest|value of|figure for)\b", re.IGNORECASE
)
# Signals multi-step reasoning, comparison, or judgment — worth the strongest
# model regardless of how much evidence backs it.
_DEEP_ANALYSIS_RE = re.compile(
    r"\b(why|compare|comparison|versus|vs\.?|trend|outlook|risk|sustainable|red flag|"
    r"explain|reason|driver|hypothesis|guidance|consistent|discrepancy)\b",
    re.IGNORECASE,
)
_LARGE_EVIDENCE_THRESHOLD = 40
_SHORT_QUESTION_WORDS = 12
_SMALL_EVIDENCE_THRESHOLD = 15


@dataclass(frozen=True)
class HardnessResult:
    tier: Tier
    level: int
    reason: str

    @property
    def min_reasoning_strength(self) -> int:
        return TIER_MIN_REASONING_STRENGTH[self.tier.value]


def classify(question: str, company_ids: list[str], evidence_count: int) -> HardnessResult:
    """Classify one research question into a Tier. A peer-comparison question
    (>1 company) always gets DEEP — cross-referencing several companies'
    evidence is inherently the hardest case. For a single company, a question
    that reads like multi-step reasoning/comparison, or that's grounded in a
    lot of evidence, also goes to DEEP; a short, plain factual lookup with
    little evidence goes to QUICK; everything else gets STANDARD."""
    if len(company_ids) > 1:
        return HardnessResult(Tier.DEEP, TIER_LEVEL[Tier.DEEP], "peer comparison across multiple companies")
    if _DEEP_ANALYSIS_RE.search(question):
        return HardnessResult(Tier.DEEP, TIER_LEVEL[Tier.DEEP], "question reads as multi-step reasoning/comparison")
    if evidence_count > _LARGE_EVIDENCE_THRESHOLD:
        return HardnessResult(Tier.DEEP, TIER_LEVEL[Tier.DEEP], f"grounded in {evidence_count} evidence lines")
    if (
        _QUICK_LOOKUP_RE.search(question)
        and evidence_count <= _SMALL_EVIDENCE_THRESHOLD
        and len(question.split()) <= _SHORT_QUESTION_WORDS
    ):
        return HardnessResult(Tier.QUICK, TIER_LEVEL[Tier.QUICK], "short factual lookup, little evidence")
    return HardnessResult(Tier.STANDARD, TIER_LEVEL[Tier.STANDARD], "general question, default tier")


def fixed(tier: Tier, reason: str) -> HardnessResult:
    """A HardnessResult for call sites with no free-text question to classify
    (research/insights.py summarizes a report; research/signals_report.py is
    pinned to a fixed quality tier today). Only feeds observability logging —
    a pinned_model route (llm/router.py) never consults min_reasoning_strength."""
    return HardnessResult(tier, TIER_LEVEL[tier], reason)
