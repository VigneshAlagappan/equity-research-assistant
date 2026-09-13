"""storage/raw_object_repository.py -- the raw-object catalog/lineage
control plane docs/ADR/022-s3-raw-processed-object-store-with-lineage-
catalog.md calls for. Covers exactly the acceptance-criteria items that
ADR names as needing automated coverage: dedup, state transitions/retry,
filtered replay listing, and lineage."""

from __future__ import annotations

import pytest

from storage import raw_object_repository as ror


def _insert_sample(conn, **overrides):
    kwargs = dict(
        source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", source_url="https://example.test/x",
        raw_prefix="market-data", s3_key="raw/market-data/HDFCBANK/x.json", content_hash="hash-a",
    )
    kwargs.update(overrides)
    return ror.insert_raw_object(conn, **kwargs)


def test_find_duplicate_matches_identical_tuple(db_conn) -> None:
    oid = _insert_sample(db_conn)
    dup = ror.find_duplicate(
        db_conn, source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", content_hash="hash-a",
    )
    assert dup is not None
    assert dup["object_id"] == oid


def test_find_duplicate_misses_on_different_hash(db_conn) -> None:
    _insert_sample(db_conn)
    dup = ror.find_duplicate(
        db_conn, source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", content_hash="hash-b",
    )
    assert dup is None


def test_find_duplicate_is_null_safe_on_entity_and_period(db_conn) -> None:
    oid = _insert_sample(db_conn, entity=None, period=None, source="fred", object_type="fred_series_csv")
    dup = ror.find_duplicate(
        db_conn, source="fred", entity=None, object_type="fred_series_csv", period=None, content_hash="hash-a",
    )
    assert dup is not None
    assert dup["object_id"] == oid


def test_duplicate_insert_rejected_by_unique_index(db_conn) -> None:
    _insert_sample(db_conn)
    with pytest.raises(Exception):
        _insert_sample(db_conn, s3_key="raw/market-data/HDFCBANK/y.json")


def test_different_hash_creates_new_immutable_row(db_conn) -> None:
    first = _insert_sample(db_conn)
    second = _insert_sample(db_conn, content_hash="hash-b", s3_key="raw/market-data/HDFCBANK/y.json")
    assert first != second
    assert ror.get_raw_object(db_conn, first)["content_hash"] == "hash-a"
    assert ror.get_raw_object(db_conn, second)["content_hash"] == "hash-b"


def test_insert_rejects_unknown_raw_prefix(db_conn) -> None:
    with pytest.raises(ValueError):
        _insert_sample(db_conn, raw_prefix="not-a-real-prefix")


def test_insert_rejects_unknown_state(db_conn) -> None:
    with pytest.raises(ValueError):
        _insert_sample(db_conn, state="not-a-real-state")


def test_state_transitions_through_to_ingested(db_conn) -> None:
    oid = _insert_sample(db_conn)
    assert ror.get_raw_object(db_conn, oid)["state"] == "fetched"
    for state in ("stored", "validated", "parsed", "ingested"):
        ror.update_raw_object_state(db_conn, oid, state=state)
        assert ror.get_raw_object(db_conn, oid)["state"] == state


def test_failed_state_records_error_and_increments_retry(db_conn) -> None:
    oid = _insert_sample(db_conn)
    ror.update_raw_object_state(db_conn, oid, state="failed", last_error="boom", increment_retry=True)
    row = ror.get_raw_object(db_conn, oid)
    assert row["state"] == "failed"
    assert row["last_error"] == "boom"
    assert row["retry_count"] == 1

    # A retry that fails again increments further and never silently drops the object.
    ror.update_raw_object_state(db_conn, oid, state="failed", last_error="boom again", increment_retry=True)
    row = ror.get_raw_object(db_conn, oid)
    assert row["retry_count"] == 2
    assert row["last_error"] == "boom again"


def test_mark_processed_sets_processed_at(db_conn) -> None:
    oid = _insert_sample(db_conn)
    assert ror.get_raw_object(db_conn, oid)["processed_at"] is None
    ror.update_raw_object_state(db_conn, oid, state="ingested", mark_processed=True)
    assert ror.get_raw_object(db_conn, oid)["processed_at"] is not None


def test_list_raw_objects_filters_by_entity_source_and_state(db_conn) -> None:
    _insert_sample(db_conn, entity="HDFCBANK")
    _insert_sample(db_conn, entity="RELIANCE", content_hash="hash-b", s3_key="raw/market-data/RELIANCE/x.json")
    ror.update_raw_object_state(
        db_conn, _insert_sample(db_conn, entity="HDFCBANK", content_hash="hash-c",
                                 s3_key="raw/market-data/HDFCBANK/z.json"),
        state="failed",
    )

    by_entity = ror.list_raw_objects(db_conn, entity="HDFCBANK")
    assert {r["entity"] for r in by_entity} == {"HDFCBANK"}
    assert len(by_entity) == 2

    by_state = ror.list_raw_objects(db_conn, state="failed")
    assert len(by_state) == 1
    assert by_state[0]["entity"] == "HDFCBANK"


def test_list_raw_objects_filters_by_period_range(db_conn) -> None:
    _insert_sample(db_conn, period="FY2023", content_hash="h1", s3_key="k1")
    _insert_sample(db_conn, period="FY2024", content_hash="h2", s3_key="k2")
    _insert_sample(db_conn, period="FY2025", content_hash="h3", s3_key="k3")

    rows = ror.list_raw_objects(db_conn, period_start="FY2024", period_end="FY2025")
    assert {r["period"] for r in rows} == {"FY2024", "FY2025"}


def test_lineage_links_raw_object_to_derived_record_both_directions(db_conn) -> None:
    oid = _insert_sample(db_conn)
    ror.insert_lineage(
        db_conn, object_id=oid, derived_store="postgres", derived_table="daily_prices",
        derived_record_id="HDFCBANK:2026-09-11",
    )

    forward = ror.get_lineage_for_object(db_conn, oid)
    assert len(forward) == 1
    assert forward[0]["derived_table"] == "daily_prices"

    backward = ror.get_lineage_for_record(
        db_conn, derived_store="postgres", derived_table="daily_prices",
        derived_record_id="HDFCBANK:2026-09-11",
    )
    assert len(backward) == 1
    assert backward[0]["object_id"] == oid


def test_lineage_supports_multiple_raw_objects_per_derived_record(db_conn) -> None:
    """A reconciled value chosen among several source candidates traces
    back to more than one raw object -- ADR-022's own example."""
    first = _insert_sample(db_conn, content_hash="h1", s3_key="k1")
    second = _insert_sample(db_conn, content_hash="h2", s3_key="k2")
    ror.insert_lineage(
        db_conn, object_id=first, derived_store="postgres", derived_table="canonical_financials",
        derived_record_id="42",
    )
    ror.insert_lineage(
        db_conn, object_id=second, derived_store="postgres", derived_table="canonical_financials",
        derived_record_id="42",
    )
    back = ror.get_lineage_for_record(
        db_conn, derived_store="postgres", derived_table="canonical_financials", derived_record_id="42",
    )
    assert {r["object_id"] for r in back} == {first, second}
