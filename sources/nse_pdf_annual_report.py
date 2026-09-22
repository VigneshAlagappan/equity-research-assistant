"""Extract Balance Sheet / Profit & Loss / Cash Flow face-statement figures
from an already-downloaded NSE Annual Report PDF (storage/raw_object_store.py
-- see scripts/backfill_nse_annual_reports.py) and turn them into
NormalizedObservation rows through this app's standard
normalization/financials.py transform, same as every other adapter.

Scope, deliberately narrow: the face-statement line items on the Balance
Sheet, Profit & Loss Account, and Cash Flow Statement pages themselves --
not the numbered Schedules that break each line down further (Schedule 3:
Deposits, Schedule 9: Advances, etc.). Verified live against a real AU
Small Finance Bank FY2023 annual report: these three pages carry every
metric_key this app's existing NSE-XBRL bank vocabulary already tracks
(deposits, advances, investments, borrowings, equity_share_capital,
reserves, total_assets, interest_earned, interest_expended, other_income,
operating_expenses, net_profit, eps) PLUS three cash-flow metrics XBRL
never reports at all (net_cash_from_operating/investing/financing_activities)
and profit_before_tax/tax, both derivable from the Cash Flow Statement's own
"Profit after tax" + "Add: Provision for tax" = "Net Profit Before Taxes"
reconciliation lines (the face P&L bundles tax into "Provisions &
contingencies" with no separate line).

Provenance: every observation's `source` is "nse" (matching
sources/nse_xbrl.py, NOT a separate "nse_pdf" source_id) -- both genuinely
originate from an NSE filing, and financial_observations.source is what
storage/repositories.py's reconcile() uses for trust_rank lookup (0 for
"nse", same as real XBRL). `parser_version` is what actually distinguishes
a PDF-derived fact from an XBRL one downstream (web/charts_feed.py's
_classify_provenance() already recognizes any parser_version starting
"nse_pdf" as PDF-sourced) -- this module's is "nse_pdf_annual_report_v1".
Because trust_rank is equal, reconcile() picks whichever value is chosen
by its own conflict-resolution rule (retrieved_at, most-recently-ingested
wins among equal-trust candidates) -- fine here since this only ever adds
values for (company, metric, period) combinations XBRL has never reported
at all (verified before writing this module: zero cash-flow rows, and
zero pre-FY2024 balance-sheet/income-statement rows, existed for AUBANK).

This is registered under its OWN adapter key in ingestion/detector.py's
ADAPTER_CLASSES ("nse_pdf_annual_report", not "nse" -- that key already
routes to NSEXbrlAdapter) so it can be invoked via the standard
ingest_file() pipeline without colliding with the XBRL adapter, while
still emitting source="nse" on every observation it produces.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pypdf

from normalization.financials import build_observations_from_periods
from sources.base import NormalizedObservation, SourceAdapter
from storage.db_types import DBConnection

logger = logging.getLogger(__name__)

PARSER_VERSION = "nse_pdf_annual_report_v1"
_SOURCE = "nse"

# Normalized row_label tokens this module emits -- metric_aliases rows for
# source="nse" mapping each one to a real metric_key are registered in
# normalization/financials.py's DEFAULT_METRIC_ALIASES (search
# PARSER_VERSION's own name in that file's own comment for the exact list).
# A number like "82,054,083" or "(3,059,355)" (parens = negative, per
# standard accounting notation -- verified live: AU SFB's Cash Flow
# Statement uses parens for every outflow line).
_NUMBER_RE = re.compile(r"\(?-?[\d,]+\.?\d*\)?")


def _parse_number(token: str) -> float | None:
    token = token.strip()
    if not token or token in ("-", "—", "–"):
        return None
    negative = token.startswith("(") and token.endswith(")")
    cleaned = token.strip("()").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _extract_two_year_line(text_line: str, label_prefix: str) -> tuple[float | None, float | None] | None:
    """A face-statement line reads "<label> <schedule?> <this-year-value>
    <prior-year-value>" -- schedule references (a bare 1-2 digit number
    right after the label, verified live: "Capital 1 6,667,450 3,149,000")
    are NOT the same shape as a real value (which always has thousands
    separators or is a genuine amount), so the schedule number, when
    present, is skipped by keeping only the LAST TWO number-shaped tokens
    on the line -- the two year columns are always the trailing pair,
    schedule references never are."""
    if not text_line.strip().lower().startswith(label_prefix.lower()):
        return None
    numbers = _NUMBER_RE.findall(text_line)
    numbers = [n for n in numbers if n not in ("", "-")]
    if len(numbers) < 2:
        return None
    current, prior = numbers[-2], numbers[-1]
    return _parse_number(current), _parse_number(prior)


# (label prefix as it appears in the PDF, normalized row_label token,
# whether this line lives on the Balance Sheet / P&L / Cash Flow page).
# Order matters where one label prefix is a substring of another further
# down this list (e.g. "Total" alone is deliberately NOT matched here --
# too ambiguous, appears on both sides of the Balance Sheet with different
# meaning -- total_assets is derived as Deposits+Borrowings+Capital+
# Reserves+EmployeeStockOptions+OtherLiabilities instead, see
# _derive_total_assets()).
_BALANCE_SHEET_LINES = [
    ("Capital", "PDF_Capital"),
    ("Employees stock options outstanding", "PDF_EmployeeStockOptions"),
    ("Reserves & Surplus", "PDF_ReservesAndSurplus"),
    ("Deposits", "PDF_Deposits"),
    ("Borrowings", "PDF_Borrowings"),
    ("Other Liabilities and Provisions", "PDF_OtherLiabilitiesAndProvisions"),
    ("Investments", "PDF_Investments"),
    ("Advances", "PDF_Advances"),
]

_PL_LINES = [
    ("Interest earned", "PDF_InterestEarned"),
    ("Other income", "PDF_OtherIncome"),
    ("Interest expended", "PDF_InterestExpended"),
    ("Operating expenses", "PDF_OperatingExpenses"),
    ("Net profit for the year", "PDF_NetProfit"),
    ("Basic (", "PDF_BasicEPS"),
]

_CASH_FLOW_LINES = [
    ("Net Profit Before Taxes", "PDF_ProfitBeforeTax"),
    ("Net Cash Flow from Operating Activities", "PDF_NetCashFromOperatingActivities"),
    ("Net Cash Flow from / (used in) Operating Activities", "PDF_NetCashFromOperatingActivities"),
    ("Net cash flow (used) in Investing Activities", "PDF_NetCashFromInvestingActivities"),
    ("Net cash flow from / (used in) Investing Activities", "PDF_NetCashFromInvestingActivities"),
    ("Net cash flow (used in)/ from Financing Activities", "PDF_NetCashFromFinancingActivities"),
    ("Net cash flow from/(used in) Financing Activities", "PDF_NetCashFromFinancingActivities"),
    ("Net cash flow from / (used in) Financing Activities", "PDF_NetCashFromFinancingActivities"),
    ("Cash and Cash Equivalents at the end of the year", "PDF_CashAndBank"),
]

# "Profit after tax" + "Add: Provision for tax" both appear on the Cash
# Flow Statement's reconciliation section, ahead of "Net Profit Before
# Taxes" -- tax itself (not directly labeled "Tax" anywhere on the face
# statements) is derived as their difference, not matched as its own line.
_TAX_LINE_LABEL = "Add: Provision for tax"

_ALL_LINE_DEFS = _BALANCE_SHEET_LINES + _PL_LINES + _CASH_FLOW_LINES


def _find_statement_pages(reader: "pypdf.PdfReader") -> dict[str, int]:
    """First page (0-indexed) whose text contains a LINE that starts the
    Balance Sheet / Profit and Loss Account / Cash Flow Statement --
    located by content, not a fixed page number, since that varies year
    to year. Searches each page's FULL text (every line, via re.MULTILINE
    ^-anchoring), not just the first few lines -- verified live this was
    a real bug, not overcautious: some years' running headers push the
    real heading down several lines (a multi-line "Report title / page
    numbers / ANNUAL REPORT yyyy-yy" block ahead of it), and some pages'
    two-column text extraction order isn't strictly top-to-bottom, so a
    fixed line-count window missed the real heading for 4 of AU SFB's 10
    already-downloaded reports (FY2020-2022, FY2024) before this was
    widened to the whole page. Still safe against a schedule page's own
    running header ("Schedules\\nforming part of the Balance Sheet as at
    ...") -- that text never starts a line with "Balance Sheet" (it starts
    with "forming"), so the ^-anchor doesn't false-positive on it even
    scanning the complete page. Uses pypdf (not pdfplumber) deliberately --
    pdfplumber's own layout analysis took 90+ seconds to scan a 334-page
    report (long enough to trip the same Neon idle-connection-drop this
    app has hit repeatedly elsewhere this session), pypdf does the full
    document in under 25 seconds with equivalent plain-text output for
    this kind of simple-layout financial-statement page."""
    # Each pattern requires the heading AND the very next line's own
    # opening words -- "Balance Sheet" alone (or "Profit and Loss
    # Account"/"Cash Flow Statement") also appears as a bare Table of
    # Contents entry on an early page (verified live: widening the search
    # to a whole page without this second line matched TOC pages 2/4/8
    # instead of the real statement pages 220+ for every one of AU SFB's
    # later reports). The real statement page always follows its heading
    # immediately with "as at <date>" (Balance Sheet) or "for the Year
    # ended"/"for the year ended" (P&L/Cash Flow, case varies by year) --
    # a TOC entry never does, so this two-line anchor discriminates them
    # without needing a page-number allowlist that would vary every year.
    _PAGE_PATTERNS = {
        "balance_sheet": re.compile(r"^Balance Sheet\s*\n\s*as at\b", re.MULTILINE | re.IGNORECASE),
        "profit_and_loss": re.compile(r"^Profit and Loss Account\s*\n\s*for the year ended\b", re.MULTILINE | re.IGNORECASE),
        "cash_flow": re.compile(r"^Cash Flow Statement\s*\n\s*for the year ended\b", re.MULTILINE | re.IGNORECASE),
    }
    found: dict[str, int] = {}
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        for key, pattern in _PAGE_PATTERNS.items():
            if key not in found and pattern.search(text):
                found[key] = i
        if len(found) == 3:
            break
    return found


class NSEPdfAnnualReportAdapter(SourceAdapter):
    source_id = "nse_pdf_annual_report"

    def __init__(self, conn: DBConnection):
        self._conn = conn

    def parse(
        self, file_path: Path, company_id: str, statement_type: str = "standalone", **kwargs: object,
    ) -> list[NormalizedObservation]:
        reader = pypdf.PdfReader(file_path)
        pages = _find_statement_pages(reader)
        missing = {"balance_sheet", "profit_and_loss", "cash_flow"} - pages.keys()
        if missing:
            logger.warning("%s: could not locate face-statement page(s) for %s -- skipping this file",
                            file_path, missing)
            return []

        # Cash flow statement's tax reconciliation, plus the two
        # statement pages that sometimes run onto a following page
        # (verified live: AU SFB's Cash Flow Statement spans 2 pages),
        # so each section reads its own page plus the next one.
        bs_text = (reader.pages[pages["balance_sheet"]].extract_text() or "") + "\n" + \
                  (reader.pages[pages["balance_sheet"] + 1].extract_text() or "")
        pl_text = (reader.pages[pages["profit_and_loss"]].extract_text() or "") + "\n" + \
                  (reader.pages[pages["profit_and_loss"] + 1].extract_text() or "")
        cf_text = (reader.pages[pages["cash_flow"]].extract_text() or "") + "\n" + \
                  (reader.pages[pages["cash_flow"] + 1].extract_text() or "")

        # Report period end date (e.g. "March 31, 2023") drives fiscal-year
        # labeling -- read from the Balance Sheet's own "as at" line rather
        # than the raw_objects `period` field this file was stored under,
        # so a mislabeled S3 key never silently mislabels the extracted
        # fiscal year too.
        as_at_match = re.search(r"as at\s+(?:March|Dec\w*)\s+\d{1,2},?\s*(\d{4})", bs_text, re.IGNORECASE)
        if not as_at_match:
            logger.warning("%s: could not determine report fiscal year from Balance Sheet text -- skipping", file_path)
            return []
        current_year = int(as_at_match.group(1))
        prior_year = current_year - 1
        # This app's fiscal_year label convention (see normalization/periods.py):
        # a bank with fiscal_year_end_month=3 (March) reporting "as at March
        # 31, 2023" is FY2023 -- verified against this exact company's own
        # existing canonical_financials rows (FY2024 = year ended March 2024).
        current_fy, prior_fy = f"FY{current_year}", f"FY{prior_year}"

        values_by_label: dict[str, dict[str, float]] = {}

        def _collect(text: str, line_defs: list[tuple[str, str]]) -> None:
            for line in text.splitlines():
                for prefix, label in line_defs:
                    result = _extract_two_year_line(line, prefix)
                    if result is None:
                        continue
                    current_val, prior_val = result
                    bucket = values_by_label.setdefault(label, {})
                    if current_val is not None:
                        bucket.setdefault(current_fy, current_val)
                    if prior_val is not None:
                        bucket.setdefault(prior_fy, prior_val)
                    break

        _collect(bs_text, _BALANCE_SHEET_LINES)
        _collect(pl_text, _PL_LINES)
        _collect(cf_text, _CASH_FLOW_LINES)

        # Tax = Provision for tax line (Cash Flow reconciliation) --
        # collected separately since it isn't a fixed "PDF_*" output label
        # on its own line def list above (derives PDF_Tax directly here).
        tax_bucket: dict[str, float] = {}
        for line in cf_text.splitlines():
            result = _extract_two_year_line(line, _TAX_LINE_LABEL)
            if result is None:
                continue
            current_val, prior_val = result
            if current_val is not None:
                tax_bucket.setdefault(current_fy, current_val)
            if prior_val is not None:
                tax_bucket.setdefault(prior_fy, prior_val)
            break
        if tax_bucket:
            values_by_label["PDF_Tax"] = tax_bucket

        # total_assets has no single labeled face-statement line (the PDF
        # just repeats "Total" on both the Capital&Liabilities and Assets
        # sides) -- derived instead as the Capital&Liabilities-side sum,
        # which the Balance Sheet's own printed "Total" already equals
        # (verified live: 6,667,450+440,252+102,665,735+693,649,864+
        # 62,986,521+35,751,362 = 902,161,184, matching the PDF's own
        # printed Total exactly for FY2023).
        total_assets_bucket: dict[str, float] = {}
        component_labels = [
            "PDF_Capital", "PDF_EmployeeStockOptions", "PDF_ReservesAndSurplus",
            "PDF_Deposits", "PDF_Borrowings", "PDF_OtherLiabilitiesAndProvisions",
        ]
        for fy in (current_fy, prior_fy):
            components = [values_by_label.get(lbl, {}).get(fy) for lbl in component_labels]
            if all(c is not None for c in components):
                total_assets_bucket[fy] = sum(components)  # type: ignore[arg-type]
        if total_assets_bucket:
            values_by_label["PDF_TotalAssets"] = total_assets_bucket

        # This PDF reports every rupee figure in thousands ("C in '000",
        # verified live on every one of the three statement pages' own
        # column header) -- this app's INR_CRORE metrics store crore, so
        # every value except EPS (already a per-share rupee figure, same
        # "no rescaling" carve-out sources/nse_xbrl.py's own PER_SHARE_TAGS
        # applies) is rescaled thousands -> crore (x1,000 to rupees, /1e7
        # to crore = /10,000 net) before reaching build_observations_from_periods,
        # which assumes every value it's handed is already in the metric's
        # registered unit and does no scaling of its own.
        _NO_RESCALE_LABELS = {"PDF_BasicEPS"}
        _THOUSANDS_TO_CRORE = 1 / 10_000
        # Intermediate-only labels: inputs to the total_assets derivation
        # above, never emitted as their own fact -- no metric_alias is
        # registered for either (no standalone metric_key exists for
        # "employee stock options outstanding" or the undifferentiated
        # "other liabilities and provisions" line), so emitting them here
        # would just be a guaranteed "no metric_alias" skip downstream.
        _INTERMEDIATE_ONLY_LABELS = {"PDF_EmployeeStockOptions", "PDF_OtherLiabilitiesAndProvisions"}

        # The PDF scan above (page-finding + text extraction across a
        # 300+-page report) can run 20-30+ seconds of pure CPU work with
        # zero DB activity -- long enough, verified live, to trip the same
        # Neon idle-connection-drop this app's backfill scripts have hit
        # repeatedly elsewhere this session. self._conn is constructor-
        # injected (the standard SourceAdapter shape every caller,
        # including ingest_file(), already relies on), so this can't just
        # delay opening it the way a standalone script can -- instead,
        # reconnect-and-retry-once on the FIRST real DB touch below, same
        # defensive shape scripts/backfill_nse_annual_reports.py's
        # backfill_company() already uses for its own per-company stretch.
        def _is_stale_connection_error(exc: BaseException) -> bool:
            name = type(exc).__name__
            text = str(exc)
            return name in ("OperationalError", "InterfaceError") and (
                "server closed the connection" in text
                or "connection already closed" in text
                or "terminat" in text.lower()
            )

        # Reconnects on EVERY stale-connection error encountered, not just
        # once per file -- verified live this was necessary: a file with
        # ~15-20 metrics makes 30-40+ round trips (resolve_metric_key +
        # default-unit lookup per metric), enough opportunity for Neon to
        # drop the connection more than once across one file's own
        # processing, and a single-retry backstop still crashed on the
        # second drop. Bounded by _MAX_ATTEMPTS per label (not unbounded),
        # so a genuinely broken connection still fails loudly rather than
        # looping forever.
        _MAX_ATTEMPTS = 4

        def _build_with_retry(row_label: str, keyed_values: dict) -> list[NormalizedObservation]:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    return build_observations_from_periods(
                        self._conn,
                        company_id=company_id,
                        source=_SOURCE,
                        source_file=str(file_path),
                        parser_version=PARSER_VERSION,
                        period_type="annual",
                        statement_type=statement_type,
                        row_label=row_label,
                        period_values=keyed_values,
                    )
                except Exception as exc:  # noqa: BLE001 -- reconnect-and-retry backstop
                    if attempt == _MAX_ATTEMPTS or not _is_stale_connection_error(exc):
                        raise
                    logger.warning(
                        "%s: DB connection went stale during PDF parsing (%s) -- reopening and retrying (attempt %d/%d)",
                        file_path, exc, attempt + 1, _MAX_ATTEMPTS,
                    )
                    from storage.backend_bootstrap import open_db
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001 -- best-effort close of an already-broken connection
                        pass
                    self._conn = open_db()
            return []  # unreachable -- the loop above always returns or raises

        observations: list[NormalizedObservation] = []
        for label, period_values in values_by_label.items():
            if label in _INTERMEDIATE_ONLY_LABELS:
                continue
            if label not in _NO_RESCALE_LABELS:
                period_values = {fy: v * _THOUSANDS_TO_CRORE for fy, v in period_values.items()}
            keyed_values = {(fy, None): v for fy, v in period_values.items()}
            observations.extend(_build_with_retry(label, keyed_values))
        return observations
