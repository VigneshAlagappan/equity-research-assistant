"""Point-in-time (`as_of`) evidence scoping — the generic answer to "using
only information available at each historical point in time, could Signal
have detected X before it became obvious?".

Every evidence-gathering capability the Investigation Planner routes to
(research/capabilities.py) retrieves *everything on file* by default, which
is right for "what do we know now?" and wrong — silently, invisibly wrong —
for any question framed historically: the LLM is handed post-hoc data and
then asked whether the deterioration was detectable beforehand. That is
look-ahead bias, and no amount of prompt wording fixes it, because the
leaked facts are in the evidence block itself.

So the cutoff is enforced in retrieval, not in the prompt. `as_of` is a
plain ISO date (`YYYY-MM-DD`) threaded through
`research.capabilities.default_capabilities(as_of=...)`, which binds it into
each capability's concrete implementation. The Protocol signatures the
Planner calls are unchanged — the Planner does not know the cutoff exists,
which is deliberate: it means a future capability gets point-in-time support
by honouring one keyword argument, not by every caller learning a new
contract.

What the cutoff means per source (all "would this have been on file on that
date?", never "is this period interesting?"):

* structured financials — annual/quarterly periods whose fiscal period END
  is on or before `as_of` (`fiscal_year_visible`).
* macro series — observations whose `period` is on or before `as_of`
  (`period_visible`, tolerant of `YYYY`, `YYYY-MM` and `YYYY-MM-DD`).
* documents / passages / knowledge claims — items whose publication date is
  on or before `as_of` (`date_visible`); an item with no date at all is
  EXCLUDED, because "we cannot show it was available then" must fail closed.

**Known limitation, deliberately not modelled:** reporting lag. A fiscal
year ending 31-Mar-2013 is treated as visible from 31-Mar-2013, though the
audited result was only published weeks later. Modelling it properly needs a
per-company filing-date fact this database does not store; the cutoff here is
therefore mildly generous, never generous by more than one reporting cycle,
and is recorded on the investigation row (`investigations.as_of`) so any
conclusion drawn under it is auditable.
"""

from __future__ import annotations

import re

#: Default fiscal-year end for a company with no explicit `fiscal_year_end`
#: on record — 31 March, the Indian standard this database is dominated by.
DEFAULT_FISCAL_YEAR_END = "03-31"

_FY_LABEL_RE = re.compile(r"^FY(\d{4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")

#: Quarter -> how many months after the fiscal year START that quarter ends.
_QUARTER_MONTH_OFFSET = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}


def normalize_as_of(as_of: str | None) -> str | None:
    """Accepts `YYYY`, `YYYY-MM` or `YYYY-MM-DD` and returns a full ISO date
    (a bare year/month is widened to its LAST day, so "as of 2013" means "by
    the end of 2013"). Returns None for None/blank/unparseable input, which
    every caller treats as "no cutoff" — a malformed cutoff must not silently
    become a restrictive one."""
    if not as_of:
        return None
    match = _ISO_DATE_RE.match(as_of.strip())
    if match is None:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3)
    if month is None:
        return f"{year}-12-31"
    if day is None:
        return f"{year}-{month}-{_last_day_of_month(int(year), int(month)):02d}"
    return f"{year}-{month}-{day}"


def _last_day_of_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _fiscal_year_end_date(fiscal_year: str, fiscal_year_end: str | None) -> str | None:
    """The calendar date a fiscal-year label ends on. `FY2013` with a 03-31
    year end ends 2013-03-31; with a 12-31 year end it ends 2013-12-31 — the
    label always names the calendar year the period ENDS in, which is the
    convention `canonical_financials.fiscal_year` already uses."""
    match = _FY_LABEL_RE.match((fiscal_year or "").strip())
    if match is None:
        return None
    end = (fiscal_year_end or DEFAULT_FISCAL_YEAR_END).strip()
    if not re.match(r"^\d{2}-\d{2}$", end):
        end = DEFAULT_FISCAL_YEAR_END
    return f"{match.group(1)}-{end}"


def fiscal_year_visible(
    fiscal_year: str | None, as_of: str | None, *, quarter: str | None = None, fiscal_year_end: str | None = None
) -> bool:
    """Would this fiscal period have ended on or before `as_of`?

    Quarters are resolved against the same fiscal-year end, so Q1 of a
    31-March-ending FY2013 ends 2012-06-30 while its Q4 ends 2013-03-31 — a
    cutoff mid-year keeps the earlier quarters and drops the later ones,
    rather than keeping or dropping the whole year.

    An unparseable/absent fiscal_year is visible only when there is no cutoff
    at all: with a cutoff in force, "we can't date this" means "we can't show
    it was available", so it is excluded.
    """
    if not as_of:
        return True
    year_end = _fiscal_year_end_date(fiscal_year, fiscal_year_end)
    if year_end is None:
        return False
    if quarter:
        offset = _QUARTER_MONTH_OFFSET.get(quarter.strip().upper())
        if offset is not None:
            year, month_day = year_end.split("-", 1)
            end_month = int(month_day[:2])
            # Fiscal year start month = the month after the year-end month.
            start_month = end_month % 12 + 1
            start_year = int(year) - (1 if start_month > 1 else 0)
            total = (start_month - 1) + offset  # months elapsed since Jan of start_year
            q_year = start_year + (total - 1) // 12
            q_month = (total - 1) % 12 + 1
            year_end = f"{q_year:04d}-{q_month:02d}-{_last_day_of_month(q_year, q_month):02d}"
    return year_end <= as_of


def period_visible(period: str | None, as_of: str | None) -> bool:
    """Would a macro observation stamped `period` have existed by `as_of`?
    `macro_observations.period` is `YYYY`, `YYYY-MM` or `YYYY-MM-DD`
    depending on the series' own frequency; each is widened to the last day
    it could refer to before comparing, so a monthly `2013-03` counts as
    available on 2013-03-31 but not on 2013-03-01."""
    if not as_of:
        return True
    normalized = normalize_as_of(period)
    return normalized is not None and normalized <= as_of


def date_visible(value: str | None, as_of: str | None) -> bool:
    """Would something published/retrieved at `value` have been available by
    `as_of`? Fails closed: with a cutoff in force, an item carrying no usable
    date is treated as NOT visible, because an undated document cannot be
    shown to predate the cutoff."""
    if not as_of:
        return True
    if not value:
        return False
    match = _ISO_DATE_RE.match(str(value).strip())
    return match is not None and str(value).strip()[:10] <= as_of
