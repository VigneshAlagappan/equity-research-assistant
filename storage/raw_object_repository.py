"""Repository layer for the `raw_objects`/`raw_object_lineage` catalog
(docs/ADR/022-s3-raw-processed-object-store-with-lineage-catalog.md) --
the Postgres/SQLite control-plane record of every externally fetched
artifact stored immutably under the S3 (or local-disk) `raw/` prefix.

This module is the ONLY place that decides "is this a duplicate fetch" --
every source's ingestion code calls find_duplicate() before writing
anything to the raw object store, and insert_raw_object() after a
genuinely new object has been written. Callers never build the dedup
query themselves, the same "one shared mechanism, not per-source ad hoc
checks" principle ADR-022 calls for.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from storage.database import utcnow_iso

RAW_PREFIXES = ("companies", "market-data", "macro", "regulatory")
STATES = ("fetched", "stored", "validated", "parsed", "ingested", "reconciled", "failed", "quarantined")


def find_duplicate(
    conn: sqlite3.Connection, *, source: str, entity: str | None, object_type: str,
    period: str | None, content_hash: str,
) -> sqlite3.Row | None:
    """The one dedup check every source's ingestion code must call before
    writing a new raw object. NULL-safe on `entity`/`period` via `IS ?`
    (SQLite's own NULL-safe equality, works directly with a bound NULL or
    a real value -- unlike Postgres's `IS`, which only accepts a literal;
    see storage/raw_object_repository_pg.py's `IS NOT DISTINCT FROM`
    translation for that side). Returns the existing row (reuse-by-
    reference) if an object already exists for this exact (source, entity,
    object_type, period, content_hash) tuple, None if this is genuinely
    new content."""
    return conn.execute(
        """
        SELECT * FROM raw_objects
        WHERE source = ? AND entity IS ? AND object_type = ? AND period IS ? AND content_hash = ?
        """,
        (source, entity, object_type, period, content_hash),
    ).fetchone()


def insert_raw_object(
    conn: sqlite3.Connection, *, source: str, entity: str | None, object_type: str, period: str | None,
    source_url: str | None, raw_prefix: str, s3_key: str, content_hash: str, parser_version: str | None = None,
    state: str = "fetched", fetched_at: str | None = None,
) -> int:
    """Catalog a genuinely new raw object -- call find_duplicate() first;
    this does not check for you (a caller that already knows it's new,
    e.g. a bulk backfill that pre-filters, shouldn't pay for a second
    query). raw_prefix must be one of RAW_PREFIXES, state one of STATES --
    the schema's own CHECK constraints enforce this too, but failing here
    with a clear message beats a driver-specific constraint-violation
    exception bubbling up from a random INSERT deep in a batch loop."""
    if raw_prefix not in RAW_PREFIXES:
        raise ValueError(f"insert_raw_object: raw_prefix must be one of {RAW_PREFIXES}, got {raw_prefix!r}")
    if state not in STATES:
        raise ValueError(f"insert_raw_object: state must be one of {STATES}, got {state!r}")
    cursor = conn.execute(
        """
        INSERT INTO raw_objects (
            source, entity, object_type, period, source_url, raw_prefix, s3_key,
            content_hash, fetched_at, parser_version, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source, entity, object_type, period, source_url, raw_prefix, s3_key,
            content_hash, fetched_at or utcnow_iso(), parser_version, state,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_raw_object_state(
    conn: sqlite3.Connection, object_id: int, *, state: str, last_error: str | None = None,
    increment_retry: bool = False, mark_processed: bool = False,
) -> None:
    """Advance (or fail/quarantine) one object's state. `last_error` is
    always written verbatim (including None, to clear a stale error once a
    retry succeeds) -- same "full replace, not merge" convention storage/
    indicator_repository.py's upsert_indicator_config() docstring
    explains for its own nullable fields. `increment_retry` is a separate
    flag rather than inferred from state='failed', since a caller
    replaying an already-`failed` object back to `stored` (a deliberate
    retry) also wants the counter to go up."""
    if state not in STATES:
        raise ValueError(f"update_raw_object_state: state must be one of {STATES}, got {state!r}")
    conn.execute(
        f"""
        UPDATE raw_objects
        SET state = ?, last_error = ?
            {', retry_count = retry_count + 1' if increment_retry else ''}
            {', processed_at = ?' if mark_processed else ''}
        WHERE object_id = ?
        """,
        (state, last_error, *((utcnow_iso(),) if mark_processed else ()), object_id),
    )
    conn.commit()


def get_raw_object(conn: sqlite3.Connection, object_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM raw_objects WHERE object_id = ?", (object_id,)).fetchone()


def list_raw_objects(
    conn: sqlite3.Connection, *, source: str | None = None, entity: str | None = None,
    object_type: str | None = None, state: str | None = None,
    period_start: str | None = None, period_end: str | None = None, limit: int = 500,
) -> list[sqlite3.Row]:
    """Filtered catalog listing -- the query behind replay-from-S3's
    "filter by entity/company, source/type, and period/date range"
    requirement (ADR-022). `period_start`/`period_end` do a plain string
    BETWEEN on the `period` column, which is fine for the ISO-date-range
    and "FY2025"/"Q1FY2025"-style period strings every current source
    uses (they sort correctly as text) -- a source with a genuinely
    different period shape would need its own comparison, not addressed
    here since none exists yet."""
    clauses: list[str] = []
    params: list = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if entity is not None:
        clauses.append("entity = ?")
        params.append(entity)
    if object_type is not None:
        clauses.append("object_type = ?")
        params.append(object_type)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if period_start is not None:
        clauses.append("period >= ?")
        params.append(period_start)
    if period_end is not None:
        clauses.append("period <= ?")
        params.append(period_end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM raw_objects {where} ORDER BY fetched_at DESC LIMIT ?", params,
    ).fetchall()


def list_all_raw_objects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every raw_objects row, unfiltered -- for the S3<->Postgres catalog
    reconciliation job (scripts/reconcile_raw_objects.py), which needs the
    FULL catalog to cross-check against a full S3/local-store listing.
    Never used by an interactive/replay path -- list_raw_objects()'s
    filtered+limited query is for that. A full scan is cheap at this
    app's scale (hundreds of companies); revisit if raw_objects grows
    large enough to need pagination here too."""
    return conn.execute("SELECT * FROM raw_objects").fetchall()


def insert_lineage(
    conn: sqlite3.Connection, *, object_id: int, derived_store: str, derived_table: str, derived_record_id: str,
) -> int:
    """Record that a derived Postgres/Qdrant/Neo4j record was computed
    from `object_id`. One row per (derived record, raw object)
    contribution -- a derived record with more than one source raw object
    (e.g. a reconciled canonical value chosen among several candidates)
    gets more than one lineage row, not a comma-joined list in one row."""
    cursor = conn.execute(
        """
        INSERT INTO raw_object_lineage (object_id, derived_store, derived_table, derived_record_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (object_id, derived_store, derived_table, derived_record_id, utcnow_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def get_lineage_for_object(conn: sqlite3.Connection, object_id: int) -> list[sqlite3.Row]:
    """Every derived record this raw object contributed to."""
    return conn.execute(
        "SELECT * FROM raw_object_lineage WHERE object_id = ? ORDER BY lineage_id", (object_id,),
    ).fetchall()


def get_lineage_for_record(
    conn: sqlite3.Connection, *, derived_store: str, derived_table: str, derived_record_id: str,
) -> list[sqlite3.Row]:
    """The raw object(s) behind one derived record -- "trace this
    canonical_financials row back to what it was computed from"."""
    return conn.execute(
        """
        SELECT ro.* FROM raw_object_lineage l
        JOIN raw_objects ro ON ro.object_id = l.object_id
        WHERE l.derived_store = ? AND l.derived_table = ? AND l.derived_record_id = ?
        ORDER BY l.lineage_id
        """,
        (derived_store, derived_table, derived_record_id),
    ).fetchall()
