"""Batch loop over a configured list of FRED series -- the gap the
disabled "FRED macro data" Schedule panel row (web/app.py) has flagged:
"Live fetch works one series at a time -- needs a loop over a configured
series list". Wraps ingestion/pipeline.py::ingest_fred_series() (nothing
new about how one series gets fetched -- see that function's own
docstring for the dedup-on-repeat-run behavior this loop relies on),
looped over TRACKED_SERIES below, with every run and every series'
outcome recorded to the batch job audit log (ingestion/batch_log.py ->
batch_job_runs/batch_job_items) -- same shape as scripts/batch_fetch_nse.py
and scripts/batch_fetch_sec_edgar.py.

TRACKED_SERIES is a plain Python list, not a database table -- there's no
existing "list of series to track" concept anywhere in this codebase to
extend (company_index_membership is company-keyed, not series-keyed), and
a list this short-lived-editable doesn't earn a schema migration + admin
UI of its own. Add a series by adding one FredSeries(...) entry below;
nothing else in this script needs to change.

Usage:
  python -m scripts.batch_fetch_fred
  python -m scripts.batch_fetch_fred --series FEDFUNDS,DGS10
  python -m scripts.batch_fetch_fred --scope "FRED core series"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from ingestion.batch_log import BatchRun
from ingestion.pipeline import ingest_fred_series
from storage.database import init_db

_JOB_NAME = "fred_macro_fetch"


@dataclass(frozen=True)
class FredSeries:
    series_id: str  # FRED's own id, e.g. "FEDFUNDS" -- also this job's batch_job_items.company_id column (repurposed: no company involved, macro data is company-agnostic, see MacroIngestionResult's own lack of a company_id field)
    unit: str  # required -- FRED's CSV export has no unit column (see fetch_fred_series's own docstring)
    series_key: str | None = None  # defaults to series_id.lower() if omitted
    region: str | None = None  # None = national/US-wide


# Starter set -- broad US macro indicators an equity research workflow
# would reference regardless of which company/sector is under review.
# Grow this list incrementally as new series are needed; each entry is
# independent, so adding one never touches the others.
TRACKED_SERIES: list[FredSeries] = [
    FredSeries("FEDFUNDS", unit="PERCENT"),
    FredSeries("DGS10", unit="PERCENT"),
    FredSeries("CPIAUCSL", unit="INDEX"),
    FredSeries("UNRATE", unit="PERCENT"),
    FredSeries("GDP", unit="USD_BILLION"),
]


def _run_one_series(conn, series: FredSeries) -> str:
    result = ingest_fred_series(
        conn, series.series_id, unit=series.unit, series_key=series.series_key, region=series.region,
    )
    detail = f"parsed={result.parsed_count} inserted={result.inserted_count} skipped={result.skipped_count}"
    if result.skip_reasons:
        detail += f" ({len(result.skip_reasons)} skip reason(s) logged)"
    return detail


def run_fred_batch(conn, series_list: list[FredSeries], scope_label: str | None = None, job_name: str | None = None) -> int:
    """The actual series-list loop, factored out of main() so the Settings >
    Data Operations > Schedule panel's "Run now" button (web/app.py) can
    drive the exact same batch -- same one-capability-two-triggers shape
    scripts/batch_fetch_nse.py's run_nse_batch() already uses. Returns the
    BatchRun's run_id."""
    if not series_list:
        raise ValueError("series list is empty")

    scope_label = scope_label or f"FRED ({len(series_list)} series)"
    job_name = job_name or _JOB_NAME

    print(f"{job_name}: {len(series_list)} series, scope={scope_label!r}", flush=True)
    ok = failed = 0
    with BatchRun(conn, job_name, scope_label) as run:
        print(f"run_id={run.run_id}", flush=True)
        for series in series_list:
            # run.item()'s `company_id` column is repurposed here as the
            # series_id -- there's no company involved in a macro fetch,
            # and this is the only identifying label the audit log
            # (Audit Log > Job Runs) has to show per item.
            with run.item(series.series_id) as item:
                try:
                    item.detail = _run_one_series(conn, series)
                    ok += 1
                    print(f"{series.series_id}: OK -- {item.detail}", flush=True)
                except Exception as exc:  # noqa: BLE001 -- let run.item() record it, then keep looping
                    failed += 1
                    print(f"{series.series_id}: FAILED -- {exc}", flush=True)
                    raise

    print(f"\nDone. run_id={run.run_id} ok={ok} failed={failed}", flush=True)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", help="comma-separated FRED series_id list (default: every series in TRACKED_SERIES)")
    parser.add_argument("--scope", help="human label for the audit log (defaults to the count)")
    args = parser.parse_args()

    if args.series:
        wanted = {s.strip().upper() for s in args.series.split(",") if s.strip()}
        series_list = [s for s in TRACKED_SERIES if s.series_id.upper() in wanted]
        missing = wanted - {s.series_id.upper() for s in series_list}
        if missing:
            raise SystemExit(f"not in TRACKED_SERIES (add a FredSeries entry first): {sorted(missing)}")
    else:
        series_list = TRACKED_SERIES

    conn = init_db()
    run_fred_batch(conn, series_list, scope_label=args.scope)
    conn.close()


if __name__ == "__main__":
    main()
