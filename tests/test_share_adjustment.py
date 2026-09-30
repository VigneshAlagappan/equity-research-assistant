from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from companies.registry import register_company
from web.share_adjustment import restate_to_latest_share_basis, share_multiplier


@pytest.mark.parametrize(
    "action_type, subject, expected",
    [
        ("bonus", " Bonus 1:1", 2.0),
        ("bonus", "Bonus 1:2", 1.5),
        ("fv_split", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", 10.0),
        ("fv_split", "Face Value Split From Rs.10/- To Rs.2/-", 5.0),
        ("fv_split", "Face Value Split Rs 10 To Rs 5", 2.0),
        ("bonus", "Bonus", None),
        ("dividend", "Dividend - Rs 5 Per Share", None),
    ],
)
def test_share_multiplier(action_type: str, subject: str, expected: float | None) -> None:
    assert share_multiplier(action_type, subject) == expected


def _seed(conn: sqlite3.Connection, subject: str = "Bonus 1:1") -> None:
    register_company(conn, "SPLITCO", legal_name="Split Co", display_name="Split Co")
    conn.execute(
        "INSERT INTO corporate_actions_raw (company_id, subject, ex_date, raw_json, retrieved_at) "
        "VALUES ('SPLITCO', ?, '2024-08-01', '{}', 'x')", (subject,),
    )
    conn.execute(
        "INSERT INTO corporate_actions (raw_id, company_id, action_type, subject, ex_date, classifier_version, created_at) "
        "SELECT raw_id, company_id, 'bonus', subject, ex_date, 'v3', 'x' FROM corporate_actions_raw"
    )
    conn.commit()


ENDS = {(2023, 0): date(2023, 3, 31), (2024, 0): date(2024, 3, 31), (2025, 0): date(2025, 3, 31)}


def test_restates_earlier_years_when_share_count_jumped(db_conn: sqlite3.Connection) -> None:
    _seed(db_conn)
    raw = {
        "eps": {(2023, 0): 20.0, (2024, 0): 24.0, (2025, 0): 13.0},
        "shares_outstanding": {(2023, 0): 100.0, (2024, 0): 100.0, (2025, 0): 200.0},
    }
    changed = restate_to_latest_share_basis(db_conn, "SPLITCO", ENDS, raw)
    assert raw["eps"] == {(2023, 0): 10.0, (2024, 0): 12.0, (2025, 0): 13.0}
    assert raw["shares_outstanding"] == {(2023, 0): 200.0, (2024, 0): 200.0, (2025, 0): 200.0}
    assert changed == {"eps", "shares_outstanding"}


def test_skips_when_filings_are_already_on_the_new_basis(db_conn: sqlite3.Connection) -> None:
    _seed(db_conn)
    raw = {
        "eps": {(2023, 0): 10.0, (2024, 0): 12.0, (2025, 0): 13.0},
        "shares_outstanding": {(2023, 0): 200.0, (2024, 0): 200.0, (2025, 0): 200.0},
    }
    assert restate_to_latest_share_basis(db_conn, "SPLITCO", ENDS, raw) == set()
    assert raw["eps"][(2023, 0)] == 10.0


def test_unparseable_ratio_leaves_history_untouched(db_conn: sqlite3.Connection) -> None:
    _seed(db_conn, subject="Bonus")
    raw = {"eps": {(2023, 0): 20.0}, "shares_outstanding": {(2023, 0): 100.0, (2025, 0): 200.0}}
    assert restate_to_latest_share_basis(db_conn, "SPLITCO", ENDS, raw) == set()
    assert raw["eps"][(2023, 0)] == 20.0
