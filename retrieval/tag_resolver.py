"""Deterministic tag detection for investigation questions -- resolves a
group reference (index, sector, industry, macro-economic sector, country,
or lifecycle status) mentioned in a question's text into the company_ids
of every member, so "build an investigation across Nifty 50" or "Active
Technology companies in India" doesn't require hand-picking companies in
the picker first. research/investigation_planner.py and research/
hypothesis_generator.py already handle a multi-company company_ids list
correctly end to end -- nothing upstream of them ever resolved a group
NAME into that list, which is the actual gap this closes.

Deterministic word-boundary matching against each dimension's own closed,
known vocabulary -- not an LLM call, by explicit request: a tag name is a
proper noun/fixed label someone would type close to verbatim, not an
indirect reference needing semantic mapping the way a macro question is
(research/macro_evidence.py's _plan_retrieval is the one place this app
DOES spend an LLM call mapping free text to a known vocabulary, because
"rainfall's effect on demand" never says "IMD rainfall index" literally --
a tag name doesn't have that problem, so paying for an LLM call to solve
it would be pure waste).

Combination semantics (explicitly asked for -- "combinations around
them"): matches WITHIN one dimension union together (naming two sectors,
"Technology or Healthcare companies", means either sector, not the
impossible intersection of two sectors no company can belong to at once);
matches ACROSS different dimensions intersect (naming an index AND a
sector, "Nifty 50 Technology companies", means Technology companies that
are ALSO in Nifty 50, not the two groups combined) -- the same "each
filter narrows further" semantics every faceted-search/filter-panel UI
already uses, including this app's own Settings > Audit Log filters
(Search + Status + Index all narrow together, never widen).

Only ever called when the caller's own company_ids list is empty (see
web/app.py's /investigate/generate) -- a tag mention alongside an already
explicit company selection is left alone rather than silently
widening/narrowing scope the user didn't ask to touch.
"""

from __future__ import annotations

import re

from storage.company_repository import (
    select_active_companies_by_country,
    select_companies_by_sector_column,
    select_company_ids_by_index,
    select_company_ids_by_status,
    select_company_ids_by_tag_column,
)
from storage.db_types import DBConnection
from storage.repositories import list_index_definitions, list_industries, list_macro_economic_sectors, list_sectors

# Country/status have no admin-curated name table to drive matching off of
# (unlike indices/sectors/industries) -- countries.country is a bare ISO
# code ("IN"/"US") too short and too collision-prone to word-match
# case-insensitively (lowercase "us" is also the pronoun -- "help us
# understand" must not match), so this maps the names a person would
# actually type to the stored code instead. "US" itself is still the most
# natural way to type this (more common than "USA" in practice, verified
# against a real miss: "US Technology companies" matched nothing until
# this was added, silently returning the whole Technology universe
# unfiltered instead of intersecting) -- handled as a case-SENSITIVE
# exact-case match on the token "US" specifically (see _mentions_us()
# below), never lowercased like every other synonym here.
_COUNTRY_SYNONYMS: dict[str, list[str]] = {
    "IN": ["india", "indian"],
    "US": ["usa", "united states", "american"],
}
_US_TOKEN_RE = re.compile(r"\bUS\b")
_STATUSES = ("active", "archived")  # companies.status's own CHECK constraint


def _mentions(text_lower: str, name: str) -> bool:
    """Word-boundary match, tolerant of how much whitespace sits between a
    multi-word name's own words -- a real miss, not hypothetical: "nifty50"
    (no space) matched nothing against the index name "Nifty 50" under a
    literal-space match, silently returning zero companies for a phrasing
    at least as common as the spaced-out one. Joining the name's words with
    `\\s*` instead of a literal space lets "nifty 50", "nifty  50", and
    "nifty50" all match the same pattern; a single-word name (most sectors/
    industries) is unaffected, since there's nothing to join."""
    pattern = r"\b" + r"\s*".join(re.escape(word) for word in name.lower().split()) + r"\b"
    return re.search(pattern, text_lower) is not None


def _dimension_hits(conn: DBConnection, text_lower: str, names: list[str], lookup) -> set[str]:
    """Union of every company_id from every name in `names` that appears in
    the text -- one dimension's own within-dimension OR, e.g. two sector
    names both mentioned. `lookup(conn, name) -> list[Row w/ company_id]`."""
    hits: set[str] = set()
    for name in names:
        if _mentions(text_lower, name):
            hits.update(r["company_id"] for r in lookup(conn, name))
    return hits


def resolve_tags_in_text(conn: DBConnection, text: str) -> list[str]:
    """Every company_id matching the group(s) named in `text`, across
    indices/sectors/industries/macro-economic sectors/countries/status --
    [] if nothing recognized. See module docstring for the union-within-
    dimension, intersect-across-dimensions combination rule."""
    text_lower = text.lower()

    dimension_sets: list[set[str]] = []

    index_hits = _dimension_hits(conn, text_lower, list_index_definitions(conn), select_company_ids_by_index)
    if index_hits:
        dimension_sets.append(index_hits)

    sector_hits = _dimension_hits(
        conn, text_lower, list_sectors(conn), lambda c, name: select_company_ids_by_tag_column(c, "sector", name)
    )
    if sector_hits:
        dimension_sets.append(sector_hits)

    industry_hits = _dimension_hits(
        conn, text_lower, list_industries(conn), lambda c, name: select_company_ids_by_tag_column(c, "industry", name)
    )
    if industry_hits:
        dimension_sets.append(industry_hits)

    macro_hits = _dimension_hits(
        conn, text_lower, list_macro_economic_sectors(conn),
        lambda c, name: select_companies_by_sector_column(c, "macro_economic_sector", name, ""),
    )
    if macro_hits:
        dimension_sets.append(macro_hits)

    country_hits: set[str] = set()
    for code, synonyms in _COUNTRY_SYNONYMS.items():
        if any(_mentions(text_lower, syn) for syn in synonyms):
            country_hits.update(r["company_id"] for r in select_active_companies_by_country(conn, code))
    if _US_TOKEN_RE.search(text):  # case-sensitive, against the ORIGINAL text -- see _US_TOKEN_RE's own comment
        country_hits.update(r["company_id"] for r in select_active_companies_by_country(conn, "US"))
    if country_hits:
        dimension_sets.append(country_hits)

    status_hits = _dimension_hits(conn, text_lower, list(_STATUSES), select_company_ids_by_status)
    if status_hits:
        dimension_sets.append(status_hits)

    if not dimension_sets:
        return []
    result = dimension_sets[0]
    for s in dimension_sets[1:]:
        result &= s
    return sorted(result)
