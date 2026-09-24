"""Tests for the economic graph, Phase 1 (registry schema + canonical
observation model). Parametrized over both backends (SQLite via
storage.repositories, Postgres via storage.repositories_pg against the
local docker-compose.test.yml container) using the `backend` fixture below
-- same discipline test_backend_compatibility.py documents, except this
suite runs the Postgres half against the always-available LOCAL test
container (tests/postgres_test_db.py / conftest.py's own `pg_conn`
fixture), not a Neon branch, so it isn't gated behind an env var.

Covers:
- The four canonical-observation query functions (latest/as_of/history/
  vintages) against a synthetic 3-vintage series (provisional -> revised
  -> final), with known expected results per function.
- Registry CRUD (upsert idempotency, get/list, category+status filters).
- The indicator/series relationship: one indicator -> many series, no
  field duplication between the two tables.
- The `status` field never defaulting to anything but 'registered_only'.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from storage import repositories as repositories_sqlite
from storage import repositories_pg


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request) -> Iterator[tuple[object, object]]:
    """Yields (conn, repo_module) for each backend. Postgres runs against
    the local docker-compose.test.yml container via conftest.py's pg_conn
    fixture (skips, not fails, if that container isn't running)."""
    if request.param == "sqlite":
        conn = request.getfixturevalue("db_conn")
        yield conn, repositories_sqlite
    else:
        conn = request.getfixturevalue("pg_conn")
        yield conn, repositories_pg


# ------------------------------------------------------------------
# Registry CRUD
# ------------------------------------------------------------------


def test_upsert_economic_indicator_is_idempotent_by_name(backend) -> None:
    conn, repo = backend
    id1 = repo.upsert_economic_indicator(conn, "CPI Combined", "inflation", economic_meaning="v1")
    id2 = repo.upsert_economic_indicator(conn, "CPI Combined", "inflation", economic_meaning="v2")
    assert id1 == id2
    row = repo.get_economic_indicator(conn, id1)
    assert row["economic_meaning"] == "v2"
    assert row["name"] == "CPI Combined"


def test_upsert_economic_indicator_defaults_status_registered_only(backend) -> None:
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "Repo Rate", "monetary")
    row = repo.get_economic_indicator(conn, indicator_id)
    assert row["status"] == "registered_only"


def test_upsert_economic_indicator_rejects_invalid_status(backend) -> None:
    conn, repo = backend
    with pytest.raises(ValueError):
        repo.upsert_economic_indicator(conn, "Bogus Indicator", "growth", status="not_a_real_status")


def test_get_economic_indicator_by_name(backend) -> None:
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "IIP General", "industry")
    row = repo.get_economic_indicator_by_name(conn, "IIP General")
    assert row["indicator_id"] == indicator_id


def test_list_economic_indicators_filters_by_category_and_status(backend) -> None:
    conn, repo = backend
    repo.upsert_economic_indicator(conn, "CPI Combined", "inflation", status="registered_only")
    repo.upsert_economic_indicator(conn, "WPI", "inflation", status="live")
    repo.upsert_economic_indicator(conn, "Repo Rate", "monetary", status="registered_only")

    inflation_rows = repo.list_economic_indicators(conn, category="inflation")
    assert {r["name"] for r in inflation_rows} == {"CPI Combined", "WPI"}

    live_rows = repo.list_economic_indicators(conn, status="live")
    assert {r["name"] for r in live_rows} == {"WPI"}

    registered_only_rows = repo.list_economic_indicators(conn, category="inflation", status="registered_only")
    assert {r["name"] for r in registered_only_rows} == {"CPI Combined"}


# ------------------------------------------------------------------
# Indicator / Series relationship
# ------------------------------------------------------------------


def test_one_indicator_maps_to_many_series_no_field_duplication(backend) -> None:
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(
        conn, "Consumer Price Inflation", "inflation", economic_meaning="Headline retail inflation"
    )

    series_a = repo.insert_economic_series(
        conn, indicator_id, "cpi_combined_yoy_in", geography="IN", unit="percent_yoy", frequency="monthly"
    )
    series_b = repo.insert_economic_series(
        conn, indicator_id, "cpi_combined_yoy_mh", geography="IN-MH", unit="percent_yoy", frequency="monthly"
    )

    rows = repo.list_series_for_indicator(conn, indicator_id)
    assert {r["series_id"] for r in rows} == {series_a, series_b}
    assert {r["geography"] for r in rows} == {"IN", "IN-MH"}

    # EconomicSeries carries no name/economic_meaning/category columns of
    # its own -- those live exactly once, on the indicator.
    for row in rows:
        assert "name" not in row.keys()
        assert "economic_meaning" not in row.keys()
        assert "category" not in row.keys()
        assert row["indicator_id"] == indicator_id

    indicator_row = repo.get_economic_indicator(conn, indicator_id)
    assert "series_key" not in indicator_row.keys()


def test_series_key_lookup(backend) -> None:
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "Bank Credit Growth", "banking")
    series_id = repo.insert_economic_series(conn, indicator_id, "bank_credit_growth_yoy_in")
    row = repo.get_economic_series_by_key(conn, "bank_credit_growth_yoy_in")
    assert row["series_id"] == series_id
    assert row["indicator_id"] == indicator_id


# ------------------------------------------------------------------
# Source organizations / datasets / endpoints
# ------------------------------------------------------------------


def test_upsert_source_organization_is_idempotent_by_name(backend) -> None:
    conn, repo = backend
    id1 = repo.upsert_source_organization(conn, "Reserve Bank of India (RBI)", authority_level="official_primary")
    id2 = repo.upsert_source_organization(conn, "Reserve Bank of India (RBI)", authority_level="official_primary")
    assert id1 == id2


def test_dataset_and_endpoint_link_to_indicator_via_series(backend) -> None:
    conn, repo = backend
    org_id = repo.upsert_source_organization(conn, "Reserve Bank of India (RBI)")
    dataset_id = repo.insert_source_dataset(
        conn, org_id, access_method="html_table", cadence="daily", backfill_supported=True
    )
    endpoint_id = repo.insert_source_endpoint(
        conn, dataset_id, url="https://dbie.rbi.org.in", access_method="html_table"
    )
    indicator_id = repo.upsert_economic_indicator(conn, "Repo Rate", "monetary")
    repo.insert_economic_series(conn, indicator_id, "repo_rate_in", dataset_id=dataset_id)

    endpoints = repo.list_source_endpoints_for_dataset(conn, dataset_id)
    assert len(endpoints) == 1
    assert endpoints[0]["endpoint_id"] == endpoint_id

    series = repo.list_series_for_indicator(conn, indicator_id)
    assert series[0]["dataset_id"] == dataset_id
    dataset_row = repo.get_source_dataset(conn, dataset_id)
    assert dataset_row["source_org_id"] == org_id


def test_stub_indicator_has_no_linked_series_or_dataset(backend) -> None:
    """A 'registered_only' indicator with no real source is registered
    with zero economic_series rows -- never a fabricated dataset/endpoint
    just to fill the shape."""
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "MGNREGA Person-Days Generated", "labour")
    row = repo.get_economic_indicator(conn, indicator_id)
    assert row["status"] == "registered_only"
    assert repo.list_series_for_indicator(conn, indicator_id) == []


# ------------------------------------------------------------------
# Canonical observation model -- the four query functions, against a
# synthetic 3-vintage series: period "2026-07" reported provisional on
# 2026-08-01, revised on 2026-09-01, made final on 2026-12-01. A second,
# earlier period "2026-06" (already final) anchors the "most recent
# period" checks.
# ------------------------------------------------------------------


@pytest.fixture
def three_vintage_series(backend):
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "CPI Combined", "inflation")
    series_id = repo.insert_economic_series(conn, indicator_id, "cpi_combined_yoy_in", unit="percent_yoy")

    observations = [
        # period "2026-06", single final vintage
        _obs(series_id, "2026-06", "2026-07-01", "2026-07-01", "final", 5.00),
        # period "2026-07", three vintages
        _obs(series_id, "2026-07", "2026-08-01", "2026-08-01", "provisional", 5.20),
        _obs(series_id, "2026-07", "2026-09-01", "2026-09-01", "revised", 5.35),
        _obs(series_id, "2026-07", "2026-12-01", "2026-12-01", "final", 5.30),
    ]
    repo.insert_economic_observations(conn, observations)
    return conn, repo, series_id


class _SyntheticObs:
    def __init__(self, series_id, period, release_date, vintage, revision_status, value):
        self.series_id = series_id
        self.period = period
        self.period_type = "monthly"
        self.release_date = release_date
        self.vintage = vintage
        self.revision_status = revision_status
        self.value = value
        self.unit = "percent_yoy"
        self.raw_object_id = None
        self.ingested_at = ""


def _obs(series_id, period, release_date, vintage, revision_status, value):
    return _SyntheticObs(series_id, period, release_date, vintage, revision_status, value)


def test_latest_returns_highest_vintage_of_most_recent_period(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    row = repo.economic_observation_latest(conn, series_id)
    assert row["period"] == "2026-07"
    assert row["vintage"] == "2026-12-01"
    assert row["revision_status"] == "final"
    assert row["value"] == 5.30


def test_as_of_before_any_2026_07_vintage_returns_the_prior_period(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    row = repo.economic_observation_as_of(conn, series_id, "2026-07-15")
    assert row["period"] == "2026-06"
    assert row["revision_status"] == "final"
    assert row["value"] == 5.00


def test_as_of_between_provisional_and_revised_returns_provisional(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    row = repo.economic_observation_as_of(conn, series_id, "2026-08-15")
    assert row["period"] == "2026-07"
    assert row["revision_status"] == "provisional"
    assert row["value"] == 5.20


def test_as_of_between_revised_and_final_returns_revised(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    row = repo.economic_observation_as_of(conn, series_id, "2026-10-01")
    assert row["period"] == "2026-07"
    assert row["revision_status"] == "revised"
    assert row["value"] == 5.35


def test_as_of_after_final_returns_final(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    row = repo.economic_observation_as_of(conn, series_id, "2027-01-01")
    assert row["period"] == "2026-07"
    assert row["revision_status"] == "final"
    assert row["value"] == 5.30


def test_history_returns_one_row_per_period_at_latest_vintage(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    rows = repo.economic_observation_history(conn, series_id, "2026-01", "2026-12")
    assert [r["period"] for r in rows] == ["2026-06", "2026-07"]
    by_period = {r["period"]: r for r in rows}
    assert by_period["2026-06"]["value"] == 5.00
    # The July row is the FINAL vintage, not provisional/revised -- history()
    # never surfaces a stale vintage.
    assert by_period["2026-07"]["value"] == 5.30
    assert by_period["2026-07"]["revision_status"] == "final"


def test_history_respects_period_range_bounds(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    rows = repo.economic_observation_history(conn, series_id, "2026-07", "2026-07")
    assert [r["period"] for r in rows] == ["2026-07"]


def test_vintages_returns_every_revision_in_release_order(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    rows = repo.economic_observation_vintages(conn, series_id, "2026-07")
    assert [r["revision_status"] for r in rows] == ["provisional", "revised", "final"]
    assert [r["value"] for r in rows] == [5.20, 5.35, 5.30]
    assert [r["release_date"] for r in rows] == ["2026-08-01", "2026-09-01", "2026-12-01"]


def test_vintages_for_single_vintage_period_returns_one_row(three_vintage_series) -> None:
    conn, repo, series_id = three_vintage_series
    rows = repo.economic_observation_vintages(conn, series_id, "2026-06")
    assert len(rows) == 1
    assert rows[0]["revision_status"] == "final"


def test_duplicate_vintage_for_same_period_is_rejected(backend) -> None:
    """UNIQUE(series_id, period, vintage) is a real constraint, not just
    documentation -- a second insert of the exact same tuple must fail
    rather than silently duplicate."""
    conn, repo = backend
    indicator_id = repo.upsert_economic_indicator(conn, "IIP General", "industry")
    series_id = repo.insert_economic_series(conn, indicator_id, "iip_general_yoy_in")
    obs = _obs(series_id, "2026-07", "2026-08-01", "2026-08-01", "provisional", 3.5)
    repo.insert_economic_observations(conn, [obs])

    with pytest.raises(Exception):
        repo.insert_economic_observations(conn, [obs])
