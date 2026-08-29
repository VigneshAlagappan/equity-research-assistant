"""Price-history db: schema creation and repository upsert/read behavior."""

from __future__ import annotations

from pathlib import Path

from storage.database import list_tables
from storage.price_database import init_price_db
from storage.price_repository import get_price_history, upsert_daily_bar


def test_init_price_db_creates_daily_prices_table(tmp_path: Path) -> None:
    conn = init_price_db(db_path=tmp_path / "test.db")
    try:
        assert "daily_prices" in list_tables(conn)
    finally:
        conn.close()


def test_init_price_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = init_price_db(db_path=db_path)
    conn.close()

    conn = init_price_db(db_path=db_path)  # should not raise, and not add/drop tables
    try:
        assert "daily_prices" in list_tables(conn)
    finally:
        conn.close()


def test_upsert_daily_bar_overwrites_existing_row(tmp_path: Path) -> None:
    conn = init_price_db(db_path=tmp_path / "test.db")
    try:
        upsert_daily_bar(
            conn, company_id="RELIANCE", trade_date="2026-08-27",
            open_=100.0, high=105.0, low=99.0, close=102.0, volume=1000,
        )
        conn.commit()
        upsert_daily_bar(
            conn, company_id="RELIANCE", trade_date="2026-08-27",
            open_=100.0, high=106.0, low=99.0, close=103.5, volume=1500,
        )
        conn.commit()

        rows = conn.execute("SELECT * FROM daily_prices WHERE company_id = 'RELIANCE'").fetchall()
        assert len(rows) == 1
        assert rows[0]["close"] == 103.5
        assert rows[0]["volume"] == 1500
    finally:
        conn.close()


def test_get_price_history_orders_by_date_within_range(tmp_path: Path) -> None:
    conn = init_price_db(db_path=tmp_path / "test.db")
    try:
        for trade_date, close in [("2026-08-24", 100.0), ("2026-08-26", 102.0), ("2026-08-25", 101.0)]:
            upsert_daily_bar(
                conn, company_id="TCS", trade_date=trade_date,
                open_=close, high=close, low=close, close=close, volume=500,
            )
        conn.commit()

        rows = get_price_history(conn, "TCS", "2026-08-25", "2026-08-26")
        assert [r["trade_date"] for r in rows] == ["2026-08-25", "2026-08-26"]
        assert [r["close"] for r in rows] == [101.0, 102.0]
    finally:
        conn.close()
