"""sources/xbrl_generic.py — the taxonomy-agnostic parser, tested against
minimal real-shaped fixtures (not the full real filing inline). Structure
(context/unit/dimension shapes, decimals/xsi:nil, taxonomy tag names)
verified against the real INFY Q4 FY26 filing on disk this session."""

from __future__ import annotations

from pathlib import Path

from sources.xbrl_generic import (
    build_filing_metadata,
    build_validation_summary,
    categorize_concept,
    classify_period_length,
    parse_contexts,
    parse_raw_facts,
    parse_units,
    parse_xbrl_document,
    resolve_fact,
)
from xml.etree import ElementTree as ET

_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"
_NS_CAPMKT = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _write(tmp_path: Path, xml: str, filename: str = "filing.xml") -> Path:
    path = tmp_path / filename
    path.write_text(xml)
    return path


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


def test_parses_duration_and_instant_contexts() -> None:
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneD">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="OneI">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
</xbrli:context>
</xbrli:xbrl>""")
    contexts = parse_contexts(root)

    assert contexts["OneD"].period_type == "duration"
    assert contexts["OneD"].start_date == "2026-01-01"
    assert contexts["OneD"].end_date == "2026-03-31"
    assert contexts["OneD"].entity_identifier == "500209"
    assert contexts["OneI"].period_type == "instant"
    assert contexts["OneI"].instant_date == "2026-03-31"


def test_parses_explicit_and_typed_member_dimensions() -> None:
    """Real shape (verified: 138/142 contexts in the real INFY Q4 FY26
    filing carry exactly this kind of dimension) — segment lives inside
    <xbrli:entity>, scenario is a sibling of <xbrli:period>."""
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:xbrldi="{_NS_XBRLDI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<xbrli:context id="OneExpenses1D">
  <xbrli:entity>
    <xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier>
    <xbrli:segment>
      <xbrldi:explicitMember dimension="in-capmkt:DetailsOfOtherExpensesAxis">in-capmkt:OtherExpenses1Member</xbrldi:explicitMember>
    </xbrli:segment>
  </xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
  <xbrli:scenario>
    <xbrldi:typedMember dimension="in-capmkt:CounterpartyAxis"><in-capmkt:CounterpartyName>Acme Corp</in-capmkt:CounterpartyName></xbrldi:typedMember>
  </xbrli:scenario>
</xbrli:context>
</xbrli:xbrl>""")
    contexts = parse_contexts(root)

    dims = {d.axis: d for d in contexts["OneExpenses1D"].dimensions}
    assert dims["in-capmkt:DetailsOfOtherExpensesAxis"].member == "in-capmkt:OtherExpenses1Member"
    assert dims["in-capmkt:CounterpartyAxis"].typed_value == "Acme Corp"
    assert dims["in-capmkt:CounterpartyAxis"].member is None


def test_context_missing_period_is_skipped_with_warning(caplog) -> None:
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="Broken"><xbrli:entity><xbrli:identifier>500209</xbrli:identifier></xbrli:entity></xbrli:context>
</xbrli:xbrl>""")
    with caplog.at_level("WARNING"):
        contexts = parse_contexts(root, "test.xml")

    assert contexts == {}
    assert any("neither an instant nor a start/end period" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_parses_simple_and_divide_units() -> None:
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}">
<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
<xbrli:unit id="INRPerShare">
  <xbrli:divide>
    <xbrli:unitNumerator><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
  </xbrli:divide>
</xbrli:unit>
</xbrli:xbrl>""")
    units = parse_units(root)

    assert units["INR"].label == "iso4217:INR"
    assert units["INRPerShare"].label == "iso4217:INR/xbrli:shares"


# ---------------------------------------------------------------------------
# Facts: numeric normalization, xsi:nil, decimals passthrough, unresolved context
# ---------------------------------------------------------------------------


def test_resolves_a_numeric_fact_against_context_and_unit() -> None:
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<xbrli:context id="OneD">
  <xbrli:entity><xbrli:identifier>500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
<in-capmkt:RevenueFromOperations contextRef="OneD" decimals="-7" unitRef="INR">464020000000</in-capmkt:RevenueFromOperations>
</xbrli:xbrl>""")
    contexts = parse_contexts(root)
    units = parse_units(root)
    raw = parse_raw_facts(root)
    assert len(raw) == 1

    fact = resolve_fact(raw[0], contexts, units)

    assert fact.concept == "RevenueFromOperations"
    assert fact.namespace == _NS_CAPMKT
    assert fact.is_numeric is True
    assert fact.normalized_value == 464020000000.0
    assert fact.raw_value == "464020000000"
    assert fact.decimals == "-7"
    assert fact.unit_label == "iso4217:INR"
    assert fact.period_type == "duration"
    assert fact.period_length == "quarterly"
    assert fact.category == "income_statement"
    assert fact.canonical_metric == "revenue"


def test_xsi_nil_fact_has_no_raw_or_normalized_value() -> None:
    root = _parse(
        f'<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier>500209</xbrli:identifier></xbrli:entity>'
        f'<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>'
        f'<in-capmkt:SomeOptionalFact contextRef="OneD" xsi:nil="true"/>'
        f"</xbrli:xbrl>"
    )
    contexts = parse_contexts(root)
    units = parse_units(root)
    raw = parse_raw_facts(root)

    fact = resolve_fact(raw[0], contexts, units)

    assert fact.is_nil is True
    assert fact.raw_value is None
    assert fact.is_numeric is False
    assert fact.normalized_value is None


def test_non_numeric_text_fact_is_preserved_not_dropped() -> None:
    """A large fraction of real facts in this taxonomy are text (company
    name, ISIN, auditor, dates as strings) — must come through as-is, not
    be treated as a parse failure."""
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier>500209</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<in-capmkt:NameOfTheCompany contextRef="OneD" decimals="INF">INFOSYS LIMITED</in-capmkt:NameOfTheCompany>
</xbrli:xbrl>""")
    contexts = parse_contexts(root)
    units = parse_units(root)
    raw = parse_raw_facts(root)

    fact = resolve_fact(raw[0], contexts, units)

    assert fact.raw_value == "INFOSYS LIMITED"
    assert fact.is_numeric is False
    assert fact.normalized_value is None
    assert fact.category == "filing_metadata"


def test_fact_with_unresolvable_context_ref_still_parses(caplog) -> None:
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<in-capmkt:RevenueFromOperations contextRef="Nonexistent">100</in-capmkt:RevenueFromOperations>
</xbrli:xbrl>""")
    raw = parse_raw_facts(root)
    with caplog.at_level("WARNING"):
        fact = resolve_fact(raw[0], contexts={}, units={}, file_path="test.xml")

    assert fact.normalized_value == 100.0  # the fact itself still parses
    assert fact.period_type is None
    assert fact.dimensions == ()
    assert any("unresolved contextRef" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Period-length classification (date-driven, never from context-ID naming)
# ---------------------------------------------------------------------------


def test_period_length_classification_boundaries() -> None:
    assert classify_period_length("instant", None, None, "2026-03-31") == "instant"
    assert classify_period_length("duration", "2026-01-01", "2026-03-31", None) == "quarterly"  # 90 days
    assert classify_period_length("duration", "2025-10-01", "2026-03-31", None) == "half_year"  # 182 days
    assert classify_period_length("duration", "2025-07-01", "2026-03-31", None) == "nine_month"  # 273 days
    assert classify_period_length("duration", "2025-04-01", "2026-03-31", None) == "annual"  # 365 days


# ---------------------------------------------------------------------------
# Category classification — a keyword table, no per-concept branching
# ---------------------------------------------------------------------------


def test_category_classification_prefers_more_specific_rule_over_generic_one() -> None:
    """"InterSegmentRevenue" contains both "Segment" and "Revenue" —
    must land in "segment", not "income_statement", because that rule
    comes first (verified against the real filing's own concept list)."""
    assert categorize_concept("InterSegmentRevenue") == "segment"
    assert categorize_concept("RevenueFromOperations") == "income_statement"
    assert categorize_concept("CashAndCashEquivalents") == "balance_sheet"
    assert categorize_concept("CashAndCashEquivalentsCashFlowStatement") == "cash_flow"
    assert categorize_concept("ScripCode") == "filing_metadata"
    assert categorize_concept("SomeConceptNoRuleHasEverSeen") == "other"


# ---------------------------------------------------------------------------
# Filing metadata
# ---------------------------------------------------------------------------


def test_filing_metadata_tries_candidate_tags_in_order() -> None:
    """"LevelOfRounding" (INFY/in-capmkt) vs "LevelOfRoundingUsedIn
    FinancialStatements" (IDFC First Bank/in-bse-fin) are the same field
    under two taxonomy families — the first candidate present wins."""
    root = _parse(f"""<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier>500209</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<in-capmkt:NameOfTheCompany contextRef="OneD">INFOSYS LIMITED</in-capmkt:NameOfTheCompany>
<in-capmkt:ScripCode contextRef="OneD">500209</in-capmkt:ScripCode>
<in-capmkt:LevelOfRounding contextRef="OneD">Crores</in-capmkt:LevelOfRounding>
<in-capmkt:ReportingQuarter contextRef="OneD">Fourth quarter</in-capmkt:ReportingQuarter>
</xbrli:xbrl>""")
    contexts = parse_contexts(root)
    units = parse_units(root)
    facts = [resolve_fact(r, contexts, units) for r in parse_raw_facts(root)]

    filing = build_filing_metadata("test.xml", facts)

    assert filing.company_name == "INFOSYS LIMITED"
    assert filing.scrip_code == "500209"
    assert filing.scale == "Crores"
    assert filing.reporting_quarter == "Fourth quarter"


# ---------------------------------------------------------------------------
# End-to-end: parse_xbrl_document() + build_validation_summary()
# ---------------------------------------------------------------------------


def test_parse_xbrl_document_end_to_end(tmp_path: Path) -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="{_NS_XBRLI}" xmlns:in-capmkt="{_NS_CAPMKT}">
<xbrli:context id="OneD">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="FourD">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="OneI">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">500209</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
</xbrli:context>
<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
<in-capmkt:NameOfTheCompany contextRef="OneD">INFOSYS LIMITED</in-capmkt:NameOfTheCompany>
<in-capmkt:ScripCode contextRef="OneD">500209</in-capmkt:ScripCode>
<in-capmkt:ReportingQuarter contextRef="OneD">Fourth quarter</in-capmkt:ReportingQuarter>
<in-capmkt:DateOfEndOfFinancialYear contextRef="OneD">2026-03-31</in-capmkt:DateOfEndOfFinancialYear>
<in-capmkt:RevenueFromOperations contextRef="OneD" decimals="-7" unitRef="INR">46402000000</in-capmkt:RevenueFromOperations>
<in-capmkt:RevenueFromOperations contextRef="FourD" decimals="-7" unitRef="INR">178650000000</in-capmkt:RevenueFromOperations>
<in-capmkt:Assets contextRef="OneI" decimals="-7" unitRef="INR">1559670000000</in-capmkt:Assets>
</xbrli:xbrl>
"""
    path = _write(tmp_path, xml)

    parsed = parse_xbrl_document(path)

    assert parsed["filing"]["company_name"] == "INFOSYS LIMITED"
    assert parsed["filing"]["scrip_code"] == "500209"
    assert parsed["filing"]["reporting_quarter"] == "Fourth quarter"
    assert len(parsed["contexts"]) == 3
    assert len(parsed["units"]) == 1
    assert len(parsed["facts"]) == 7

    revenue_facts = [f for f in parsed["facts"] if f["canonical_metric"] == "revenue"]
    by_period = {f["period_length"]: f["normalized_value"] for f in revenue_facts}
    assert by_period["quarterly"] == 46402000000.0
    assert by_period["annual"] == 178650000000.0

    assets_fact = next(f for f in parsed["facts"] if f["concept"] == "Assets")
    assert assets_fact["period_type"] == "instant"
    assert assets_fact["instant_date"] == "2026-03-31"
    assert assets_fact["category"] == "balance_sheet"
    assert assets_fact["canonical_metric"] == "total_assets"

    summary = build_validation_summary(parsed)
    assert summary["num_contexts"] == 3
    assert summary["num_facts"] == 7
    assert summary["num_units"] == 1
    assert summary["company"] == "INFOSYS LIMITED"
    assert summary["earliest_date"] == "2025-04-01"
    assert summary["latest_date"] == "2026-03-31"
