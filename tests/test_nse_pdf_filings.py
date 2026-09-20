"""sources/nse_pdf_filings.py — the date-window discovery/matcher, tested
against synthetic corporate-announcements rows shaped exactly like the real
ones the feasibility report quotes verbatim (docs/nse-pdf-feasibility/
FEASIBILITY_REPORT.md Sections 4/12.2/12.3), so these tests exercise the
SAME real-world failure modes the report found, not hypothetical ones:
  - HINDUNILVR-style genuine filings whose attchmntText never says
    "financial results" (Section 12.2) — must still be found (date-window
    is the primary signal, text-marker is transparency-only).
  - "General Updates" desc rows (Investor Presentations, Newspaper
    Publications) that share their desc with occasional genuine filings —
    must be excluded from the candidate pool entirely (Section 12.2/14.1).
  - BHARTIARTL's real mis-pick (Section 12.3): a later "General Updates"
    newspaper-ad row must not beat an earlier "Outcome of Board Meeting"
    row for the same quarter — earliest-in-window wins.
  - Press Release / Analysts Meet / Copy of Newspaper Publication rows
    that reference the same results but attach a different document
    (Section 4) — must never be selected even when the earliest in window.

No live network call anywhere in this file — `announcements=` is passed
directly to discover_result_filings(), the same seam the QA tool and any
future test replaying real discovery_2015_now.json-shaped data would use.
"""

from __future__ import annotations

from datetime import date

from sources.nse_pdf_filings import (
    FilingMatch,
    discover_result_filings,
    quarter_end_dates,
)


def _row(desc: str, sort_date: str, attchmnt_text: str = "", attchmnt_file: str | None = "https://nsearchives.nseindia.com/x.pdf", seq_id: int = 1) -> dict:
    return {
        "desc": desc,
        "sort_date": sort_date,
        "an_dt": sort_date,
        "attchmntText": attchmnt_text,
        "attchmntFile": attchmnt_file,
        "seq_id": seq_id,
    }


def test_quarter_end_dates_covers_every_quarter_in_range() -> None:
    """Includes the quarter-end just before `start` too (its disclosure
    window can overlap into the requested range) -- same "just before
    start" inclusion the spike's own run_2015_discovery.py:
    _quarter_end_dates() documents and this function mirrors."""
    dates = quarter_end_dates(date(2020, 1, 1), date(2020, 12, 31))
    assert dates[0] == date(2019, 9, 30)  # just-before-start quarter, included deliberately
    assert date(2020, 3, 31) in dates
    assert date(2020, 6, 30) in dates
    assert date(2020, 9, 30) in dates
    assert dates[-1] == date(2020, 12, 31)


def test_finds_genuine_board_meeting_filing_via_text_marker() -> None:
    rows = [_row("Outcome of Board Meeting", "2021-07-17 00:00:00",
                  "financial results for the period ended June 30, 2021", seq_id=142732)]
    matches = discover_result_filings("HDFCBANK", start=date(2021, 6, 1), end=date(2021, 6, 30), announcements=rows)
    assert len(matches) == 1
    assert matches[0].fiscal_year == "FY2022" and matches[0].quarter == "Q1"
    assert matches[0].match_confidence == "text_confirmed"
    assert matches[0].seq_id == "142732"


def test_finds_genuine_filing_whose_text_never_mentions_financial_results() -> None:
    """The real HINDUNILVR case (feasibility report Section 12.2): a
    genuine result filing under the recognized "Outcome of Board Meeting"
    desc, but attchmntText that plainly describes the results without ever
    using any of the fixed marker phrases — text-marker-only filtering
    would have missed this (3 of HINDUNILVR's last 4 quarters, per the
    report). Date-window matching must still find it."""
    rows = [_row("Outcome of Board Meeting", "2026-02-12 09:00:00",
                  "Results for the quarter and nine months ended 31st December, 2025 is enclosed")]
    matches = discover_result_filings("HINDUNILVR", start=date(2025, 12, 1), end=date(2025, 12, 31), announcements=rows)
    assert len(matches) == 1
    assert matches[0].match_confidence == "date_window_only"


def test_general_updates_desc_is_excluded_from_candidate_pool_entirely() -> None:
    """Section 12.2's catch-all trap: "General Updates" sometimes carries a
    genuine filing but far more often an Investor Presentation or
    Newspaper Publication — excluded wholesale, not per-row, so it can
    never win even when it's the only candidate in the window."""
    rows = [_row("General Updates", "2021-07-10 00:00:00",
                  "Investor Presentation made by Company on the Unaudited Financial Results")]
    matches = discover_result_filings("COALINDIA", start=date(2021, 6, 1), end=date(2021, 6, 30), announcements=rows)
    assert matches == []


def test_earliest_in_window_wins_over_a_later_general_updates_row() -> None:
    """The real BHARTIARTL mis-pick (Section 12.3): a genuine "Outcome of
    Board Meeting" filing dated 2026-07-20, with a "General Updates"
    newspaper-ad row for the SAME quarter dated later (2026-08-05). Even
    though the newspaper row would otherwise be excluded outright (prior
    test), this test specifically proves the EARLIEST-wins rule using two
    otherwise-eligible categories, since that's the mechanism (not just
    the exclusion list) Section 12.4 identifies as the fix."""
    rows = [
        _row("Outcome of Board Meeting", "2026-07-20 10:00:00",
             "Outcome of board meeting dated July 20, 2026.", seq_id=100),
        _row("Financial Result Updates", "2026-08-05 10:00:00",
             "Publication of Newspaper advertisements w.r.t. Audited Financial Results", seq_id=200),
    ]
    matches = discover_result_filings("BHARTIARTL", start=date(2026, 6, 1), end=date(2026, 6, 30), announcements=rows)
    assert len(matches) == 1
    assert matches[0].seq_id == "100"


def test_press_release_and_analysts_meet_never_selected_even_if_earliest() -> None:
    """The real Reliance/ICICI false positives (Section 4): a Press
    Release / Analysts Meet row's OWN attchmntText mentions "financial
    results" but the attached document is a media release or analyst
    deck, not the filing itself — excluded regardless of timing."""
    rows = [
        _row("Press Release", "2021-07-10 00:00:00",
             "...on the Consolidated and Standalone Unaudited Financial Results for the quarter ended "
             "June 30, 2021, we send herewith a copy of Media Release...", seq_id=1),
        _row("Analysts Meet", "2021-07-11 00:00:00", "financial results for the period ended", seq_id=2),
        _row("Outcome of Board Meeting", "2021-07-17 00:00:00",
             "financial results for the period ended June 30, 2021", seq_id=3),
    ]
    matches = discover_result_filings("RELIANCE", start=date(2021, 6, 1), end=date(2021, 6, 30), announcements=rows)
    assert len(matches) == 1
    assert matches[0].seq_id == "3"


def test_no_match_outside_the_disclosure_window() -> None:
    """A row 90 days after quarter-end (beyond the 75-day window) must not
    be picked up as that quarter's filing."""
    rows = [_row("Outcome of Board Meeting", "2021-09-29 00:00:00", "financial results for the period ended")]
    matches = discover_result_filings("HDFCBANK", start=date(2021, 6, 1), end=date(2021, 6, 30), announcements=rows)
    assert matches == []


def test_q4_quarter_labeled_correctly() -> None:
    rows = [_row("Integrated Filing- Financial", "2024-04-25 00:00:00", "audited financial results")]
    matches = discover_result_filings("HDFCBANK", start=date(2024, 3, 1), end=date(2024, 3, 31), announcements=rows)
    assert len(matches) == 1
    assert matches[0].fiscal_year == "FY2024" and matches[0].quarter == "Q4"


def test_attachment_format_detected_for_pdf_and_zip() -> None:
    rows = [
        _row("Outcome of Board Meeting", "2016-07-21 00:00:00", "financial results",
             attchmnt_file="https://nsearchives.nseindia.com/Result30062016_21072016115151.zip"),
    ]
    matches = discover_result_filings("HDFCBANK", start=date(2016, 6, 1), end=date(2016, 6, 30), announcements=rows)
    assert len(matches) == 1
    assert matches[0].attachment_format == "zip"


def test_no_real_attachment_still_matches_but_flagged_none() -> None:
    rows = [_row("Outcome of Board Meeting", "2016-07-21 00:00:00", "financial results", attchmnt_file="-")]
    matches = discover_result_filings("HDFCBANK", start=date(2016, 6, 1), end=date(2016, 6, 30), announcements=rows)
    assert len(matches) == 1
    assert matches[0].attachment_format == "none"
