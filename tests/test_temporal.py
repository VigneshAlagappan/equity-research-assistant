"""research/temporal.py — the point-in-time (`as_of`) evidence cutoff, and
its enforcement inside the capability bindings.

The behaviour that actually matters for look-ahead bias is not "does the
helper compute a date correctly" but "can a capability bound to a cutoff
still hand back post-cutoff data" — so the second half of this module tests
default_capabilities(as_of=...) against a real database, not the helpers in
isolation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from research.capabilities import default_capabilities
from research.temporal import date_visible, fiscal_year_visible, normalize_as_of, period_visible
from tests.test_screener_adapter import _make_screener_workbook


def test_normalize_widens_a_bare_year_or_month_to_its_last_day() -> None:
    assert normalize_as_of("2013") == "2013-12-31"
    assert normalize_as_of("2013-02") == "2013-02-28"
    assert normalize_as_of("2012-02") == "2012-02-29"  # leap year
    assert normalize_as_of("2013-04") == "2013-04-30"
    assert normalize_as_of("2013-03-31") == "2013-03-31"


def test_normalize_treats_blank_and_garbage_as_no_cutoff() -> None:
    """A malformed cutoff must not silently become a restrictive one — every
    caller reads None as "no cutoff at all"."""
    assert normalize_as_of(None) is None
    assert normalize_as_of("") is None
    assert normalize_as_of("last tuesday") is None


def test_fiscal_year_visibility_uses_the_period_end_not_the_label() -> None:
    # FY2013 with a 31-March year end ended 2013-03-31.
    assert fiscal_year_visible("FY2013", "2013-03-31")
    assert not fiscal_year_visible("FY2013", "2013-03-30")
    assert not fiscal_year_visible("FY2014", "2013-03-31")


def test_fiscal_year_visibility_honours_a_non_march_year_end() -> None:
    assert not fiscal_year_visible("FY2013", "2013-06-30", fiscal_year_end="12-31")
    assert fiscal_year_visible("FY2013", "2013-12-31", fiscal_year_end="12-31")


def test_quarters_within_a_fiscal_year_are_dated_independently() -> None:
    """A cutoff mid-year keeps the earlier quarters and drops the later ones,
    rather than keeping or dropping the whole fiscal year."""
    assert fiscal_year_visible("FY2013", "2012-09-30", quarter="Q1")  # Q1 FY2013 ended 2012-06-30
    assert not fiscal_year_visible("FY2013", "2012-09-30", quarter="Q3")  # Q3 ended 2012-12-31
    assert fiscal_year_visible("FY2013", "2013-03-31", quarter="Q4")


def test_undated_items_fail_closed_under_a_cutoff_and_open_without_one() -> None:
    """"We cannot show this was available then" must exclude, not include —
    but with no cutoff in force nothing is filtered at all."""
    assert not fiscal_year_visible(None, "2013-03-31")
    assert not date_visible(None, "2013-03-31")
    assert not period_visible(None, "2013-03-31")
    assert fiscal_year_visible(None, None)
    assert date_visible(None, None)
    assert period_visible(None, None)


def test_macro_periods_are_compared_at_their_own_granularity() -> None:
    assert period_visible("2013-03", "2013-03-31")
    assert not period_visible("2013-03", "2013-03-01")  # a March monthly point isn't known on 1 March
    assert period_visible("2012", "2013-01-01")
    assert not period_visible("2013-04-05", "2013-03-31")


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    """Same real-ingestion fixture tests/test_structured_search.py uses —
    FY2023 and FY2024 annual data for HDFCBANK."""
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_bound_financial_capability_cannot_return_post_cutoff_years(ingested_conn: sqlite3.Connection) -> None:
    """The point of binding `as_of` in default_capabilities: the Planner calls
    the same Protocol either way, and the cutoff is enforced in retrieval —
    there is no path by which FY2024 reaches an as-of-FY2023 investigation."""
    unrestricted = default_capabilities().financial_evidence(ingested_conn, "HDFCBANK")
    restricted = default_capabilities(as_of="2023-03-31").financial_evidence(ingested_conn, "HDFCBANK")

    assert any("FY2024" in e.label for e in unrestricted)
    assert restricted, "the cutoff should keep the pre-cutoff years, not empty the evidence"
    assert not any("FY2024" in e.label for e in restricted)
    assert any("FY2023" in e.label for e in restricted)


def test_derived_calculations_are_recomputed_on_the_truncated_series(ingested_conn: sqlite3.Connection) -> None:
    """A cutoff must not leave a YoY/CAGR line describing a year the cutoff
    hides — the derived lines are computed from the truncated series, not
    filtered after the fact."""
    restricted = default_capabilities(as_of="2023-03-31").financial_evidence(ingested_conn, "HDFCBANK")
    assert not any("FY2024" in e.label or "FY2024" in (e.citation or "") for e in restricted)


def test_bound_indicator_capability_is_disabled_entirely_under_a_cutoff(
    ingested_conn: sqlite3.Connection,
) -> None:
    """Indicator rules evaluate against the latest facts on file and have no
    historical mode, so under a cutoff the capability must contribute nothing
    rather than leak a post-cutoff finding into a historical investigation."""
    assert default_capabilities(as_of="2023-03-31").indicator_evidence(ingested_conn, "HDFCBANK") == []


def test_a_bare_year_cutoff_is_accepted_by_the_bindings(ingested_conn: sqlite3.Connection) -> None:
    restricted = default_capabilities(as_of="2023").financial_evidence(ingested_conn, "HDFCBANK")
    assert any("FY2023" in e.label for e in restricted)
    assert not any("FY2024" in e.label for e in restricted)
