"""NSEXbrlAdapter — parses a quarterly-results XBRL filing (pulled live from
NSE's corporates-financial-results API — see scripts/backfill_company_websites.py's
sibling script for the fetch step, not written yet) into NormalizedObservations.

Context-ID convention (SEBI/BSE's standard quarterly-results XBRL taxonomy —
verified against real Reliance Industries and IDFC First Bank filings): a
context id prefixed "One" (e.g. "OneD" for a duration fact, "OneI" for an
instant) is always this filing's own single reported quarter; "Two".."Six"
are comparative periods (preceding quarter, same quarter prior year,
year-to-date, prior full year, ...) filed alongside it in the SAME document.
Only "OneD" (this quarter) and, on a Q4 filing only, "FourD" are read here —
every other comparative quarter this app cares about already gets its own
file ingested separately, so there's no need to also mine them out of this
one. A "FourD" context's own declared <xbrli:period> dates can't be trusted
at face value in general — found (IDFC First Bank Q3 FY25) to be IDENTICAL
to "OneD"'s despite holding an economically different (year-to-date, ~2.9x
larger) figure — so this adapter never resolves periods from a comparative
context's own dates, only from the "One" slot's positional convention.
"FourD" is positionally "year-to-date through the reported quarter"; on a Q4
filing specifically that YTD span IS the full fiscal year, so "FourD" there
carries genuine annual figures — verified against real Infosys and IDFC
First Bank Q4 FY26 filings (both taxonomy families): "FourD"'s own declared
dates there are correctly Apr 1 -> Mar 31 (unlike the Q3 case above), and its
values run ~3.8x "OneD"'s Q4-only figures, consistent with a 12- vs. 3-month
sum. That gives this app real annual observations straight out of the same
quarterly-results filing it already fetches, no separate annual-report XBRL
taxonomy needed. Trust here still runs off quarter position (this is a Q4
filing), not "FourD"'s own dates — same reasoning as "OneD" above.

Balance-sheet facts (Assets/Liabilities/Equity/Deposits/Advances/...) live
under "OneI" — an INSTANT context (a point-in-time snapshot as of the
filing's own period end), not a duration one — read separately from
"OneD"/"FourD" above. "OneI" is one snapshot regardless of framing: as-of-
quarter-end doubles as as-of-fiscal-year-end on a Q4 filing (there's no
distinct "annual OneI"), so it's read once and stamped into every period
framing this filing produces.

Tag -> metric_key aliasing lives in normalization/financials.py's
DEFAULT_METRIC_ALIASES (source="nse"), same as every other adapter — this
module only handles the two things an alias table can't: which raw tags need
rescaling before they match their metric's canonical unit (this app stores
crores, not rupees; percent-as-number, not decimal fractions), and reading
XBRL's context/tag structure at all.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from normalization.financials import build_observations_from_periods
from normalization.periods import fiscal_year_and_quarter_from_date
from sources.base import NormalizedObservation, SourceAdapter

logger = logging.getLogger(__name__)

PARSER_VERSION = "nse-xbrl-v1"

_NS_XBRLI = "http://www.xbrl.org/2003/instance"

# The BSE/NSE quarterly-results taxonomy has already changed namespace
# several times, independently per taxonomy family (bank vs. general
# Ind-AS — see DEFAULT_METRIC_ALIASES's "nse" block for what those two
# families actually look like): "in-bse-fin" dated "2019-09-30" (banking)
# and "2020-03-31" (Ind-AS) for filings through Q3 FY25 (SEBI's older
# financial-results-only XBRL, fetched via sources/nse_fetch.py's
# fetch_filing_index()), then "in-capmkt" dated "2025-01-31" for Q4 FY25
# through Q3 FY26, then "in-capmkt" dated "2026-01-31" from Q4 FY26 onward
# (SEBI's newer "Integrated Filing" framework, which folds financial
# results into one combined filing alongside other periodic disclosures —
# fetch_integrated_filing_index()). Every version bump so far was found by
# a real filing silently producing zero observations (namespace URI didn't
# match any registered prefix, so the whole tag-matching loop never fired —
# nothing else in the file signals a problem, hence the warning below), not
# by a documented changelog anywhere. Tag LOCAL NAMES are identical across
# every version within the same taxonomy family (verified against real IDFC
# First Bank filings for banking, real Infosys filings for Ind-AS, across
# all four namespace/date combinations below) — only the namespace URI
# differs, so this adapter matches on local name regardless of which
# produced it, rather than hardcoding one. A future taxonomy version is
# just another entry here, not a new adapter.
_FIN_NAMESPACES = (
    "http://www.bseindia.com/xbrl/fin/2019-09-30/in-bse-fin",
    "http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin",
    "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt",
    "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt",
)
_FIN_TAG_PREFIXES = tuple(f"{{{ns}}}" for ns in _FIN_NAMESPACES)

_CURRENT_DURATION_CONTEXT = "OneD"
_YTD_DURATION_CONTEXT = "FourD"
_CURRENT_INSTANT_CONTEXT = "OneI"

# Raw XBRL value is a decimal fraction (0.0194) rather than an already-
# scaled percent number (1.94) — this app's PERCENT-unit metrics store the
# latter (matches Screener's own convention, e.g. "Gross NPA %" as "1.94").
# Verified against real IDFC First Bank Q3 FY25 values: PercentageOfGrossNpa
# 0.0194 next to a real reported Gross NPA of 1.94%.
_FRACTION_TO_PERCENT_TAGS = {"PercentageOfGrossNpa", "PercentageOfNpa", "ReturnOnAssets"}

# These three are RBI-mandated ratios computed off a bank's own standalone
# books — they don't have a meaningful consolidated-group equivalent (a
# consolidated filing folds in non-banking subsidiaries the ratio was never
# defined over). Verified against every real IDFC First Bank consolidated
# filing pulled this session (Q2 FY24 - Q3 FY25, all eight): every one of
# them files a literal "0.00" for all three tags in the "OneD" context,
# while the matching standalone filing for the same quarter reports real
# values (e.g. GNPA 1.90%-2.11% across the same range) — a placeholder the
# XBRL schema apparently requires some value for, not a genuine reported
# zero (no real bank runs exactly 0.00% gross NPA every quarter for years).
# Treated as "not applicable" (skipped, not stored) for a consolidated
# filing specifically — guardrail: missing/0/not-applicable are distinct
# states, and a literal 0 here is the "not applicable" state, not "missing"
# (the tag IS present) or a real zero.
_CONSOLIDATED_ONLY_PLACEHOLDER_ZERO_TAGS = _FRACTION_TO_PERCENT_TAGS

# EPS is already per-share rupees (no rescaling) — everything else mapped in
# metric_aliases for source="nse" that isn't a fraction-to-percent tag is a
# plain rupee figure; this app's INR_CRORE metrics store crores. One EPS tag
# per taxonomy (banking's own "BasicEarningsPerShareBeforeExtraordinaryItems"
# doesn't exist in the Ind-AS taxonomy at all, and vice versa — verified
# against real IDFC First Bank and Infosys filings; missing this one for the
# newer taxonomy silently divided a real EPS of 17.87 by 1e7 instead of
# leaving it alone, caught by a real-shaped test, not a live filing).
_PER_SHARE_TAGS = {
    "BasicEarningsPerShareBeforeExtraordinaryItems",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
}

# shares_outstanding has no direct XBRL tag in this taxonomy — derived as
# PaidUpValueOfEquityShareCapital / FaceValueOfEquityShareCapital, the two
# tags a filing does carry. Verified against the real IDFC First Bank Q4
# FY24 filing: 70,699,200,000 / 10 = 7,069,920,000 shares = 706.99 Cr,
# matching the independently-sourced legacy figure (707.0 Cr) to within
# rounding. Both tags were confirmed identical across a filing's
# consolidated and standalone documents (share capital is a whole-company
# fact, not one that differs by consolidation basis), so this derivation
# runs unconditionally, unlike the standalone-only ratios above.
# row_label "DerivedSharesOutstanding" is synthetic (not a real XBRL tag) —
# metric_aliases (source="nse") maps it to shares_outstanding same as any
# other row_label; only this adapter ever produces it.
_PAID_UP_SHARE_CAPITAL_TAG = "PaidUpValueOfEquityShareCapital"
_FACE_VALUE_TAG = "FaceValueOfEquityShareCapital"
_DERIVED_SHARES_OUTSTANDING_LABEL = "DerivedSharesOutstanding"


class NSEXbrlAdapter(SourceAdapter):
    source_id = "nse"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def parse(
        self,
        file_path: Path,
        company_id: str,
        statement_type: str = "consolidated",
        **kwargs: object,
    ) -> list[NormalizedObservation]:
        """statement_type applies to the whole file — NSE files consolidated
        and standalone results as separate XBRL documents (each with its own
        filing metadata), same reasoning sources/screener.py's own
        statement_type parameter documents, so the caller states which one
        this file is."""
        root = ET.parse(file_path).getroot()

        period_end = self._current_quarter_end(root, file_path)
        if period_end is None:
            return []
        fiscal_year, quarter = fiscal_year_and_quarter_from_date(period_end, "quarterly")

        # On a Q4 filing, "FourD" (YTD-through-this-quarter) spans the whole
        # fiscal year — see module docstring — so this filing produces a
        # second, annual period framing alongside the always-present
        # quarterly one.
        period_framings: list[tuple[str, str, str | None]] = [("quarterly", fiscal_year, quarter)]
        if quarter == "Q4":
            period_framings.append(("annual", fiscal_year, None))

        observations: list[NormalizedObservation] = []
        matched_known_namespace = False

        # Duration facts: "OneD" feeds the quarterly framing, "FourD" (Q4
        # filings only) feeds the annual one — one context per framing.
        duration_context_by_period_type = {"quarterly": _CURRENT_DURATION_CONTEXT, "annual": _YTD_DURATION_CONTEXT}
        for period_type, fy, q in period_framings:
            context_id = duration_context_by_period_type[period_type]
            values, paid_up_share_capital, face_value, matched = self._extract_context_values(
                root, file_path, context_id, statement_type
            )
            matched_known_namespace = matched_known_namespace or matched
            observations.extend(
                self._observations_for_values(company_id, file_path, statement_type, period_type, fy, q, values)
            )
            if paid_up_share_capital is not None and face_value:
                shares_outstanding = paid_up_share_capital / face_value / 1e7  # share count, in crore of shares
                observations.extend(
                    self._observations_for_values(
                        company_id, file_path, statement_type, period_type, fy, q,
                        {_DERIVED_SHARES_OUTSTANDING_LABEL: shares_outstanding},
                    )
                )

        # Instant (balance-sheet) facts: "OneI" is one point-in-time
        # snapshot as of the filing's own period end — as-of-quarter-end
        # doubles as as-of-fiscal-year-end on a Q4 filing (see docstring),
        # so it's read once and stamped into every framing this filing
        # produces, rather than re-walked per framing like the duration
        # contexts above (there's no separate "annual OneI").
        instant_values, _, _, instant_matched = self._extract_context_values(
            root, file_path, _CURRENT_INSTANT_CONTEXT, statement_type
        )
        matched_known_namespace = matched_known_namespace or instant_matched
        for period_type, fy, q in period_framings:
            observations.extend(
                self._observations_for_values(company_id, file_path, statement_type, period_type, fy, q, instant_values)
            )

        if not matched_known_namespace:
            # A real, valid "OneD" context was found (we got this far), but
            # not one single element in the whole document matched any
            # registered _FIN_NAMESPACES — almost certainly a taxonomy
            # version bump this adapter hasn't been taught about yet (this
            # exact failure mode is how the "2025-01-31" in-capmkt version
            # was discovered: silently zero observations, no other signal).
            # Loud on purpose, unlike every other skip above.
            logger.warning(
                "%s: 'OneD' context found but no element matched any known fin namespace "
                "(%s) — likely a new taxonomy version; parsing produced zero observations",
                file_path, ", ".join(_FIN_NAMESPACES),
            )

        return observations

    def _observations_for_values(
        self,
        company_id: str,
        file_path: Path,
        statement_type: str,
        period_type: str,
        fiscal_year: str,
        quarter: str | None,
        values: dict[str, float],
    ) -> list[NormalizedObservation]:
        observations: list[NormalizedObservation] = []
        for tag, value in values.items():
            observations.extend(
                build_observations_from_periods(
                    self._conn,
                    company_id=company_id,
                    source=self.source_id,
                    source_file=str(file_path),
                    parser_version=PARSER_VERSION,
                    period_type=period_type,
                    statement_type=statement_type,
                    row_label=tag,
                    period_values={(fiscal_year, quarter): value},
                )
            )
        return observations

    @staticmethod
    def _extract_context_values(
        root: ET.Element, file_path: Path, context_id: str, statement_type: str
    ) -> tuple[dict[str, float], float | None, float | None, bool]:
        """One pass over every fact tagged `context_id` -> {tag: rescaled
        value}, plus PaidUpValueOfEquityShareCapital/FaceValueOfEquityShareCapital
        captured separately for the shares_outstanding derivation (only
        ever found on a duration context in practice, but harmless to look
        for here regardless), plus whether ANY element in the whole
        document matched a known fin namespace — that flag is document-wide
        by construction (checked before the contextRef filter), not
        specific to `context_id`, so the caller ORs it across every call
        rather than treating one context's absence of matches as
        meaningful on its own."""
        values: dict[str, float] = {}
        paid_up_share_capital: float | None = None
        face_value: float | None = None
        matched_known_namespace = False

        for el in root.iter():
            prefix = next((p for p in _FIN_TAG_PREFIXES if el.tag.startswith(p)), None)
            if prefix is None:
                continue
            matched_known_namespace = True
            if el.get("contextRef") != context_id:
                continue
            tag = el.tag[len(prefix):]
            raw_text = (el.text or "").strip()
            if not raw_text:
                continue
            try:
                value = float(raw_text)
            except ValueError:
                logger.warning("%s: tag %r contextRef=%s has a non-numeric value %r — skipping", file_path, tag, context_id, raw_text)
                continue

            # Captured raw (un-rescaled) for the shares_outstanding
            # derivation below, not stored under their own tag name — this
            # taxonomy has no direct shares-count tag at all.
            if tag == _PAID_UP_SHARE_CAPITAL_TAG:
                paid_up_share_capital = value
                continue
            if tag == _FACE_VALUE_TAG:
                face_value = value
                continue

            if (
                value == 0
                and statement_type == "consolidated"
                and tag in _CONSOLIDATED_ONLY_PLACEHOLDER_ZERO_TAGS
            ):
                logger.info(
                    "%s: tag %r is a standalone-only ratio filed as a placeholder 0 on this "
                    "consolidated filing — treating as not-applicable, not a real zero",
                    file_path, tag,
                )
                continue

            if tag in _FRACTION_TO_PERCENT_TAGS:
                value *= 100
            elif tag not in _PER_SHARE_TAGS:
                value /= 1e7  # rupees -> crore

            values[tag] = value

        return values, paid_up_share_capital, face_value, matched_known_namespace

    @staticmethod
    def _current_quarter_end(root: ET.Element, file_path: Path):
        """The <xbrli:endDate> of the "OneD" context — this filing's own
        reported quarter's period end. None (with a warning) if "OneD" isn't
        present, rather than guessing at some other context — an unexpected
        context-ID convention on a real filing deserves a human look, not a
        silent fallback that might pick a comparative period instead."""
        context = root.find(f"{{{_NS_XBRLI}}}context[@id='{_CURRENT_DURATION_CONTEXT}']")
        if context is None:
            logger.warning("%s: no %r context — skipping (unexpected context-ID convention)", file_path, _CURRENT_DURATION_CONTEXT)
            return None
        end_date_el = context.find(f"{{{_NS_XBRLI}}}period/{{{_NS_XBRLI}}}endDate")
        if end_date_el is None or not end_date_el.text:
            logger.warning("%s: %r context has no endDate — skipping", file_path, _CURRENT_DURATION_CONTEXT)
            return None
        return date.fromisoformat(end_date_el.text.strip())
