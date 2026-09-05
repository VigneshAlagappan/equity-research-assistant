"""Reuse-before-recompute for full Signals investigations
(research/signals_report.py). Search generated_reports for a prior report on
the exact same companies + statement type, asked in essentially the same
question, and still fresh relative to the underlying financial/document data
— if one qualifies, hand it back instead of spending a fresh LLM call on
(functionally) the same question (README §3, §4).

Retrieval here is deterministic and re-derives byte-identical evidence every
time it runs (README: Retrieval Architecture) — so the recompute this module
actually saves is the LLM call itself, not the evidence gathering.

Two independent "same question" signals, fused the same way
retrieval/hybrid_search.py fuses FTS5 + semantic for documents (neither
replaces the other, either firing is enough) — NOT a straight embedding
swap, for a reason specific to this module: word-overlap (Jaccard) was
"deliberately conservative... a false miss just costs a normal LLM call, but
a false hit would hand back a wrong-shaped answer." Embedding similarity
alone would make that worse, not better — empirically, against this app's
actual local embedding model (all-MiniLM-L6-v2), "Net profit in Q1 FY24?"
vs "Net profit in Q1 FY25?" scores 0.968 cosine similarity, indistinguishable
from a genuine paraphrase, despite being a different fiscal period entirely.
So:

  - `_similarity()` (Jaccard) — cheap, exact-token, still catches
    near-identical rephrasing on its own.
  - `_cosine_similarity()` (over EmbeddingProvider vectors,
    retrieval/embedding_provider.py — the same abstraction document chunks
    use) — catches genuine paraphrases Jaccard misses entirely (e.g. "What
    drove the change in net profit?" vs "How did net profit change?" scores
    0.33 Jaccard but 0.966 cosine).
  - `_period_hint_conflicts()` — a HARD gate, not a similarity signal: if
    both questions name a fiscal year/quarter and they differ, reuse is
    blocked regardless of how similar either score says they are. This is
    load-bearing, not a nicety — it's the only thing that catches the
    FY24-vs-FY25 case above; neither similarity signal reliably does on its
    own with this model.

SEMANTIC_THRESHOLD (0.92) is set high on purpose, calibrated against this
model's real output, not guessed: several genuinely-different questions on
the same topic ("Why did margins decline?" vs "Will margins recover next
quarter?", 0.710; "outlook for deposit growth" vs "outlook for loan growth",
0.655) scored uncomfortably close to genuine paraphrases (0.596-0.888) — the
true/false bands overlap for short financial questions on this model, so the
threshold sits above where any observed false-paraphrase pair scored, at the
cost of also missing some genuine paraphrases in that same range. Narrower,
safer coverage over Jaccard alone, not a general-purpose semantic matcher.
"""

from __future__ import annotations

import math
import re
from storage.db_types import DBConnection
from dataclasses import dataclass

from research.documents import _extract_period_hint
from retrieval.embedding_provider import EmbeddingProvider, EmbeddingProviderUnavailable, default_embedding_provider
from storage.fact_store import FactStore, default_fact_store

# Conservative on purpose (README §23: aggressive reuse that damages answer
# quality is a failure) — only reuse when the new question overlaps at least
# this much with the one a prior report actually answered.
SIMILARITY_THRESHOLD = 0.8

#: See module docstring — empirically calibrated against real cosine scores
#: from this app's default local embedding model, not guessed. Sits above
#: every observed false-paraphrase pair, below the tightest genuine ones.
SEMANTIC_SIMILARITY_THRESHOLD = 0.92

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _similarity(a: str, b: str) -> float:
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_question(
    embedding_provider: EmbeddingProvider | None, question: str
) -> tuple[EmbeddingProvider | None, list[float] | None]:
    """Resolves a provider (the given one, or default_embedding_provider())
    and embeds `question` in one step — EmbeddingProviderUnavailable can come
    from either (retrieval/embedding_provider_voyage.py raises it straight
    from __init__ on a missing key, same as embed_text() failing later), and
    both mean the same thing here: (None, None), caller degrades to
    word-overlap-only. Same "catch at the orchestrating call site, not a
    buried helper" shape retrieval/hybrid_search.py uses around
    search_documents_semantic_timed()."""
    try:
        provider = embedding_provider or default_embedding_provider()
        return provider, provider.embed_text(question)
    except EmbeddingProviderUnavailable:
        return None, None


def _period_hint_conflicts(question_a: str, question_b: str) -> bool:
    """True only when BOTH questions name a fiscal year/quarter and they
    disagree — a hard block regardless of either similarity score (see
    module docstring for why this can't be left to the similarity signals
    alone). Silent when either question doesn't mention a period at all, so
    a generic question's reuse behavior is unchanged."""
    fy_a, q_a = _extract_period_hint(question_a)
    fy_b, q_b = _extract_period_hint(question_b)
    if fy_a is not None and fy_b is not None and fy_a != fy_b:
        return True
    if q_a is not None and q_b is not None and q_a != q_b:
        return True
    return False


@dataclass(frozen=True)
class ReuseCandidate:
    thread_id: str
    report_markdown: str
    evidence: list[dict]
    followups: list[str]
    similarity: float
    generated_at: str
    #: "jaccard" | "semantic" — which signal actually cleared its threshold;
    #: observability only, never branched on downstream.
    match_kind: str = "jaccard"


def find_reusable_report(
    conn: DBConnection,
    question: str,
    company_ids: list[str],
    statement_type: str | None,
    *,
    fact_store: FactStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ReuseCandidate | None:
    """The freshest prior Signals report on these exact companies/statement
    type, asked in near-enough the same question, whose underlying data
    hasn't changed since it was generated — or None if nothing qualifies. An
    unrelated question about the same companies correctly returns None.

    `embedding_provider` defaults to `default_embedding_provider()` — pass a
    fake for tests, or omit the semantic layer entirely by whatever means
    makes it raise/return nothing (word-overlap alone still works, same as
    before this layer existed). A provider being unavailable degrades this
    call, never fails it."""
    fs = fact_store or default_fact_store()
    target_companies = sorted(company_ids)
    latest_data_at = fs.get_latest_data_timestamp(conn, company_ids)
    provider, question_embedding = _embed_question(embedding_provider, question)

    best: ReuseCandidate | None = None
    for report in fs.list_generated_reports(conn):
        if sorted(report["company_ids"]) != target_companies:
            continue
        if report["statement_type"] != statement_type:
            continue
        if latest_data_at is not None and report["generated_at"] < latest_data_at:
            continue  # underlying data changed since this report was generated — stale
        if _period_hint_conflicts(question, report["question"]):
            continue  # hard gate — see module docstring; never overridden by a similarity score

        # Each signal is judged against its OWN threshold independently, and
        # either clearing its bar is enough (same "either method finding it
        # is enough" fusion retrieval/hybrid_search.py uses for documents) —
        # NOT "take whichever raw score is higher, then check its
        # threshold": a Jaccard=0.85/semantic=0.87 case must still qualify
        # via Jaccard even though 0.87 alone would fail the semantic bar.
        jaccard_similarity = _similarity(question, report["question"])
        jaccard_passes = jaccard_similarity >= SIMILARITY_THRESHOLD

        # .get(), not [...] — a FactStore is an injectable interface (test
        # doubles, a future non-SQLite implementation) and mustn't be
        # required to populate columns that only exist to support this one
        # capability; a report with neither key just never qualifies via the
        # semantic signal, same as one saved before this layer existed.
        semantic_similarity = None
        report_embedding = report.get("question_embedding")
        if (
            question_embedding is not None
            and report_embedding is not None
            and report.get("question_embedding_model") == provider.model_id
        ):
            semantic_similarity = _cosine_similarity(question_embedding, report_embedding)
        semantic_passes = semantic_similarity is not None and semantic_similarity >= SEMANTIC_SIMILARITY_THRESHOLD

        if not (jaccard_passes or semantic_passes):
            continue
        if semantic_passes and (not jaccard_passes or semantic_similarity > jaccard_similarity):
            similarity, match_kind = semantic_similarity, "semantic"
        else:
            similarity, match_kind = jaccard_similarity, "jaccard"

        if best is None or report["generated_at"] > best.generated_at:
            best = ReuseCandidate(
                thread_id=report["thread_id"],
                report_markdown=report["report_markdown"],
                evidence=fs.list_report_evidence(conn, report["thread_id"]),
                followups=fs.list_report_followups(conn, report["thread_id"]),
                similarity=similarity,
                generated_at=report["generated_at"],
                match_kind=match_kind,
            )
    return best
