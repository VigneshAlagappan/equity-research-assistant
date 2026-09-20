"""sources/nse_filing_documents.py -- pure classification/parsing logic
only (no real network calls). Row shapes and text below are lifted from
real NSE `/api/corporate-announcements` data captured by the NSE PDF
feasibility spike: spikes/nse_pdf_feasibility/data/nifty50_filter_matches.csv
(real CSV rows, ADANIENT/ADANIPORTS/COALINDIA/KOTAKBANK),
spikes/nse_pdf_feasibility/data/results.json (real HDFCBANK/RELIANCE/
ICICIBANK rows, spike's own transformed field names translated back to the
raw NSE shape: attchmnt_text -> attchmntText, attchmnt_file -> attchmntFile),
and direct quotes from docs/nse-pdf-feasibility/FEASIBILITY_REPORT.md
Sections 4 and 12.2 (ETERNAL/TRENT/HINDUNILVR false-negative examples,
BHARTIARTL newspaper-ad false-positive example, and the Investor
Presentation / Con. Call Updates exclusion-category phrasing quoted
verbatim in Section 4) -- no fictional data shapes are used."""

from __future__ import annotations

from datetime import date

from sources.nse_filing_documents import (
    ClassifiedFiling,
    attachment_extension,
    classify_announcement_row,
    classify_announcements,
    has_downloadable_attachment,
    period_string,
)


def test_financial_result_updates_desc_is_quarterly_result_filing() -> None:
    """Real HDFCBANK row (results.json) -- desc="Financial Result Updates"
    is one of NSE's own unambiguous result categories, no text check
    needed."""
    row = {
        "desc": "Financial Result Updates",
        "attchmntText": "HDFC Bank Limited has submitted to the Exchange, the financial results for the period ended June 30, 2021.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/SEResult30June2021_1707202112533.pdf",
        "seq_id": "142732",
        "an_dt": "2021-07-17 12:53:37",
    }
    assert classify_announcement_row(row) == "quarterly_result_filing"


def test_outcome_of_board_meeting_with_results_text_is_quarterly_result_filing() -> None:
    """Real ADANIENT row (nifty50_filter_matches.csv) -- desc="Outcome of
    Board Meeting" is NSE's most common vehicle for the genuine result PDF
    (FEASIBILITY_REPORT.md Section 4), confirmed here by attchmntText."""
    row = {
        "desc": "Outcome of Board Meeting",
        "attchmntText": "Adani Enterprises Limited has submitted to the Exchange, the financial results for the period ended Jun 30, 2026.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/ADANIENT_29072026143517_AELBMOutcome29072026_1.pdf",
        "sort_date": "2026-07-29 14:36:38",
    }
    assert classify_announcement_row(row) == "quarterly_result_filing"


def test_outcome_of_board_meeting_without_results_mention_is_unclassified() -> None:
    """Real ETERNAL row, quoted verbatim in FEASIBILITY_REPORT.md Section
    12.2 -- a genuine "Outcome of Board Meeting" desc whose own text says
    nothing about results at all. This is the report's own documented
    false-negative limitation: correctly left unclassified rather than
    guessed at, per this module's "skip rather than guess" design."""
    row = {
        "desc": "Outcome of Board Meeting",
        "attchmntText": "Outcome of board meeting dated July 22, 2026.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/ETERNAL_outcome.pdf",
        "sort_date": "2026-07-22 18:00:00",
    }
    assert classify_announcement_row(row) is None


def test_outcome_of_board_meeting_with_results_word_only_is_quarterly_result_filing() -> None:
    """Real HINDUNILVR row, quoted verbatim in FEASIBILITY_REPORT.md
    Section 12.2 -- doesn't match any of the spike's original
    _FIN_TEXT_MARKERS phrases ("financial results for the period ended",
    etc.) but does say "Results ... is enclosed". This module's broader
    "result" substring check (added specifically because of this real,
    documented miss) catches it where the original spike's marker-only
    filter did not."""
    row = {
        "desc": "Outcome of Board Meeting",
        "attchmntText": "Results for the quarter and nine months ended 31st December, 2025 is enclosed",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/HINDUNILVR_q3fy26.pdf",
        "sort_date": "2026-02-12 10:00:00",
    }
    assert classify_announcement_row(row) == "quarterly_result_filing"


def test_dedicated_investor_presentation_desc() -> None:
    """desc="Investor Presentation" is NSE's own dedicated category
    (FEASIBILITY_REPORT.md Section 4 quotes real HDFCBANK attchmntText for
    this exact category: "...informed the Exchange about Investor
    Presentation")."""
    row = {
        "desc": "Investor Presentation",
        "attchmntText": "HDFC Bank Limited has informed the Exchange about Investor Presentation.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK_investor_presentation.pdf",
        "an_dt": "2026-07-18 09:00:00",
    }
    assert classify_announcement_row(row) == "investor_presentation"


def test_general_updates_leaking_investor_presentation_is_reclassified_correctly() -> None:
    """Real COALINDIA row (nifty50_filter_matches.csv) -- desc="General
    Updates" is NSE's catch-all bucket, confirmed (Section 12.2) to
    co-mingle real investor presentations under a non-dedicated desc
    label. Content is the only signal available for this bucket."""
    row = {
        "desc": "General Updates",
        "attchmntText": (
            "Investor Presentation made by Company on the Unaudited Financial "
            "Results of Coal India Limited (Standalone & Consolidated) for the "
            "1st Quarter ended 30th Jun 26."
        ),
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/COALINDIA_27072026175754_Presentation.pdf",
        "sort_date": "2026-07-27 17:58:05",
    }
    assert classify_announcement_row(row) == "investor_presentation"


def test_general_updates_kotakbank_investor_presentation_variant_phrasing() -> None:
    """Real KOTAKBANK row (nifty50_filter_matches.csv) -- a differently
    worded "General Updates" investor-presentation leak, to confirm the
    text-marker set isn't overfit to COALINDIA's exact phrasing."""
    row = {
        "desc": "General Updates",
        "attchmntText": (
            "Investor Presentation for Earnings Conference Call on the "
            "Consolidated and Standalone Unaudited Financial Results of the "
            "Bank for the quarter ended June 30, 2026"
        ),
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/KMBLTAB_18072026125020_InvestorPresentationJuly182026signed.pdf",
        "sort_date": "2026-07-18 12:50:33",
    }
    assert classify_announcement_row(row) == "investor_presentation"


def test_general_updates_newspaper_ad_is_unclassified_not_investor_presentation() -> None:
    """Real BHARTIARTL row, quoted verbatim in FEASIBILITY_REPORT.md
    Section 12.2 -- this is the exact row the original spike's own filter
    mis-picked as the "most recent PDF-bearing match" for a quarterly
    result. This module must NOT classify it as investor_presentation or
    quarterly_result_filing -- it's a newspaper advertisement, none of the
    three wanted types, and must be left unclassified."""
    row = {
        "desc": "General Updates",
        "attchmntText": "...Publication of Newspaper advertisements w.r.t. Audited Financial Results...",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/BHARTIARTL_newspaper_ad.pdf",
        "sort_date": "2026-08-05 09:00:00",
    }
    assert classify_announcement_row(row) is None


def test_dedicated_concall_transcript_desc() -> None:
    row = {
        "desc": "Transcript of Analysts/Institutional Investor Meet/Con. Call",
        "attchmntText": "Transcript of the earnings conference call held on July 18, 2026.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK_concall_transcript.pdf",
        "an_dt": "2026-07-20 09:00:00",
    }
    assert classify_announcement_row(row) == "concall_transcript"


def test_dedicated_concall_recording_desc() -> None:
    """FEASIBILITY_REPORT.md Section 4 quotes real HDFCBANK attchmntText
    for the recording/con-call-updates category: "...Link of Recording"."""
    row = {
        "desc": "Recording of Analysts/Institutional Investor Meet/Con. Call",
        "attchmntText": "Link of Recording of the earnings call.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK_concall_recording.pdf",
        "an_dt": "2026-07-18 09:30:00",
    }
    assert classify_announcement_row(row) == "concall_transcript"


def test_analysts_institutional_investor_meet_con_call_updates_desc_is_unclassified() -> None:
    """FEASIBILITY_REPORT.md Section 4's own real quote: the "Analysts/
    Institutional Investor Meet/Con. Call Updates" desc category's
    attchmntText reads "...Link of Recording" -- this is a SCHEDULE/notice
    about an upcoming call, not the transcript/recording document itself
    (it isn't one of the two dedicated Transcript/Recording desc values,
    nor does its own text mention a transcript). Left unclassified rather
    than guessed."""
    row = {
        "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
        "attchmntText": "Analysts/Institutional Investor Meet/Con. Call Updates - Link of Recording",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK_concall_updates.pdf",
        "an_dt": "2026-07-18 09:00:00",
    }
    assert classify_announcement_row(row) is None


def test_press_release_is_never_classified_even_when_it_mentions_results() -> None:
    """Real Reliance false-positive verified live by the feasibility spike
    (Section 4): a Press Release's own attchmntText mentions financial
    results even though the attached document is a media release, not the
    filing itself. desc="Press Release" doesn't match any of this
    module's three classification branches, so it's correctly left
    unclassified without needing an explicit exclusion list."""
    row = {
        "desc": "Press Release",
        "attchmntText": (
            "...on the Consolidated and Standalone Unaudited Financial Results "
            "for the quarter ended June 30, 2021, we send herewith a copy of "
            "Media Release..."
        ),
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/RELIANCE_press_release.pdf",
        "sort_date": "2021-07-23 20:00:00",
    }
    assert classify_announcement_row(row) is None


def test_dividend_row_is_unclassified() -> None:
    """Real RELIANCE row referenced in FEASIBILITY_REPORT.md Section 12.2
    -- a Dividend announcement that plausibly co-occurs with a results
    board meeting, but isn't itself one of the three wanted document
    types."""
    row = {
        "desc": "Dividend",
        "attchmntText": "Dividend recommendation for the year ended March 31, 2026.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/RELIANCE_dividend.pdf",
        "sort_date": "2026-05-01 10:00:00",
    }
    assert classify_announcement_row(row) is None


def test_no_attachment_is_unclassified() -> None:
    """NSE's own "-" placeholder for a row with nothing attached."""
    row = {"desc": "Outcome of Board Meeting", "attchmntText": "financial results for the period ended...", "attchmntFile": "-"}
    assert classify_announcement_row(row) is None


def test_zip_attachment_is_still_classifiable() -> None:
    """Section 13: some pre-2019 filings arrive as a ZIP wrapping a real
    PDF rather than a bare .pdf URL -- these must still classify (the
    unzip/OCR question is a separate, out-of-scope processing concern)."""
    row = {
        "desc": "Financial Result Updates",
        "attchmntText": "ICICI Bank Limited has submitted to the Exchange the standalone financial results for the period ended June 30, 2016.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/BSE_NSE_29072016_f.zip",
        "sort_date": "2016-07-29 10:00:00",
    }
    assert classify_announcement_row(row) == "quarterly_result_filing"


def test_html_or_other_non_pdf_zip_attachment_is_unclassified() -> None:
    """The old resultDetailedDataLink-style HTML page (Section 8) isn't a
    PDF or ZIP -- has_downloadable_attachment() must reject it."""
    row = {
        "desc": "Financial Result Updates",
        "attchmntText": "financial results for the period ended...",
        "attchmntFile": "https://nsearchives.nseindia.com/archives/financial_results/old.html",
    }
    assert has_downloadable_attachment(row) is False
    assert classify_announcement_row(row) is None


def test_has_downloadable_attachment() -> None:
    assert has_downloadable_attachment({"attchmntFile": "https://x/y.pdf"}) is True
    assert has_downloadable_attachment({"attchmntFile": "https://x/y.PDF"}) is True
    assert has_downloadable_attachment({"attchmntFile": "https://x/y.zip"}) is True
    assert has_downloadable_attachment({"attchmntFile": "-"}) is False
    assert has_downloadable_attachment({"attchmntFile": None}) is False
    assert has_downloadable_attachment({}) is False
    assert has_downloadable_attachment({"attchmntFile": "https://x/y.html"}) is False


def test_attachment_extension() -> None:
    assert attachment_extension("https://x/y.pdf") == "pdf"
    assert attachment_extension("https://x/Y.ZIP") == "zip"


def test_period_string_quarterly() -> None:
    # Quarter ended 30-Jun-2021 -> India fiscal year Apr-Mar -> FY2022 Q1.
    assert period_string(date(2021, 6, 30)) == "FY2022Q1"
    assert period_string(None) is None


def test_classify_announcements_splits_classified_from_unclassified() -> None:
    """End-to-end over a small realistic mixed batch (real row shapes from
    the sources above) -- confirms classify_announcements() partitions
    correctly and populates ClassifiedFiling fields (including best-effort
    period extraction) rather than just delegating to the row classifier."""
    rows = [
        {
            "desc": "Financial Result Updates",
            "attchmntText": "HDFC Bank Limited has submitted to the Exchange, the financial results for the period ended June 30, 2021.",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/SEResult30June2021_1707202112533.pdf",
            "seq_id": "142732",
            "an_dt": "2021-07-17 12:53:37",
        },
        {
            "desc": "Investor Presentation",
            "attchmntText": "HDFC Bank Limited has informed the Exchange about Investor Presentation.",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK_investor_presentation.pdf",
            "seq_id": "142999",
            "an_dt": "2026-07-18 09:00:00",
        },
        {
            "desc": "Press Release",
            "attchmntText": "...financial results for the quarter ended June 30, 2021, copy of Media Release...",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/RELIANCE_press_release.pdf",
            "seq_id": "142862",
            "sort_date": "2021-07-23 20:00:00",
        },
        {
            "desc": "Outcome of Board Meeting",
            "attchmntText": "Outcome of board meeting dated July 22, 2026.",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/ETERNAL_outcome.pdf",
            "seq_id": "150000",
            "sort_date": "2026-07-22 18:00:00",
        },
        {
            # No attachment at all -- must not appear in either output list.
            "desc": "Newspaper Publication",
            "attchmntText": "some text",
            "attchmntFile": "-",
        },
    ]

    classified, unclassified = classify_announcements(rows, "HDFCBANK")

    assert {c.document_type for c in classified} == {"quarterly_result_filing", "investor_presentation"}
    assert len(classified) == 2
    result_filing = next(c for c in classified if c.document_type == "quarterly_result_filing")
    assert isinstance(result_filing, ClassifiedFiling)
    assert result_filing.symbol == "HDFCBANK"
    assert result_filing.seq_id == "142732"
    assert result_filing.period == "FY2022Q1"
    assert result_filing.broadcast_date == date(2021, 7, 17)

    # Press Release and the un-worded Board Meeting outcome both HAVE an
    # attachment but couldn't be confidently classified -- flagged for
    # review, not silently dropped. The no-attachment row is dropped
    # entirely (never a candidate for any document type).
    assert len(unclassified) == 2
    assert {r["desc"] for r in unclassified} == {"Press Release", "Outcome of Board Meeting"}
