"""Table-aware extraction of Balance Sheet (+ scoped P&L) facts from an NSE
quarterly-results filing PDF, via `pdfplumber` (feasibility report Section
9 point 4: `pypdf`'s raw-text extraction doesn't reliably preserve table
structure; a table-aware extractor is needed for real fact-level parsing —
`pdfplumber` wasn't installed before this module, now in requirements.txt).

Scope (per the production backfill task, not the original spike): Balance
Sheet extraction for any period missing it (the confirmed real gap —
feasibility report Section 7), Profit & Loss extraction only for periods
where XBRL doesn't exist at all (pre-2019ish, per company). Cash flow is
explicitly out of scope (SEBI LODR Reg 33 doesn't require it quarterly).

Two real, verified document layouts (feasibility report Sections 7/13.2,
directly re-verified against the spike's own downloaded PDFs while writing
this module):

  * Banking "Statement of Assets and Liabilities" (HDFCBANK): one label per
    physical line, followed immediately by that line's numbers on the same
    line (`Capital 55267 54903 55128`) -- units printed as "( in lac)".
  * ICICI Bank's own "Summarised/Summary ... Balance Sheet": some labels
    wrap across 2-3 physical lines before their numbers appear
    (`Borrowings (includes\nsubordinated debt) 164,918 91,631 89,131`) --
    units printed as "` crore" (backtick is a broken currency-symbol glyph
    in the extracted text, verified against the real PDF).

Neither layout survives `pdfplumber.extract_tables()` cleanly (verified
directly against these same files): un-ruled tables get merged into one
newline-joined cell per column, losing the row/number alignment tables()
exists to preserve. This module instead treats `extract_text()`'s own
preserved line order as the table structure and parses it with an explicit
label-accumulation state machine (below) -- still table-AWARE (it knows
the shape it's looking for: N numbers per logical row, a fixed column
count per section) rather than a generic raw-text regex scan of the whole
document.

Never guesses: a page/section that doesn't match one of the two known
patterns produces zero observations for that page, logged, not a
best-effort partial parse — same "malformed data is rejected with a
warning, never silently accepted" convention as normalization/financials.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from sources.base import NormalizedObservation

logger = logging.getLogger(__name__)

PARSER_VERSION = "nse_pdf_v1"  # web/charts_feed.py's _classify_provenance() matches on this "nse_pdf" prefix

# ------------------------------------------------------------------
# Row-label -> metric_key. Deliberately a LOCAL map, not an addition to
# normalization/financials.py's DEFAULT_METRIC_ALIASES: those are XBRL tag
# *local names* (e.g. "ReservesAndSurplus") for source="nse", a different
# vocabulary than a PDF's own human-readable row labels (e.g. "Reserves and
# Surplus") even though they mean the same thing and both are stamped with
# source="nse" on the resulting observation (see this module's docstring
# for why: PDF and XBRL are both "the same NSE filing", just a different
# extraction_method — web/charts_feed.py's _classify_provenance() already
# expects exactly this shape and tells them apart by parser_version, not
# source). Every label below is copied verbatim (case preserved for
# reading, matched case-insensitively) from a real downloaded filing —
# feasibility report Section 7 (HDFCBANK) and Section 13.2 (ICICIBANK).
# ------------------------------------------------------------------
_BALANCE_SHEET_ROW_ALIASES: dict[str, str] = {
    "capital": "equity_share_capital",
    "reserves and surplus": "reserves",
    "deposits": "deposits",
    "borrowings": "borrowings",
    "borrowings (includes subordinated debt)": "borrowings",
    "borrowings (incl. sub debt)": "borrowings",
    "other liabilities and provisions": "other_liabilities",
    "other liabilities": "other_liabilities",
    "cash and balances with reserve bank of india": "cash_and_bank",
    "cash & bal. with rbi": "cash_and_bank",
    "investments": "investments",
    "advances": "advances",
    "fixed assets": "net_block",
    "other assets": "other_assets",
    "total assets": "total_assets",
    # Banking's balance-sheet "Total" line is printed identically on both
    # the liabilities side and the assets side (same value, a genuine
    # accounting identity, not a duplicate to reconcile) -- only the
    # ASSETS-side occurrence is mapped to total_assets (see
    # _split_capital_and_liabilities_vs_assets below); this entry exists
    # so a caller that already knows which side a "Total"/"Total Capital
    # and Liabilities" row is on can still look it up by label alone.
    "total capital and liabilities": "total_assets",
    "total": "total_assets",
}

# Rows deliberately NOT mapped (present in real filings, verified, but with
# no corresponding metrics_dictionary entry / no safe single-metric
# meaning) -- listed so a future reviewer knows this was a decision, not an
# oversight: "Employee stock options outstanding" (ICICI-specific small
# equity sub-line, no dedicated metric), "Balances with banks and money at
# call and short notice" (a sub-component of cash_and_bank the app doesn't
# split out separately -- summing it into cash_and_bank would silently
# change what that metric has always meant for every other source).

_PNL_ROW_ALIASES: dict[str, str] = {
    "interest earned": "interest_earned",
    "other income": "other_income",
    "interest expended": "interest_expended",
    "operating expenses": "operating_expenses",
    "provisions and contingencies": "provisions_and_contingencies",
    "provisions & contingencies": "provisions_and_contingencies",
    "profit before tax": "profit_before_tax",
    "tax expense": "tax",
    "net profit for the period": "net_profit",
    "net profit / (loss) for the period": "net_profit",
    "earnings per share": "eps",
    "basic earnings per share": "eps",
}

_SECTION_UNIT_PATTERNS: list[tuple[re.Pattern, float]] = [
    # (pattern matched against the section's own header text, multiplier to INR_CRORE)
    (re.compile(r"\(\s*(?:in\s+)?lac(?:s|hs)?\s*\)", re.IGNORECASE), 0.01),   # lakh -> crore
    (re.compile(r"\(\s*(?:in\s+)?crore\s*\)", re.IGNORECASE), 1.0),
    (re.compile(r"crore", re.IGNORECASE), 1.0),  # ICICI's "` crore" header (no parens)
    (re.compile(r"\(\s*(?:in\s+)?lakh\s*\)", re.IGNORECASE), 0.01),
]

_BALANCE_SHEET_HEADING_RE = re.compile(
    r"(?i)(standalone\s+|consolidated\s+)?(summari[sz]ed?\s+)?"
    r"(statement of assets and liabilities|balance sheet)"
)
_CONSOLIDATED_SECTION_RE = re.compile(r"(?i)\bconsolidated\s+(financial\s+results|segmental)")
_STANDALONE_SECTION_RE = re.compile(r"(?i)\bstandalone\s+financial\s+results")

#: A line is "numeric" if it ends in one or more amount-shaped tokens —
#: digits with optional thousands separators/decimals, or "-"/"—" for a
#: reported nil/blank (kept as a 0.0 value, distinguished from "not
#: present at all" the same way sources/screener.py's own numeric parser
#: treats a dash).
_AMOUNT_TOKEN = r"(?:-|—|\(?[\d,]+(?:\.\d+)?\)?)"
_TRAILING_AMOUNTS_RE = re.compile(rf"((?:{_AMOUNT_TOKEN}\s*)+)$")

_DATE_PATTERNS = [
    re.compile(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})"),                         # 30.06.2021 / 30-06-2021
    re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})"),                           # 30-Jun-21 / 30-Jun-2021
    re.compile(r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})"),                         # June 30, 2021
]
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_token(text: str) -> date | None:
    text = text.strip()
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            if groups[0].isdigit() and groups[1].isdigit():
                day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            elif groups[1].isalpha():
                day, month_s, year = int(groups[0]), groups[1].lower()[:3], int(groups[2])
                month = _MONTH_ABBR.get(month_s)
                if month is None:
                    continue
            else:
                month_s, day, year = groups[0].lower()[:3], int(groups[1]), int(groups[2])
                month = _MONTH_ABBR.get(month_s)
                if month is None:
                    continue
            if year < 100:
                year += 2000 if year <= (date.today().year % 100 + 1) else 1900
            return date(year, month, day)
        except ValueError:
            continue
    return None


@dataclass
class ExtractedFact:
    metric_key: str
    period_type: str  # "quarterly" | "annual"
    fiscal_year: str
    quarter: str | None
    statement_type: str
    value: float
    unit: str = "INR_CRORE"


def _parse_amount(token: str, multiplier: float) -> float | None:
    token = token.strip()
    if token in ("-", "—", ""):
        return 0.0
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()")
    token = token.replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    if negative:
        value = -value
    return value * multiplier


def _find_unit_multiplier(context_text: str) -> float:
    for pattern, multiplier in _SECTION_UNIT_PATTERNS:
        if pattern.search(context_text):
            return multiplier
    logger.warning("No recognized unit marker in section header text %r — assuming already INR_CRORE", context_text[:80])
    return 1.0


_MONTH_DAY_COMMA_RE = re.compile(r"[A-Za-z]+\s+\d{1,2},")
_BARE_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _reconstruct_split_date_headers(header_lines: list[str]) -> list[str]:
    """A real, verified layout (ICICIBANK's pre-2019 "Summary Balance
    Sheet", e.g. the 2016 filing) prints "June 30, June 30, March 31," on
    one line and the matching years "2015 2016 2016" on the NEXT line —
    the month/day and year halves of the same 3 dates, split across lines
    by the PDF's own column-wrapping. Neither line alone contains a
    complete date _DATE_PATTERNS can match. When a line looks like N
    repeated "Month Day," groups and the very next line is exactly N bare
    4-digit numbers, this zips them back into one reconstructed line
    ("June 30, 2015 June 30, 2016 March 31, 2016") that the normal
    finditer-based extraction below can then parse like any other header
    line — a targeted fix for this one verified shape, not a generic
    multi-line-header solver."""
    reconstructed: list[str] = []
    i = 0
    while i < len(header_lines):
        line = header_lines[i]
        month_day_groups = _MONTH_DAY_COMMA_RE.findall(line)
        if month_day_groups and i + 1 < len(header_lines):
            year_groups = _BARE_YEAR_RE.findall(header_lines[i + 1])
            if len(year_groups) == len(month_day_groups):
                zipped = " ".join(f"{md} {yr}" for md, yr in zip(month_day_groups, year_groups))
                reconstructed.append(zipped)
                i += 2
                continue
        reconstructed.append(line)
        i += 1
    return reconstructed


def _extract_period_columns(
    header_lines: list[str], expected_quarter_end: date, expected_fiscal_year: str, expected_quarter: str | None,
) -> dict[int, tuple[str, str, str | None]]:
    """Column index -> (period_type, fiscal_year, quarter) for every header
    column whose parsed date matches EITHER this filing's own reported
    quarter-end OR (on a Q4 filing only) its fiscal-year-end -- comparative
    columns (prior year, prior quarter) are deliberately left unmapped
    (skipped, not stored), same reasoning sources/nse_xbrl.py's module
    docstring gives for only reading "OneD"/"OneI": every other period this
    app cares about already gets its own separately-filed document."""
    # Real header layouts verified against actual filings vary in spacing
    # convention: HDFCBank's "30.06.2021 30.06.2020 31.03.2021" and ICICI's
    # "30-Jun-20 31-Mar-21 30-Jun-21" both single-space-separate three
    # complete date tokens on one line (no internal spaces within a date
    # itself for either format) -- splitting on whitespace first would
    # merge them back together and _parse_date_token would only ever find
    # the FIRST one per line (a real bug this fixes: it silently collapsed
    # 3 columns into 1). Scanning the whole line with each pattern's own
    # `finditer` instead finds every self-contained date token regardless
    # of the separator between them, in left-to-right order (== column
    # order).
    dates: list[date | None] = []
    for line in _reconstruct_split_date_headers(header_lines):
        matches: list[tuple[int, date]] = []
        for pattern in _DATE_PATTERNS:
            for m in pattern.finditer(line):
                d = _parse_date_token(m.group(0))
                if d is not None:
                    matches.append((m.start(), d))
        matches.sort(key=lambda pair: pair[0])
        dates.extend(d for _, d in matches)
    columns: dict[int, tuple[str, str, str | None]] = {}
    for idx, d in enumerate(dates):
        if d is None:
            continue
        if d == expected_quarter_end:
            columns[idx] = ("quarterly", expected_fiscal_year, expected_quarter)
            if expected_quarter == "Q4":
                # Same instant serves the annual framing too (a Q4 quarter-end
                # IS the fiscal-year-end) -- same convention as nse_xbrl.py's
                # "OneI" instant context, stamped into both framings.
                columns.setdefault(idx, columns[idx])
    return columns


def _section_statement_type(current_section: str | None, heading_text: str) -> str | None:
    m = _BALANCE_SHEET_HEADING_RE.search(heading_text)
    if m and m.group(1):
        return "consolidated" if "consolidated" in m.group(1).lower() else "standalone"
    return current_section


def _accumulate_labelled_rows(lines: list[str]) -> list[tuple[str, list[str]]]:
    """[(label, [amount_token, ...])] from a block of lines where a label
    may wrap across several lines before its amounts appear on the final
    line of that logical row (ICICI's own wrapped-label layout) or share a
    single line with its amounts (HDFCBANK's layout) -- both handled by the
    same accumulation rule: keep buffering lines with no trailing numeric
    tokens as "pending label text"; the first line that DOES end in numeric
    tokens closes out the row using (buffer + that line's own leading
    text) as the full label."""
    rows: list[tuple[str, list[str]]] = []
    buffer: list[str] = []
    for line in lines:
        m = _TRAILING_AMOUNTS_RE.search(line.strip())
        if not m:
            buffer.append(line.strip())
            continue
        leading_text = line[: m.start()].strip()
        if leading_text:
            buffer.append(leading_text)
        label = " ".join(part for part in buffer if part).strip()
        amounts = m.group(1).split()
        if label:
            rows.append((label, amounts))
        buffer = []
    return rows


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().lower()


def extract_balance_sheet_from_text(
    page_texts: list[str],
    *,
    expected_quarter_end: date,
    expected_fiscal_year: str,
    expected_quarter: str | None,
) -> list[ExtractedFact]:
    """Every Balance Sheet fact extractable from `page_texts` (one string
    per PDF page, in order — caller gets these from pdfplumber, this
    function never opens a file itself, so it's independently testable
    against a fixture string). Handles a filing with BOTH a standalone and
    a consolidated Balance Sheet section (each detected and tagged
    separately) and, on a Q4 filing, stamps the one balance-sheet instant
    into both the quarterly and annual period framing (see
    _extract_period_columns's docstring)."""
    facts: list[ExtractedFact] = []
    current_section: str | None = None

    for page_text in page_texts:
        if _CONSOLIDATED_SECTION_RE.search(page_text):
            current_section = "consolidated"
        elif _STANDALONE_SECTION_RE.search(page_text):
            current_section = "standalone"

        heading_match = _BALANCE_SHEET_HEADING_RE.search(page_text)
        if not heading_match:
            continue
        statement_type = _section_statement_type(current_section, page_text) or "standalone"

        lines = page_text.splitlines()
        heading_line_idx = next(
            (i for i, line in enumerate(lines) if _BALANCE_SHEET_HEADING_RE.search(line)), None
        )
        if heading_line_idx is None:
            continue

        body_lines = lines[heading_line_idx + 1:]
        # Header (date) lines: everything up to the first line that starts
        # a labelled section ("Capital and Liabilities" / "CAPITAL AND
        # LIABILITIES" / "ASSETS") -- verified against both real layouts.
        section_start_re = re.compile(r"(?i)^\s*(capital and liabilities|assets)\s*$")
        header_end = next((i for i, line in enumerate(body_lines) if section_start_re.match(line)), len(body_lines))
        header_lines = body_lines[:header_end]
        unit_multiplier = _find_unit_multiplier(" ".join([page_text[:heading_match.start()]] + header_lines))
        period_columns = _extract_period_columns(header_lines, expected_quarter_end, expected_fiscal_year, expected_quarter)
        if not period_columns:
            logger.info(
                "Balance sheet section found (%s) but no header column matched expected quarter-end %s — skipping",
                statement_type, expected_quarter_end,
            )
            continue

        # Body: everything from header_end to the next blank-ish break
        # (a numbered note, "Notes", or the second "Total" row closing the
        # ASSETS side — used as the natural stop so trailing narrative
        # text below the table never gets mis-parsed as rows).
        stop_re = re.compile(r"(?i)^\s*(notes?\s*:|\d+\s+[A-Z])")
        body_end = header_end + 1
        total_seen = 0
        for i, line in enumerate(body_lines[header_end:], start=header_end):
            if stop_re.match(line):
                body_end = i
                break
            if re.match(r"(?i)^\s*total\b", line.strip()):
                total_seen += 1
                if total_seen >= 2:
                    body_end = i + 1
                    break
        else:
            body_end = len(body_lines)

        table_lines = body_lines[header_end:body_end]

        # Split at the "ASSETS" section-header line FIRST, before label
        # accumulation -- a bare section-header line (no trailing numbers
        # of its own) would otherwise get swallowed into the NEXT row's
        # label by _accumulate_labelled_rows' own wrap-handling (real bug,
        # verified: "CAPITAL AND LIABILITIES" merging into "CAPITAL AND
        # LIABILITIES Capital", which no alias matches). Splitting first
        # means each half only ever contains real row labels plus at most
        # one "Total" line, and which side a "Total" belongs to is then
        # known from which half it came from, not guessed from wording
        # (HDFCBank prints bare "Total" on both sides; ICICI prints "Total
        # Capital and Liabilities" / "Total Assets" — both handled the
        # same way here).
        assets_header_re = re.compile(r"(?i)^\s*assets\s*$")
        assets_idx = next((i for i, line in enumerate(table_lines) if assets_header_re.match(line)), None)
        if assets_idx is None:
            liabilities_lines, asset_lines = table_lines, []
        else:
            liabilities_lines, asset_lines = table_lines[:assets_idx], table_lines[assets_idx + 1:]
        capital_header_re = re.compile(r"(?i)^\s*capital and liabilities\s*$")
        liabilities_lines = [line for line in liabilities_lines if not capital_header_re.match(line)]

        for side_lines, on_assets_side in ((liabilities_lines, False), (asset_lines, True)):
            for label, amounts in _accumulate_labelled_rows(side_lines):
                normalized = _normalize_label(label)
                if normalized in ("total", "total capital and liabilities", "total assets"):
                    if not on_assets_side:
                        continue  # liabilities-side "Total" -- same value as total_assets, not a separate fact
                    metric_key = "total_assets"
                else:
                    metric_key = _BALANCE_SHEET_ROW_ALIASES.get(normalized)
                if metric_key is None:
                    logger.info("No row-label alias for balance-sheet label %r — skipping", label)
                    continue

                for col_idx, (period_type, fiscal_year, quarter) in period_columns.items():
                    if col_idx >= len(amounts):
                        continue
                    value = _parse_amount(amounts[col_idx], unit_multiplier)
                    if value is None:
                        continue
                    facts.append(
                        ExtractedFact(
                            metric_key=metric_key, period_type=period_type, fiscal_year=fiscal_year,
                            quarter=quarter, statement_type=statement_type, value=value,
                        )
                    )
                    if expected_quarter == "Q4" and period_type == "quarterly":
                        facts.append(
                            ExtractedFact(
                                metric_key=metric_key, period_type="annual", fiscal_year=fiscal_year,
                                quarter=None, statement_type=statement_type, value=value,
                            )
                        )

    return facts


def extract_from_pdf(
    pdf_path,
    *,
    expected_quarter_end: date,
    expected_fiscal_year: str,
    expected_quarter: str | None,
    include_pnl: bool = False,
) -> list[ExtractedFact]:
    """Open `pdf_path` with pdfplumber and run the text-based extraction
    above. Returns [] (logged) for a PDF with no usable text layer —
    caller is responsible for routing that case to nse_filing_discovery_log
    as needs_ocr, this function never attempts OCR itself."""
    import pdfplumber

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_texts = [(page.extract_text() or "") for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - a malformed/corrupt PDF must not crash the caller
        logger.warning("Failed to open %s with pdfplumber: %s", pdf_path, exc)
        return []

    total_chars = sum(len(t) for t in page_texts)
    if total_chars < 500:
        logger.info("%s: only %d extractable characters — likely scanned/no text layer, skipping", pdf_path, total_chars)
        return []

    facts = extract_balance_sheet_from_text(
        page_texts, expected_quarter_end=expected_quarter_end,
        expected_fiscal_year=expected_fiscal_year, expected_quarter=expected_quarter,
    )

    if include_pnl:
        facts.extend(
            _extract_pnl_from_text(
                page_texts, expected_quarter_end=expected_quarter_end,
                expected_fiscal_year=expected_fiscal_year, expected_quarter=expected_quarter,
            )
        )

    return facts


_PNL_HEADING_RE = re.compile(r"(?i)(standalone\s+|consolidated\s+)?(unaudited|audited)?\s*financial results")


def _extract_pnl_from_text(
    page_texts: list[str], *, expected_quarter_end: date, expected_fiscal_year: str, expected_quarter: str | None,
) -> list[ExtractedFact]:
    """P&L extraction — scoped (by the caller) to pre-2019 periods only,
    where XBRL genuinely doesn't exist (feasibility report Section 8);
    never used to compete with an XBRL-covered period (redundant, risks
    disagreeing with it on rounding/standalone-consolidated mixups per the
    task's own scope note). Same label-accumulation technique as the
    balance sheet extractor, over `_PNL_ROW_ALIASES` instead."""
    facts: list[ExtractedFact] = []
    current_section: str | None = None
    for page_text in page_texts:
        if _CONSOLIDATED_SECTION_RE.search(page_text):
            current_section = "consolidated"
        elif _STANDALONE_SECTION_RE.search(page_text):
            current_section = "standalone"

        heading_match = _PNL_HEADING_RE.search(page_text)
        if not heading_match:
            continue
        statement_type = _section_statement_type(current_section, page_text) or current_section or "standalone"

        lines = page_text.splitlines()
        heading_idx = next((i for i, line in enumerate(lines) if _PNL_HEADING_RE.search(line)), None)
        if heading_idx is None:
            continue
        body_lines = lines[heading_idx + 1:heading_idx + 60]  # a P&L statement is short; cap the scan window
        header_lines = body_lines[:6]
        unit_multiplier = _find_unit_multiplier(" ".join(header_lines))
        period_columns = _extract_period_columns(header_lines, expected_quarter_end, expected_fiscal_year, expected_quarter)
        if not period_columns:
            continue

        rows = _accumulate_labelled_rows(body_lines)
        for label, amounts in rows:
            metric_key = _PNL_ROW_ALIASES.get(_normalize_label(label))
            if metric_key is None:
                continue
            for col_idx, (period_type, fiscal_year, quarter) in period_columns.items():
                if col_idx >= len(amounts):
                    continue
                value = _parse_amount(amounts[col_idx], unit_multiplier)
                if value is None:
                    continue
                facts.append(
                    ExtractedFact(
                        metric_key=metric_key, period_type=period_type, fiscal_year=fiscal_year,
                        quarter=quarter, statement_type=statement_type, value=value,
                    )
                )
    return facts


def facts_to_observations(
    facts: list[ExtractedFact], *, company_id: str, source_file: str, source_document_id: int | None = None,
) -> list[NormalizedObservation]:
    """ExtractedFact -> NormalizedObservation, stamped with this module's
    own PARSER_VERSION and source="nse" (see module docstring: a PDF-
    derived fact is still an "nse" filing, same as XBRL — parser_version,
    not source, is what tells them apart downstream)."""
    from datetime import datetime, timezone

    retrieved_at = datetime.now(timezone.utc).isoformat()
    observations = []
    for fact in facts:
        observations.append(
            NormalizedObservation(
                company_id=company_id,
                metric_key=fact.metric_key,
                period_type=fact.period_type,
                fiscal_year=fact.fiscal_year,
                quarter=fact.quarter,
                statement_type=fact.statement_type,
                value=fact.value,
                unit=fact.unit,
                source="nse",
                source_file=source_file,
                parser_version=PARSER_VERSION,
                retrieved_at=retrieved_at,
                source_document_id=source_document_id,
            )
        )
    return observations
