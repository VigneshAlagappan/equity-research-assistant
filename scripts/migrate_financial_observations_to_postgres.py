"""One-time bulk migration: financial_observations + reconciliation_log,
local SQLite -> production Neon. Both tables were added to schemas/
postgres_schema.sql on 2026-09-13 (previously excluded citing a Neon
free-tier storage cap production has already grown past -- see that
file's own comment and docs/ADR/021) -- this script copies the actual
historical rows over now that the schema exists there.

Two real integrity subtleties, not just a blind row copy:

1. financial_observations.observation_id is preserved exactly (via
   `OVERRIDING SYSTEM VALUE`, required since the Postgres column is
   `GENERATED ALWAYS AS IDENTITY`) -- reconciliation_log.observation_id
   references it, so a caller-assigned ID would break that FK on the
   reconciliation_log side.

2. reconciliation_log.canonical_id CANNOT be preserved as-is: production's
   canonical_financials was already migrated independently, earlier, with
   its own fresh auto-generated IDs that don't match SQLite's. Verified
   directly before writing this script: SQLite and production canonical_
   financials have the exact same 581,441 rows by natural key (company_id,
   metric_key, period_type, fiscal_year, quarter, statement_type) -- zero
   rows in either side missing from the other -- so this script maps old
   canonical_id -> new canonical_id via that natural key instead of
   copying the raw integer. NULL canonical_id (a rejected-outright
   candidate, or a stale canonical row deleted after XBRL migration) stays
   NULL either way.

Batched (execute_values, ~5000 rows/batch) and safe to re-run: every
INSERT uses ON CONFLICT (observation_id)/(log_id) DO NOTHING, so an
interrupted run just picks up wherever it left off on the next run
(re-scans rows already migrated, which is cheap relative to the network
round trip of actually inserting them).

Usage:
    python -m scripts.migrate_financial_observations_to_postgres
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from psycopg2.extras import execute_values

from storage.database import init_db, init_postgres_db

_BATCH_SIZE = 5000


def _build_canonical_id_map(sqlite_conn, pg_conn) -> dict[int, int]:
    """old (SQLite) canonical_id -> new (production) canonical_id, via the
    natural key both sides share."""
    print("Building canonical_id mapping (SQLite -> production)...")
    sq_rows = sqlite_conn.execute(
        "SELECT canonical_id, company_id, metric_key, period_type, fiscal_year, quarter, statement_type "
        "FROM canonical_financials"
    ).fetchall()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_id, company_id, metric_key, period_type, fiscal_year, quarter, statement_type "
            "FROM canonical_financials"
        )
        pg_rows = cur.fetchall()

    def _key(row) -> tuple:
        return (
            row["company_id"], row["metric_key"], row["period_type"],
            row["fiscal_year"], row["quarter"], row["statement_type"],
        )

    pg_by_key = {_key(r): r["canonical_id"] for r in pg_rows}
    mapping: dict[int, int] = {}
    unmapped = 0
    for row in sq_rows:
        new_id = pg_by_key.get(_key(row))
        if new_id is None:
            unmapped += 1
            continue
        mapping[row["canonical_id"]] = new_id

    print(f"  Mapped {len(mapping)}/{len(sq_rows)} canonical_financials rows ({unmapped} unmapped).")
    if unmapped:
        print(
            f"  WARNING: {unmapped} SQLite canonical_financials rows have no matching production row -- "
            "any reconciliation_log row pointing at one of these will get canonical_id=NULL instead.",
            file=sys.stderr,
        )
    return mapping


def _migrate_financial_observations(sqlite_conn, pg_conn) -> int:
    total = sqlite_conn.execute("SELECT COUNT(*) AS n FROM financial_observations").fetchone()["n"]
    print(f"=== financial_observations: {total} rows in SQLite ===")

    cur = sqlite_conn.execute("SELECT * FROM financial_observations ORDER BY observation_id")
    migrated = 0
    while True:
        rows = cur.fetchmany(_BATCH_SIZE)
        if not rows:
            break
        values = [
            (
                r["observation_id"], r["company_id"], r["metric_key"], r["period_type"], r["fiscal_year"],
                r["quarter"], r["statement_type"], r["value"], r["unit"], r["currency"], r["source"],
                r["source_document_id"], r["source_file"], r["source_url"], r["retrieved_at"],
                r["parser_version"], r["normalization_version"], r["created_at"],
            )
            for r in rows
        ]
        with pg_conn.cursor() as pg_cur:
            execute_values(
                pg_cur,
                """
                INSERT INTO financial_observations (
                    observation_id, company_id, metric_key, period_type, fiscal_year, quarter,
                    statement_type, value, unit, currency, source, source_document_id, source_file,
                    source_url, retrieved_at, parser_version, normalization_version, created_at
                ) OVERRIDING SYSTEM VALUE VALUES %s
                ON CONFLICT (observation_id) DO NOTHING
                """,
                values,
            )
        pg_conn.commit()
        migrated += len(rows)
        print(f"  ...{migrated}/{total}")

    with pg_conn.cursor() as pg_cur:
        pg_cur.execute(
            "SELECT setval(pg_get_serial_sequence('financial_observations', 'observation_id'), "
            "COALESCE((SELECT MAX(observation_id) FROM financial_observations), 1))"
        )
    pg_conn.commit()
    return migrated


def _migrate_reconciliation_log(sqlite_conn, pg_conn, canonical_id_map: dict[int, int]) -> int:
    total = sqlite_conn.execute("SELECT COUNT(*) AS n FROM reconciliation_log").fetchone()["n"]
    print(f"=== reconciliation_log: {total} rows in SQLite ===")

    cur = sqlite_conn.execute("SELECT * FROM reconciliation_log ORDER BY log_id")
    migrated = 0
    unmapped_canonical = 0
    while True:
        rows = cur.fetchmany(_BATCH_SIZE)
        if not rows:
            break
        values = []
        for r in rows:
            old_canonical_id = r["canonical_id"]
            if old_canonical_id is None:
                new_canonical_id = None
            else:
                new_canonical_id = canonical_id_map.get(old_canonical_id)
                if new_canonical_id is None:
                    unmapped_canonical += 1
            values.append((r["log_id"], new_canonical_id, r["observation_id"], r["considered_at"], r["was_chosen"], r["note"]))
        with pg_conn.cursor() as pg_cur:
            execute_values(
                pg_cur,
                """
                INSERT INTO reconciliation_log (log_id, canonical_id, observation_id, considered_at, was_chosen, note)
                OVERRIDING SYSTEM VALUE VALUES %s
                ON CONFLICT (log_id) DO NOTHING
                """,
                values,
            )
        pg_conn.commit()
        migrated += len(rows)
        print(f"  ...{migrated}/{total}")

    with pg_conn.cursor() as pg_cur:
        pg_cur.execute(
            "SELECT setval(pg_get_serial_sequence('reconciliation_log', 'log_id'), "
            "COALESCE((SELECT MAX(log_id) FROM reconciliation_log), 1))"
        )
    pg_conn.commit()
    if unmapped_canonical:
        print(f"  WARNING: {unmapped_canonical} rows had an unmappable canonical_id -> written as NULL.", file=sys.stderr)
    return migrated


def main() -> None:
    sqlite_conn = init_db()
    pg_conn = init_postgres_db()

    before_fo = _count(pg_conn, "financial_observations")
    before_rl = _count(pg_conn, "reconciliation_log")
    print(f"Production before: financial_observations={before_fo}, reconciliation_log={before_rl}")

    canonical_id_map = _build_canonical_id_map(sqlite_conn, pg_conn)
    fo_migrated = _migrate_financial_observations(sqlite_conn, pg_conn)
    rl_migrated = _migrate_reconciliation_log(sqlite_conn, pg_conn, canonical_id_map)

    after_fo = _count(pg_conn, "financial_observations")
    after_rl = _count(pg_conn, "reconciliation_log")
    print(f"Production after: financial_observations={after_fo}, reconciliation_log={after_rl}")
    print(f"This run inserted (attempted): financial_observations={fo_migrated}, reconciliation_log={rl_migrated}")

    sqlite_conn.close()
    pg_conn.close()


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]


if __name__ == "__main__":
    main()
