"""Stock actions — discrete corporate events (split/bonus/rights) that change
a company's outstanding share count.

Raw records only for now — no split-adjustment of historical shares/EPS/
price series and no chart markers yet (a documented follow-up, not built
here); this module just gives every action a durable, auditable home, the
same reasoning company_notes exists as a plain append-only log before
anything derives from it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from normalization.companies import normalize_company_id
from storage.database import utcnow_iso

#: split and bonus are the same share-count math (ratio_from -> ratio_to);
#: rights differs only by optionally carrying a subscription_price, since
#: it's the one type involving real cash, not just a share-count change.
ACTION_TYPES = {"split", "bonus", "rights"}


class InvalidStockActionError(ValueError):
    pass


class StockActionNotFoundError(ValueError):
    pass


def add_stock_action(
    conn: sqlite3.Connection,
    company_id: str,
    action_type: str,
    action_date: str,
    ratio_from: float,
    ratio_to: float,
    *,
    subscription_price: float | None = None,
    source: str | None = None,
    source_url: str | None = None,
    notes: str | None = None,
) -> sqlite3.Row:
    if action_type not in ACTION_TYPES:
        raise InvalidStockActionError(f"action_type must be one of {sorted(ACTION_TYPES)}, got {action_type!r}")
    try:
        dt.date.fromisoformat(action_date)
    except ValueError:
        raise InvalidStockActionError(f"action_date must be 'YYYY-MM-DD', got {action_date!r}") from None
    if ratio_from <= 0 or ratio_to <= 0:
        raise InvalidStockActionError(f"ratio_from/ratio_to must be positive, got {ratio_from!r}/{ratio_to!r}")
    if action_type != "rights" and subscription_price is not None:
        raise InvalidStockActionError("subscription_price only applies to a 'rights' action")

    company_id = normalize_company_id(company_id)
    now = utcnow_iso()
    cursor = conn.execute(
        """
        INSERT INTO stock_actions (
            company_id, action_type, action_date, ratio_from, ratio_to,
            subscription_price, source, source_url, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, action_type, action_date, ratio_from, ratio_to,
         subscription_price, source, source_url, notes, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM stock_actions WHERE action_id = ?", (cursor.lastrowid,)).fetchone()


def list_stock_actions(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every recorded action for this company, most recent first."""
    company_id = normalize_company_id(company_id)
    return conn.execute(
        "SELECT * FROM stock_actions WHERE company_id = ? ORDER BY action_date DESC, action_id DESC",
        (company_id,),
    ).fetchall()


def delete_stock_action(conn: sqlite3.Connection, company_id: str, action_id: int) -> None:
    company_id = normalize_company_id(company_id)
    cursor = conn.execute(
        "DELETE FROM stock_actions WHERE action_id = ? AND company_id = ?", (action_id, company_id)
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise StockActionNotFoundError(f"No stock action id={action_id} for company_id={company_id!r}")
