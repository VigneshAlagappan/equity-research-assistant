from __future__ import annotations

import psycopg2
import pytest

import storage.database as database


class _FakeConn:
    closed = 0

    def close(self) -> None:
        self.closed = 1

    def rollback(self) -> None:
        pass


@pytest.fixture
def fake_pg(monkeypatch):
    applied: list[str] = []
    monkeypatch.setenv("NEON", "postgresql://fake/db")
    monkeypatch.delenv("LOCAL_DEV_DATABASE_URL", raising=False)
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(database, "_apply_postgres_schema", lambda conn, sql: applied.append("applied"))
    monkeypatch.setattr(database, "_SCHEMA_APPLIED", set())
    return applied


def test_schema_applied_once_per_process_on_the_default_path(fake_pg) -> None:
    database.init_postgres_db()
    database.init_postgres_db()
    database.init_postgres_db()
    assert fake_pg == ["applied"]


def test_explicit_connection_string_always_applies_schema(fake_pg) -> None:
    database.init_postgres_db(connection_string="postgresql://fake/throwaway")
    database.init_postgres_db(connection_string="postgresql://fake/throwaway")
    assert fake_pg == ["applied", "applied"]


def test_release_closes_a_connection_that_did_not_come_from_the_pool() -> None:
    conn = _FakeConn()
    database.release_postgres_connection(conn)
    assert conn.closed == 1
