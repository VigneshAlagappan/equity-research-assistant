"""Catalogs the FRED historical S3 snapshots (scripts/fred_historical_
s3_pull.py) into Postgres/Neon's raw_objects table -- pure metadata, no
observation values -- so Research (or anything else querying raw_objects)
can discover these 34 series exist in S3 without duplicating the time
series itself into Postgres.

Why this exists as a separate step, not folded into fred_historical_
s3_pull.py itself: that script's own task was explicitly S3-only, no
Postgres writes at all (a one-time archival pull). This script is the
deliberate, later decision to also make that S3 content discoverable from
Postgres -- one raw_objects row per (series, object) pair already written
to S3, pointing at the existing key. It does NOT call storage.raw_object_
store.store_raw_object() (that function always writes bytes to its own
deterministic hash-based key under raw/{prefix}/{source}/... -- it would
create a SECOND, redundant copy of content already sitting at
raw/fred/snapshots/<run_id>/... ). Instead it inserts directly via
storage.raw_object_repository_pg.insert_raw_object(), reusing the existing
S3 key verbatim, after find_duplicate() confirms this exact (source,
entity, object_type, period, content_hash) hasn't been cataloged yet --
safe to re-run after a future fred_historical_s3_pull.py run without
creating duplicate catalog rows for unchanged content.

state='stored': these objects are landed, immutable, and (per the original
task's own rule) never parsed/transformed into a derived table -- there is
no 'ingested' step coming for this raw archival copy, unlike the ADR-022
lifecycle a company filing goes through.

Usage:
  python -m scripts.catalog_fred_s3_snapshots [--run-id <run_id> ...]
      (default: catalogs every snapshot run_id currently referenced by
       scripts/build_economic_graph.py's INDICATOR_CONCEPTS resolution --
       i.e. the same two runs the economic graph build already reads)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from storage import raw_object_repository_pg as ror
from storage.database import init_postgres_db
from storage.document_store import S3DocumentStore
from storage.raw_object_store import content_hash

_DEFAULT_RUN_IDS = ["20260926T163031Z", "20260926T163310Z", "20260926T222730Z"]
_METADATA_SOURCE_URL_TEMPLATE = "https://api.stlouisfed.org/fred/series?series_id={series_id}&file_type=json"
_OBSERVATIONS_SOURCE_URL_TEMPLATE = (
    "https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
    "&file_type=json&observation_start={observation_start}"
)


def _catalog_one_object(conn, store: S3DocumentStore, *, series_id: str, object_type: str, s3_key: str,
                         period: str | None, source_url: str) -> tuple[bool, int]:
    """Returns (is_new, object_id)."""
    content = store.retrieve(s3_key)
    digest = content_hash(content)
    existing = ror.find_duplicate(conn, source="fred", entity=series_id, object_type=object_type, period=period, content_hash=digest)
    if existing is not None:
        return False, existing["object_id"]
    object_id = ror.insert_raw_object(
        conn, source="fred", entity=series_id, object_type=object_type, period=period,
        source_url=source_url, raw_prefix="macro", s3_key=s3_key, content_hash=digest, state="stored",
    )
    return True, object_id


def catalog_run(conn, store: S3DocumentStore, run_id: str) -> dict:
    manifest_key = f"raw/fred/snapshots/{run_id}/manifest.json"
    if not store.exists(manifest_key):
        return {"run_id": run_id, "found": False}

    manifest = json.loads(store.retrieve(manifest_key))
    observation_start = manifest.get("observation_start", "2015-01-01")
    new_count = reused_count = 0
    skipped: list[str] = []
    for entry in manifest["series"]:
        if entry["status"] != "ok":
            skipped.append(entry["series_id"])
            continue
        series_id = entry["series_id"]
        period = f"{entry['first_observation_date']}..{entry['last_observation_date']}"

        is_new, _ = _catalog_one_object(
            conn, store, series_id=series_id, object_type="fred_historical_metadata",
            s3_key=entry["s3_locations"]["metadata"], period=None,
            source_url=_METADATA_SOURCE_URL_TEMPLATE.format(series_id=series_id),
        )
        new_count += is_new
        reused_count += not is_new

        is_new, _ = _catalog_one_object(
            conn, store, series_id=series_id, object_type="fred_historical_observations",
            s3_key=entry["s3_locations"]["observations"], period=period,
            source_url=_OBSERVATIONS_SOURCE_URL_TEMPLATE.format(series_id=series_id, observation_start=observation_start),
        )
        new_count += is_new
        reused_count += not is_new

    return {"run_id": run_id, "found": True, "new_rows": new_count, "already_cataloged": reused_count, "skipped_series": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", action="append", dest="run_ids", help="a snapshot run_id to catalog (repeatable)")
    args = parser.parse_args()
    run_ids = args.run_ids or _DEFAULT_RUN_IDS

    store = S3DocumentStore()
    conn = init_postgres_db()
    try:
        for run_id in run_ids:
            result = catalog_run(conn, store, run_id)
            if not result["found"]:
                print(f"{run_id}: manifest not found, skipped", flush=True)
                continue
            print(
                f"{run_id}: {result['new_rows']} new raw_objects row(s), "
                f"{result['already_cataloged']} already cataloged"
                + (f", skipped (failed series): {result['skipped_series']}" if result["skipped_series"] else ""),
                flush=True,
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
