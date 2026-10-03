"""Question scope: which companies does a research question cover?

Before this module the answer came from plain text matching: any word that
happened to equal a sector or industry name ("services" in "services mix")
selected that whole group, and a group hit short-circuited the company-name
resolver, so "Why did Nvidia's margin expand ... services mix" was scoped to
fourteen Indian Services companies and Nvidia was never considered.

Now one LLM call reads the question and says what it is actually about:

  company_ids  companies the question names, chosen ONLY from a pool of real
               companies found by research.company_resolver's deterministic
               prefilter (an id outside the pool is dropped, never trusted)
  groups       group names the question asks to scope to ("Nifty 50",
               "Technology companies"), not incidental words

Resolution order, first hit wins:
  1. named companies  -- a question about a specific company is about it; a
                         group word elsewhere in the sentence does not widen it
  2. groups           -- the LLM's group phrases (only those, not the whole
                         question) go through the deterministic tag resolver
                         (retrieval/tag_resolver.py), so membership is still
                         exact and reproducible
  3. neither          -- macro / company-less question: no scope

If the call fails or is unparseable, the previous behaviour runs unchanged
(tags over the whole text, then the company-name resolver), so this module can
only ever make scoping better informed, never leave it empty-handed where the
old path found something.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.company_resolver import _candidate_companies, resolve_companies
from retrieval.tag_resolver import resolve_tags_in_text
from storage.db_types import DBConnection

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_TOKENS = 512


@dataclass(frozen=True)
class ScopeResolution:
    company_ids: list[str] = field(default_factory=list)
    #: "companies" | "groups" | "none" | "fallback" (LLM unavailable -> legacy path)
    source: str = "none"
    groups: list[str] = field(default_factory=list)


def _system_prompt(candidates: list[dict]) -> str:
    lines = "\n".join(f'- {c["company_id"]}: {c["display_name"]}' for c in candidates) or "(none)"
    return (
        "You decide which companies a research question is about.\n\n"
        "Candidate companies (real, found by word search -- many are incidental matches):\n"
        f"{lines}\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown fences:\n"
        '{"company_ids": ["<id>", ...], "groups": ["<group name>", ...]}\n\n'
        "Rules:\n"
        "- company_ids: only candidates the question genuinely names or clearly means, in the order "
        "mentioned. A candidate matched by an ordinary word (e.g. 'services', 'express', 'shipping' used "
        "as a general term) is NOT named. Never use an id that is not listed above.\n"
        "- groups: only when the question asks to scope to a whole group -- an index (\"Nifty 50\"), a "
        'sector or industry ("Technology companies", "banks"), or a country\'s companies. Use the name as '
        "a person would type it. A sector word used descriptively inside a question about one named "
        "company (e.g. 'services mix' in a question about Nvidia) is NOT a group.\n"
        "- A question about macro data or no company at all returns both lists empty."
    )


def _parse(text: str, candidates: list[dict]) -> tuple[list[str], list[str]] | None:
    match = _JSON_OBJECT_RE.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    raw_ids, raw_groups = data.get("company_ids"), data.get("groups", [])
    if not isinstance(raw_ids, list) or not isinstance(raw_groups, list):
        return None
    known = {c["company_id"] for c in candidates}
    ids: list[str] = []
    for company_id in raw_ids:
        if isinstance(company_id, str) and company_id in known and company_id not in ids:
            ids.append(company_id)
    groups = [g.strip() for g in raw_groups if isinstance(g, str) and g.strip()]
    return ids, groups


def _legacy(conn: DBConnection, question: str) -> ScopeResolution:
    ids = resolve_tags_in_text(conn, question) or resolve_companies(conn, question).company_ids
    return ScopeResolution(company_ids=ids, source="fallback")


def resolve_scope(conn: DBConnection, question: str, *, model: str | None = None) -> ScopeResolution:
    candidates = _candidate_companies(conn, question)
    if not candidates and not resolve_tags_in_text(conn, question):
        return ScopeResolution(source="none")  # nothing to choose between: skip the LLM call
    try:
        result = route(
            system=_system_prompt(candidates), user_message=question,
            hardness=fixed(Tier.STANDARD, "question scope resolution"),
            max_tokens=_MAX_TOKENS, pinned_model=model or ANTHROPIC_MODEL,
        )
    except AllProvidersUnavailableError:
        return _legacy(conn, question)
    except Exception:  # noqa: BLE001 -- scoping must never take the question down
        logger.exception("Scope resolution call failed; using text matching")
        return _legacy(conn, question)

    observability.record(conn, task_name="scope_resolution", company_ids=[], question=question, result=result)
    parsed = _parse(result.response.text, candidates)
    if parsed is None:
        return _legacy(conn, question)
    company_ids, groups = parsed

    if company_ids:
        return ScopeResolution(company_ids=company_ids, source="companies")
    if groups:
        group_ids = resolve_tags_in_text(conn, " ; ".join(groups))
        if group_ids:
            return ScopeResolution(company_ids=group_ids, source="groups", groups=groups)
    return ScopeResolution(source="none", groups=groups)
