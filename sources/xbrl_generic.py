"""Generic, taxonomy-agnostic SEBI/NSE XBRL extraction layer.

Deliberately separate from sources/nse_xbrl.py — that adapter only ever
reads two hardcoded contextRefs ("OneD"/"FourD"), matches a curated tag
alias table, and feeds the flat financial_observations table (see its own
docstring). This module does the opposite: it parses EVERY context, unit
and fact in a filing with full fidelity — dates, dimensions, unit,
decimals, precision, xsi:nil — into a taxonomy-independent structure, with
no per-concept branching anywhere in the extraction path. A small, clearly
separate keyword table (_CATEGORY_RULES) and concept->canonical-metric map
(SEMANTIC_MAP) are bolted on top of the parsed facts afterward, not baked
into parsing itself — see parse_xbrl_document()'s docstring for why that
split matters (a taxonomy field or a new SEBI concept this module has never
seen still parses correctly; it just comes out uncategorized/unmapped
instead of being dropped).

Nothing here touches the database or financial_observations — this is a
standalone parse-to-JSON layer ("filing"/"units"/"contexts"/"facts"), per
the spec this was built against: "Later this can feed SQLite/Postgres."
scripts/xbrl_diagnostic.py is the milestone-1 acceptance tool built on top
of it.

Verified against a real filing on disk (data/raw/INFY/nse/
2026-03-31_consolidated_152465.xml, taxonomy in-capmkt 2026-01-31):
"OneD" (2026-01-01->2026-03-31, duration), "FourD" (2025-04-01->2026-03-31,
duration), "OneI"/"PY_I" (instants), 142 contexts total, 138 of them
dimensioned via a real xbrldi:explicitMember or xbrldi:typedMember inside
<xbrli:entity>/<xbrli:segment> (e.g. context "OneExpenses1D" carries
{"in-capmkt:DetailsOfOtherExpensesAxis": "in-capmkt:OtherExpenses1Member"})
— 245 of the file's 526 facts inherit a dimension this way (expense/asset/
liability sub-breakdowns, reportable segments, OCI categories). Genuine
dimensional markup is the norm in this taxonomy family, not an edge case —
_extract_dimensions() implements the real XBRL segment/scenario shape
(xbrldi:explicitMember/typedMember), and it is exercised on nearly every
parse, not just a rare filing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


# ---------------------------------------------------------------------------
# Data model — plain, JSON-friendly dataclasses. Deliberately NOT
# sources/base.py's NormalizedObservation: that dataclass mirrors the flat
# financial_observations schema (one row = one company/metric/period/value)
# and has no room for a fact's unit/decimals/dimensions/raw-vs-normalized
# value split, which this module's whole point is to preserve.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """One xbrldi:explicitMember or xbrldi:typedMember, as declared — axis
    and member/typed_value kept as the raw QName strings from the file
    (e.g. "in-capmkt:DetailsOfOtherExpensesAxis"), not resolved to a URI,
    so provenance is never lossy even for a dimension this module has never
    seen before."""

    axis: str
    member: str | None = None
    typed_value: str | None = None


@dataclass(frozen=True)
class XbrlContext:
    context_id: str
    entity_identifier: str | None
    period_type: str  # "instant" | "duration"
    start_date: str | None  # ISO date text, exactly as declared
    end_date: str | None
    instant_date: str | None
    dimensions: tuple[Dimension, ...] = ()


@dataclass(frozen=True)
class XbrlUnit:
    unit_id: str
    measures: tuple[str, ...] = ()  # simple <measure> unit, e.g. ("iso4217:INR",)
    numerator: tuple[str, ...] = ()  # <divide> unit's numerator measures
    denominator: tuple[str, ...] = ()  # <divide> unit's denominator measures

    @property
    def label(self) -> str | None:
        if self.measures:
            return "/".join(self.measures)
        if self.numerator or self.denominator:
            num = "*".join(self.numerator) or "1"
            den = "*".join(self.denominator)
            return f"{num}/{den}" if den else num
        return None


#: Pure date-driven classification (spec: "Do not rely solely on context
#: IDs" / "Do not infer quarter/annual status only from names like OneD or
#: FourD"). Day-count bands with tolerance around the nominal 91/182/273/365
#: day quarter/half/nine-month/annual spans a real filing's declared dates
#: land near (verified: this taxonomy's real "OneD"/"FourD" spans are
#: 90 and 365 days respectively for a Q4 filing).
_PERIOD_LENGTH_BANDS = (
    (100, "quarterly"),
    (200, "half_year"),
    (290, "nine_month"),
    (380, "annual"),
)


def classify_period_length(
    period_type: str, start_date: str | None, end_date: str | None, instant_date: str | None
) -> str:
    if period_type == "instant":
        return "instant" if instant_date else "unknown"
    if not start_date or not end_date:
        return "unknown"
    from datetime import date as _date

    days = (_date.fromisoformat(end_date) - _date.fromisoformat(start_date)).days + 1
    for max_days, label in _PERIOD_LENGTH_BANDS:
        if days <= max_days:
            return label
    return "unknown"


@dataclass(frozen=True)
class XbrlFact:
    concept: str  # local name, e.g. "RevenueFromOperations"
    namespace: str  # full taxonomy namespace URI, exactly as declared
    context_id: str
    unit_ref: str | None
    decimals: str | None  # kept as the raw string ("-7", "INF", ...)
    precision: str | None
    is_nil: bool
    raw_value: str | None  # exact element text; None if xsi:nil or empty
    is_numeric: bool
    normalized_value: float | None  # float(raw_value) iff is_numeric — never rescaled
    # Resolved from the fact's context (None if contextRef didn't resolve):
    period_type: str | None
    start_date: str | None
    end_date: str | None
    instant_date: str | None
    period_length: str | None
    dimensions: tuple[Dimension, ...]
    # Resolved from the fact's unit:
    unit_label: str | None
    # Bolted on afterward — see module docstring:
    category: str
    canonical_metric: str | None


@dataclass
class FilingMetadata:
    source_file: str
    source_type: str = "SEBI_XBRL"
    taxonomy_namespaces: tuple[str, ...] = ()
    company_name: str | None = None
    scrip_code: str | None = None
    symbol: str | None = None
    isin: str | None = None
    currency: str | None = None
    scale: str | None = None  # e.g. "Crores" — the filing's OWN declared presentation scale
    consolidation: str | None = None  # "Consolidated" | "Standalone", as declared
    type_of_reporting_period: str | None = None  # e.g. "Quarterly"
    reporting_quarter: str | None = None  # e.g. "Fourth quarter"
    financial_year_start: str | None = None
    financial_year_end: str | None = None


# ---------------------------------------------------------------------------
# Context / unit parsing
# ---------------------------------------------------------------------------


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    stripped = el.text.strip()
    return stripped or None


def _extract_dimensions(container: ET.Element | None) -> list[Dimension]:
    """xbrldi:explicitMember / xbrldi:typedMember under one <xbrli:segment>
    or <xbrli:scenario> element. Real XBRL shape (spec section 9), even
    though this taxonomy family barely uses it in practice — see module
    docstring."""
    if container is None:
        return []
    dims: list[Dimension] = []
    for member_el in container.findall(f"{{{_NS_XBRLDI}}}explicitMember"):
        axis = member_el.get("dimension", "")
        dims.append(Dimension(axis=axis, member=_text(member_el)))
    for typed_el in container.findall(f"{{{_NS_XBRLDI}}}typedMember"):
        axis = typed_el.get("dimension", "")
        child = next(iter(typed_el), None)
        dims.append(Dimension(axis=axis, member=None, typed_value=_text(child)))
    return dims


def parse_contexts(root: ET.Element, file_path: str = "") -> dict[str, XbrlContext]:
    contexts: dict[str, XbrlContext] = {}
    for ctx_el in root.findall(f"{{{_NS_XBRLI}}}context"):
        context_id = ctx_el.get("id")
        if not context_id:
            continue

        entity_el = ctx_el.find(f"{{{_NS_XBRLI}}}entity")
        identifier_el = entity_el.find(f"{{{_NS_XBRLI}}}identifier") if entity_el is not None else None
        segment_el = entity_el.find(f"{{{_NS_XBRLI}}}segment") if entity_el is not None else None
        scenario_el = ctx_el.find(f"{{{_NS_XBRLI}}}scenario")
        dimensions = tuple(_extract_dimensions(segment_el) + _extract_dimensions(scenario_el))

        period_el = ctx_el.find(f"{{{_NS_XBRLI}}}period")
        instant = _text(period_el.find(f"{{{_NS_XBRLI}}}instant")) if period_el is not None else None
        start = _text(period_el.find(f"{{{_NS_XBRLI}}}startDate")) if period_el is not None else None
        end = _text(period_el.find(f"{{{_NS_XBRLI}}}endDate")) if period_el is not None else None

        if instant is not None:
            contexts[context_id] = XbrlContext(
                context_id, _text(identifier_el), "instant", None, None, instant, dimensions
            )
        elif start is not None and end is not None:
            contexts[context_id] = XbrlContext(
                context_id, _text(identifier_el), "duration", start, end, None, dimensions
            )
        else:
            logger.warning("%s: context %r has neither an instant nor a start/end period — skipping", file_path, context_id)
    return contexts


def parse_units(root: ET.Element) -> dict[str, XbrlUnit]:
    units: dict[str, XbrlUnit] = {}
    for unit_el in root.findall(f"{{{_NS_XBRLI}}}unit"):
        unit_id = unit_el.get("id")
        if not unit_id:
            continue
        divide_el = unit_el.find(f"{{{_NS_XBRLI}}}divide")
        if divide_el is not None:
            numerator = tuple(
                _text(m) for m in divide_el.findall(f"{{{_NS_XBRLI}}}unitNumerator/{{{_NS_XBRLI}}}measure") if _text(m)
            )
            denominator = tuple(
                _text(m) for m in divide_el.findall(f"{{{_NS_XBRLI}}}unitDenominator/{{{_NS_XBRLI}}}measure") if _text(m)
            )
            units[unit_id] = XbrlUnit(unit_id=unit_id, numerator=numerator, denominator=denominator)
        else:
            measures = tuple(_text(m) for m in unit_el.findall(f"{{{_NS_XBRLI}}}measure") if _text(m))
            units[unit_id] = XbrlUnit(unit_id=unit_id, measures=measures)
    return units


# ---------------------------------------------------------------------------
# Fact parsing
# ---------------------------------------------------------------------------


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


@dataclass(frozen=True)
class _RawFact:
    """One pass over the tree, before contextRef/unitRef are resolved —
    kept separate from XbrlFact so a fact referencing an unresolvable
    context still parses (with period fields left blank + a warning)
    instead of vanishing silently, same reasoning as sources/nse_xbrl.py's
    own "unexpected context-ID convention deserves a human look" stance."""

    tag: str
    context_id: str
    unit_ref: str | None
    decimals: str | None
    precision: str | None
    is_nil: bool
    raw_value: str | None


def parse_raw_facts(root: ET.Element) -> list[_RawFact]:
    """Every element carrying a contextRef IS a fact — the one attribute
    that's unique to XBRL items (never present on a context, unit,
    schemaRef, or dimension-member element), so it's the only discriminator
    needed. No taxonomy namespace allow-list, unlike sources/nse_xbrl.py —
    that's the whole point of "generic"."""
    facts: list[_RawFact] = []
    for el in root.iter():
        context_id = el.get("contextRef")
        if context_id is None:
            continue
        is_nil = el.get(_XSI_NIL) == "true"
        raw_value = None if is_nil else _text(el)
        facts.append(
            _RawFact(
                tag=el.tag,
                context_id=context_id,
                unit_ref=el.get("unitRef"),
                decimals=el.get("decimals"),
                precision=el.get("precision"),
                is_nil=is_nil,
                raw_value=raw_value,
            )
        )
    return facts


def _to_number(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except ValueError:
        return None


def resolve_fact(
    raw: _RawFact, contexts: dict[str, XbrlContext], units: dict[str, XbrlUnit], file_path: str = ""
) -> XbrlFact:
    namespace, concept = _split_tag(raw.tag)

    context = contexts.get(raw.context_id)
    if context is None:
        logger.warning(
            "%s: fact %r references unresolved contextRef=%r — period/dimensions left blank",
            file_path, concept, raw.context_id,
        )
        period_type = start_date = end_date = instant_date = period_length = None
        dimensions: tuple[Dimension, ...] = ()
    else:
        period_type = context.period_type
        start_date, end_date, instant_date = context.start_date, context.end_date, context.instant_date
        period_length = classify_period_length(period_type, start_date, end_date, instant_date)
        dimensions = context.dimensions

    unit = units.get(raw.unit_ref) if raw.unit_ref else None
    normalized_value = _to_number(raw.raw_value) if not raw.is_nil else None

    return XbrlFact(
        concept=concept,
        namespace=namespace,
        context_id=raw.context_id,
        unit_ref=raw.unit_ref,
        decimals=raw.decimals,
        precision=raw.precision,
        is_nil=raw.is_nil,
        raw_value=raw.raw_value,
        is_numeric=normalized_value is not None,
        normalized_value=normalized_value,
        period_type=period_type,
        start_date=start_date,
        end_date=end_date,
        instant_date=instant_date,
        period_length=period_length,
        dimensions=dimensions,
        unit_label=unit.label if unit else None,
        category=categorize_concept(concept),
        canonical_metric=SEMANTIC_MAP.get(concept),
    )


# ---------------------------------------------------------------------------
# Category classification — a keyword table, never a per-concept if/elif
# chain (this taxonomy has hundreds of concepts; spec section 12 is explicit
# that this must stay data-driven so a brand-new concept still lands
# somewhere sane instead of needing a new branch). Order matters: earlier
# rules win, so a concept matching more than one keyword set (e.g.
# "InterSegmentRevenue" contains both "Segment" and "Revenue") is
# classified by whichever rule comes first.
# ---------------------------------------------------------------------------

_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("filing_metadata", (
        "ScripCode", "Symbol", "ISIN", "NameOf", "Date", "TypeOf", "LevelOfRounding",
        "Auditor", "NatureOfReport", "WhetherCashFlow", "IsCompanyReporting", "MSEISymbol",
        "ClassOfSecurity", "TypeOfCashFlowStatement", "Whether", "Declaration", "TimeOf",
        "BoardMeeting",
    )),
    ("segment", ("Segment",)),
    ("related_party", ("RelatedPart",)),
    ("eps", ("EarningsPerShare", "EarningsLossPerShare")),
    ("cash_flow", (
        "CashFlow", "ClassifiedAsOperatingActivities", "ClassifiedAsInvestingActivities",
        "ClassifiedAsFinancingActivities", "ProceedsFrom", "PurchaseOf", "PaymentsOf",
        "CashPayment", "CashReceipt", "CashAdvancesAndLoans",
    )),
    ("balance_sheet", (
        "Asset", "Liabilit", "Equity", "Payable", "Receivable", "ShareCapital", "Reserve",
        "Provision", "Investment", "PropertyPlantAndEquipment", "Inventor", "Borrowing",
        "Cash", "Loan", "Goodwill", "CapitalWorkInProgress", "OutstandingDues",
    )),
    ("income_statement", (
        "Revenue", "Income", "Expense", "Profit", "Tax", "Depreciation", "Interest",
        "Cost", "FinanceCosts",
    )),
)


def categorize_concept(concept: str) -> str:
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in concept for keyword in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Semantic mapping — SEBI XBRL concept -> canonical financial metric.
# Deliberately a plain, separately maintained config, NOT the DB-backed
# metric_aliases table normalization/financials.py uses for the narrow
# nse_xbrl.py pipeline (that table has no concept of periods/dimensions/
# units and is keyed for a different flat schema — see module docstring).
# Starter set covers exactly what scripts/xbrl_diagnostic.py's milestone-1
# summary needs; extend as more concepts need a canonical name. Tag names
# verified against the real INFY Q4 FY26 filing on disk.
# ---------------------------------------------------------------------------

SEMANTIC_MAP: dict[str, str] = {
    "RevenueFromOperations": "revenue",
    "ProfitLossForPeriod": "net_profit",
    "ProfitLossForThePeriod": "net_profit",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps",
    "BasicEarningsPerShareBeforeExtraordinaryItems": "eps",
    "CashAndCashEquivalents": "cash",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "Equity": "equity",
    "CashFlowsFromUsedInOperatingActivities": "operating_cash_flow",
}


# ---------------------------------------------------------------------------
# Filing metadata
# ---------------------------------------------------------------------------

#: canonical field -> candidate raw concept names to try, in order (a
#: taxonomy version can rename a metadata tag the same way it renames a
#: financial one — verified: "LevelOfRounding" (INFY, in-capmkt) vs
#: "LevelOfRoundingUsedInFinancialStatements" (IDFC First Bank, in-bse-fin)
#: are the same field under two different taxonomy families).
_METADATA_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "company_name": ("NameOfTheCompany", "NameOfBank"),
    "scrip_code": ("ScripCode",),
    "symbol": ("Symbol",),
    "isin": ("ISIN",),
    "currency": ("DescriptionOfPresentationCurrency",),
    "scale": ("LevelOfRounding", "LevelOfRoundingUsedInFinancialStatements"),
    "consolidation": ("NatureOfReportStandaloneConsolidated",),
    "type_of_reporting_period": ("TypeOfReportingPeriod",),
    "reporting_quarter": ("ReportingQuarter",),
    "financial_year_start": ("DateOfStartOfFinancialYear",),
    "financial_year_end": ("DateOfEndOfFinancialYear",),
}


def build_filing_metadata(file_path: str, facts: list[XbrlFact]) -> FilingMetadata:
    first_value_by_concept: dict[str, str] = {}
    for fact in facts:
        if fact.raw_value is not None and fact.concept not in first_value_by_concept:
            first_value_by_concept[fact.concept] = fact.raw_value

    values: dict[str, str | None] = {}
    for field_name, candidates in _METADATA_FIELD_CANDIDATES.items():
        values[field_name] = next(
            (first_value_by_concept[c] for c in candidates if c in first_value_by_concept), None
        )

    taxonomy_namespaces = tuple(sorted({f.namespace for f in facts if f.namespace}))

    return FilingMetadata(source_file=file_path, taxonomy_namespaces=taxonomy_namespaces, **values)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def parse_xbrl_document(file_path: Path) -> dict:
    """Parse one XBRL instance document into {"filing", "units", "contexts",
    "facts"} — plain dicts/lists, JSON-serializable as-is. This is the
    generic extraction layer's whole contract: every context and every fact
    the file declares comes through with full fidelity (dates, dimensions,
    unit, decimals, xsi:nil), regardless of taxonomy version or which
    concepts SEMANTIC_MAP/_CATEGORY_RULES happen to recognize today — a
    concept neither table knows about still gets a row, just with
    category="other" and canonical_metric=None, never dropped.
    """
    root = ET.parse(file_path).getroot()
    file_path_str = str(file_path)

    contexts = parse_contexts(root, file_path_str)
    units = parse_units(root)
    raw_facts = parse_raw_facts(root)
    facts = [resolve_fact(raw, contexts, units, file_path_str) for raw in raw_facts]
    filing = build_filing_metadata(file_path_str, facts)

    return {
        "filing": _filing_to_dict(filing),
        "units": [_unit_to_dict(u) for u in units.values()],
        "contexts": [_context_to_dict(c) for c in contexts.values()],
        "facts": [_fact_to_dict(f) for f in facts],
    }


def _dimensions_to_dict(dimensions: tuple[Dimension, ...]) -> dict[str, str | None]:
    return {d.axis: (d.member if d.member is not None else d.typed_value) for d in dimensions}


def _context_to_dict(context: XbrlContext) -> dict:
    return {
        "context_id": context.context_id,
        "entity_identifier": context.entity_identifier,
        "period_type": context.period_type,
        "start_date": context.start_date,
        "end_date": context.end_date,
        "instant_date": context.instant_date,
        "dimensions": _dimensions_to_dict(context.dimensions),
    }


def _unit_to_dict(unit: XbrlUnit) -> dict:
    return {
        "unit_id": unit.unit_id,
        "measures": list(unit.measures),
        "numerator": list(unit.numerator),
        "denominator": list(unit.denominator),
        "label": unit.label,
    }


def _fact_to_dict(fact: XbrlFact) -> dict:
    return {
        "concept": fact.concept,
        "namespace": fact.namespace,
        "context_id": fact.context_id,
        "unit_ref": fact.unit_ref,
        "unit": fact.unit_label,
        "decimals": fact.decimals,
        "precision": fact.precision,
        "is_nil": fact.is_nil,
        "raw_value": fact.raw_value,
        "is_numeric": fact.is_numeric,
        "normalized_value": fact.normalized_value,
        "period_type": fact.period_type,
        "period_start": fact.start_date,
        "period_end": fact.end_date,
        "instant_date": fact.instant_date,
        "period_length": fact.period_length,
        "dimensions": _dimensions_to_dict(fact.dimensions),
        "category": fact.category,
        "canonical_metric": fact.canonical_metric,
        "source": "SEBI_XBRL",
    }


def _filing_to_dict(filing: FilingMetadata) -> dict:
    return {
        "source_file": filing.source_file,
        "source_type": filing.source_type,
        "taxonomy_namespaces": list(filing.taxonomy_namespaces),
        "company_name": filing.company_name,
        "scrip_code": filing.scrip_code,
        "symbol": filing.symbol,
        "isin": filing.isin,
        "currency": filing.currency,
        "scale": filing.scale,
        "consolidation": filing.consolidation,
        "type_of_reporting_period": filing.type_of_reporting_period,
        "reporting_quarter": filing.reporting_quarter,
        "financial_year_start": filing.financial_year_start,
        "financial_year_end": filing.financial_year_end,
    }


# ---------------------------------------------------------------------------
# Validation / summary counts (spec section 10)
# ---------------------------------------------------------------------------


def build_validation_summary(parsed: dict) -> dict:
    facts = parsed["facts"]
    contexts = parsed["contexts"]
    units = parsed["units"]
    filing = parsed["filing"]

    dimensional_facts = sum(1 for f in facts if f["dimensions"])
    period_dates = [f["period_start"] for f in facts if f["period_start"]]
    period_dates += [f["period_end"] for f in facts if f["period_end"]]
    period_dates += [f["instant_date"] for f in facts if f["instant_date"]]

    return {
        "num_contexts": len(contexts),
        "num_facts": len(facts),
        "num_units": len(units),
        "num_dimensional_facts": dimensional_facts,
        "earliest_date": min(period_dates) if period_dates else None,
        "latest_date": max(period_dates) if period_dates else None,
        "company": filing["company_name"],
        "scrip_code": filing["scrip_code"],
        "reporting_quarter": filing["reporting_quarter"],
        "financial_year_end": filing["financial_year_end"],
        "currency": filing["currency"],
        "scale": filing["scale"],
    }
