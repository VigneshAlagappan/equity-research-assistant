"""One-time FRED historical data pull -- lands raw, untouched FRED API
responses (series metadata + observations) in S3 as an immutable
source-of-truth snapshot, independent of this app's existing FRED pipeline.

Deliberately NOT ingestion/pipeline.py::ingest_fred_series(): that function
(a) fetches FRED's unauthenticated fredgraph.csv export (no metadata, no
API key), and (b) writes to Postgres (macro_observations, raw_objects) on
every run. This script instead calls FRED's official JSON API
(api.stlouisfed.org, api_key required -- config.settings.FRED_API_KEY) to
also capture each series' metadata (title, units, frequency, seasonal
adjustment, notes, ...), and writes nothing to Postgres/Neo4j/Qdrant --
S3 alone is the destination, via storage/document_store.py's S3DocumentStore
(same backend + credential chain the rest of the app uses for S3, per
config.settings.S3_BUCKET_NAME/S3_REGION_NAME -- boto3's normal credential
chain, e.g. the local AWS CLI's ~/.aws/credentials).

Response bytes are stored exactly as FRED returned them -- no reshaping,
unit conversion, interpolation, or missing-value substitution. FRED's
observations endpoint already marks a missing observation with
value: "." (see https://fred.stlouisfed.org/docs/api/fred/), and that
token is preserved verbatim in observations.json rather than filtered out
(sources/fred.py's CSV parser skips "." rows for macro_observations --
deliberately different here, since this is a raw archival copy, not a
normalized series).

Layout (per the one-time task spec):
  raw/fred/snapshots/<run_id>/series/<series_id>/metadata.json
  raw/fred/snapshots/<run_id>/series/<series_id>/observations.json
  raw/fred/snapshots/<run_id>/manifest.json

run_id is a UTC timestamp ("20260926T151230Z"), not a DB-issued id --
there's no batch_job_runs row for this script (no DB writes at all), so
the S3 prefix itself is the only record of "when this snapshot was taken".

Usage:
  python -m scripts.fred_historical_s3_pull --series DGS10,UNRATE,CPIAUCSL
  python -m scripts.fred_historical_s3_pull            # full CURATED_SERIES
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # run standalone, same as scripts/migrate_documents_to_s3.py

from config import settings
from storage.document_store import S3DocumentStore

_METADATA_URL = "https://api.stlouisfed.org/fred/series"
_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_OBSERVATION_START = "2015-01-01"
_REQUEST_TIMEOUT_SECONDS = 30

# Curated set -- the "SIGNALS U.S. MACRO LAYER" taxonomy: 8 categories the
# equity research workflow watches regardless of sector. Started from the 5
# series scripts/batch_fetch_fred.py's TRACKED_SERIES already tracks (GDP,
# FEDFUNDS, DGS10, CPIAUCSL, UNRATE) and grown to this full list on request.
# Each entry is independent -- adding/removing one never affects the others
# or this script's DB-free, S3-only archival behavior.
CURATED_SERIES: list[str] = [
    # Economic Growth
    "GDP", "GDPC1", "INDPRO",
    # Monetary Policy
    "FEDFUNDS", "SOFR", "DGS3MO", "DGS2", "DGS10", "T10Y2Y",
    # Inflation
    "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "T10YIE",
    # Employment
    "UNRATE", "PAYEMS", "ICSA", "JTSJOL",
    # Consumer
    "UMCSENT", "RSAFS", "DSPIC96", "PSAVERT",
    # Housing
    "HOUST", "PERMIT", "CSUSHPISA", "MORTGAGE30US",
    # Liquidity / Credit
    "M2SL", "WALCL", "NFCI", "BAMLH0A0HYM2",
    # Markets
    "VIXCLS", "DTWEXBGS",
    # Commodities
    "DCOILWTICO", "DHHNGSP",
]

# The task's required smoke-test subset, run first before the full list.
SMOKE_TEST_SERIES: list[str] = ["DGS10", "UNRATE", "CPIAUCSL"]


@dataclass
class SeriesResult:
    series_id: str
    status: str  # "ok" | "failed"
    observation_count: int = 0
    first_observation_date: str | None = None
    last_observation_date: str | None = None
    retrieved_at: str | None = None
    metadata_key: str | None = None
    observations_key: str | None = None
    error: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _get_json(url: str, params: dict) -> tuple[bytes, dict]:
    """Returns (raw_response_bytes, parsed_json) -- the raw bytes are what
    gets stored in S3 verbatim; the parsed dict is only used here to pull
    out manifest fields (observation count, date range), never to reshape
    what's written."""
    response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content, response.json()


def fetch_series_metadata_raw(series_id: str, api_key: str) -> tuple[bytes, dict]:
    return _get_json(_METADATA_URL, {"series_id": series_id, "api_key": api_key, "file_type": "json"})


def fetch_series_observations_raw(series_id: str, api_key: str, *, observation_start: str = _OBSERVATION_START) -> tuple[bytes, dict]:
    return _get_json(
        _OBSERVATIONS_URL,
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": observation_start,
        },
    )


def _series_prefix(run_id: str, series_id: str) -> str:
    return f"raw/fred/snapshots/{run_id}/series/{series_id}"


def pull_one_series(store: S3DocumentStore, run_id: str, series_id: str, api_key: str, *, observation_start: str = _OBSERVATION_START) -> SeriesResult:
    prefix = _series_prefix(run_id, series_id)
    metadata_key = f"{prefix}/metadata.json"
    observations_key = f"{prefix}/observations.json"

    metadata_bytes, _metadata_json = fetch_series_metadata_raw(series_id, api_key)
    observations_bytes, observations_json = fetch_series_observations_raw(series_id, api_key, observation_start=observation_start)

    observations = observations_json.get("observations", [])
    first_date = observations[0]["date"] if observations else None
    last_date = observations[-1]["date"] if observations else None

    store.store(metadata_key, metadata_bytes)
    store.store(observations_key, observations_bytes)

    return SeriesResult(
        series_id=series_id,
        status="ok",
        observation_count=len(observations),
        first_observation_date=first_date,
        last_observation_date=last_date,
        retrieved_at=_utc_now_iso(),
        metadata_key=metadata_key,
        observations_key=observations_key,
    )


def run_snapshot(
    series_ids: list[str], *, store: S3DocumentStore | None = None, run_id: str | None = None,
    observation_start: str = _OBSERVATION_START,
) -> tuple[str, list[SeriesResult]]:
    api_key = settings.FRED_API_KEY
    if not api_key:
        raise SystemExit("FRED_API_KEY is not set (check .env)")

    store = store or S3DocumentStore()
    run_id = run_id or _run_id()

    print(f"fred_historical_s3_pull: run_id={run_id} bucket={store._bucket} observation_start={observation_start} series={series_ids}", flush=True)

    results: list[SeriesResult] = []
    for series_id in series_ids:
        try:
            result = pull_one_series(store, run_id, series_id, api_key, observation_start=observation_start)
            print(
                f"{series_id}: OK -- {result.observation_count} observations "
                f"({result.first_observation_date}..{result.last_observation_date})",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 -- record and keep going, per task spec
            result = SeriesResult(series_id=series_id, status="failed", retrieved_at=_utc_now_iso(), error=str(exc))
            print(f"{series_id}: FAILED -- {exc}", flush=True)
        results.append(result)

    manifest = {
        "run_id": run_id,
        "generated_at": _utc_now_iso(),
        "observation_start": observation_start,
        "bucket": store._bucket,
        "series": [
            {
                "series_id": r.series_id,
                "status": r.status,
                "observation_count": r.observation_count,
                "first_observation_date": r.first_observation_date,
                "last_observation_date": r.last_observation_date,
                "retrieved_at": r.retrieved_at,
                "s3_locations": (
                    {"metadata": r.metadata_key, "observations": r.observations_key}
                    if r.status == "ok"
                    else None
                ),
                "error": r.error,
            }
            for r in results
        ],
    }
    manifest_key = f"raw/fred/snapshots/{run_id}/manifest.json"
    store.store(manifest_key, json.dumps(manifest, indent=2).encode("utf-8"))

    _print_summary(run_id, manifest_key, results)
    return run_id, results


def _print_summary(run_id: str, manifest_key: str, results: list[SeriesResult]) -> None:
    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "failed"]
    total_observations = sum(r.observation_count for r in ok)

    print("\n--- FRED historical S3 pull summary ---", flush=True)
    print(f"snapshot: raw/fred/snapshots/{run_id}/  (manifest: {manifest_key})", flush=True)
    print(f"series succeeded: {len(ok)}/{len(results)}", flush=True)
    print(f"total observations: {total_observations}", flush=True)
    if failed:
        print(f"series failed ({len(failed)}):", flush=True)
        for r in failed:
            print(f"  - {r.series_id}: {r.error}", flush=True)
    else:
        print("series failed: none", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--series",
        help="comma-separated FRED series_id list (default: full CURATED_SERIES). "
        "Use --smoke-test to run the fixed DGS10/UNRATE/CPIAUCSL subset instead.",
    )
    parser.add_argument(
        "--smoke-test", action="store_true",
        help="run only the task's required test subset (DGS10, UNRATE, CPIAUCSL)",
    )
    parser.add_argument(
        "--observation-start", default=_OBSERVATION_START,
        help=f"FRED observation_start date, YYYY-MM-DD (default: {_OBSERVATION_START})",
    )
    args = parser.parse_args()

    if args.series:
        series_ids = [s.strip().upper() for s in args.series.split(",") if s.strip()]
    elif args.smoke_test:
        series_ids = SMOKE_TEST_SERIES
    else:
        series_ids = CURATED_SERIES

    run_snapshot(series_ids, observation_start=args.observation_start)


if __name__ == "__main__":
    main()
