from __future__ import annotations

import sqlite3

import pytest

from companies.registry import seed_companies
from companies.stock_actions import (
    InvalidStockActionError,
    StockActionNotFoundError,
    add_stock_action,
    delete_stock_action,
    list_stock_actions,
)


@pytest.fixture
def db_conn_with_companies(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    return db_conn


def test_add_and_list_split(db_conn_with_companies: sqlite3.Connection) -> None:
    action = add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", 1, 2)
    assert action["company_id"] == "HDFCBANK"
    assert action["action_type"] == "split"
    assert action["ratio_from"] == 1
    assert action["ratio_to"] == 2
    assert action["subscription_price"] is None

    actions = list_stock_actions(db_conn_with_companies, "HDFCBANK")
    assert len(actions) == 1
    assert actions[0]["action_id"] == action["action_id"]


def test_list_orders_most_recent_first(db_conn_with_companies: sqlite3.Connection) -> None:
    add_stock_action(db_conn_with_companies, "HDFCBANK", "bonus", "2020-01-01", 1, 2)
    add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", 1, 5)

    actions = list_stock_actions(db_conn_with_companies, "HDFCBANK")
    assert [a["action_date"] for a in actions] == ["2024-06-15", "2020-01-01"]


def test_rights_issue_with_subscription_price(db_conn_with_companies: sqlite3.Connection) -> None:
    action = add_stock_action(
        db_conn_with_companies, "HDFCBANK", "rights", "2023-03-01", 5, 6, subscription_price=250.0
    )
    assert action["subscription_price"] == 250.0


def test_subscription_price_rejected_for_non_rights_action(db_conn_with_companies: sqlite3.Connection) -> None:
    with pytest.raises(InvalidStockActionError, match="subscription_price"):
        add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", 1, 2, subscription_price=100.0)


def test_invalid_action_type_rejected(db_conn_with_companies: sqlite3.Connection) -> None:
    with pytest.raises(InvalidStockActionError, match="action_type"):
        add_stock_action(db_conn_with_companies, "HDFCBANK", "merger", "2024-06-15", 1, 2)


@pytest.mark.parametrize("bad_date", ["2024/06/15", "15-06-2024", "not-a-date", ""])
def test_invalid_action_date_rejected(db_conn_with_companies: sqlite3.Connection, bad_date: str) -> None:
    with pytest.raises(InvalidStockActionError, match="action_date"):
        add_stock_action(db_conn_with_companies, "HDFCBANK", "split", bad_date, 1, 2)


@pytest.mark.parametrize("ratio_from,ratio_to", [(0, 2), (1, 0), (-1, 2)])
def test_non_positive_ratio_rejected(db_conn_with_companies: sqlite3.Connection, ratio_from: float, ratio_to: float) -> None:
    with pytest.raises(InvalidStockActionError, match="ratio"):
        add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", ratio_from, ratio_to)


def test_delete_stock_action(db_conn_with_companies: sqlite3.Connection) -> None:
    action = add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", 1, 2)
    delete_stock_action(db_conn_with_companies, "HDFCBANK", action["action_id"])
    assert list_stock_actions(db_conn_with_companies, "HDFCBANK") == []


def test_delete_unknown_action_raises(db_conn_with_companies: sqlite3.Connection) -> None:
    with pytest.raises(StockActionNotFoundError):
        delete_stock_action(db_conn_with_companies, "HDFCBANK", 999)


def test_delete_scoped_to_company(db_conn_with_companies: sqlite3.Connection) -> None:
    """A stock action can't be deleted through a different company_id than
    the one it was recorded under — same defense-in-depth company_id/id
    pairing storage/repositories.py's company_notes CRUD already uses."""
    action = add_stock_action(db_conn_with_companies, "HDFCBANK", "split", "2024-06-15", 1, 2)
    with pytest.raises(StockActionNotFoundError):
        delete_stock_action(db_conn_with_companies, "ICICIBANK", action["action_id"])
