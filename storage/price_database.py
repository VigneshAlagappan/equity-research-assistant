"""SQLite connection and schema initialization for the price-history db
(config/settings.py's PRICE_DB_PATH) -- a separate file from the main
equity_research.db, deliberately smaller than storage/database.py: no
migration history and nothing to seed, since this table has no prior
installed versions to reconcile and no reference data to bootstrap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings


def get_price_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection to the price db, WAL-enabled and with row
    access by name. db_path defaults to settings.PRICE_DB_PATH, read at call
    time (same reason storage/database.py's get_connection() does) so tests/
    callers can pass an alternate path without monkeypatching.

    WAL mode (not the main db's plain rollback journal) because this db has
    a genuinely concurrent access pattern: a daily fetch job writing while
    the web app reads for a chart -- WAL lets those proceed without blocking
    on each other."""
    db_path = db_path if db_path is not None else settings.PRICE_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_price_db(db_path: Path | None = None, schema_path: Path | None = None) -> sqlite3.Connection:
    """Create the daily_prices table (if missing) and return an open
    connection. No _migrate_*/_seed_* steps, unlike storage/database.py's
    init_db() -- there's no pre-existing install to migrate onto and no
    reference data (sources, sectors, admin user, ...) to seed here."""
    schema_path = schema_path if schema_path is not None else settings.PRICE_SCHEMA_PATH
    schema_sql = schema_path.read_text()
    conn = get_price_connection(db_path)
    conn.executescript(schema_sql)
    conn.commit()
    return conn
