"""Backfill diluted EPS (metric `diluted_eps`) from NSE XBRL filings already
on disk under data/raw/<COMPANY>/nse/ -- no NSE re-fetch. Each file is
re-parsed, and ONLY observations for diluted_eps that aren't already in
canonical_financials for that period are inserted/reconciled (a plain re-
ingest would duplicate every other metric the same file also yields).

Idempotent: re-running skips periods that already have a diluted_eps value.
Meant to be run in small batches (--companies / --limit) with a check between
rounds, like other bulk ingestion here.

Usage:
  python -m scripts.backfill_diluted_eps --companies TCS,INFY,HDFCBANK
  python -m scripts.backfill_diluted_eps --index "Nifty 50" --limit 5
"""

from __future__ import annotations

import argparse

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from config import settings as app_settings  # noqa: E402
from ingestion.batch_log import BatchRun  # noqa: E402
from ingestion.pipeline import ingest_file  # noqa: E402
from sources.nse_xbrl import NSEXbrlAdapter  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402
from storage.company_repository import select_company_ids_by_index  # noqa: E402
from storage.repositories import get_canonical_series  # noqa: E402

JOB_NAME = "diluted_eps_backfill"
METRIC = "diluted_eps"


def _cache_alias_lookups() -> None:
    """Parsing resolves every row label to a metric via a metric_aliases
    query -- ~100 round trips per filing, which over a WAN link to Neon was
    minutes per file. The alias table is static for the life of this run, so
    memoize the lookups (script-scoped; nothing else is affected)."""
    import normalization.financials as nf

    original = nf.get_metric_key_for_alias
    cache: dict = {}

    def cached(conn, source, raw_label):
        key = (source, raw_label)
        if key not in cache:
            cache[key] = original(conn, source, raw_label)
        return cache[key]

    nf.get_metric_key_for_alias = cached


def _existing_periods(conn, company_id: str) -> set[tuple[str, str, str | None, str]]:
    have = set()
    for statement_type in ("consolidated", "standalone"):
        for period_type in ("annual", "quarterly"):
            for row in get_canonical_series(conn, company_id, METRIC, period_type, statement_type):
                have.add((period_type, row["fiscal_year"], row["quarter"], statement_type))
    return have


def backfill_company(conn, company_id: str) -> str:
    raw_dir = app_settings.RAW_DIR / company_id / "nse"
    files = sorted(raw_dir.glob("*.xml")) if raw_dir.is_dir() else []
    have = _existing_periods(conn, company_id)
    inserted = files_used = 0
    for path in files:
        statement_type = path.stem.split("_")[1]

        def keep(obs, st=statement_type) -> bool:
            return obs.metric_key == METRIC and (obs.period_type, obs.fiscal_year, obs.quarter, st) not in have

        # Parse first (local, instant) and only run the full ingest pipeline --
        # events + reconciliation, several DB round trips -- for files that
        # actually have something new; most old filings have none.
        if not any(keep(o) for o in NSEXbrlAdapter(conn).parse(path, company_id, statement_type=statement_type)):
            continue
        result = ingest_file(conn, path, company_id=company_id, source_id="nse",
                             statement_type=statement_type, observation_filter=keep)
        if result.inserted_count:
            files_used += 1
            inserted += result.inserted_count
            have = _existing_periods(conn, company_id)
    return f"files={len(files)} files_with_new_diluted_eps={files_used} inserted={inserted}"


def run_backfill(conn, company_ids: list[str]) -> int:
    import psycopg2

    _cache_alias_lookups()
    work_conn = conn
    with BatchRun(conn, JOB_NAME, f"{len(company_ids)} companies") as run:
        for company_id in company_ids:
            with run.item(company_id) as item:
                for attempt in (1, 2):
                    try:
                        item.detail = backfill_company(work_conn, company_id)
                        break
                    except (psycopg2.OperationalError, psycopg2.InterfaceError):
                        # Neon's pooler drops idle/long connections now and
                        # then; reconnect once and retry (idempotent).
                        if attempt == 2:
                            raise
                        work_conn = open_db()
                print(f"{company_id}: {item.detail}", flush=True)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--companies", help="comma-separated company_id list")
    parser.add_argument("--index", help='company_index_membership index_name, e.g. "Nifty 50"')
    parser.add_argument("--limit", type=int, help="only the first N companies of the selection")
    args = parser.parse_args()
    conn = open_db()
    if args.companies:
        ids = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
    elif args.index:
        ids = [r["company_id"] for r in select_company_ids_by_index(conn, args.index)]
    else:
        parser.error("give --companies or --index")
    if args.limit:
        ids = ids[: args.limit]
    run_backfill(conn, ids)


if __name__ == "__main__":
    main()
