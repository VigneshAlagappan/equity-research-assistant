"""Repository layer for the price-history db (storage/price_database.py) --
upsert daily OHLCV bars and read them back for charting.

Every write is an upsert keyed on daily_prices' (company_id, trade_date)
primary key, not a plain insert: re-running the daily fetch job or the
backfill script for a date already on file must overwrite that row with the
latest values, not raise a UNIQUE-constraint error or create a duplicate --
the whole point of the composite PK is "at most one bar per company per
day," enforced by the schema itself rather than by caller discipline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from storage.database import utcnow_iso


def upsert_daily_bar(
    conn: sqlite3.Connection,
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
    """Insert one (company_id, trade_date) bar, or overwrite it in place if
    a bar for that day already exists."""
    conn.execute(
        """
        INSERT INTO daily_prices
            (company_id, trade_date, open, high, low, close, volume, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, trade_date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        (company_id, trade_date, open_, high, low, close, volume, source, utcnow_iso()),
    )


def upsert_daily_bars(conn: sqlite3.Connection, bars: Iterable[dict]) -> int:
    """Upsert a batch of bars (each a dict matching upsert_daily_bar's
    keyword args) and commit once at the end -- typically one company's full
    history per call, same per-company commit granularity
    scripts/backfill_sector_industry.py uses, so an interrupted run only
    loses whatever company was mid-flight, not everything already written."""
    count = 0
    for bar in bars:
        upsert_daily_bar(conn, **bar)
        count += 1
    conn.commit()
    return count


def get_price_history(
    conn: sqlite3.Connection, company_id: str, start_date: str, end_date: str
) -> list[sqlite3.Row]:
    """Bars for one company within an inclusive date range, oldest first --
    for feeding a chart. Hits daily_prices' primary-key index directly (an
    indexed range scan), never a full-table scan, regardless of how large
    the table grows."""
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM daily_prices
        WHERE company_id = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (company_id, start_date, end_date),
    ).fetchall()


def get_close_as_of_range(
    conn: sqlite3.Connection, company_id: str, start_date: str, end_date: str
) -> float | None:
    """Closing price on the last trading day within [start_date, end_date],
    or None if there's no price data in that exact range -- for web/
    charts_feed.py's per-fiscal-period Close Price attribute, deliberately
    bounded to the period's own dates rather than falling back to the
    nearest price from outside it, which would silently show a stale
    figure for a period the price db has no real coverage of yet."""
    row = conn.execute(
        """
        SELECT close FROM daily_prices
        WHERE company_id = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (company_id, start_date, end_date),
    ).fetchone()
    return row["close"] if row else None


def get_avg_volume(conn: sqlite3.Connection, company_id: str, start_date: str, end_date: str) -> float | None:
    """Average daily trading volume within [start_date, end_date], or None
    if there's no volume data in that range -- for web/charts_feed.py's
    per-fiscal-period Volume attribute. Average (not a period total) so the
    figure is comparable across periods regardless of trading-day count."""
    row = conn.execute(
        """
        SELECT AVG(volume) AS avg_volume FROM daily_prices
        WHERE company_id = ? AND trade_date BETWEEN ? AND ? AND volume IS NOT NULL
        """,
        (company_id, start_date, end_date),
    ).fetchone()
    return row["avg_volume"] if row and row["avg_volume"] is not None else None


def list_latest_close(conn: sqlite3.Connection) -> dict[str, float]:
    """Latest close price per company, one query for every company at once
    -- avoids an N+1 across ~500 rows on the Companies list, same reasoning
    storage/repositories.py's list_latest_shares_outstanding() documents."""
    rows = conn.execute(
        """
        SELECT company_id, close FROM (
            SELECT company_id, close,
                   ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY trade_date DESC) AS rn
            FROM daily_prices
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["company_id"]: row["close"] for row in rows}


def list_52_week_range(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """(low, high) close over the trailing 52 weeks per company, one query
    for every company at once."""
    rows = conn.execute(
        """
        SELECT company_id, MIN(close) AS lo, MAX(close) AS hi
        FROM daily_prices
        WHERE trade_date >= date('now', '-364 days')
        GROUP BY company_id
        """
    ).fetchall()
    return {row["company_id"]: (row["lo"], row["hi"]) for row in rows}


def list_all_time_range(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """(low, high) close over the full history on file per company, one
    query for every company at once. "All-time" means all of what
    scripts/backfill_price_history.py has actually pulled, not a claim
    about the company's real trading history predating that."""
    rows = conn.execute(
        "SELECT company_id, MIN(close) AS lo, MAX(close) AS hi FROM daily_prices GROUP BY company_id"
    ).fetchall()
    return {row["company_id"]: (row["lo"], row["hi"]) for row in rows}


def get_latest_close(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    """Most recent bar on file for one company, or None if it has none yet."""
    return conn.execute(
        """
        SELECT trade_date, close
        FROM daily_prices
        WHERE company_id = ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
