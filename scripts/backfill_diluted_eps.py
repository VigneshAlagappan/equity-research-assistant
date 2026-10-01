"""Backfill diluted EPS (metric `diluted_eps`) from NSE XBRL filings already
on disk under data/raw/<COMPANY>/nse/ -- no NSE re-fetch. Each file is
re-parsed, and ONLY observations for diluted_eps that aren't already in
canonical_financials for that period are inserted/reconciled (a plain re-
ingest would duplicate every other metric the same file also yields).

Idempotent: re-running skips periods that already have a diluted_eps value.
Each company is one batched insert, then a reconcile of just the new diluted_eps keys.
Meant to be run in small batches (--companies / --limit) with a check between
rounds, like other bulk ingestion here.

Usage:
  python -m scripts.backfill_diluted_eps --country US          # EDGAR, FY2022+
  python -m scripts.backfill_diluted_eps --companies TCS,INFY,HDFCBANK
  python -m scripts.backfill_diluted_eps --index "Nifty 50" --limit 5
"""

from __future__ import annotations

import argparse

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from config import settings as app_settings  # noqa: E402
from ingestion.batch_log import BatchRun  # noqa: E402
from sources.nse_xbrl import NSEXbrlAdapter  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402
from storage.company_repository import select_company_ids_by_index  # noqa: E402
from storage.repositories import get_canonical_series  # noqa: E402

JOB_NAME = "diluted_eps_backfill"
METRIC = "diluted_eps"
FIRST_PERIOD_END = "2021-04-01"  # start of FY2022 for a March year-end
FIRST_US_FISCAL_YEAR = 2022


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

    # Every observation also looks its metric up in metrics_dictionary (unit
    # etc.) -- equally static, equally one round trip per row.
    original_entry = nf.get_metric_dictionary_entry
    entry_cache: dict = {}

    def cached_entry(conn, metric_key):
        if metric_key not in entry_cache:
            entry_cache[metric_key] = original_entry(conn, metric_key)
        return entry_cache[metric_key]

    nf.get_metric_dictionary_entry = cached_entry


def _existing_periods(conn, company_id: str) -> set[tuple[str, str, str | None, str]]:
    have = set()
    for statement_type in ("consolidated", "standalone"):
        for period_type in ("annual", "quarterly"):
            for row in get_canonical_series(conn, company_id, METRIC, period_type, statement_type):
                have.add((period_type, row["fiscal_year"], row["quarter"], statement_type))
    return have


def backfill_company(conn, company_id: str) -> str:
    """Parse every on-disk NSE filing locally, keep only diluted_eps periods
    not already in canonical_financials, then insert and reconcile them in
    ONE batch per statement type -- a handful of DB round trips per company
    instead of the full pipeline per filing."""
    from companies.lifecycle import assert_active
    from ingestion.validation import validate_observation
    from storage.repositories import insert_financial_observations, reconcile

    assert_active(conn, company_id)
    raw_dir = app_settings.RAW_DIR / company_id / "nse"
    # Filenames start with the filing's period end (YYYY-MM-DD). The app only
    # shows FY2023 onward for Indian companies; FY2022 (from 2021-04-01) is
    # kept as one year of lead-in context, older filings are skipped.
    files = sorted(p for p in raw_dir.glob("*.xml") if p.name[:10] >= FIRST_PERIOD_END) if raw_dir.is_dir() else []
    have = _existing_periods(conn, company_id)
    adapter = NSEXbrlAdapter(conn)

    new_by_statement: dict[str, dict[tuple, object]] = {}
    for path in files:
        statement_type = path.stem.split("_")[1]
        for obs in adapter.parse(path, company_id, statement_type=statement_type):
            key = (obs.period_type, obs.fiscal_year, obs.quarter, statement_type)
            if obs.metric_key == METRIC and key not in have and not validate_observation(obs):
                new_by_statement.setdefault(statement_type, {})[key] = obs

    inserted = reconciled = 0
    for statement_type, by_key in new_by_statement.items():
        valid = list(by_key.values())
        insert_financial_observations(conn, valid)
        inserted += len(valid)
        # Reconcile ONLY these keys. The pipeline's own path
        # (compute_reconciliation_keys) expands every XBRL observation to
        # every metric in its period -- dozens of reconciles each with
        # several round trips -- which is needed when a period first becomes
        # XBRL-validated, but not here: these periods already are, and only
        # diluted_eps changed.
        for obs in valid:
            if reconcile(conn, obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year,
                         obs.quarter, obs.statement_type) is not None:
                reconciled += 1
    return f"files={len(files)} inserted={inserted} reconciled={reconciled}"


def backfill_company_us(conn, company_id: str) -> str:
    """US companies: fetch SEC EDGAR companyfacts (no on-disk filings to
    reuse here) and keep only diluted_eps rows for FY2022 onward that aren't
    already stored; reconcile just those keys."""
    from companies.registry import get_company
    from ingestion.validation import validate_observation
    from sources.sec_edgar import SECEdgarAdapter, get_cik_for_ticker
    from storage.repositories import insert_financial_observations, reconcile

    company = get_company(conn, company_id)
    cik = get_cik_for_ticker(company["fetch_symbol"] or company_id)
    if cik is None:
        raise ValueError(f"could not resolve a SEC CIK for {company_id}")
    have = _existing_periods(conn, company_id)
    if have:  # already backfilled; new filings arrive through the weekly EDGAR job
        return "skipped (already has diluted_eps)"
    new_obs = {}
    for obs in SECEdgarAdapter(conn).fetch(company_id, cik, currency=company["currency"]):
        key = (obs.period_type, obs.fiscal_year, obs.quarter, "consolidated")
        if (obs.metric_key == METRIC and int(obs.fiscal_year.removeprefix("FY")) >= FIRST_US_FISCAL_YEAR
                and key not in have and not validate_observation(obs)):
            new_obs[key] = obs
    valid = list(new_obs.values())
    insert_financial_observations(conn, valid)
    reconciled = sum(
        1 for o in valid
        if reconcile(conn, o.company_id, o.metric_key, o.period_type, o.fiscal_year, o.quarter, o.statement_type) is not None
    )
    return f"cik={cik} inserted={len(valid)} reconciled={reconciled}"


def _is_us(conn, company_id: str) -> bool:
    from companies.registry import get_company

    company = get_company(conn, company_id)
    return company is not None and company["currency"] == "USD"


def _alive(conn) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()
        return True
    except Exception:  # noqa: BLE001
        return False


def run_backfill(conn, company_ids: list[str]) -> int:
    import psycopg2

    _cache_alias_lookups()
    with BatchRun(conn, JOB_NAME, f"{len(company_ids)} companies") as run:
        for company_id in company_ids:
            # Neon's pooler drops idle connections (a slow SEC download leaves
            # this one idle for a while): check it, and reconnect -- for the
            # audit log too -- instead of dying on the next query.
            if not _alive(conn):
                conn = open_db()
                run._conn = conn
            with run.item(company_id) as item:
                for attempt in (1, 2):
                    try:
                        item.detail = (
                            backfill_company_us(conn, company_id) if _is_us(conn, company_id)
                            else backfill_company(conn, company_id)
                        )
                        break
                    except (psycopg2.OperationalError, psycopg2.InterfaceError):
                        if attempt == 2:
                            raise
                        conn = open_db()
                        run._conn = conn
                print(f"{company_id}: {item.detail}", flush=True)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--companies", help="comma-separated company_id list")
    parser.add_argument("--index", help='company_index_membership index_name, e.g. "Nifty 50"')
    parser.add_argument("--country", help='every active company of a country, e.g. "US"')
    parser.add_argument("--limit", type=int, help="only the first N companies of the selection")
    args = parser.parse_args()
    conn = open_db()
    if args.companies:
        ids = [c.strip().upper() for c in args.companies.split(",") if c.strip()]
    elif args.index:
        ids = [r["company_id"] for r in select_company_ids_by_index(conn, args.index)]
    elif args.country:
        # Only companies that actually have financials ingested -- the US
        # universe on file is thousands of registered tickers, most with no
        # statements at all, and each is a SEC download.
        from storage.company_repository import select_company_ids_with_metrics

        ids = [r["company_id"] for r in select_company_ids_with_metrics(conn, args.country, ("eps", "net_profit"))]
    else:
        parser.error("give --companies, --index or --country")
    if args.limit:
        ids = ids[: args.limit]
    run_backfill(conn, ids)


if __name__ == "__main__":
    main()
