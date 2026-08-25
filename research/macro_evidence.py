"""Macro/regulatory evidence retrieval — the third evidence source (alongside
Financials and Docs) research/assistant.py's SYSTEM_PROMPT grounds answers in,
for questions about macro/regulatory data (rainfall, repo rate, credit
growth, the Fed funds rate, CPI, ...) rather than one company's own reported
numbers. Spans both India (rbi/imd/iitm/mospi/irda) and US (fred) sources —
see _SOURCE_COUNTRY_LABEL below for how a matched series is attributed to a
country in the rendered evidence.

Deliberately generic, not a per-series hardcoded lookup: macro_observations
already has 490+ distinct series_key values across several sources (rbi,
iitm, fred, ...) and more will land as mospi/irda get ingested.

Series/date-range *selection* is an LLM call (_plan_retrieval below) — a
deliberate, narrow exception to README's "Retrieval never calls the LLM"
rule, made because the cheap keyword-overlap heuristic this module used to
rely on alone kept failing on real natural-language phrasing (a company
question containing the word "bank" pulling in RBI banking-sector series; an
absolute range like "1950s to early 2000s" not being recognized as a date
range at all). The LLM only ever picks *which* catalog entries to fetch and
what year range to use — it never sees or invents a data value; the actual
fetch is still plain deterministic SQL (get_macro_series), and every
series_key it names is validated against the real catalog before use, so a
hallucinated series_key is silently dropped rather than trusted. The
original keyword/regex heuristic (_matching_series, _year_range below) is
kept as the fallback when the LLM call fails or returns something
unparseable — this module always returns *something* sensible rather than
nothing just because one provider call had a bad day.

Regional/subdivisional series (region NOT NULL — e.g. IITM's zone/
subdivision breakdowns) aren't in the catalog at all; those stay reachable
directly via storage.repositories.get_macro_series, just not surfaced by
either the LLM planner or the heuristic fallback.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

from config.settings import ANTHROPIC_MODEL
from llm import observability
from llm.hardness import Tier, fixed
from llm.router import AllProvidersUnavailableError, route
from research.evidence import Evidence
from storage.repositories import get_macro_series

logger = logging.getLogger(__name__)

# Evidence.company_id is a required field, but this evidence isn't about any
# one company — this stand-in makes that legible in the rendered prompt line
# ("[FACT] INDIA — Repo Rate 2015: ..." / "[FACT] USA — Fed Funds Rate 2015: ...")
# without changing Evidence's shape. Keyed by macro_observations.source, not
# a single global constant, since series now come from both India and US
# sources (config.settings.DEFAULT_SOURCES) — falls back to the source_id
# itself, uppercased, for any source not listed here rather than mislabeling it.
_SOURCE_COUNTRY_LABEL = {
    "rbi": "INDIA", "imd": "INDIA", "iitm": "INDIA", "mospi": "INDIA", "irda": "INDIA", "mfin": "INDIA",
    "fred": "USA",
}

MAX_SERIES = 3  # cap how many distinct series one question can pull in
DEFAULT_YEAR_WINDOW = 50  # how far back to look when the question doesn't say

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "is", "was", "are", "were", "of", "in", "on", "for", "and", "to",
    "what", "how", "does", "did", "do", "has", "have", "had", "over", "last", "past", "years", "year",
    "india", "indian", "usa",  # country names carry no series-matching signal on their own
}
_MIN_WORD_LEN = 3  # drops noise like the stray "s" apostrophe-splitting leaves from "bank's"
_LAST_N_YEARS_RE = re.compile(r"(?:last|past)\s+(\d+)\s+years?", re.IGNORECASE)

# An absolute range ("1950s to early 2000s", "from 1990 to 2010") takes
# priority over the "last N years" default below when the question names one
# — otherwise it silently gets clipped to DEFAULT_YEAR_WINDOW years back from
# today, which for old series (e.g. IITM's 8-all_ind.txt, 1813-2006) drops
# the very period being asked about.
_YEAR_TOKEN_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_DECADE_TOKEN_RE = re.compile(r"\b(early|mid|late)?\s*((?:1[5-9]|20)\d0)s\b", re.IGNORECASE)
_DECADE_OFFSET = {"early": 1, "mid": 4, "late": 7}

# sources/iitm_rainfall.py's monthly/seasonal suffixes — excluded here so a
# plain "rainfall" question surfaces the two annual totals (rainfall_regional_
# annual, rainfall_subdivision_annual) rather than 34 near-duplicate monthly/
# seasonal series all scoring the same. Those per-month series are still
# reachable directly via get_macro_series(conn, "rainfall_regional_jun", ...)
# — this module just doesn't guess at them from question wording alone.
_IITM_NON_ANNUAL_SUFFIX_RE = re.compile(
    r"^rainfall_(regional|subdivision)_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|jf|mam|jjas|ond)$"
)

# Cheapest allowed model (llm/capability_registry.py's operator cap tops out
# at Sonnet — this planning step is a simple pick-from-a-list task, not deep
# reasoning, so it defaults to Haiku rather than research/assistant.py's
# DEFAULT_ANTHROPIC_MODEL, which is tuned for the harder answer-writing call).
# ANTHROPIC_MODEL (operator env override) still wins if set, same as every
# other LLM call site in this app.
_DEFAULT_PLANNER_MODEL = "claude-haiku-4-5"
_PLANNER_MAX_TOKENS = 300

_PLANNER_SYSTEM_PROMPT = """You select which macro/regulatory data series (India and US sources), if \
any, are relevant to a research question, and what year range to retrieve.

You are given a catalog of every available series, one per line, formatted as \
"<series_key> (<source>, <earliest period>-<latest period>)". Most questions are about a specific \
company's own financials and match NONE of these — that is the correct, expected answer for them. \
Only pick a series that is genuinely what the question is asking about. A word in the question \
merely appearing inside a series_key (e.g. a company name that happens to contain a common English \
word like "bank") is not evidence of a macro topic — judge the actual meaning of the question.

Respond in exactly this format and nothing else — no explanation, no other lines:
SERIES: <comma-separated series_key values copied exactly from the catalog, or NONE>
YEARS: <start_year>-<end_year>, or ALL if the question implies no specific period

Pick at most {max_series} series."""

_SERIES_LINE_RE = re.compile(r"^SERIES:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_YEARS_LINE_RE = re.compile(r"^YEARS:\s*(?:(\d{1,4})\s*-\s*(\d{1,4})|(ALL))\s*$", re.MULTILINE | re.IGNORECASE)


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= _MIN_WORD_LEN} - _STOPWORDS


def _pretty_label(series_key: str) -> str:
    label = series_key.replace("_", " ").title()
    return re.sub(r"\s+Annual$", "", label)  # period already carries "annual"-ness (e.g. "1990")


def _year_points(question: str) -> list[int]:
    """Every explicit 4-digit year ("1990") and decade phrase ("1950s",
    "early 2000s") named in the question, resolved to a single representative
    year each (e.g. "late 1990s" -> 1997)."""
    points = [int(y) for y in _YEAR_TOKEN_RE.findall(question)]
    for prefix, decade_start in _DECADE_TOKEN_RE.findall(question):
        points.append(int(decade_start) + _DECADE_OFFSET.get(prefix.lower(), 4))
    return points


def _year_range(question: str) -> tuple[int, int]:
    """(start_year, end_year) to restrict evidence to.

    Priority: an explicit "last/past N years" phrase; else the span implied
    by any absolute year/decade references in the question (a single
    reference is treated as "since then", open-ended to the present — two or
    more use their min/max as the range); else DEFAULT_YEAR_WINDOW years back
    from today, for a question that names no period at all."""
    current_year = date.today().year
    match = _LAST_N_YEARS_RE.search(question)
    if match:
        return current_year - int(match.group(1)), current_year

    points = _year_points(question)
    if points:
        return min(points), max(points) if len(points) > 1 else current_year

    return current_year - DEFAULT_YEAR_WINDOW, current_year


def _candidate_series(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Every distinct (series_key, source, earliest_period, latest_period) at
    the national level, minus the IITM per-month/season series (see
    _IITM_NON_ANNUAL_SUFFIX_RE above)."""
    rows = conn.execute(
        "SELECT series_key, source, MIN(period) AS earliest, MAX(period) AS latest "
        "FROM macro_observations WHERE region IS NULL GROUP BY series_key, source"
    ).fetchall()
    return [
        (r["series_key"], r["source"], r["earliest"], r["latest"])
        for r in rows if not _IITM_NON_ANNUAL_SUFFIX_RE.match(r["series_key"])
    ]


def _matching_series(conn: sqlite3.Connection, question: str) -> list[str]:
    """Keyword-overlap fallback — see _plan_retrieval, which is tried first."""
    question_words = _words(question)
    if not question_words:
        return []
    scored = [
        (len(question_words & _words(series_key)), series_key)
        for series_key, _source, _earliest, _latest in _candidate_series(conn)
    ]
    scored = [(score, key) for score, key in scored if score > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [key for _score, key in scored[:MAX_SERIES]]


def _render_catalog(candidates: list[tuple[str, str, str, str]]) -> str:
    return "\n".join(f"{key} ({source}, {earliest}-{latest})" for key, source, earliest, latest in candidates)


def _plan_retrieval(conn: sqlite3.Connection, question: str) -> tuple[list[str], int, int] | None:
    """Ask the LLM which catalog series (if any) and what year range apply to
    this question. Returns None — the caller falls back to _matching_series/
    _year_range — if there's no catalog to choose from, the call fails, or
    the response doesn't parse; never raises."""
    candidates = _candidate_series(conn)
    if not candidates:
        return None
    valid_keys = {key for key, _source, _earliest, _latest in candidates}

    system = _PLANNER_SYSTEM_PROMPT.format(max_series=MAX_SERIES)
    user_message = f"Catalog:\n{_render_catalog(candidates)}\n\nQuestion: {question}"
    hardness = fixed(Tier.QUICK, "macro series/date-range selection")
    pinned_model = ANTHROPIC_MODEL or _DEFAULT_PLANNER_MODEL

    try:
        result = route(
            system=system, user_message=user_message, hardness=hardness,
            max_tokens=_PLANNER_MAX_TOKENS, pinned_model=pinned_model,
        )
    except AllProvidersUnavailableError:
        logger.warning("macro retrieval planner unavailable — falling back to keyword heuristic")
        return None

    observability.record(conn, task_name="macro_retrieval_plan", company_ids=[], question=question, result=result)

    text = result.response.text
    series_match = _SERIES_LINE_RE.search(text)
    years_match = _YEARS_LINE_RE.search(text)
    if series_match is None or years_match is None:
        logger.warning("macro retrieval planner returned an unparseable response — falling back: %r", text)
        return None

    raw_series = series_match.group(1).strip()
    if raw_series.upper() == "NONE":
        series_keys: list[str] = []
    else:
        # Never trust an LLM-named series_key without checking it's real —
        # a hallucinated one is silently dropped, not fetched.
        series_keys = [s.strip() for s in raw_series.split(",") if s.strip() in valid_keys][:MAX_SERIES]

    start_str, end_str, is_all = years_match.groups()
    start_year, end_year = (1, date.today().year) if is_all else (int(start_str), int(end_str))
    return series_keys, start_year, end_year


def get_macro_evidence(conn: sqlite3.Connection, question: str) -> list[Evidence]:
    """Gather Evidence for the (at most MAX_SERIES) national macro series
    the LLM planner (_plan_retrieval) picks as relevant to this question,
    restricted to the year range it infers — falling back to the keyword/
    regex heuristic (_matching_series/_year_range) only if that call fails
    or doesn't parse. Returns [] if nothing applies — most company-financials
    questions won't match any macro series, and that's the expected common
    case, decided by the LLM itself (SERIES: NONE) rather than assumed."""
    planned = _plan_retrieval(conn, question)
    if planned is not None:
        series_keys, start_year, end_year = planned
    else:
        series_keys = _matching_series(conn, question)
        start_year, end_year = _year_range(question)
    if not series_keys:
        return []

    evidence: list[Evidence] = []
    for series_key in series_keys:
        rows = get_macro_series(conn, series_key, region=None)
        label = _pretty_label(series_key)

        # Downsample to (at most) one point per calendar year: weekly/monthly/
        # dated series (RBI's repo rate, CPI, ...) would otherwise contribute
        # hundreds-to-thousands of near-duplicate lines for one matched
        # series — get_macro_series returns oldest-to-newest, so keeping the
        # last row seen per year keeps that year's most recent observation.
        by_year: dict[int, sqlite3.Row] = {}
        for row in rows:
            year = int(row["period"][:4])
            if year < start_year or year > end_year:
                continue
            by_year[year] = row

        for year, row in sorted(by_year.items()):
            country_label = _SOURCE_COUNTRY_LABEL.get(row["source"], row["source"].upper())
            evidence.append(
                Evidence(
                    kind="FACT",
                    company_id=country_label,
                    label=f"{label} {row['period']}",
                    value=f"{row['value']:,.1f} {row['unit']}",
                    citation=f"{row['source'].upper()} ({Path(row['source_file']).name})",
                )
            )
    return evidence
