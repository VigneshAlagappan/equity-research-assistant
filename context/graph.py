"""Knowledge Graph traversal — the cross-company / cross-metric relationship
layer context/reuse.py can't provide. context/reuse.py only matches the
*exact* same company_ids + statement_type; this module surfaces a prior
investigation about a DIFFERENT company when a real relationship connects it
to the current question — same sector, bridged through a domain-knowledge
causal link (config/knowledge_graph_seed.py), e.g. "an HDFC investigation
about NIM is relevant to an ICICI NIM question because they're both private
banks, and separately relevant to an ICICI repo-rate question because repo
rate AFFECTS NIM."

Two backends, same public interface (find_related_investigations): the
default is this module's own pure-Python/SQLite traversal, computed live
from data that already exists (companies.basic_industry for sector peers,
generated_reports/research_thread_evidence for which investigation discussed
which metric) or read from the static, hand-curated seed list — no persisted
graph tables needed, same "retrieval is cheap and deterministic, redo it
every time" philosophy the rest of retrieval/ and context/ already follow.
Setting config.settings.GRAPH_BACKEND="neo4j" switches to the Neo4j-backed
traversal (context/graph_neo4j.py) instead — a real, queryable/visualizable
graph, with automatic fallback back to this module's traversal if Neo4j
isn't reachable.

A graph hit is never Evidence — it's someone else's reasoning about a
different company, not a fact about this one. Callers must render it as a
clearly separate, clearly labeled prompt section and instruct the model to
cite it only as [INFERENCE], never [FACT] — see
research/signals_report.py's SIGNALS_SYSTEM_PROMPT.
"""

from __future__ import annotations

import logging
import re
from storage.db_types import DBConnection
from dataclasses import dataclass

from config.knowledge_graph_seed import KNOWLEDGE_GRAPH_SEED_EDGES
from config.settings import GRAPH_BACKEND
from financials.report import TREND_METRICS, VENDOR_RATIO_METRICS
from storage.fact_store import FactStore, default_fact_store

logger = logging.getLogger(__name__)

# Deterministic, not a benchmark score — same spirit as
# llm/capability_registry.py's reasoning_strength. "Same sector" is a strong
# but not certain signal that a peer's reasoning pattern applies.
SECTOR_PEER_STRENGTH = 0.9
DIRECT_METRIC_MATCH_STRENGTH = 1.0
MAX_CANDIDATES = 3

_METRIC_TITLES = dict(TREND_METRICS + VENDOR_RATIO_METRICS)  # metric_key -> human title
_METRIC_ALIASES = {
    "net_interest_margin": ["nim"],
    "return_on_equity_percent": ["roe"],
    "gross_npa_percent": ["gnpa"],
    "net_npa_percent": ["nnpa"],
    "rbi_repo_rate": ["repo rate", "rbi repo rate", "repo"],
}
# Every term a seed edge names — company metrics (already in _METRIC_TITLES)
# plus macro variables (e.g. "rbi_repo_rate") that aren't in the company
# metric catalog at all. A question naming either side of an AFFECTS edge
# must be able to trigger it, not just the company-metric side.
_SEED_TERMS = {term for edge in KNOWLEDGE_GRAPH_SEED_EDGES for term in (edge[0], edge[2])}
_KNOWN_TERMS = set(_METRIC_TITLES) | _SEED_TERMS


def _keywords_for(term: str) -> set[str]:
    words = {term, term.replace("_", " ")}
    title = _METRIC_TITLES.get(term)
    if title:
        words.add(title.lower())
    words.update(_METRIC_ALIASES.get(term, []))
    return words


def _metrics_mentioned(text: str) -> set[str]:
    """Which known metric_keys/macro-variable terms this text names —
    matched by keyword/title substring, the same lightweight approach
    context/optimizer.py's relevance scoring already uses (no LLM, no
    embeddings)."""
    text_lower = text.lower()
    return {term for term in _KNOWN_TERMS if any(kw in text_lower for kw in _keywords_for(term))}


def _expand_via_seed_edges(metric_keys: set[str]) -> dict[str, float]:
    """metric_key -> best strength connecting it to something in
    metric_keys: 1.0 if it's directly in metric_keys, or a seed edge's
    strength if it's a "driver" metric one AFFECTS hop away (in either
    direction — a question about the cause or the effect should surface
    the other)."""
    expanded: dict[str, float] = {mk: DIRECT_METRIC_MATCH_STRENGTH for mk in metric_keys}
    for source, _relationship, target, strength, _reason in KNOWLEDGE_GRAPH_SEED_EDGES:
        if source in metric_keys:
            expanded[target] = max(expanded.get(target, 0.0), strength)
        if target in metric_keys:
            expanded[source] = max(expanded.get(source, 0.0), strength)
    return expanded


def _sector_peers(conn: DBConnection, company_id: str, fact_store: FactStore) -> tuple[list[str], str | None]:
    """Other companies sharing this company's sector — basic_industry
    preferred (more specific, e.g. "Private Sector Bank") over
    macro_economic_sector (broader, e.g. "Financial Services") when both are
    set. Returns ([], None) when the company has neither set."""
    row = fact_store.get_company(conn, company_id)
    if row is None:
        return [], None
    if row["basic_industry"]:
        field, value = "basic_industry", row["basic_industry"]
    elif row["macro_economic_sector"]:
        field, value = "macro_economic_sector", row["macro_economic_sector"]
    else:
        return [], None
    rows = fact_store.list_companies_by_sector_field(conn, field, value, company_id)
    return [r["company_id"] for r in rows], value


@dataclass(frozen=True)
class GraphCandidate:
    thread_id: str
    company_ids: list[str]
    question: str
    report_markdown: str
    score: float
    path: str  # human-readable traversal explanation — inspectability (README §7)


def find_related_investigations(
    conn: DBConnection, question: str, company_ids: list[str], *, fact_store: FactStore | None = None
) -> list[GraphCandidate]:
    """Prior investigations about a DIFFERENT (sector-peer) company, relevant
    to this question through a direct or seed-edge-bridged metric match.

    Dispatches to the Neo4j-backed traversal (context/graph_neo4j.py) when
    config.settings.GRAPH_BACKEND="neo4j", falling back to this module's own
    SQLite/Python traversal if Neo4j isn't reachable — same
    graceful-degradation pattern as llm/router.py's provider fallback: a
    graph backend being down never fails the investigation itself."""
    fs = fact_store or default_fact_store()
    if GRAPH_BACKEND == "neo4j":
        try:
            from context import graph_neo4j
            driver = graph_neo4j.get_driver()
            graph_neo4j.sync_graph(conn, driver, fact_store=fs)
            return graph_neo4j.find_related_investigations(driver, question, company_ids)
        except Exception:
            logger.warning("Neo4j graph backend unavailable, falling back to SQLite traversal", exc_info=True)
    return _find_related_investigations_sqlite(conn, question, company_ids, fs)


def _find_related_investigations_sqlite(
    conn: DBConnection, question: str, company_ids: list[str], fact_store: FactStore
) -> list[GraphCandidate]:
    """Pure-Python/SQLite traversal — the default backend, and the fallback
    when GRAPH_BACKEND="neo4j" but no server is reachable. Returns [] if
    there's no sector data, no metric mentioned, or no matching prior
    investigation."""
    if len(company_ids) != 1:
        return []
    company_id = company_ids[0]

    peers, sector = _sector_peers(conn, company_id, fact_store)
    if not peers:
        return []

    direct_metrics = _metrics_mentioned(question)
    if not direct_metrics:
        return []
    relevant_metrics = _expand_via_seed_edges(direct_metrics)

    candidates: list[GraphCandidate] = []
    for report in fact_store.list_generated_reports(conn):
        if company_id in report["company_ids"]:
            continue  # about the target company itself — context/reuse.py's job, not this
        matched_peers = [c for c in report["company_ids"] if c in peers]
        if not matched_peers:
            continue

        evidence_text = " ".join(e["label"] for e in fact_store.list_report_evidence(conn, report["thread_id"])).lower()
        matched_metrics = [
            (mk, strength) for mk, strength in relevant_metrics.items()
            if any(kw in evidence_text for kw in _keywords_for(mk))
        ]
        if not matched_metrics:
            continue

        best_metric, metric_strength = max(matched_metrics, key=lambda m: m[1])
        score = SECTOR_PEER_STRENGTH * metric_strength
        bridge = "" if best_metric in direct_metrics else f" (bridged via {best_metric})"
        candidates.append(GraphCandidate(
            thread_id=report["thread_id"], company_ids=report["company_ids"], question=report["question"],
            report_markdown=report["report_markdown"], score=score,
            path=(
                f"{company_id} --SAME_SECTOR_AS[{sector}]--> {matched_peers[0]} "
                f"--DISCUSSED_IN--> investigation on {best_metric}{bridge} (score {score:.2f})"
            ),
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:MAX_CANDIDATES]


def render_related_investigations(candidates: list[GraphCandidate]) -> str:
    """The prompt block for a fresh-generation call (research/signals_report.py)
    when find_related_investigations() found something — kept structurally
    separate from the Evidence block it's appended after."""
    lines = [
        "Related prior investigations (about a DIFFERENT company — a reasoning pattern that MAY "
        "apply here, not a fact about the company/companies in this question; cite any use of this "
        "as [INFERENCE] only, never [FACT] or [CALCULATION]):"
    ]
    for c in candidates:
        lines.append(f"- {', '.join(c.company_ids)}: \"{c.question}\" — {c.path}")
    return "\n".join(lines)
