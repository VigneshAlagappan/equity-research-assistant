from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from sources.sec_edgar import SECEdgarAdapter


def _facts() -> dict:
    annual = {"start": "2023-01-01", "end": "2023-12-31", "val": 6.13, "form": "10-K", "filed": "2024-02-01", "fy": 2023, "fp": "FY"}
    basic = {**annual, "val": 6.16}
    return {"facts": {"us-gaap": {
        "EarningsPerShareDiluted": {"units": {"USD/shares": [annual]}},
        "EarningsPerShareBasic": {"units": {"USD/shares": [basic]}},
    }}}


def test_diluted_eps_is_a_separate_metric_and_existing_eps_is_unchanged(db_conn: sqlite3.Connection) -> None:
    register_company(db_conn, "AAPLX", legal_name="Apple Test", display_name="Apple Test", currency="USD", fiscal_year_end_month=12)

    observations = SECEdgarAdapter(db_conn).fetch("AAPLX", 1, currency="USD", facts=_facts())

    annual = {o.metric_key: o.value for o in observations if o.period_type == "annual"}
    assert annual["diluted_eps"] == pytest.approx(6.13)  # per-share dollars, not divided by 1e6
    assert "eps" in annual  # the original mapping still produces its row
