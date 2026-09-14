"""One-time bootstrap for a local dev Postgres database (SQLite removal,
stage "1.5" -- see docker-compose.test.yml's own docstring for how to start
the container this targets).

Applies the schema (storage.database.init_postgres_db, same function/code
path production itself uses -- never a second, drifting copy of the schema
logic) then seeds the same reference data storage/database.py's init_db()
seeds for a fresh SQLite database: default sources, sectors/industries
backfilled from whatever companies exist, the standard index definitions,
the built-in admin user, and the metric vocabulary (that last one was
already ported to storage/repositories_pg.py -- this script is the small
remainder that wasn't: _seed_sources/_seed_sectors_and_industries/
_seed_index_definitions/_seed_admin_user are SQLite-only in storage/
database.py, since production's own Postgres database was seeded once via
a direct data migration when it was first stood up, not by re-running
those functions in Postgres form -- this script is that missing Postgres
form, scoped to local dev bootstrap only). Also registers the two demo
companies (HDFCBANK, ICICIBANK), same as `python main.py seed-companies`.

Every seed here is idempotent (INSERT ... ON CONFLICT DO NOTHING) -- safe
to re-run any time, e.g. after `docker compose -f docker-compose.test.yml
down -v` wipes the volume.

Deliberately reads ONLY the LOCAL_DEV_DATABASE_URL env var, never falling
back to NEON (production) the way storage.database.init_postgres_db()'s
default does elsewhere -- this script seeds demo/reference data, which
must never be able to land in production by a missing env var, so it
refuses to run at all rather than guess.

Usage:
    LOCAL_DEV_DATABASE_URL=postgresql://signals_test:signals_test@localhost:5433/signals_dev \\
        python -m scripts.seed_local_dev_db
"""

from __future__ import annotations

import os
import sys

# Must run before any `from storage.company_repository import ...` (or
# companies.registry, which imports it) -- see storage/backend_bootstrap.py's
# own docstring and scripts/backfill_price_history.py's established fix for
# the exact bug class this avoids (a module-level import binding to the
# pre-swap SQLite version because install() hadn't run yet).
os.environ["DATABASE_BACKEND"] = "postgres"
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from psycopg2.extras import execute_values

from companies.registry import seed_companies
from config.settings import DEFAULT_SOURCES, INDEX_NAMES
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_postgres_db, utcnow_iso
from werkzeug.security import generate_password_hash


def _seed_sources_pg(conn) -> None:
    rows = [(s["source_id"], s["name"], s["trust_rank"], s["description"]) for s in DEFAULT_SOURCES]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO sources (source_id, name, trust_rank, description) VALUES %s "
            "ON CONFLICT (source_id) DO NOTHING",
            rows,
        )


def _seed_sectors_and_industries_pg(conn) -> None:
    now = utcnow_iso()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sectors (name, created_at) "
            "SELECT DISTINCT sector, %s FROM companies WHERE sector IS NOT NULL AND sector != '' "
            "ON CONFLICT (name) DO NOTHING",
            (now,),
        )
        cur.execute(
            "INSERT INTO industries (name, created_at) "
            "SELECT DISTINCT industry, %s FROM companies WHERE industry IS NOT NULL AND industry != '' "
            "ON CONFLICT (name) DO NOTHING",
            (now,),
        )


def _seed_index_definitions_pg(conn) -> None:
    now = utcnow_iso()
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO index_definitions (name, created_at) VALUES %s ON CONFLICT (name) DO NOTHING",
            [(name, now) for name in INDEX_NAMES],
        )


def _seed_admin_user_pg(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES ('admin', %s, 1, %s) ON CONFLICT (username) DO NOTHING",
            (generate_password_hash("admin"), utcnow_iso()),
        )


def main() -> None:
    if not os.environ.get("LOCAL_DEV_DATABASE_URL"):
        print("LOCAL_DEV_DATABASE_URL is not set -- refusing to run (see this script's own docstring)", file=sys.stderr)
        sys.exit(1)

    conn = init_postgres_db()

    _seed_sources_pg(conn)
    _seed_index_definitions_pg(conn)
    _seed_admin_user_pg(conn)
    ensure_metric_vocabulary(conn)
    conn.commit()

    company_ids = seed_companies(conn)
    _seed_sectors_and_industries_pg(conn)  # after companies exist, so it has rows to backfill from
    conn.commit()

    print(f"Seeded local dev database. Companies: {company_ids}")
    print("Admin login: username 'admin', password 'admin'")


if __name__ == "__main__":
    main()
