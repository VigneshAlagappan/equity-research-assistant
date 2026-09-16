"""Company/entity resolution for free-text research questions -- replaces
the client-side regex matcher (web/templates/research.html's own
detectCompaniesInText(), still used by chat.html/company pages for their
own scoped flows) that requires an EXACT whole-word match of a company's
full registered name or ticker, and silently returns nothing for a
completely natural shorthand: "IDFC Bank" for the registered "IDFC First
Bank", "Federal Bank" for the registered "The Federal Bank" -- both real,
observed failures that sent a real comparison question through empty-
handed (company_ids=[]) and produced a confusing "no evidence found"
instead of an answer, even though both companies' financials were fully
ingested.

Two-stage, not one LLM call over the whole company catalog (2500+ rows --
too large/expensive to send on every question, and nothing here should
ever accept a company_id the model invents rather than one that
demonstrably exists in this app's own data):

  1. Deterministic prefilter (_candidate_companies): every meaningful word
     in the question is substring-matched against companies.company_id/
     display_name/legal_name/nse_symbol via companies.registry.
     search_companies() -- the same function the header search box's own
     typeahead already uses -- building a bounded pool of REAL companies
     the question might plausibly be about. Matching per-WORD rather than
     matching the whole phrase is what fixes the actual bug: "IDFC" alone
     surfaces "IDFC First Bank" as a candidate even though "IDFC Bank"
     never appears verbatim anywhere in its name.
  2. LLM disambiguation (resolve_companies): given the question and that
     candidate pool (id + display name only, nothing else), the model
     decides which candidates (if any) the question is actually about.
     Validated against the pool before being trusted -- a company_id
     outside the candidates the model was actually shown is dropped, not
     accepted, same "never trust a hallucinated value" discipline
     research/aggregate_query.py's metric_key validation and research/
     macro_evidence.py's series_key validation already follow.

STANDARD tier, not QUICK, for the same measured reason research/
aggregate_query.py's extract_aggregate_intent() gives: QUICK's preferred
local model returned a blank response on a meaningful fraction of real
calls in that module's own testing for a same-shaped classification task,
and a wrong resolution here is worse than an expensive one -- this
decision determines whether the user gets a real answer or another silent
"no evidence found".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from storage.db_types import DBConnection

from companies.registry import search_companies
from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_MIN_WORD_LEN = 3
_STOPWORDS = {
    "the", "a", "an", "is", "was", "are", "were", "of", "in", "on", "for", "and", "to", "vs",
    "what", "how", "does", "did", "do", "has", "have", "had", "over", "last", "past", "years", "year",
    "why", "compare", "comparison", "versus", "trend", "outlook", "driving", "driver", "gap", "between",
}
MAX_CANDIDATES = 40
CANDIDATES_PER_TOKEN = 5
_INTENT_MAX_TOKENS = 512


@dataclass(frozen=True)
class ResolvedCompany:
    company_id: str
    display_name: str


@dataclass(frozen=True)
class CompanyResolution:
    companies: list[ResolvedCompany] = field(default_factory=list)

    @property
    def company_ids(self) -> list[str]:
        return [c.company_id for c in self.companies]


def _candidate_words(question: str) -> list[str]:
    words = [w for w in _WORD_RE.findall(question) if len(w) >= _MIN_WORD_LEN]
    seen: set[str] = set()
    ordered: list[str] = []
    for word in words:
        lower = word.lower()
        if lower in _STOPWORDS or lower in seen:
            continue
        seen.add(lower)
        ordered.append(word)
    return ordered


def _candidate_companies(conn: DBConnection, question: str) -> list[dict]:
    """Bounded pool of real (company_id, display_name) pairs -- see this
    module's own docstring for why per-word substring matching, not a
    single phrase match, is what actually fixes the bug."""
    seen_ids: set[str] = set()
    candidates: list[dict] = []
    for word in _candidate_words(question):
        for row in search_companies(conn, word, limit=CANDIDATES_PER_TOKEN):
            if row["company_id"] in seen_ids:
                continue
            seen_ids.add(row["company_id"])
            candidates.append({"company_id": row["company_id"], "display_name": row["display_name"]})
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates[:MAX_CANDIDATES]


def _build_system_prompt(candidates: list[dict]) -> str:
    candidate_lines = "\n".join(f'- {c["company_id"]}: {c["display_name"]}' for c in candidates)
    return (
        "You identify which companies (if any) a research question is actually about, from a "
        "fixed list of real candidates -- never name a company that isn't in this list, even if "
        "you recognize a name the question uses that isn't an exact match to any candidate's own "
        "display name (e.g. a shortened or informal version of a candidate's name still means "
        "that candidate).\n\n"
        "Candidates:\n" + candidate_lines + "\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown fences:\n"
        '{"company_ids": ["<id>", ...]}\n\n'
        "Rules:\n"
        "- Include a candidate's id only if the question is genuinely about that company.\n"
        "- Preserve the order the question mentions them in.\n"
        "- Return {\"company_ids\": []} if the question doesn't name or clearly imply any of the "
        "given candidates (e.g. it's a macro/regulatory question, or about a company not in the list)."
    )


def resolve_companies(conn: DBConnection, question: str, *, model: str | None = None) -> CompanyResolution:
    """Returns CompanyResolution([]) -- never raises -- on any failure
    (no candidates found at all, provider unavailable, unparseable
    response): a caller falls through to treating the question as
    company-less (macro-only, or an error if the flow requires a company),
    same "absence isn't an error, fail toward doing less rather than doing
    the wrong thing" contract every other soft-fail LLM call in this app
    follows (research/aggregate_query.py's extract_aggregate_intent,
    research/macro_evidence.py's _plan_retrieval)."""
    candidates = _candidate_companies(conn, question)
    if not candidates:
        return CompanyResolution([])

    hardness = fixed(Tier.STANDARD, "company entity resolution")
    try:
        result = route(
            system=_build_system_prompt(candidates), user_message=question,
            hardness=hardness, max_tokens=_INTENT_MAX_TOKENS, pinned_model=model or ANTHROPIC_MODEL,
        )
    except AllProvidersUnavailableError:
        return CompanyResolution([])

    observability.record(conn, task_name="company_resolution", company_ids=[], question=question, result=result)

    text = result.response.text or ""
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return CompanyResolution([])
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return CompanyResolution([])

    by_id = {c["company_id"]: c["display_name"] for c in candidates}
    raw_ids = data.get("company_ids")
    if not isinstance(raw_ids, list):
        return CompanyResolution([])

    resolved: list[ResolvedCompany] = []
    seen: set[str] = set()
    for company_id in raw_ids:
        if not isinstance(company_id, str) or company_id not in by_id or company_id in seen:
            continue  # not trusted blindly -- must be one of the real candidates actually shown
        seen.add(company_id)
        resolved.append(ResolvedCompany(company_id=company_id, display_name=by_id[company_id]))
    return CompanyResolution(resolved)
