"""Live fetch + parse of NSE's Shareholding Pattern (SEBI LODR Regulation 31)
filings — a separate domain from sources/nse_xbrl.py's quarterly-results
financials, with its own listing API and its own XBRL taxonomy (in-bse-shp,
not in-bse-fin/in-capmkt).

Two-step data model, matching what NSE's own site shows:

1. fetch_shareholding_master() — the lightweight per-quarter listing
   (corporate-share-holdings-master, verified live against real INFY/TCS/
   HDFCBANK data) that already reports promoter/public/employee-trust
   holding PERCENTAGES with no XBRL parsing needed at all — just three
   numbers per quarter plus a link to that quarter's full SHP XBRL filing.

2. fetch_named_holders() / parse_shp_xbrl() — drills into that XBRL link
   for individually-NAMED holders (e.g. "NANDAN M NILEKANI", "HDFC MUTUAL
   FUND", "LIFE INSURANCE CORPORATION OF INDIA") and which side of the
   register (promoter vs. public) each belongs to. The master listing
   itself carries no shareholder identity at all — only this XBRL does.

Named-holder taxonomy (in-bse-shp; verified against real INFY, TCS, and
HDFCBANK SHP filings): only a subset of the taxonomy's dimensional
"DetailsOfSharesHeldBy...Axis" / "DetailsSharesHeldBy...Axis" sub-categories
carry an individually-named NameOfTheShareholder fact per member — most
sub-categories (retail individuals, bodies corporate in aggregate, etc.)
are aggregate-only by design (too many holders, or below the disclosure
threshold, to name individually). Which side of the register a named axis
belongs to is NOT encoded anywhere in the context itself — verified: these
contexts carry only a single typedMember for the holder's own identity, no
CategoryOfShareholdersAxis explicitMember alongside it — so it's fixed by
which axis it is, per the published SEBI/BSE schema, the same kind of
curated/verified tag table sources/nse_xbrl.py already keeps for its own
tag->metric mapping rather than introspecting it from the document.
_PROMOTER_HOLDER_AXES / _PUBLIC_HOLDER_AXES below are that table:

- _PROMOTER_HOLDER_AXES verified two ways: real INFY filing names its
  Individuals/HUF axis members as the founder family (Nilekani, Murthy,
  Gopalakrishnan, Shibulal, ...); real TCS filing (promoted by Tata Sons,
  a body corporate, not individuals) has ZERO Individuals/HUF entries but
  instead names its "Others - Indian/Foreign Shareholders" axis members as
  Tata Group promoter-group companies (Kaleyra Inc, Agratas Limited, Tata
  AutoComp subsidiaries, ...) — i.e. promoter-group bodies corporate, not
  public holders, despite the axis name's surface similarity to a public
  "institutions foreign" bucket.
- Cross-check: real HDFCBANK filing (0% promoter holding — a
  professionally-managed bank with no promoter) has NONE of the promoter
  axes present at all, exactly as expected if the classification is right.

An axis not in either table is a real, not-yet-verified gap — skipped
(with a debug log), never guessed, same "loud on an unknown, don't
silently misclassify" discipline as nse_xbrl.py's unknown-namespace
warning.

Reuses sources/nse_fetch.py's session bootstrap/pacing/retry machinery
directly (same WAF/anti-bot cookie dance, same host) rather than
duplicating it — this module owns nothing about HTTP session mechanics of
its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from normalization.periods import fiscal_year_and_quarter_from_date
from sources.nse_fetch import NSEFetchError, _BASE, _get_with_retries, _new_session

logger = logging.getLogger(__name__)

_SHAREHOLDING_MASTER_API_PATH = "/api/corporate-share-holdings-master"

# SHP taxonomy versions handled for named-holder extraction — same "match
# by local tag name, keep a table of known namespace URIs" approach as
# sources/nse_xbrl.py's _FIN_NAMESPACES. Only "2025-10-31" (roughly the
# last several quarters as of 2026-08, verified against real INFY/TCS/
# HDFCBANK filings) is actually supported here: a real older INFY filing
# (2022-03-31, namespace "2020-09-30") was found live to use a materially
# different context-id scheme (holder identity+numeric facts share ONE
# context id directly, e.g. "DetailsSharesHeldByIndividualsOrHUF001D",
# not this version's "D_"-prefixed/numeric-context PAIR) and even
# different axis names (e.g. "...MutualFundsOrUtiAxis" vs this version's
# "...MutualFundsOrUTIAxis") — a genuinely different parser, not just a
# namespace bump, and not built here. parse_shp_xbrl() degrades gracefully
# on an unmatched namespace (empty list + a warning, not a crash or wrong
# data) — a company whose only history predates 2025-10-31 simply gets no
# named holders yet, while its aggregate percentages (from the master
# listing, version-independent) are unaffected. Known gap, not a bug.
_SHP_NAMESPACES = ("http://www.bseindia.com/xbrl/shp/2025-10-31/in-bse-shp",)
_SHP_TAG_PREFIXES = tuple(f"{{{ns}}}" for ns in _SHP_NAMESPACES)

_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"

_NAME_TAG = "NameOfTheShareholder"
_SHARES_TAG = "NumberOfShares"
_PERCENT_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"

_PROMOTER_HOLDER_AXES = {
    "DetailsSharesHeldByIndividualsOrHUFAxis": "Individuals / HUF",
    "DetailsOfSharesHeldByOthersIndianShareholdersAxis": "Other Indian Shareholders (Promoter Group)",
    "DetailsOfSharesHeldByOtherForeignShareholdersAxis": "Other Foreign Shareholders (Promoter Group)",
}

# Institutions Foreign/Domestic naming below is the public-side "Details...
# Axis" family — verified distinct from the similarly-named but
# promoter-side "...OthersIndianShareholders"/"...OtherForeignShareholders"
# axes above (real TCS filing has both kinds simultaneously with clearly
# different holder identities). Banks/NBFCs/AIFs are the same Table III
# "Institutions" bucket as the verified Mutual Funds/Insurance/Provident
# Fund entries but weren't themselves seen carrying a named entry in any
# filing checked this session — included by taxonomy-family pattern, not
# independently verified; flagged here rather than silently assumed.
_PUBLIC_HOLDER_AXES = {
    "DetailsOfSharesHeldByMutualFundsOrUTIAxis": "Mutual Funds / UTI",
    "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOneAxis": "Foreign Portfolio Investors (Category I)",
    "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorTwoAxis": "Foreign Portfolio Investors (Category II)",
    "DetailsOfSharesHeldByInsuranceCompaniesAxis": "Insurance Companies",
    "DetailsOfSharesHeldByProvidentFundsOrPensionFundsAxis": "Provident / Pension Funds",
    "DetailsOfSharesHeldByOtherInstitutionsForeignAxis": "Other Foreign Institutions",
    "DetailsOfSharesHeldByOtherNonInstitutionsAxis": "Other Non-Institutions",
    # Not independently verified against a real filing (see comment above).
    "DetailsOfSharesHeldByBanksAxis": "Banks",
    "DetailsOfSharesHeldByNBFCsRegisteredWithRBIAxis": "NBFCs Registered with RBI",
    "DetailsOfSharesHeldByAlternativeInvestmentFundsAxis": "Alternative Investment Funds",
    # Technically SEBI's own separate "Non Promoter-Non Public" table
    # (custodian/depository-receipt holders), not Table III public — folded
    # into "public" here to match NSE's own master-listing convention:
    # verified pr_and_prgrp + public_val + employeeTrusts sums to 100.00 for
    # real INFY data, i.e. NSE's own "public_val" aggregate already counts
    # DR/custodian holders as public, not as a fourth bucket.
    "DetailsOfSharesHeldByCustodianOrDRHolderAxis": "Custodian / DR Holder",
}

# Table I category-rollup, read via a DIFFERENT dimension mechanism than
# the named-holder tables above: an explicitMember (not typedMember) on
# CategoryOfShareholdersAxis, one context per rollup category, each
# carrying its own ShareholdingAsAPercentageOfTotalNumberOfShares directly
# — no per-holder aggregation needed. Verified against real INFY: summing
# InstitutionsDomestic (42.96%) + InstitutionsForeign (27.09%) +
# Governments (0.02%) + NonInstitutions (15.89%) = 85.96%, matching the
# master listing's own public_val (85.97%) to rounding — and
# ShareholdingOfPromoterAndPromoterGroup (13.82%) independently matches
# pr_and_prgrp exactly. This mapping is what turns NSE's own 3-bucket
# aggregate (promoter/public/employee-trust) into the finer Promoters/
# FIIs/DIIs/Government/Public breakdown other trackers (e.g. Screener)
# show — InstitutionsForeign = "FIIs", InstitutionsDomestic = "DIIs",
# Governments = "Government", NonInstitutions = "Public" (the
# non-institutional/retail residual, NOT the same number as public_val).
_CATEGORY_AXIS_LOCAL_NAME = "CategoryOfShareholdersAxis"
_FII_MEMBER = "InstitutionsForeignMember"
_DII_MEMBER = "InstitutionsDomesticMember"
_GOVERNMENT_MEMBER = "GovernmentsMember"
_NON_INSTITUTIONAL_PUBLIC_MEMBER = "NonInstitutionsMember"
_TOTAL_MEMBER = "ShareholdingPatternMember"  # grand total row -- only used here for NumberOfShareholders

# Groups a NAMED public-side holder's _PUBLIC_HOLDER_AXES category (above)
# into the same FII/DII buckets the Table I CategoryOfShareholdersAxis
# rollup above reports in aggregate (InstitutionsForeign/InstitutionsDomestic)
# -- for web/shareholding_feed.py's Major Holders panel, which lists named
# holders under whichever of those two aggregate numbers they belong to.
# "Custodian / DR Holder" and "Other Non-Institutions" fall through to
# "public", the same bucket NSE's own public_val/NonInstitutions put them in
# (see _PUBLIC_HOLDER_AXES's own comment) -- there is no named-holder
# equivalent of the Governments rollup member, so that one has nothing to
# classify here.
_FII_CATEGORIES = frozenset({
    "Foreign Portfolio Investors (Category I)",
    "Foreign Portfolio Investors (Category II)",
    "Other Foreign Institutions",
})
_DII_CATEGORIES = frozenset({
    "Mutual Funds / UTI",
    "Insurance Companies",
    "Provident / Pension Funds",
    "Banks",
    "NBFCs Registered with RBI",
    "Alternative Investment Funds",
})


def classify_public_category(category: str) -> str:
    """"fii" | "dii" | "public" for a shareholding_holders.category value on
    the public side. Promoter-side categories are never passed here -- that
    side is always its own "promoter" bucket, decided by `side`, not by
    category name."""
    if category in _FII_CATEGORIES:
        return "fii"
    if category in _DII_CATEGORIES:
        return "dii"
    return "public"


_CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ShareholdingSummary:
    """One quarterly submission's aggregate percentages, straight off the
    master listing — no XBRL parse needed for this part."""

    symbol: str
    period_end: date
    fiscal_year: str
    quarter: str
    promoter_percent: float | None
    public_percent: float | None
    employee_trust_percent: float | None
    submission_date: str
    source_url: str | None  # the SHP xbrl link for this submission, if NSE published one


@dataclass(frozen=True)
class ShareholderHolding:
    """One individually-named holder, drilled out of a submission's own SHP
    XBRL — see module docstring for which sub-categories are named at all."""

    side: str  # "promoter" | "public"
    category: str
    holder_name: str
    num_shares: float | None
    percent_of_shares: float | None  # already scaled to a percent number (4.92, not 0.0492)


@dataclass(frozen=True)
class CategoryBreakdown:
    """The Screener-style FII / DII / Government / Public(non-institutional)
    split of one quarter's public_holding_percent -- see
    parse_shp_category_breakdown()'s docstring for where these numbers come
    from and how they were verified."""

    fii_percent: float | None
    dii_percent: float | None
    government_percent: float | None
    public_non_institutional_percent: float | None
    num_shareholders: int | None


def _parse_shp_date(value: str) -> date:
    """"30-JUN-2026" / "15-JUL-2026 18:52:47" -> date(2026, 6, 30) — this
    endpoint's own all-caps month abbreviation, unlike sources/nse_fetch.py's
    mixed-case "25-Jan-2025" (a different NSE endpoint, own date format)."""
    return datetime.strptime(value.split(" ")[0].title(), "%d-%b-%Y").date()


def fetch_shareholding_master(
    symbol: str,
    *,
    session: requests.Session | None = None,
) -> list[ShareholdingSummary]:
    """List every quarterly Shareholding Pattern submission NSE has on file
    for `symbol` — full history in one response, same "no server-side
    date-range filtering, caller filters client-side if it wants a window"
    convention as sources/nse_fetch.py's fetch_filing_index()."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_SHAREHOLDING_MASTER_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    finally:
        if owns_session:
            session.close()

    rows = response.json()
    summaries: list[ShareholdingSummary] = []
    for row in rows:
        date_str = row.get("date")
        if not date_str:
            continue
        period_end = _parse_shp_date(date_str)
        fiscal_year, quarter = fiscal_year_and_quarter_from_date(period_end, "quarterly")
        summaries.append(
            ShareholdingSummary(
                symbol=row.get("symbol", symbol),
                period_end=period_end,
                fiscal_year=fiscal_year,
                quarter=quarter,
                promoter_percent=_to_float(row.get("pr_and_prgrp")),
                public_percent=_to_float(row.get("public_val")),
                employee_trust_percent=_to_float(row.get("employeeTrusts")),
                submission_date=row.get("submissionDate", ""),
                source_url=row.get("xbrl") or None,
            )
        )
    return summaries


def _to_float(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _detect_shp_prefix(root) -> str | None:
    """The "{namespace-uri}" prefix every real fact tag in this document
    starts with, or None if nothing in the whole document matched a known
    SHP namespace (see module docstring's namespace-version note) — shared
    by parse_shp_xbrl() and parse_shp_category_breakdown() since both need
    to know the same thing about the same document."""
    for el in root.iter():
        candidate = next((p for p in _SHP_TAG_PREFIXES if el.tag.startswith(p)), None)
        if candidate is not None:
            return candidate
    return None


def parse_shp_xbrl(content: bytes) -> list[ShareholderHolding]:
    """Parse one SHP XBRL document's bytes into every individually-named
    holder it discloses, on either side of the register. Returns an empty
    list (not an error) for a document that matches no known SHP namespace
    or carries no named holders at all — both real, unremarkable states
    (see module docstring)."""
    from xml.etree import ElementTree as ET

    root = ET.fromstring(content)

    prefix = _detect_shp_prefix(root)
    if prefix is None:
        logger.warning("SHP XBRL document matched no known namespace (%s)", ", ".join(_SHP_NAMESPACES))
        return []

    # context id -> (side, category) for every context whose scenario
    # carries a typedMember dimension on a known named-holder axis.
    context_side_category: dict[str, tuple[str, str]] = {}
    for context in root.iter(f"{{{_NS_XBRLI}}}context"):
        context_id = context.get("id") or ""
        typed_member = context.find(f"{{{_NS_XBRLI}}}scenario/{{{_NS_XBRLDI}}}typedMember")
        if typed_member is None:
            continue
        dimension = typed_member.get("dimension") or ""
        axis = dimension.rsplit(":", 1)[-1]
        if axis in _PROMOTER_HOLDER_AXES:
            context_side_category[context_id] = ("promoter", _PROMOTER_HOLDER_AXES[axis])
        elif axis in _PUBLIC_HOLDER_AXES:
            context_side_category[context_id] = ("public", _PUBLIC_HOLDER_AXES[axis])
        elif axis.startswith(("DetailsOfSharesHeldBy", "DetailsSharesHeldBy")):
            logger.debug("SHP XBRL: unrecognized named-holder axis %r — skipping its holder(s)", axis)

    if not context_side_category:
        return []

    names: dict[str, str] = {}
    shares_by_context: dict[str, float] = {}
    percent_by_context: dict[str, float] = {}
    for el in root.iter():
        if not el.tag.startswith(prefix):
            continue
        tag = el.tag[len(prefix):]
        context_id = el.get("contextRef") or ""
        text = (el.text or "").strip()
        if not text:
            continue
        if tag == _NAME_TAG:
            if context_id in context_side_category:
                names[context_id] = text
        elif tag in (_SHARES_TAG, _PERCENT_TAG):
            try:
                value = float(text)
            except ValueError:
                continue
            if tag == _SHARES_TAG:
                shares_by_context[context_id] = value
            else:
                percent_by_context[context_id] = value

    holdings: list[ShareholderHolding] = []
    for named_context_id, holder_name in names.items():
        side, category = context_side_category[named_context_id]
        # The holder's identity lives on a "D_"-prefixed (typed-dimension)
        # context; its numeric facts (shares, percent) live on the SAME
        # context id with that "D_" stripped — verified against real
        # INFY/TCS/HDFCBANK filings across every named-holder axis above.
        numeric_context_id = named_context_id[2:] if named_context_id.startswith("D_") else named_context_id
        percent = percent_by_context.get(numeric_context_id)
        holdings.append(
            ShareholderHolding(
                side=side,
                category=category,
                holder_name=holder_name,
                num_shares=shares_by_context.get(numeric_context_id),
                percent_of_shares=percent * 100 if percent is not None else None,
            )
        )
    return holdings


def parse_shp_category_breakdown(content: bytes) -> CategoryBreakdown | None:
    """Parse one SHP XBRL document's Table I category rollups into the
    Screener-style FII/DII/Government/Public(non-institutional) split (see
    the constants above for the mapping and how it was verified). None
    (not an error) for a document that matches no known SHP namespace or
    carries no CategoryOfShareholdersAxis contexts at all — a real,
    unremarkable state for an older filing (see module docstring)."""
    from xml.etree import ElementTree as ET

    root = ET.fromstring(content)

    prefix = _detect_shp_prefix(root)
    if prefix is None:
        return None

    member_to_context: dict[str, str] = {}
    for context in root.iter(f"{{{_NS_XBRLI}}}context"):
        context_id = context.get("id") or ""
        explicit_member = context.find(f"{{{_NS_XBRLI}}}scenario/{{{_NS_XBRLDI}}}explicitMember")
        if explicit_member is None:
            continue
        dimension_axis = (explicit_member.get("dimension") or "").rsplit(":", 1)[-1]
        if dimension_axis != _CATEGORY_AXIS_LOCAL_NAME:
            continue
        member = (explicit_member.text or "").strip().rsplit(":", 1)[-1]
        member_to_context[member] = context_id

    if not member_to_context:
        return None

    needed_contexts = {
        member_to_context[m]
        for m in (_FII_MEMBER, _DII_MEMBER, _GOVERNMENT_MEMBER, _NON_INSTITUTIONAL_PUBLIC_MEMBER, _TOTAL_MEMBER)
        if m in member_to_context
    }

    facts: dict[tuple[str, str], float] = {}
    for el in root.iter():
        if not el.tag.startswith(prefix):
            continue
        context_id = el.get("contextRef") or ""
        if context_id not in needed_contexts:
            continue
        text = (el.text or "").strip()
        if not text:
            continue
        try:
            facts[(context_id, el.tag[len(prefix):])] = float(text)
        except ValueError:
            continue

    def _percent(member: str) -> float | None:
        context_id = member_to_context.get(member)
        if context_id is None:
            return None
        value = facts.get((context_id, _PERCENT_TAG))
        return value * 100 if value is not None else None

    num_shareholders: int | None = None
    total_context_id = member_to_context.get(_TOTAL_MEMBER)
    if total_context_id is not None:
        raw = facts.get((total_context_id, "NumberOfShareholders"))
        if raw is not None:
            num_shareholders = int(raw)

    return CategoryBreakdown(
        fii_percent=_percent(_FII_MEMBER),
        dii_percent=_percent(_DII_MEMBER),
        government_percent=_percent(_GOVERNMENT_MEMBER),
        public_non_institutional_percent=_percent(_NON_INSTITUTIONAL_PUBLIC_MEMBER),
        num_shareholders=num_shareholders,
    )


def fetch_named_holders(xbrl_url: str, *, session: requests.Session | None = None) -> list[ShareholderHolding]:
    """Download one submission's SHP XBRL and parse it for named holders —
    download_filing()-style but content isn't staged to disk (this domain
    has no data/raw/ archive of its own; the XBRL is fetched, parsed, and
    discarded, same as this module's caller only ever wants the parsed
    result, not the file)."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(session, xbrl_url)
    finally:
        if owns_session:
            session.close()
    return parse_shp_xbrl(response.content)


def fetch_shareholding_detail(
    xbrl_url: str, *, session: requests.Session | None = None
) -> tuple[list[ShareholderHolding], CategoryBreakdown | None]:
    """Download one submission's SHP XBRL ONCE and run both parses over it
    — named holders and the category breakdown — rather than the caller
    fetching the same URL twice (fetch_named_holders() alone, kept for
    direct/test use, only covers the first)."""
    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(session, xbrl_url)
    finally:
        if owns_session:
            session.close()
    return parse_shp_xbrl(response.content), parse_shp_category_breakdown(response.content)
