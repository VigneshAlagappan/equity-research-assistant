"""Context Optimizer — "what information does this task actually need?"
Deliberately separate from the Model Router (llm/router.py — "which model
should reason over it?"): this module only ever touches the Evidence list,
never a model or provider.

Retrieval itself (retrieval/structured_search.py, research/documents.py)
already builds a compact, structured Evidence list, not raw prose — and it's
a deterministic Python/SQL read, cheap to redo, not an expensive step worth
caching. What this module cuts is prompt tokens: a peer-comparison or
multi-year evidence block that would run past this tier's budget gets
trimmed to its highest-value lines instead of being sent in full. Runs in
three cheap, inspectable steps:

    dedupe -> score each line's value -> keep the highest-value lines that
    fit this tier's token budget, dropping the rest (only when actually over
    budget — nothing is trimmed otherwise, since aggressive compression that
    damages answer quality is worse than the tokens it saves).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from llm.hardness import Tier
from research.evidence import Evidence

# Rough chars-per-token heuristic (~4 chars/token for English) — good enough
# to budget against without a real tokenizer or an extra dependency.
_CHARS_PER_TOKEN = 4

# Evidence token budget per hardness tier — a deliberate cost-control cap
# (README §6, Token Budgeting), not a context-window safety margin: every
# model in llm/capability_registry.py has a window far larger than any
# evidence block this app produces. Only kicks in when evidence actually
# exceeds it.
TIER_EVIDENCE_TOKEN_BUDGET = {
    Tier.QUICK: 2_000,
    Tier.STANDARD: 6_000,
    Tier.DEEP: 20_000,
}

# Evidence kind -> confidence weight for scoring (README §7, Context Value
# Scoring): FACT/CALCULATION are deterministic ground truth; MANAGEMENT_STATEMENT
# is a company's own framing, lower trust per the Signals system prompt;
# INFERENCE never appears in retrieved evidence today but is scored low if it
# ever does.
_KIND_CONFIDENCE = {"FACT": 1.0, "CALCULATION": 1.0, "MANAGEMENT_STATEMENT": 0.7, "INFERENCE": 0.5}

_WORD_RE = re.compile(r"[a-z0-9]+")
_FISCAL_YEAR_RE = re.compile(r"FY(\d{4})")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _token_estimate(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _fiscal_year_of(evidence: Evidence) -> int | None:
    match = _FISCAL_YEAR_RE.search(evidence.label)
    return int(match.group(1)) if match else None


def _relevance(question_words: set[str], evidence: Evidence) -> float:
    """1.0 baseline, +1.0 per word the question shares with this evidence
    line's label (metric name, fiscal year, ratio name). A question that
    names nothing specific — the common case — leaves every line at the same
    baseline, so nothing is penalized just for not being explicitly named."""
    return 1.0 + len(question_words & _words(evidence.label))


def _freshness(evidence: Evidence, latest_fiscal_year: int | None) -> float:
    """1.0 for the most recent fiscal year in this evidence set, decaying
    0.1/year back, floored at 0.3. Evidence with no fiscal year in its label
    (e.g. a document excerpt) is left at 1.0 — nothing to date it against."""
    year = _fiscal_year_of(evidence)
    if year is None or latest_fiscal_year is None:
        return 1.0
    return max(0.3, 1.0 - 0.1 * (latest_fiscal_year - year))


@dataclass(frozen=True)
class ScoredEvidence:
    """One Evidence line's inspectable value breakdown (README §7: "keep this
    scoring inspectable so I can learn why certain context was selected")."""

    evidence: Evidence
    relevance: float
    freshness: float
    confidence: float
    token_cost: int
    score: float


@dataclass(frozen=True)
class OptimizedContext:
    evidence: list[Evidence]
    dropped: list[ScoredEvidence] = field(default_factory=list)
    total_tokens_before: int = 0
    total_tokens_after: int = 0
    budget: int = 0

    @property
    def tokens_saved(self) -> int:
        return self.total_tokens_before - self.total_tokens_after


def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    """Drop exact-duplicate lines (same kind/company/label/value) — a guard
    against any retrieval path that ends up contributing the same fact
    twice, not something today's retrieval is known to do on its own."""
    seen: set[tuple[str, str, str, str]] = set()
    result = []
    for e in evidence:
        key = (e.kind, e.company_id, e.label, e.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(e)
    return result


def _score_all(question: str, evidence: list[Evidence]) -> list[ScoredEvidence]:
    question_words = _words(question)
    fiscal_years = [y for e in evidence if (y := _fiscal_year_of(e)) is not None]
    latest_fiscal_year = max(fiscal_years) if fiscal_years else None

    scored = []
    for e in evidence:
        relevance = _relevance(question_words, e)
        freshness = _freshness(e, latest_fiscal_year)
        confidence = _KIND_CONFIDENCE.get(e.kind, 0.8)
        token_cost = _token_estimate(e.as_prompt_line())
        score = (relevance * freshness * confidence) / token_cost
        scored.append(ScoredEvidence(e, relevance, freshness, confidence, token_cost, score))
    return scored


def optimize(question: str, evidence: list[Evidence], tier: Tier) -> OptimizedContext:
    """Dedupe, score, and — only if the deduped set exceeds this tier's token
    budget — keep the highest-value lines up to that budget, in their
    original retrieval order. Always keeps at least one line (the top-scored
    one) even if it alone exceeds the budget, rather than returning an empty
    context."""
    deduped = _dedupe(evidence)
    budget = TIER_EVIDENCE_TOKEN_BUDGET[tier]
    scored = _score_all(question, deduped)
    total_tokens_before = sum(s.token_cost for s in scored)

    if total_tokens_before <= budget:
        return OptimizedContext(
            evidence=[s.evidence for s in scored],
            total_tokens_before=total_tokens_before,
            total_tokens_after=total_tokens_before,
            budget=budget,
        )

    ranked_indices = sorted(range(len(scored)), key=lambda i: scored[i].score, reverse=True)
    kept_indices: set[int] = set()
    running = 0
    for i in ranked_indices:
        cost = scored[i].token_cost
        if running + cost > budget and kept_indices:
            continue
        kept_indices.add(i)
        running += cost

    kept = [scored[i].evidence for i in range(len(scored)) if i in kept_indices]
    dropped = [scored[i] for i in range(len(scored)) if i not in kept_indices]
    return OptimizedContext(
        evidence=kept, dropped=dropped,
        total_tokens_before=total_tokens_before, total_tokens_after=running, budget=budget,
    )
