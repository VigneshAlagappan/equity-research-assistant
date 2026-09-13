"""Per-test, throwaway Postgres databases against the local Docker Postgres
(docker-compose.test.yml) -- SQLite-removal stage 1's replacement for
"each test gets its own tmp_path SQLite file". CREATE DATABASE/DROP DATABASE
are fast against a local instance (no cloud API round trips), so a fresh
database per test is affordable and gives every test the exact "empty
database, only what this test itself seeded" isolation the SQLite fixture
already gave, without rewriting test assertions to account for shared state
-- unlike tests/test_backend_compatibility.py's dedicated-Neon-branch tests,
which use unique IDs instead because a shared cloud branch can't be freely
dropped/recreated per test.

Not usable against production or any shared Postgres -- LOCAL_TEST_POSTGRES_
ADMIN_URL defaults to docker-compose.test.yml's own credentials, and every
database this module creates is named test_<uuid>, dropped again at
teardown.
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import psycopg2.extras

LOCAL_TEST_POSTGRES_ADMIN_URL = os.environ.get(
    "LOCAL_TEST_POSTGRES_ADMIN_URL",
    "postgresql://signals_test:signals_test@localhost:5433/postgres",
)


class LocalTestPostgresUnavailable(RuntimeError):
    """Raised when docker-compose.test.yml's Postgres container isn't
    reachable -- callers turn this into a pytest.skip, not a hard failure,
    so the rest of the suite still runs on a machine that hasn't started
    it (see docker-compose.test.yml's own docstring for how to)."""


def _admin_connect():
    try:
        conn = psycopg2.connect(LOCAL_TEST_POSTGRES_ADMIN_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    except psycopg2.OperationalError as exc:
        raise LocalTestPostgresUnavailable(
            f"can't reach local test Postgres at {LOCAL_TEST_POSTGRES_ADMIN_URL!r} -- "
            "start it with `docker compose -f docker-compose.test.yml up -d`"
        ) from exc
    conn.autocommit = True  # CREATE DATABASE/DROP DATABASE can't run inside a transaction block
    return conn


def create_test_database() -> tuple[str, str]:
    """Returns (db_name, connection_string) for a freshly created, empty
    database -- schema is NOT applied here, callers use storage.database.
    init_postgres_db(connection_string=...) for that, same schema-
    application code the real app/production path already uses, so this
    can never drift from what production actually runs."""
    db_name = f"test_{uuid.uuid4().hex[:16]}"
    admin_conn = _admin_connect()
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin_conn.close()

    base = LOCAL_TEST_POSTGRES_ADMIN_URL.rsplit("/", 1)[0]
    return db_name, f"{base}/{db_name}"


def drop_test_database(db_name: str) -> None:
    admin_conn = _admin_connect()
    try:
        with admin_conn.cursor() as cur:
            # Terminate anything still attached (a fixture that failed to
            # close its own connection, e.g. on a test error) -- DROP
            # DATABASE refuses while any session is connected to it.
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        admin_conn.close()
