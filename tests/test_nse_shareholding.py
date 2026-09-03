"""sources/nse_shareholding.py — real-shaped fixtures trimmed from actual
INFY/TCS SHP XBRL filings fetched live this session (context-id pairing,
namespace, and axis names copied verbatim; holder set trimmed to a handful)."""

from __future__ import annotations

from datetime import date

from sources.nse_shareholding import ShareholdingSummary, parse_shp_xbrl

_NS = "http://www.bseindia.com/xbrl/shp/2025-10-31/in-bse-shp"

_SHP_FIXTURE = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:in-bse-shp="{_NS}">
  <xbrli:context id="D_IndividualsOrHUF_Context1">
    <xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
    <xbrli:scenario>
      <xbrldi:typedMember dimension="in-bse-shp:DetailsSharesHeldByIndividualsOrHUFAxis">
        <in-bse-shp:IndividualsOrHUFDomain>IndividualsOrHUF_Context1</in-bse-shp:IndividualsOrHUFDomain>
      </xbrldi:typedMember>
    </xbrli:scenario>
  </xbrli:context>
  <xbrli:context id="IndividualsOrHUF_Context1">
    <xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="D_MutualFundsOrUTI_Context1">
    <xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
    <xbrli:scenario>
      <xbrldi:typedMember dimension="in-bse-shp:DetailsOfSharesHeldByMutualFundsOrUTIAxis">
        <in-bse-shp:MutualFundsOrUTIDomain>MutualFundsOrUTI_Context1</in-bse-shp:MutualFundsOrUTIDomain>
      </xbrldi:typedMember>
    </xbrli:scenario>
  </xbrli:context>
  <xbrli:context id="MutualFundsOrUTI_Context1">
    <xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>

  <in-bse-shp:NameOfTheShareholder contextRef="D_IndividualsOrHUF_Context1">JANE FOUNDER</in-bse-shp:NameOfTheShareholder>
  <in-bse-shp:NumberOfShares contextRef="IndividualsOrHUF_Context1" decimals="INF" unitRef="shares">40789562</in-bse-shp:NumberOfShares>
  <in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="IndividualsOrHUF_Context1" decimals="INF" unitRef="pure">0.0109</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>

  <in-bse-shp:NameOfTheShareholder contextRef="D_MutualFundsOrUTI_Context1">ACME MUTUAL FUND</in-bse-shp:NameOfTheShareholder>
  <in-bse-shp:NumberOfShares contextRef="MutualFundsOrUTI_Context1" decimals="INF" unitRef="shares">184057994</in-bse-shp:NumberOfShares>
  <in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="MutualFundsOrUTI_Context1" decimals="INF" unitRef="pure">0.0492</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
</xbrli:xbrl>
""".encode("utf-8")


def test_parse_shp_xbrl_classifies_promoter_and_public_holders():
    holdings = parse_shp_xbrl(_SHP_FIXTURE)
    by_name = {h.holder_name: h for h in holdings}

    assert by_name["JANE FOUNDER"].side == "promoter"
    assert by_name["JANE FOUNDER"].category == "Individuals / HUF"
    assert by_name["JANE FOUNDER"].num_shares == 40789562
    assert round(by_name["JANE FOUNDER"].percent_of_shares, 2) == 1.09

    assert by_name["ACME MUTUAL FUND"].side == "public"
    assert by_name["ACME MUTUAL FUND"].category == "Mutual Funds / UTI"
    assert round(by_name["ACME MUTUAL FUND"].percent_of_shares, 2) == 4.92


def test_parse_shp_xbrl_unknown_namespace_returns_empty_not_error():
    older_taxonomy = _SHP_FIXTURE.replace(_NS.encode(), b"http://www.bseindia.com/xbrl/shp/2020-09-30/in-bse-shp")
    assert parse_shp_xbrl(older_taxonomy) == []


def test_parse_shp_xbrl_no_named_holders_returns_empty():
    empty_doc = b"""<?xml version="1.0"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
                xmlns:in-bse-shp="%b">
      <in-bse-shp:NameOfTheCompany contextRef="MainD">Acme Ltd</in-bse-shp:NameOfTheCompany>
    </xbrli:xbrl>""" % _NS.encode()
    assert parse_shp_xbrl(empty_doc) == []


def test_shareholding_summary_dataclass_holds_fiscal_period():
    summary = ShareholdingSummary(
        symbol="INFY",
        period_end=date(2026, 6, 30),
        fiscal_year="FY2027",
        quarter="Q1",
        promoter_percent=13.82,
        public_percent=85.97,
        employee_trust_percent=0.21,
        submission_date="15-JUL-2026",
        source_url="https://nsearchives.nseindia.com/corporate/xbrl/SHP_x_WEB.xml",
    )
    assert summary.fiscal_year == "FY2027"
    assert summary.quarter == "Q1"
