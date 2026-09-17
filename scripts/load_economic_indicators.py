"""Idempotent loader: infrastructure/economic_graph/indicators/*.yaml ->
economic_indicator_registry (+ source_organizations/source_datasets/
source_endpoints/economic_series for the indicators that have a `source`
block).

This is the "future loader script" the Phase-1 economic graph plan calls
for: the YAML files under infrastructure/economic_graph/indicators/ are
the human-reviewable, repo-file source of truth a later Neo4j-sync phase
will also read from; this script is how they land in Postgres/SQLite
today. Safe to re-run any number of times -- every write goes through
upsert_economic_indicator()/upsert_source_organization() (idempotent by
name) or a plain insert guarded by an existing-row check (source_datasets/
source_endpoints/economic_series have no natural unique key of their own
across their few fields other than economic_series.series_key, so those
are looked up by series_key / by (dataset already linked to this
indicator) before inserting).

Usage:
    .venv/bin/python scripts/load_economic_indicators.py [--dry-run]

Never fetches anything over the network and never runs against production
Neon unless DATABASE_BACKEND=postgres and the environment's connection
string already points there -- same discipline every other script in this
repo follows; this script itself has no opinion about which database it's
pointed at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

import storage.backend_bootstrap as backend_bootstrap

BASE_DIR = Path(__file__).resolve().parent.parent
INDICATORS_DIR = BASE_DIR / "infrastructure" / "economic_graph" / "indicators"


def load_category_files() -> list[dict]:
    """Every *.yaml under infrastructure/economic_graph/indicators/, each
    holding one category's {"category": ..., "indicators": [...]}."""
    entries = []
    for path in sorted(INDICATORS_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text())
        entries.append(payload)
    return entries


def load_one_indicator(conn, repo, entry: dict, category: str) -> int:
    """Upsert one indicator row, and if it has a `source` block, its
    organization/dataset/endpoints/series too. Returns the indicator_id."""
    indicator_id = repo.upsert_economic_indicator(
        conn,
        entry["name"],
        category,
        economic_meaning=entry.get("economic_meaning"),
        higher_is=entry.get("higher_is"),
        leading_lagging=entry.get("leading_lagging"),
        report_section=entry.get("report_section"),
        headline_weight=entry.get("headline_weight"),
        preferred_chart_window=entry.get("preferred_chart_window"),
        material_change_mom=entry.get("material_change_mom"),
        material_change_yoy=entry.get("material_change_yoy"),
        material_change_ytd=entry.get("material_change_ytd"),
        status=entry.get("status", "registered_only"),
    )

    source = entry.get("source")
    if not source:
        return indicator_id

    org = source["organization"]
    source_org_id = repo.upsert_source_organization(
        conn, org["name"], authority_level=org.get("authority_level"), description=org.get("description")
    )

    # source_datasets has no natural unique key of its own; treat "this
    # indicator's series already point at a dataset under this org" as the
    # existing-row signal so a re-run doesn't create a duplicate dataset
    # every time.
    existing_series = repo.list_series_for_indicator(conn, indicator_id)
    dataset_id = None
    for s in existing_series:
        if s["dataset_id"] is not None:
            ds = repo.get_source_dataset(conn, s["dataset_id"])
            if ds is not None and ds["source_org_id"] == source_org_id:
                dataset_id = s["dataset_id"]
                break

    if dataset_id is None:
        ds = source["dataset"]
        dataset_id = repo.insert_source_dataset(
            conn, source_org_id,
            authority_level=ds.get("authority_level"), priority=ds.get("priority"),
            access_method=ds.get("access_method"), cadence=ds.get("cadence"),
            historical_start=ds.get("historical_start"), backfill_supported=ds.get("backfill_supported"),
            license_notes=ds.get("license_notes"),
        )
        for ep in source.get("endpoints", []):
            repo.insert_source_endpoint(
                conn, dataset_id, url=ep.get("url"), access_method=ep.get("access_method"),
                priority=ep.get("priority"), enabled=ep.get("enabled", True),
                authentication_type=ep.get("authentication_type"), parser_config=ep.get("parser_config"),
                availability_status=ep.get("availability_status"), last_verified_at=ep.get("last_verified_at"),
            )

    existing_keys = {s["series_key"] for s in existing_series}
    for s in source.get("series", []):
        if s["series_key"] in existing_keys:
            continue
        repo.insert_economic_series(
            conn, indicator_id, s["series_key"], dataset_id=dataset_id,
            geography=s.get("geography"), unit=s.get("unit"), frequency=s.get("frequency"),
            seasonal_adjustment=s.get("seasonal_adjustment"), notes=s.get("notes"),
        )

    return indicator_id


def run(conn, *, dry_run: bool = False) -> dict:
    backend_bootstrap.install()
    from storage import repositories as repo  # noqa: PLC0415 -- swapped by backend_bootstrap.install()

    categories = load_category_files()
    summary = {"categories": len(categories), "indicators": 0, "with_source": 0}
    for payload in categories:
        for entry in payload["indicators"]:
            summary["indicators"] += 1
            if entry.get("source"):
                summary["with_source"] += 1
            if not dry_run:
                load_one_indicator(conn, repo, entry, payload["category"])
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarize only, write nothing.")
    args = parser.parse_args()

    from config.settings import DATABASE_BACKEND

    # DATABASE_BACKEND must be read from config/settings.py (not
    # re-derived) and storage.backend_bootstrap.install() must run before
    # `run()`'s own `from storage import repositories` -- same ordering
    # scripts/seed_local_dev_db.py documents, to avoid a module-level
    # import binding to the pre-swap SQLite version.
    if DATABASE_BACKEND == "postgres":
        from storage.database import init_postgres_db

        conn = init_postgres_db()
    else:
        from storage.database import init_db

        conn = init_db()
    try:
        summary = run(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    print(f"Categories: {summary['categories']}")
    print(f"Indicators: {summary['indicators']} ({summary['with_source']} with a linked real source)")


if __name__ == "__main__":
    main()
