"""sources/nse_fetch.py — pure parsing logic only (no real network calls in
tests). Row shapes below are trimmed from real NSE API responses captured
this session (both /api/corporates-financial-results and
/api/integrated-filing-results, for IDFCFIRSTB)."""

from __future__ import annotations

from datetime import date

from sources.nse_fetch import _has_real_xbrl_file, _integrated_rows_to_refs, _rows_to_refs


def test_rows_to_refs_maps_the_older_endpoints_vocabulary() -> None:
    rows = [
        {
            "symbol": "IDFCFIRSTB", "seqNumber": "1190673", "consolidated": "Consolidated",
            "period": "Quarterly", "fromDate": "01-Oct-2024", "toDate": "31-Dec-2024",
            "filingDate": "25-Jan-2025 16:44",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/BANKING_117732_1361348.xml",
        },
        {
            "symbol": "IDFCFIRSTB", "seqNumber": "1190667", "consolidated": "Non-Consolidated",
            "period": "Quarterly", "fromDate": "01-Oct-2024", "toDate": "31-Dec-2024",
            "filingDate": "25-Jan-2025 16:41",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/BANKING_117729_1361339.xml",
        },
        {  # no xbrl link at all -- e.g. a PDF-only filing -- must be skipped, not guessed
            "symbol": "IDFCFIRSTB", "seqNumber": "999", "consolidated": "Consolidated",
            "period": "Quarterly", "fromDate": "01-Jul-2024", "toDate": "30-Sep-2024",
            "filingDate": "26-Oct-2024 17:31", "xbrl": None,
        },
    ]

    refs = _rows_to_refs(rows)

    assert len(refs) == 2
    assert refs[0].statement_type == "consolidated"
    assert refs[0].to_date == date(2024, 12, 31)
    assert refs[1].statement_type == "standalone"


def test_has_real_xbrl_file() -> None:
    assert _has_real_xbrl_file("https://nsearchives.nseindia.com/corporate/xbrl/BANKING_117732_1361348.xml") is True
    # NSE's own placeholder for a filing predating XBRL availability --
    # real INFY row, "toDate": "30-Jun-2017" -- a non-empty string, so a
    # plain truthiness check doesn't catch it.
    assert _has_real_xbrl_file("https://nsearchives.nseindia.com/corporate/xbrl/-") is False
    assert _has_real_xbrl_file(None) is False
    assert _has_real_xbrl_file("") is False


def test_rows_to_refs_skips_nses_own_placeholder_url_without_a_real_xbrl_file() -> None:
    """A row with NSE's own "-" placeholder xbrl URL -- verified real INFY
    row for the quarter ended 30-Jun-2017, before XBRL filing was
    available/mandatory -- must be skipped like a missing xbrl link, not
    downloaded (guaranteed 404) or guessed at."""
    rows = [
        {
            "symbol": "INFY", "seqNumber": "1027749", "consolidated": "Consolidated",
            "period": "Quarterly", "fromDate": "01-Apr-2017", "toDate": "30-Jun-2017",
            "filingDate": "17-Jul-2017 19:07", "format": "Old",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/-",
        },
    ]

    assert _rows_to_refs(rows) == []


def test_integrated_rows_to_refs_skips_non_financials_rows() -> None:
    rows = [
        {  # a Governance integrated filing -- shares the endpoint, no numeric XBRL
            "symbol": "IDFCFIRSTB", "seq_Id": "179999", "consolidated": None,
            "qe_Date": "30-JUN-2026", "broadcast_Date": "29-Jul-2026 18:21:39",
            "xbrl": None,
        },
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "178279", "consolidated": "Standalone",
            "qe_Date": "30-JUN-2026", "broadcast_Date": "25-Jul-2026 16:33:14",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_BANKING_178279.xml",
        },
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "178280", "consolidated": "Consolidated",
            "qe_Date": "30-JUN-2026", "broadcast_Date": "25-Jul-2026 16:34:13",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_BANKING_178280.xml",
        },
    ]

    refs = _integrated_rows_to_refs(rows)

    assert len(refs) == 2
    assert {r.statement_type for r in refs} == {"consolidated", "standalone"}
    assert all(r.to_date == date(2026, 6, 30) for r in refs)
    assert all(r.nse_period == "Quarterly" for r in refs)


def test_integrated_rows_to_refs_empty_input() -> None:
    assert _integrated_rows_to_refs([]) == []


def test_integrated_rows_to_refs_keeps_only_the_latest_revision() -> None:
    """Real IDFCFIRSTB shape (quarter ended 30-Sep-2025, standalone): NSE
    kept a buggy original filing and its correction as two separate rows
    for the same quarter — must collapse to one ref (the higher seq_Id),
    never both, or that quarter gets double-counted downstream."""
    rows = [
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "120939", "consolidated": "Standalone",
            "qe_Date": "30-SEP-2025", "broadcast_Date": "18-Oct-2025 17:44:12",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/ORIGINAL.xml",
        },
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "120955", "consolidated": "Standalone",
            "qe_Date": "30-SEP-2025", "broadcast_Date": "18-Oct-2025 18:24:37",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/REVISION.xml",
        },
    ]

    refs = _integrated_rows_to_refs(rows)

    assert len(refs) == 1
    assert refs[0].seq_number == "120955"
    assert refs[0].xbrl_url.endswith("REVISION.xml")


def test_integrated_rows_to_refs_does_not_conflate_different_statement_types_or_quarters() -> None:
    rows = [
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "1", "consolidated": "Standalone",
            "qe_Date": "30-SEP-2025", "broadcast_Date": "x", "xbrl": "https://x/a.xml",
        },
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "2", "consolidated": "Consolidated",
            "qe_Date": "30-SEP-2025", "broadcast_Date": "x", "xbrl": "https://x/b.xml",
        },
        {
            "symbol": "IDFCFIRSTB", "seq_Id": "3", "consolidated": "Standalone",
            "qe_Date": "31-DEC-2025", "broadcast_Date": "x", "xbrl": "https://x/c.xml",
        },
    ]

    refs = _integrated_rows_to_refs(rows)

    assert len(refs) == 3
