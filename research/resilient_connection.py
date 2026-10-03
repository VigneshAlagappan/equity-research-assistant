"""Connection proxy that survives Neon dropping a connection mid-run.

A research case holds one connection across minutes of LLM calls, and Neon
(autosuspend, pooler restart, a network blip) can end it at any point
("SSL SYSCALL error: EOF", "connection already closed"). TCP keepalives
(storage/database.py) only stop a connection looking idle; they cannot save
one the server has reset.

On a connection error this proxy reopens the connection and retries that one
statement -- but ONLY when nothing has been written in the current
transaction. After a write the dropped connection has taken uncommitted work
with it, and replaying only the last statement would silently lose the rest,
so the error is re-raised for the caller to handle.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_READ_PREFIXES = ("select", "show", "with", "explain")


def is_connection_error(exc: Exception) -> bool:
    try:
        import psycopg2
    except ImportError:  # SQLite-only environments
        return False
    return isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))


def _is_read(sql) -> bool:
    return str(sql).lstrip().lower().startswith(_READ_PREFIXES)


class ReconnectingConnection:
    def __init__(self, open_fn):
        self._open = open_fn
        self._conn = open_fn()
        self._dirty = False  # a write has run since the last commit/rollback

    def _reconnect(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("Database connection dropped; reconnecting")
        self._conn = self._open()
        self._dirty = False

    def cursor(self, *args, **kwargs):
        return _ReconnectingCursor(self, args, kwargs)

    def commit(self):
        # A dead connection at commit means the transaction is lost: surface it.
        self._conn.commit()
        self._dirty = False

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception as exc:  # noqa: BLE001
            if not is_connection_error(exc):
                raise
            self._reconnect()
        self._dirty = False

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _ReconnectingCursor:
    def __init__(self, owner: ReconnectingConnection, args, kwargs):
        self._owner, self._args, self._kwargs = owner, args, kwargs
        self._cur = owner._conn.cursor(*args, **kwargs)

    def execute(self, sql, *a, **k):
        owner = self._owner
        try:
            result = self._cur.execute(sql, *a, **k)
        except Exception as exc:  # noqa: BLE001
            if not is_connection_error(exc) or owner._dirty:
                raise
            owner._reconnect()
            self._cur = owner._conn.cursor(*self._args, **self._kwargs)
            result = self._cur.execute(sql, *a, **k)
        if not _is_read(sql):
            owner._dirty = True
        return result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._cur.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)
