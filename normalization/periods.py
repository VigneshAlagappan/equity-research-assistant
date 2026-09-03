"""Fiscal-year and quarter parsing for vendor period headers ("Mar-24").

Fiscal year end is per-company (companies.fiscal_year_end_month, 1-12) —
India's default is 3 (Apr-Mar: FY2024 = Apr 2023 .. Mar 2024, so a column
headed "Mar-24" is the close of FY2024), the common US default is 12
(calendar year: Jan-Dec). Quarters are always numbered Q1-Q4 counting
forward from the month right after the fiscal year end, regardless of which
month that is — e.g. for fiscal_year_end_month=3 (India), Q1 = Apr-Jun, Q2 =
Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar; for fiscal_year_end_month=12 (calendar
year), Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec.
"""

from __future__ import annotations

import datetime as dt
import re

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DEFAULT_FISCAL_YEAR_END_MONTH = 3  # India's Apr-Mar convention — every existing caller's default

_HEADER_RE = re.compile(r"^([A-Za-z]{3})[-\s]?(\d{2}|\d{4})$")


class PeriodParseError(ValueError):
    """Raised when a period header isn't a recognizable "Mon-YY" / "Mon-YYYY" label."""


def _expand_year(two_or_four_digit: str) -> int:
    if len(two_or_four_digit) == 4:
        return int(two_or_four_digit)
    # Screener-style 2-digit years are always 2000s in this system's date range.
    return 2000 + int(two_or_four_digit)


def _require_valid_period_type(period_type: str) -> None:
    if period_type not in ("annual", "quarterly"):
        raise ValueError(f"period_type must be 'annual' or 'quarterly', got {period_type!r}")


def _require_valid_fiscal_year_end_month(fiscal_year_end_month: int) -> None:
    if not 1 <= fiscal_year_end_month <= 12:
        raise ValueError(f"fiscal_year_end_month must be 1-12, got {fiscal_year_end_month!r}")


def _month_to_quarter(month: int, fiscal_year_end_month: int) -> tuple[str, int]:
    """(quarter, fiscal-year-offset) for a calendar month closing under a
    fiscal year that ends in `fiscal_year_end_month`. Offset is added to the
    calendar year before y2k expansion: a month in [fiscal_year_end_month+1,
    12] closes out the fiscal year named after the *next* calendar year (Jun
    under a Mar fiscal-year-end -> FY of next year); a month in
    [1, fiscal_year_end_month] closes out the fiscal year named after this
    same calendar year (Mar under a Mar fiscal-year-end -> FY of this year).
    Generalizes the Apr-Mar-only lookup this module used to hardcode — for
    fiscal_year_end_month=3 this reproduces that exact mapping."""
    months_after_fy_end = (month - fiscal_year_end_month - 1) % 12  # 0 = first month of the fiscal year
    quarter = f"Q{months_after_fy_end // 3 + 1}"
    fy_offset = 1 if month > fiscal_year_end_month else 0
    return quarter, fy_offset


def _fiscal_year_and_quarter(
    month: int, calendar_year: int, period_type: str, fiscal_year_end_month: int
) -> tuple[str, str | None]:
    quarter, fy_offset = _month_to_quarter(month, fiscal_year_end_month)
    fiscal_year = f"FY{calendar_year + fy_offset}"
    if period_type == "annual":
        return fiscal_year, None
    return fiscal_year, quarter


def parse_period_header(
    header: str, period_type: str, fiscal_year_end_month: int = DEFAULT_FISCAL_YEAR_END_MONTH
) -> tuple[str, str | None]:
    """Parse a "Mar-24" / "Mar24" style header into (fiscal_year, quarter).

    period_type "annual" always returns quarter=None (annual sheets close at
    the fiscal year end, but the observation itself isn't scoped to a
    quarter). period_type "quarterly" returns the quarter implied by the
    closing month. fiscal_year_end_month defaults to 3 (Apr-Mar, India) —
    every existing caller is India-only and relies on this default.
    """
    _require_valid_period_type(period_type)
    _require_valid_fiscal_year_end_month(fiscal_year_end_month)

    match = _HEADER_RE.match(header.strip())
    if not match:
        raise PeriodParseError(f"Not a recognizable period header: {header!r}")

    month_abbr, year_part = match.groups()
    month = _MONTH_ABBR.get(month_abbr.lower())
    if month is None:
        raise PeriodParseError(f"Unrecognized month abbreviation in header: {header!r}")

    return _fiscal_year_and_quarter(month, _expand_year(year_part), period_type, fiscal_year_end_month)


def fiscal_year_and_quarter_from_date(
    period_end: dt.date, period_type: str, fiscal_year_end_month: int = DEFAULT_FISCAL_YEAR_END_MONTH
) -> tuple[str, str | None]:
    """Same fiscal-year logic as parse_period_header, but from a real
    date/datetime object — Screener's "Data Sheet" tab gives period-end dates
    as actual dates, not "Mon-YY" text (README's example text header doesn't
    reflect the real export's "Data Sheet" shape). fiscal_year_end_month
    defaults to 3 (Apr-Mar, India) — Screener is India-only, so its one real
    caller relies on this default."""
    _require_valid_period_type(period_type)
    _require_valid_fiscal_year_end_month(fiscal_year_end_month)
    return _fiscal_year_and_quarter(period_end.month, period_end.year, period_type, fiscal_year_end_month)


_FISCAL_YEAR_RE = re.compile(r"^FY(\d{4})$")


def fiscal_year_number(fiscal_year: str) -> int:
    """Parse "FY2024" -> 2024."""
    match = _FISCAL_YEAR_RE.match(fiscal_year.strip())
    if not match:
        raise PeriodParseError(f"Not a valid fiscal year, expected 'FYyyyy': {fiscal_year!r}")
    return int(match.group(1))


_QUARTER_ORDER = ["Q1", "Q2", "Q3", "Q4"]


def previous_quarter(fiscal_year: str, quarter: str) -> tuple[str, str]:
    """Return the (fiscal_year, quarter) immediately before the given one.

    Q1 FY2024's previous quarter is Q4 FY2023 (crosses the fiscal-year boundary);
    Q2/Q3/Q4 just step back within the same fiscal year.
    """
    if quarter not in _QUARTER_ORDER:
        raise ValueError(f"quarter must be one of {_QUARTER_ORDER}, got {quarter!r}")
    year = fiscal_year_number(fiscal_year)
    index = _QUARTER_ORDER.index(quarter)
    if index == 0:
        return f"FY{year - 1}", "Q4"
    return fiscal_year, _QUARTER_ORDER[index - 1]
