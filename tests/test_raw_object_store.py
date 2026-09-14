"""storage/raw_object_store.py -- the seam between DocumentStore (bytes)
and the raw_objects catalog. Covers ADR-022's core acceptance criteria:
immutability (never overwrite), dedup-by-hash (identical content reused,
not re-stored), and changed content creating a new immutable object."""

from __future__ import annotations

from pathlib import Path

import pytest

from storage.document_store import LocalDocumentStore
from storage.raw_object_store import build_raw_key, content_hash, store_raw_object


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch) -> LocalDocumentStore:
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    return LocalDocumentStore()


def _store(conn, local_store, content: bytes, **overrides):
    kwargs = dict(
        source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", source_url="https://example.test",
        raw_prefix="market-data", extension="json", document_store=local_store,
    )
    kwargs.update(overrides)
    return store_raw_object(conn, content=content, **kwargs)


def test_first_store_writes_bytes_and_catalogs_new_object(db_conn, local_store) -> None:
    payload = b'{"close": 100.5}'
    result = _store(db_conn, local_store, payload)

    assert result.is_new
    assert local_store.exists(result.s3_key)
    assert local_store.retrieve(result.s3_key) == payload
    assert result.content_hash == content_hash(payload)


def test_identical_content_dedups_without_rewriting_or_new_object(db_conn, local_store) -> None:
    payload = b'{"close": 100.5}'
    first = _store(db_conn, local_store, payload)
    second = _store(db_conn, local_store, payload)

    assert not second.is_new
    assert second.object_id == first.object_id
    assert second.s3_key == first.s3_key


def test_changed_content_creates_new_immutable_object_original_untouched(db_conn, local_store) -> None:
    original = b'{"close": 100.5}'
    changed = b'{"close": 101.0}'

    first = _store(db_conn, local_store, original)
    second = _store(db_conn, local_store, changed)

    assert second.is_new
    assert second.object_id != first.object_id
    assert second.s3_key != first.s3_key
    # The original object's bytes were never mutated by the second store() call.
    assert local_store.retrieve(first.s3_key) == original
    assert local_store.retrieve(second.s3_key) == changed


def test_build_raw_key_includes_hash_so_different_content_never_collides() -> None:
    key_a = build_raw_key(
        raw_prefix="market-data", source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", content_hash_hex="a" * 64, extension="json",
    )
    key_b = build_raw_key(
        raw_prefix="market-data", source="yfinance_prices", entity="HDFCBANK", object_type="ohlcv_batch",
        period="2026-09-01..2026-09-11", content_hash_hex="b" * 64, extension="json",
    )
    assert key_a != key_b
    assert key_a.startswith("raw/market-data/")


def test_build_raw_key_uses_placeholder_for_missing_entity_and_period() -> None:
    key = build_raw_key(
        raw_prefix="macro", source="fred", entity=None, object_type="fred_series_csv",
        period=None, content_hash_hex="c" * 64, extension="csv",
    )
    assert "/_global/" in key
    assert "/_na/" in key


def test_different_periods_are_stored_as_distinct_objects_even_with_same_content(db_conn, local_store) -> None:
    """Two genuinely different fetches (different period) with
    byte-identical content are NOT deduped against each other -- dedup is
    scoped to (source, entity, object_type, period), not content alone."""
    payload = b'{"close": 100.5}'
    first = _store(db_conn, local_store, payload, period="2026-09-01..2026-09-11")
    second = _store(db_conn, local_store, payload, period="2026-09-12..2026-09-18")

    assert second.is_new
    assert second.object_id != first.object_id
