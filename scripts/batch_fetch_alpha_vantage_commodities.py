"""Batch loop over the configured Alpha Vantage commodity list (currently
gold/silver) -- same shape as scripts/batch_fetch_fred.py (see that
module's own docstring for the audit-log/BatchRun reasoning this mirrors),
wrapping ingestion/pipeline.py::ingest_alpha_vantage_commodity_series()
instead of ingest_fred_series().

TRACKED_ASSETS is a plain Python list, not a database table -- same
reasoning batch_fetch_fred.py's TRACKED_SERIES gives; add a third
commodity by adding one entry here AND to sources/alpha_vantage_
commodities.py::ASSET_SYMBOLS (that module's own docstring says where).

Usage:
  python -m scripts.batch_fetch_alpha_vantage_commodities
  python -m scripts.batch_fetch_alpha_vantage_commodities --series GOLD_USD_OZ
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from config.settings import ALPHA_VANTAGE_KEY
from ingestion.batch_log import BatchRun
from ingestion.pipeline import ingest_alpha_vantage_commodity_series
from storage.backend_bootstrap import open_db

_JOB_NAME = "alpha_vantage_commodity_fetch"


@dataclass(frozen=True)
class CommodityAsset:
    asset_id: str  # this app's own id, e.g. "GOLD_USD_OZ" -- see ASSET_SYMBOLS for the Alpha Vantage `symbol` it maps to
    unit: str
    series_key: str | None = None
    region: str | None = None


# The two commodities this app currently tracks -- weekly USD/troy-ounce
# spot prices, 2011-onward (Alpha Vantage's GOLD_SILVER_HISTORY's own
# available range).
TRACKED_ASSETS: list[CommodityAsset] = [
    CommodityAsset("GOLD_USD_OZ", unit="USD_PER_TROY_OUNCE"),
    CommodityAsset("SILVER_USD_OZ", unit="USD_PER_TROY_OUNCE"),
]


def _run_one_asset(conn, asset: CommodityAsset) -> str:
    result = ingest_alpha_vantage_commodity_series(
        conn, asset.asset_id, api_key=ALPHA_VANTAGE_KEY, unit=asset.unit,
        series_key=asset.series_key, region=asset.region,
    )
    detail = f"parsed={result.parsed_count} inserted={result.inserted_count} skipped={result.skipped_count}"
    if result.skip_reasons:
        detail += f" ({len(result.skip_reasons)} skip reason(s) logged)"
    return detail


def run_alpha_vantage_batch(conn, assets: list[CommodityAsset], scope_label: str | None = None, job_name: str | None = None) -> int:
    if not assets:
        raise ValueError("asset list is empty")
    if not ALPHA_VANTAGE_KEY:
        raise SystemExit("ALPHA_VANTAGE_KEY is not set (check .env)")

    scope_label = scope_label or f"Alpha Vantage commodities ({len(assets)} asset(s))"
    job_name = job_name or _JOB_NAME

    print(f"{job_name}: {len(assets)} asset(s), scope={scope_label!r}", flush=True)
    ok = failed = 0
    with BatchRun(conn, job_name, scope_label) as run:
        print(f"run_id={run.run_id}", flush=True)
        for asset in assets:
            with run.item(asset.asset_id) as item:
                try:
                    item.detail = _run_one_asset(conn, asset)
                    ok += 1
                    print(f"{asset.asset_id}: OK -- {item.detail}", flush=True)
                except Exception as exc:  # noqa: BLE001 -- let run.item() record it, then keep looping
                    failed += 1
                    print(f"{asset.asset_id}: FAILED -- {exc}", flush=True)
                    raise

    print(f"\nDone. run_id={run.run_id} ok={ok} failed={failed}", flush=True)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", help="comma-separated asset_id list (default: every asset in TRACKED_ASSETS)")
    parser.add_argument("--scope", help="human label for the audit log (defaults to the count)")
    args = parser.parse_args()

    if args.series:
        wanted = {s.strip().upper() for s in args.series.split(",") if s.strip()}
        assets = [a for a in TRACKED_ASSETS if a.asset_id.upper() in wanted]
        missing = wanted - {a.asset_id.upper() for a in assets}
        if missing:
            raise SystemExit(f"not in TRACKED_ASSETS (add a CommodityAsset entry first): {sorted(missing)}")
    else:
        assets = TRACKED_ASSETS

    conn = open_db()
    run_alpha_vantage_batch(conn, assets, scope_label=args.scope)
    conn.close()


if __name__ == "__main__":
    main()
