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
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # NEON/DATABASE_BACKEND/DOCUMENT_STORE_BACKEND live there

import storage.backend_bootstrap

# Must run before importing ingestion.pipeline/storage.repositories below --
# same ordering scripts/batch_fetch_sec_edgar.py follows, so DATABASE_
# BACKEND=postgres actually routes this job's writes (macro_observations,
# raw_objects) at Neon instead of silently staying on local SQLite. Without
# this, main()'s open_db() call below would still connect to the right
# database, but ingest_fred_series()'s own internal repository imports
# (already bound to the SQLite flavor by then) would not.
storage.backend_bootstrap.install()

from ingestion.batch_log import BatchRun
from ingestion.pipeline import ingest_fred_series
from storage.backend_bootstrap import open_db

_JOB_NAME = "fred_macro_fetch"


@dataclass(frozen=True)
class FredSeries:
    series_id: str  # FRED's own id, e.g. "FEDFUNDS" -- also this job's batch_job_items.company_id column (repurposed: no company involved, macro data is company-agnostic, see MacroIngestionResult's own lack of a company_id field)
    unit: str  # required -- FRED's CSV export has no unit column (see fetch_fred_series's own docstring)
    series_key: str | None = None  # defaults to series_id.lower() if omitted
    region: str | None = None  # None = national/US-wide


# Broad US macro indicators an equity research workflow would reference
# regardless of which company/sector is under review -- the same series
# scripts/fred_historical_s3_pull.py already archives to S3 (raw/fred/
# snapshots/.../{metadata,observations}.json, cataloged in raw_objects by
# scripts/catalog_fred_s3_snapshots.py). Grown from an original 5-series
# starter set to 34 ("SIGNALS U.S. MACRO LAYER"), then to 53 with a P1/P2
# priority list (rates, credit conditions, housing/trade, S&P 500, copper)
# -- each expansion reconciles the S3-only archival path with this
# CSV/no-key path, which is what actually populates macro_observations
# (queryable structured values, via ingest_fred_series() below) so
# research/macro_evidence.py's catalog of usable series_key values covers
# everything that's been archived, not just a subset of it. The S3/API-key
# path stays a separate, higher-fidelity raw archival copy (adds FRED's own
# title/frequency/seasonal-adjustment metadata the CSV export doesn't
# carry) -- both paths now cover the same series, so nothing is archived on
# one side but invisible to investigations on the other.
# `unit` is FRED's own "units" string verbatim (read from that S3 metadata
# snapshot), not a separate enum this codebase invents -- e.g. "Percent",
# "Index 2017=100", "Billions of Dollars". Each entry is independent, so
# adding one never touches the others.
TRACKED_SERIES: list[FredSeries] = [
    # Economic Growth
    FredSeries("GDP", unit="Billions of Dollars"),
    FredSeries("GDPC1", unit="Billions of Chained 2017 Dollars"),
    FredSeries("INDPRO", unit="Index 2017=100"),
    # Monetary Policy
    FredSeries("FEDFUNDS", unit="Percent"),
    FredSeries("SOFR", unit="Percent"),
    FredSeries("DGS3MO", unit="Percent"),
    FredSeries("DGS2", unit="Percent"),
    FredSeries("DGS10", unit="Percent"),
    FredSeries("T10Y2Y", unit="Percent"),
    # Inflation
    FredSeries("CPIAUCSL", unit="Index 1982-1984=100"),
    FredSeries("CPILFESL", unit="Index 1982-1984=100"),
    FredSeries("PCEPI", unit="Index 2017=100"),
    FredSeries("PCEPILFE", unit="Index 2017=100"),
    FredSeries("T10YIE", unit="Percent"),
    # Employment
    FredSeries("UNRATE", unit="Percent"),
    FredSeries("PAYEMS", unit="Thousands of Persons"),
    FredSeries("ICSA", unit="Number"),
    FredSeries("JTSJOL", unit="Level in Thousands"),
    # Consumer
    FredSeries("UMCSENT", unit="Index 1966:Q1=100"),
    FredSeries("RSAFS", unit="Millions of Dollars"),
    FredSeries("DSPIC96", unit="Billions of Chained 2017 Dollars"),
    FredSeries("PSAVERT", unit="Percent"),
    # Housing
    FredSeries("HOUST", unit="Thousands of Units"),
    FredSeries("PERMIT", unit="Thousands of Units"),
    FredSeries("CSUSHPISA", unit="Index Jan 2000=100"),
    FredSeries("MORTGAGE30US", unit="Percent"),
    # Liquidity / Credit
    FredSeries("M2SL", unit="Billions of Dollars"),
    FredSeries("WALCL", unit="Millions of U.S. Dollars"),
    FredSeries("NFCI", unit="Index"),
    FredSeries("BAMLH0A0HYM2", unit="Percent"),
    # Markets
    FredSeries("VIXCLS", unit="Index"),
    FredSeries("DTWEXBGS", unit="Index Jan 2006=100"),
    # Commodities
    FredSeries("DCOILWTICO", unit="Dollars per Barrel"),
    FredSeries("DHHNGSP", unit="Dollars per Million BTU"),
    FredSeries("PCOPPUSDM", unit="U.S. Dollars per Metric Ton"),
    # Rates (P1 additions, raw/fred/snapshots/20260926T222730Z/)
    FredSeries("DGS5", unit="Percent"),
    FredSeries("DGS30", unit="Percent"),
    # Prices / wages (P1)
    FredSeries("PPIACO", unit="Index 1982=100"),
    FredSeries("CES0500000003", unit="Dollars per Hour"),
    # Credit conditions (P1)
    FredSeries("TOTALSL", unit="Millions of U.S. Dollars"),
    FredSeries("TOTLL", unit="Billions of U.S. Dollars"),
    FredSeries("BUSLOANS", unit="Billions of U.S. Dollars"),
    FredSeries("CREACBW027SBOG", unit="Billions of U.S. Dollars"),
    FredSeries("DPSACBW027SBOG", unit="Billions of U.S. Dollars"),
    FredSeries("DRSFRMACBS", unit="Percent"),
    FredSeries("DRTSCILM", unit="Percent"),
    # Housing / industry / trade (P2)
    FredSeries("EXHOSLUSM495S", unit="Number of Units"),
    FredSeries("HSN1F", unit="Thousands"),
    FredSeries("TCU", unit="Percent"),
    FredSeries("IMPGS", unit="Billions of Dollars"),
    FredSeries("EXPGS", unit="Billions of Dollars"),
    FredSeries("BOPGSTB", unit="Millions of Dollars"),
    # Markets (P2)
    FredSeries("SP500", unit="Index"),
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

    conn = open_db()
    run_fred_batch(conn, series_list, scope_label=args.scope)
    conn.close()


if __name__ == "__main__":
    main()
