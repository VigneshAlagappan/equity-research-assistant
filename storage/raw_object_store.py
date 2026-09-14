"""Ties `storage/document_store.py`'s backend-agnostic byte storage
together with `storage/raw_object_repository.py`'s catalog into the one
function every source's ingestion code calls to land a fetched artifact
under `raw/` (docs/ADR/022-s3-raw-processed-object-store-with-lineage-
catalog.md). Neither of those two modules is allowed to know about the
other -- DocumentStore is a plain bytes-in-bytes-out abstraction (no
catalog concept), raw_object_repository is a plain SQL/DB abstraction (no
S3/local-disk concept) -- this module is the seam, same "compose two
narrow abstractions instead of widening either" shape storage/fact_store.py
and storage/price_store.py already establish for their own concerns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from storage import raw_object_repository as ror
from storage.document_store import DocumentStore, default_document_store


@dataclass(frozen=True)
class RawObjectResult:
    """What store_raw_object() actually did -- callers branch on `is_new`
    to decide whether to continue into parse/ingest (a fresh object) or
    just note the reuse (a byte-identical duplicate, already fully
    processed by whatever fetched it the first time)."""

    object_id: int
    is_new: bool
    s3_key: str
    content_hash: str


def content_hash(content: bytes) -> str:
    """sha256 of raw bytes -- the one hash function every source's
    ingestion code uses before calling store_raw_object(), so a caller
    that wants to check find_duplicate() itself first (e.g. to skip a
    network re-fetch entirely once it already knows the hash from a
    lighter preliminary request) can do so with the identical algorithm."""
    return hashlib.sha256(content).hexdigest()


def build_raw_key(
    *, raw_prefix: str, source: str, entity: str | None, object_type: str,
    period: str | None, content_hash_hex: str, extension: str,
) -> str:
    """The deterministic S3 (or local-disk, DOCUMENT_STORE_BACKEND=local)
    key for one raw object. Always includes the content hash, so two
    different contents for the same (source, entity, object_type, period)
    physically occupy different keys -- immutability enforced by the key
    shape itself, not just by callers never calling delete()/overwriting.
    `entity`/`period` fall back to a literal placeholder rather than being
    omitted, so the key structure (number of path segments) stays uniform
    whether or not this source has a per-company or per-period concept."""
    entity_segment = entity or "_global"
    period_segment = period or "_na"
    ext = extension.lstrip(".")
    return (
        f"raw/{raw_prefix}/{source}/{entity_segment}/{object_type}/"
        f"{period_segment}/{content_hash_hex[:16]}.{ext}"
    )


def store_raw_object(
    conn, *, source: str, entity: str | None, object_type: str, period: str | None,
    source_url: str | None, raw_prefix: str, content: bytes, extension: str,
    parser_version: str | None = None, document_store: DocumentStore | None = None,
) -> RawObjectResult:
    """The one call every source's ingestion code makes to land a fetched
    artifact under `raw/`. Dedup-checks by hash BEFORE writing any bytes:
    a byte-identical re-fetch never touches the object store or inserts a
    new catalog row, it just returns the existing object_id with
    is_new=False (reuse-by-reference, per ADR-022). A caller that gets
    is_new=False back should treat this fetch as a no-op for downstream
    parse/ingest too, since a genuinely new answer was never produced.

    `conn` is the caller's own connection to `storage.raw_object_repository`
    (resolves to Postgres or SQLite per DATABASE_BACKEND, same as every
    other repository module) -- this function does not open its own
    connection, unlike the *_store()/open_*_db() helpers elsewhere, since
    catalog writes here are meant to share the caller's transaction with
    whatever else it's doing (e.g. a batch job's own BatchRun bookkeeping)."""
    store = document_store or default_document_store()
    digest = content_hash(content)

    existing = ror.find_duplicate(
        conn, source=source, entity=entity, object_type=object_type, period=period, content_hash=digest,
    )
    if existing is not None:
        return RawObjectResult(
            object_id=existing["object_id"], is_new=False,
            s3_key=existing["s3_key"], content_hash=digest,
        )

    key = build_raw_key(
        raw_prefix=raw_prefix, source=source, entity=entity, object_type=object_type,
        period=period, content_hash_hex=digest, extension=extension,
    )
    store.store(key, content)
    object_id = ror.insert_raw_object(
        conn, source=source, entity=entity, object_type=object_type, period=period,
        source_url=source_url, raw_prefix=raw_prefix, s3_key=key, content_hash=digest,
        parser_version=parser_version, state="stored",
    )
    return RawObjectResult(object_id=object_id, is_new=True, s3_key=key, content_hash=digest)
