"""NSEXbrlAdapter (sources/nse_xbrl.py) — real-filing-shaped XBRL, minimal:
"OneD" context (this filing's own reported quarter) plus a handful of
in-bse-fin tags. Structure verified against real IDFC First Bank filings
downloaded this session (namespaces, context shape, rescaling factors)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import register_company
from sources.nse_xbrl import NSEXbrlAdapter

_NS_FIN = "http://www.bseindia.com/xbrl/fin/2019-09-30/in-bse-fin"
_NS_CAPMKT = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
_NS_XBRLI = "http://www.xbrl.org/2003/instance"


def _make_xbrl(
    tmp_path: Path, tags: dict[str, str], *, filename: str = "filing.xml", namespace: str = _NS_FIN,
) -> Path:
    fin_facts = "\n".join(f'<fin:{tag} contextRef="OneD">{value}</fin:{tag}>' for tag, value in tags.items())
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:fin="{namespace}" xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneD">
  <xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">IDFCFIRSTB</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2023-07-01</xbrli:startDate><xbrli:endDate>2023-09-30</xbrli:endDate></xbrli:period>
</xbrli:context>
{fin_facts}
</xbrli:xbrl>
"""
    path = tmp_path / filename
    path.write_text(xml)
    return path


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "IDFCFIRSTB", "IDFC First Bank Limited", "IDFC First Bank")
    return db_conn


def test_parses_and_rescales_a_real_shaped_filing(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = _make_xbrl(tmp_path, {
        "InterestEarned": "73561700000",  # rupees -> crore (/1e7)
        "BasicEarningsPerShareBeforeExtraordinaryItems": "1.13",  # already per-share, no rescale
        "PercentageOfGrossNpa": "0.0211",  # fraction -> percent (*100)
    })
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    by_metric = {o.metric_key: o for o in observations}
    assert by_metric["interest_earned"].value == pytest.approx(7356.17)
    assert by_metric["interest_earned"].fiscal_year == "FY2024"
    assert by_metric["interest_earned"].quarter == "Q2"
    assert by_metric["eps"].value == pytest.approx(1.13)
    assert by_metric["gross_npa_percent"].value == pytest.approx(2.11)


def test_parses_the_newer_integrated_filing_namespace(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """NSE migrated financial-results filing to SEBI's newer "Integrated
    Filing" framework partway through (Q4 FY25 onward for IDFCFIRSTB) — a
    new XML namespace (in-capmkt) but identical tag local names, verified
    against a real Q1 FY27 filing. The adapter must parse either without
    caring which produced the file."""
    path = _make_xbrl(
        tmp_path,
        {
            "InterestEarned": "110510900000",
            "ProfitLossForThePeriod": "10749600000",
            "PaidUpValueOfEquityShareCapital": "86147400000",
            "FaceValueOfEquityShareCapital": "10",
        },
        namespace=_NS_CAPMKT,
    )
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    by_metric = {o.metric_key: o for o in observations}
    assert by_metric["interest_earned"].value == pytest.approx(11051.09)
    assert by_metric["net_profit"].value == pytest.approx(1074.96)
    assert by_metric["shares_outstanding"].value == pytest.approx(861.474)


def test_parses_the_general_ind_as_taxonomy(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A second, genuinely different taxonomy ("IFIndAs") from the banking
    one — verified against a real Infosys Q1 FY27 filing. Same in-capmkt
    namespace and "OneD"/shares-outstanding-derivation mechanics, entirely
    different tag vocabulary (no InterestEarned/PercentageOfGrossNpa here
    at all — this is a non-bank company)."""
    register_company(conn, "INFY", "Infosys Limited", "Infosys")
    path = _make_xbrl(
        tmp_path,
        {
            "RevenueFromOperations": "399570000000",
            "OtherIncome": "8740000000",
            "Expenses": "306700000000",
            "DepreciationDepletionAndAmortisationExpense": "6130000000",
            "ProfitBeforeTax": "101610000000",
            "TaxExpense": "29120000000",
            "ProfitLossForPeriod": "72490000000",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "17.87",
            "PaidUpValueOfEquityShareCapital": "20280000000",
            "FaceValueOfEquityShareCapital": "5",
        },
        namespace=_NS_CAPMKT,
    )
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "INFY", statement_type="standalone")

    by_metric = {o.metric_key: o for o in observations}
    assert by_metric["total_revenue"].value == pytest.approx(39957.0)
    assert by_metric["other_income"].value == pytest.approx(874.0)
    assert by_metric["operating_expenses"].value == pytest.approx(30670.0)
    assert by_metric["depreciation"].value == pytest.approx(613.0)
    assert by_metric["profit_before_tax"].value == pytest.approx(10161.0)
    assert by_metric["tax"].value == pytest.approx(2912.0)
    assert by_metric["net_profit"].value == pytest.approx(7249.0)
    assert by_metric["eps"].value == pytest.approx(17.87)
    # 20,280,000,000 / 5 / 1e7 = 405.6 Cr shares
    assert by_metric["shares_outstanding"].value == pytest.approx(405.6)


def test_consolidated_placeholder_zero_ratios_are_skipped_not_stored(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """Real IDFC First Bank consolidated filings (every quarter checked this
    session) file a literal 0 for these RBI-mandated standalone-only ratios
    — not a genuine reported zero. Must be treated as not-applicable
    (skipped), not stored as canonical_value 0.0."""
    path = _make_xbrl(tmp_path, {
        "PercentageOfGrossNpa": "0",
        "PercentageOfNpa": "0.00",
        "ReturnOnAssets": "0",
        "InterestEarned": "73561700000",  # a real fact in the same filing must still come through
    })
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="consolidated")

    metric_keys = {o.metric_key for o in observations}
    assert "gross_npa_percent" not in metric_keys
    assert "net_npa_percent" not in metric_keys
    assert "return_on_assets_percent" not in metric_keys
    assert "interest_earned" in metric_keys


def test_standalone_placeholder_zero_ratios_are_kept(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """The consolidated-only skip must not swallow a standalone 0 — no
    evidence standalone filings ever use this placeholder pattern, so a
    literal 0 there is stored as-is like any other value."""
    path = _make_xbrl(tmp_path, {"PercentageOfGrossNpa": "0"})
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    assert len(observations) == 1
    assert observations[0].metric_key == "gross_npa_percent"
    assert observations[0].value == 0.0


def test_derives_shares_outstanding_from_paid_up_capital_and_face_value(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """No direct shares-count tag in this taxonomy — real IDFC First Bank
    Q4 FY24 numbers: 70,699,200,000 / 10 = 7,069,920,000 shares = 706.99 Cr,
    matching the independently-sourced legacy figure (707.0 Cr)."""
    path = _make_xbrl(tmp_path, {
        "PaidUpValueOfEquityShareCapital": "70699200000.00",
        "FaceValueOfEquityShareCapital": "10.00",
        "InterestEarned": "82204800000",  # a real fact in the same filing must still come through
    })
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="consolidated")

    by_metric = {o.metric_key: o for o in observations}
    assert by_metric["shares_outstanding"].value == pytest.approx(706.992)
    assert by_metric["shares_outstanding"].unit == "NUMBER"
    assert "interest_earned" in by_metric
    # The two raw tags themselves are consumed by the derivation, never
    # stored under their own (unaliased) name.
    assert "PaidUpValueOfEquityShareCapital" not in by_metric
    assert "FaceValueOfEquityShareCapital" not in by_metric


def test_shares_outstanding_omitted_when_only_one_ingredient_present(tmp_path: Path, conn: sqlite3.Connection) -> None:
    path = _make_xbrl(tmp_path, {"PaidUpValueOfEquityShareCapital": "70699200000.00"})
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="consolidated")

    assert observations == []


def test_q4_filing_also_emits_annual_observations_from_fourd(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """On a Q4 filing, "FourD" (YTD-through-this-quarter) spans the whole
    fiscal year — real IDFC First Bank Q4 FY26 filing: OneD is
    2026-01-01..2026-03-31 (the Q4 quarter alone), FourD is
    2025-04-01..2026-03-31 (the full FY) with InterestEarned ~3.8x OneD's.
    The adapter must emit both a quarterly (Q4) and an annual observation
    from the one filing, without needing FourD's own declared dates."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:fin="{_NS_FIN}" xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneD">
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="FourD">
  <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<fin:InterestEarned contextRef="OneD">105527700000</fin:InterestEarned>
<fin:InterestEarned contextRef="FourD">405488200000</fin:InterestEarned>
<fin:PercentageOfGrossNpa contextRef="OneD">0.0161</fin:PercentageOfGrossNpa>
<fin:PercentageOfGrossNpa contextRef="FourD">0.0161</fin:PercentageOfGrossNpa>
</xbrli:xbrl>
"""
    path = tmp_path / "q4_filing.xml"
    path.write_text(xml)
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    quarterly = [o for o in observations if o.period_type == "quarterly"]
    annual = [o for o in observations if o.period_type == "annual"]

    by_metric_q = {o.metric_key: o for o in quarterly}
    assert by_metric_q["interest_earned"].value == pytest.approx(10552.77)
    assert by_metric_q["interest_earned"].fiscal_year == "FY2026"
    assert by_metric_q["interest_earned"].quarter == "Q4"

    by_metric_a = {o.metric_key: o for o in annual}
    assert by_metric_a["interest_earned"].value == pytest.approx(40548.82)
    assert by_metric_a["interest_earned"].fiscal_year == "FY2026"
    assert by_metric_a["interest_earned"].quarter is None
    assert by_metric_a["gross_npa_percent"].value == pytest.approx(1.61)


def test_non_q4_filing_never_reads_fourd(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """FourD's economic content outside Q4 (a real YTD span, not a full
    fiscal year) is out of scope — the app already gets every other quarter
    from its own separately-filed document, so a non-Q4 filing must ignore
    FourD entirely, annual or otherwise."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:fin="{_NS_FIN}" xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneD">
  <xbrli:period><xbrli:startDate>2023-07-01</xbrli:startDate><xbrli:endDate>2023-09-30</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="FourD">
  <xbrli:period><xbrli:startDate>2023-07-01</xbrli:startDate><xbrli:endDate>2023-09-30</xbrli:endDate></xbrli:period>
</xbrli:context>
<fin:InterestEarned contextRef="OneD">73561700000</fin:InterestEarned>
<fin:InterestEarned contextRef="FourD">213000000000</fin:InterestEarned>
</xbrli:xbrl>
"""
    path = tmp_path / "q2_filing.xml"
    path.write_text(xml)
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    assert len(observations) == 1
    assert observations[0].period_type == "quarterly"
    assert observations[0].value == pytest.approx(7356.17)


def test_balance_sheet_facts_come_from_the_instant_context(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """Balance-sheet facts (Assets/Equity/...) live under "OneI", an
    INSTANT context, not "OneD"/"FourD" (durations) — real IDFC First Bank
    Q4 FY26 filing: Assets=3997800900000, Equity share capital=88895700000.
    Must be stamped into BOTH the quarterly and (Q4-only) annual framing,
    same instant reused, since a balance sheet is a snapshot, not a flow."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:fin="{_NS_FIN}" xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneD">
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<xbrli:context id="OneI">
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
</xbrli:context>
<fin:InterestEarned contextRef="OneD">10552770000</fin:InterestEarned>
<fin:Assets contextRef="OneI">3997800900000</fin:Assets>
<fin:Capital contextRef="OneI">88895700000</fin:Capital>
</xbrli:xbrl>
"""
    path = tmp_path / "with_balance_sheet.xml"
    path.write_text(xml)
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    quarterly = {o.metric_key: o.value for o in observations if o.period_type == "quarterly"}
    annual = {o.metric_key: o.value for o in observations if o.period_type == "annual"}

    assert quarterly["total_assets"] == pytest.approx(399780.09)
    assert quarterly["equity_share_capital"] == pytest.approx(8889.57)
    assert quarterly["interest_earned"] == pytest.approx(1055.277)  # a duration fact, unaffected
    # same instant, reused for the annual framing (not a second XBRL value):
    assert annual["total_assets"] == pytest.approx(399780.09)
    assert annual["equity_share_capital"] == pytest.approx(8889.57)
    assert "interest_earned" not in annual  # OneD's own duration fact never appears in the annual framing


def test_balance_sheet_facts_absent_when_filing_has_no_instant_context(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """No "OneI" in the file (e.g. an older or malformed filing) must mean
    zero balance-sheet observations, never an error — same graceful-absence
    handling as any other missing context."""
    path = _make_xbrl(tmp_path, {"InterestEarned": "73561700000"})
    adapter = NSEXbrlAdapter(conn)

    observations = adapter.parse(path, "IDFCFIRSTB", statement_type="standalone")

    assert {o.metric_key for o in observations} == {"interest_earned"}


def test_unrecognized_fin_namespace_warns_and_returns_no_observations(tmp_path: Path, conn: sqlite3.Connection, caplog) -> None:
    """A real "OneD" context but a taxonomy version this adapter hasn't
    been taught about (a namespace URI not in _FIN_NAMESPACES) must not
    silently return zero facts with no signal — this exact failure mode
    (real IDFCFIRSTB Q4 FY25 filings, in-capmkt dated "2025-01-31" not yet
    registered) previously produced parsed=0 with no warning at all."""
    path = _make_xbrl(
        tmp_path,
        {"InterestEarned": "110510900000"},
        namespace="http://www.sebi.gov.in/xbrl/2099-01-31/in-capmkt",  # a version not in _FIN_NAMESPACES
    )
    adapter = NSEXbrlAdapter(conn)

    with caplog.at_level("WARNING"):
        observations = adapter.parse(path, "IDFCFIRSTB")

    assert observations == []
    assert any("no element matched any known fin namespace" in r.message for r in caplog.records)


def test_missing_oned_context_returns_no_observations(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A real older-format IDFC First Bank filing (Q4 FY23) has no plain
    "OneD" context at all, only segment-qualified variants — must be
    skipped outright (logged), never guess at a different context."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:in-bse-fin="{_NS_FIN}" xmlns:xbrli="{_NS_XBRLI}">
<xbrli:context id="OneOperatingExpenses01D">
  <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-03-31</xbrli:endDate></xbrli:period>
</xbrli:context>
<in-bse-fin:InterestEarned contextRef="OneOperatingExpenses01D">73561700000</in-bse-fin:InterestEarned>
</xbrli:xbrl>
"""
    path = tmp_path / "no_oned.xml"
    path.write_text(xml)
    adapter = NSEXbrlAdapter(conn)

    assert adapter.parse(path, "IDFCFIRSTB") == []
