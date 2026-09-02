"""Builds the Docs-tab JSON feed (see web/static/js/docs_timeline.js) from
real data: fiscal years grouped by whichever periods this company actually
has financials for — quarterly (storage.repositories.list_company_periods)
and/or annual-only (list_company_annual_years) — and whichever documents
have been recorded against those periods
(storage.repositories.list_company_documents).

Ported from a Claude Design prototype (claude.ai/design, "Signals Docs
Pills.dc.html") — collapsible year groups, each with an inline Annual Report
pill plus its quarter rows, every document slot a clickable pill (published /
your own addition / not published). The prototype's per-pill "open a doc"
modal fabricates prose body text for the demo; this feed only ever returns
real recorded metadata (who added it, when, a link to the file/source) —
never invented content, same rule the rest of this app follows.

Year groups now exist for an annual-only company too (no quarterly
ingestion at all — e.g. every company sources/yfinance_financials.py has
ingested so far), not just companies with quarterly-granularity data: the
year list is the union of both, only the quarter rows under a year are
still quarterly-only.

Only years with actual content are listed — real ingested financials for
that year, or at least one manually-added document. A year with neither is
left out of "Filings by period" entirely rather than shown as an empty row;
"+ Add Missing" (backed by annual_period_options/quarter_period_options
below, which cover 2005 onward regardless of what's listed) is how a period
that isn't currently shown gets its first document. `synthetic: true` means
this company has no real ingested financials at all — every year listed (if
any) exists purely because of manually-added documents, not because
anything here confirms it as a real reporting period.

Fiscal calendars are country-aware (India: April-March, the convention
every real Screener-ingested quarterly row already uses; everywhere else
here just means "US": plain calendar year, January-December) — this only
drives quarter month-labels for scaffolding/display and the Add-document
modal's period options, never overrides an already-ingested real
fiscal_year string (a yfinance-ingested company's fiscal_year already
reflects its own true reporting periods, e.g. Apple's September year-end).
"""

from __future__ import annotations

from storage.db_types import DBConnection, Row
from datetime import datetime, timezone

from companies.registry import get_company
from normalization.periods import fiscal_year_and_quarter_from_date, fiscal_year_number
from storage.repositories import (
    list_company_annual_years,
    list_company_documents,
    list_company_periods,
)

# Docs-tab pill type key -> (document_type stored in `documents`, display label).
# Deliberately 4 of the 5 document_type values this app knows about (see
# schema comment on `documents.document_type`) — matches the design's own
# pill row, which never shows an AI-summary pill. No real ai_summary
# document has ever been added (verified against the live database before
# this port), so dropping it from the grid doesn't hide anything real.
TYPES = [
    {"key": "result", "document_type": "financial_result", "label": "Quarterly Result"},
    {"key": "transcript", "document_type": "transcript", "label": "Concall Transcript"},
    {"key": "ppt", "document_type": "investor_presentation", "label": "Concall Presentation"},
    {"key": "rec", "document_type": "concall_recording", "label": "Concall Recording"},
]
ANNUAL_DOCUMENT_TYPE = "annual_report"
# Every add-able type key — what web/app.py's docs/add route accepts and
# validates against.
KEY_TO_DOCUMENT_TYPE = {t["key"]: t["document_type"] for t in TYPES}
KEY_TO_DOCUMENT_TYPE["annual"] = ANNUAL_DOCUMENT_TYPE

# country -> {quarter: (end-month name, offset from the FY number to the
# calendar year that quarter actually ends in}. India's FY runs Apr(fy-1) to
# Mar(fy); every other country here defaults to a plain calendar year
# (Jan-Dec of fy itself), which is "US" in practice today.
_FY_CALENDARS = {
    "IN": {"Q1": ("June", -1), "Q2": ("September", -1), "Q3": ("December", -1), "Q4": ("March", 0)},
    "US": {"Q1": ("March", 0), "Q2": ("June", 0), "Q3": ("September", 0), "Q4": ("December", 0)},
}
_DEFAULT_CALENDAR_COUNTRY = "IN"
_QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
# The Add-document modal's Period dropdown offers every fiscal year in this
# range, independent of what's actually on file or shown in the main
# archive — a user filling a historical gap needs to reach further back
# than the handful of years the archive view itself displays by default.
_PERIOD_OPTIONS_START_YEAR = 2005


def _quarter_calendar(country: str) -> dict[str, tuple[str, int]]:
    return _FY_CALENDARS.get(country, _FY_CALENDARS[_DEFAULT_CALENDAR_COUNTRY])


def _year_label(fy_num: int, country: str) -> str:
    if country not in _FY_CALENDARS or country == "IN":
        return f"FY {fy_num - 1}–{str(fy_num)[-2:]}"
    return f"FY {fy_num}"  # calendar-year fiscal years read as a single year, not a span


def _current_fy_number(country: str) -> int:
    today = datetime.now(timezone.utc).date()
    if country in _FY_CALENDARS and country != "IN":
        return today.year  # calendar-year fiscal calendar: FY number is just the year
    fy, _ = fiscal_year_and_quarter_from_date(today, "quarterly")
    return fiscal_year_number(fy)


def _period_option_years(country: str) -> list[int]:
    """Every fiscal year number from _PERIOD_OPTIONS_START_YEAR through one
    year ahead of the current one, newest first."""
    end_year = _current_fy_number(country) + 1
    return list(range(end_year, _PERIOD_OPTIONS_START_YEAR - 1, -1))


def _annual_period_options(country: str) -> list[dict]:
    return [{"value": f"year:FY{y}", "label": _year_label(y, country)} for y in _period_option_years(country)]


def _quarter_period_options(country: str) -> list[dict]:
    calendar = _quarter_calendar(country)
    opts = []
    for y in _period_option_years(country):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            month, offset = calendar[q]
            fy = f"FY{y}"
            opts.append({
                "value": (q + fy).lower(),
                "label": f"{q} {_year_label(y, country)} · {month} {y + offset}",
            })
    return opts


def _doc_json(company_id: str, row: Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "document_id": row["document_id"],
        "added_by_user": row["added_by_user"],
        "source_url": row["source_url"],
        "file_url": f"/companies/{company_id}/docs/{row['document_id']}/file" if row["raw_file_path"] else None,
        "retrieved_at": row["retrieved_at"],
    }


def build_docs_feed(conn: DBConnection, company_id: str) -> dict:
    company = get_company(conn, company_id)
    country = company["country"] if company else _DEFAULT_CALENDAR_COUNTRY
    calendar = _quarter_calendar(country)

    periods = list_company_periods(conn, company_id)
    annual_years = list_company_annual_years(conn, company_id)
    docs = list_company_documents(conn, company_id)

    doc_index: dict[tuple[str, str | None, str], Row] = {}
    doc_fys: set[str] = set()
    for d in docs:
        doc_index[(d["fiscal_year"], d["quarter"], d["document_type"])] = d
        doc_fys.add(d["fiscal_year"])

    quarters_by_fy: dict[str, list[dict]] = {}
    for row in periods:
        fy, q = row["fiscal_year"], row["quarter"]
        fy_num = fiscal_year_number(fy)
        month, offset = calendar[q]
        quarters_by_fy.setdefault(fy, []).append({
            "id": (q + fy).lower(),
            "period_id": (q + fy).lower(),
            "label": f"{q} {_year_label(fy_num, country)}",
            "sub": f"Reported {month} {fy_num + offset}",
            "is_year_end": q == "Q4",
            "sort_key": _QUARTER_ORDER[q],
            "docs": {
                t["key"]: _doc_json(company_id, doc_index.get((fy, q, t["document_type"])))
                for t in TYPES
            },
        })

    real_fys = set(quarters_by_fy) | set(annual_years)
    synthetic = not real_fys  # no real ingested financials at all for this company

    # Only years with actual content: real financial data, or at least one
    # manually-added document (see module docstring — a year with neither
    # is left out entirely, "+ Add Missing" covers the gap instead).
    all_fys = sorted(real_fys | doc_fys, key=fiscal_year_number, reverse=True)

    years = []
    for fy in all_fys:
        fy_num = fiscal_year_number(fy)
        quarters = quarters_by_fy.get(fy)
        if quarters is None and fy in doc_fys:
            # This year has no real quarterly financial data, but it's
            # listed because a document was added against it (possibly a
            # quarterly one, e.g. a transcript with no matching ingested
            # quarter) — scaffold a full quarter grid (doc_index-backed, so
            # whichever quarter the document is actually for shows as
            # published) rather than showing just that one populated slot.
            # A year that's here purely from real *annual* financial data
            # (fy in annual_years, no document) stays with quarters = None
            # -> [] below — genuinely no quarterly breakdown exists for it.
            quarters = [
                {
                    "id": (q + fy).lower(),
                    "period_id": (q + fy).lower(),
                    "label": f"{q} {_year_label(fy_num, country)}",
                    "sub": f"{month} {fy_num + offset}",
                    "is_year_end": q == "Q4",
                    "sort_key": _QUARTER_ORDER[q],
                    "docs": {
                        t["key"]: _doc_json(company_id, doc_index.get((fy, q, t["document_type"])))
                        for t in TYPES
                    },
                }
                for q, (month, offset) in calendar.items()
            ]
        quarters = sorted(quarters or [], key=lambda r: r["sort_key"])
        published = sum(1 for r in quarters for t in TYPES if r["docs"][t["key"]] is not None)
        possible = len(quarters) * len(TYPES)
        years.append({
            "fy": fy,
            "period_id": f"year:{fy}",
            "label": _year_label(fy_num, country),
            "quarters": quarters,
            "quarter_count": len(quarters),
            "published_count": published,
            "gap_count": possible - published,
            "annual": _doc_json(company_id, doc_index.get((fy, None, ANNUAL_DOCUMENT_TYPE))),
        })

    return {
        "types": TYPES,
        "years": years,
        "synthetic": synthetic,
        # Independent of `years` above — the Add-document modal's Period
        # dropdown draws from these instead, so a user can attach a document
        # to a fiscal year the archive view isn't currently displaying.
        "annual_period_options": _annual_period_options(country),
        "quarter_period_options": _quarter_period_options(country),
    }
