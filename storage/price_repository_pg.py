"""Postgres (Neon) port of `storage/price_repository.py`.

Every function in `price_repository.py` is ported here, same names/
signatures, targeting a `psycopg2` connection (from `storage.database.
init_postgres_db()`, via `storage.backend_bootstrap.open_price_db()`)
instead of `sqlite3.Connection`. `daily_prices` is part of
`schemas/postgres_schema.sql` now (see that file's comment on the table for
why this port exists at all -- it wasn't part of the original ADR-021
migration).

Translation notes (see `storage/company_repository_pg.py` for the
established patterns this reuses):
- `?` -> `%s`; every query goes through an explicit `conn.cursor()`.
- `ON CONFLICT(...) DO UPDATE SET col = excluded.col` -> Postgres's
  `ON CONFLICT (...) DO UPDATE SET col = EXCLUDED.col`.
- `upsert_daily_bars()` no longer commits per-bar via a shared cursor loop
  the way the SQLite version's `conn.execute()`-per-call does implicitly --
  it still commits once at the end, matching the original's "one company's
  full history per call" granularity.
- `list_52_week_range()`'s SQLite `date('now', '-364 days')` becomes
  Postgres's `CURRENT_DATE - INTERVAL '364 days'`; the comparison is
  against `trade_date`, a TEXT column of ISO date strings, which compares
  correctly against a cast date -- `trade_date >= (CURRENT_DATE - INTERVAL
  '364 days')::text`.
"""

from __future__ import annotations

from collections.abc import Iterable

from storage.db_types import DBConnection, Row


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def upsert_daily_bar(
    conn: DBConnection,
    *,
    company_id: str,
    trade_date: str,
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float,
    volume: int | None,
    source: str = "yfinance",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_prices
                (company_id, trade_date, open, high, low, close, volume, source, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (company_id, trade_date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                source = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at
            """,
            (company_id, trade_date, open_, high, low, close, volume, source, _utcnow_iso()),
        )


def upsert_daily_bars(conn: DBConnection, bars: Iterable[dict]) -> int:
    count = 0
    for bar in bars:
        upsert_daily_bar(conn, **bar)
        count += 1
    conn.commit()
    return count


def get_price_history(conn: DBConnection, company_id: str, start_date: str, end_date: str) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, open, high, low, close, volume
            FROM daily_prices
            WHERE company_id = %s AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date
            """,
            (company_id, start_date, end_date),
        )
        return cur.fetchall()


def get_close_as_of_range(conn: DBConnection, company_id: str, start_date: str, end_date: str) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close FROM daily_prices
            WHERE company_id = %s AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date DESC LIMIT 1
            """,
            (company_id, start_date, end_date),
        )
        row = cur.fetchone()
    return row["close"] if row else None


def get_avg_volume(conn: DBConnection, company_id: str, start_date: str, end_date: str) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT AVG(volume) AS avg_volume FROM daily_prices
            WHERE company_id = %s AND trade_date BETWEEN %s AND %s AND volume IS NOT NULL
            """,
            (company_id, start_date, end_date),
        )
        row = cur.fetchone()
    return row["avg_volume"] if row and row["avg_volume"] is not None else None


def list_latest_close(conn: DBConnection) -> dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, close FROM (
                SELECT company_id, close,
                       ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY trade_date DESC) AS rn
                FROM daily_prices
            ) ranked
            WHERE rn = 1
            """
        )
        rows = cur.fetchall()
    return {row["company_id"]: row["close"] for row in rows}


def list_latest_daily_change(conn: DBConnection) -> dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, close, rn
            FROM (
                SELECT company_id, close,
                       ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY trade_date DESC) AS rn
                FROM daily_prices
            ) ranked
            WHERE rn <= 2
            """
        )
        rows = cur.fetchall()
    closes_by_company: dict[str, dict[int, float]] = {}
    for row in rows:
        closes_by_company.setdefault(row["company_id"], {})[row["rn"]] = row["close"]
    result: dict[str, float] = {}
    for company_id, closes in closes_by_company.items():
        latest, previous = closes.get(1), closes.get(2)
        if latest is not None and previous:
            result[company_id] = (latest - previous) / previous * 100
    return result


def list_52_week_range(conn: DBConnection) -> dict[str, tuple[float, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, MIN(close) AS lo, MAX(close) AS hi
            FROM daily_prices
            WHERE trade_date >= (CURRENT_DATE - INTERVAL '364 days')::text
            GROUP BY company_id
            """
        )
        rows = cur.fetchall()
    return {row["company_id"]: (row["lo"], row["hi"]) for row in rows}


def list_all_time_range(conn: DBConnection) -> dict[str, tuple[float, float]]:
    with conn.cursor() as cur:
        cur.execute("SELECT company_id, MIN(close) AS lo, MAX(close) AS hi FROM daily_prices GROUP BY company_id")
        rows = cur.fetchall()
    return {row["company_id"]: (row["lo"], row["hi"]) for row in rows}


def list_earliest_trade_dates(conn: DBConnection, company_ids: list[str]) -> dict[str, str]:
    """Postgres port of storage/price_repository.py's function of the same
    name -- see that docstring. `= ANY(%s)` replaces SQLite's `IN (?,?,...)`
    placeholder expansion (psycopg2 adapts a Python list to a Postgres
    array directly)."""
    if not company_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, MIN(trade_date) AS earliest
            FROM daily_prices
            WHERE company_id = ANY(%s)
            GROUP BY company_id
            """,
            (company_ids,),
        )
        rows = cur.fetchall()
    return {row["company_id"]: row["earliest"] for row in rows}


def get_latest_close(conn: DBConnection, company_id: str) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, close
            FROM daily_prices
            WHERE company_id = %s
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (company_id,),
        )
        return cur.fetchone()
