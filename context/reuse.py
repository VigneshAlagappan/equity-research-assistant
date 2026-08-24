"""Reuse-before-recompute for full Signals investigations
(research/signals_report.py). Search generated_reports for a prior report on
the exact same companies + statement type, asked in essentially the same
words, and still fresh relative to the underlying financial/document data —
if one qualifies, hand it back instead of spending a fresh LLM call on
(functionally) the same question (README §3, §4).

Retrieval here is deterministic and re-derives byte-identical evidence every
time it runs (README: Retrieval Architecture) — so the recompute this module
actually saves is the LLM call itself, not the evidence gathering. No
embeddings: "same question" is approximated with word-overlap (Jaccard)
similarity, deliberately conservative — a false miss just costs a normal LLM
call, but a false hit would hand back a wrong-shaped answer.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from storage.repositories import (
    get_latest_data_timestamp,
    list_generated_reports,
    list_report_evidence,
    list_report_followups,
)

# Conservative on purpose (README §23: aggressive reuse that damages answer
# quality is a failure) — only reuse when the new question overlaps at least
# this much with the one a prior report actually answered.
SIMILARITY_THRESHOLD = 0.8

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _similarity(a: str, b: str) -> float:
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


@dataclass(frozen=True)
class ReuseCandidate:
    thread_id: str
    report_markdown: str
    evidence: list[dict]
    followups: list[str]
    similarity: float
    generated_at: str


def find_reusable_report(
    conn: sqlite3.Connection,
    question: str,
    company_ids: list[str],
    statement_type: str | None,
) -> ReuseCandidate | None:
    """The freshest prior Signals report on these exact companies/statement
    type, asked in near-enough the same words, whose underlying data hasn't
    changed since it was generated — or None if nothing qualifies. An
    unrelated question about the same companies correctly returns None."""
    target_companies = sorted(company_ids)
    latest_data_at = get_latest_data_timestamp(conn, company_ids)

    best: ReuseCandidate | None = None
    for report in list_generated_reports(conn):
        if sorted(report["company_ids"]) != target_companies:
            continue
        if report["statement_type"] != statement_type:
            continue
        if latest_data_at is not None and report["generated_at"] < latest_data_at:
            continue  # underlying data changed since this report was generated — stale
        similarity = _similarity(question, report["question"])
        if similarity < SIMILARITY_THRESHOLD:
            continue
        if best is None or report["generated_at"] > best.generated_at:
            best = ReuseCandidate(
                thread_id=report["thread_id"],
                report_markdown=report["report_markdown"],
                evidence=list_report_evidence(conn, report["thread_id"]),
                followups=list_report_followups(conn, report["thread_id"]),
                similarity=similarity,
                generated_at=report["generated_at"],
            )
    return best
