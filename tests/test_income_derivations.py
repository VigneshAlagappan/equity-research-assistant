from __future__ import annotations

import sqlite3

import pytest

from companies.registry import register_company
from web.charts_feed import build_charts_feed
from web.income_derivations import derive_income_rows, finalize_income_rows
from web.valuation_feed import build_valuation_feed

# Reliance Industries, consolidated Q1 FY27 (NSE XBRL), in crore.
RELIANCE_Q1 = {
    "total_revenue": 311850.0, "operating_expenses": 287770.0, "interest_expended": 8337.0,
    "depreciation": 15100.0, "profit_before_tax": 30630.0, "other_expenses": 45252.0, "other_income": 6550.0,
    "cost_of_materials_consumed": 129857.0, "purchases_of_stock_in_trade": 82833.0, "changes_in_inventories": -1326.0,
}


def _raw(values: dict[str, float], key="Q1") -> dict[str, dict]:
    keys = ("total_revenue", "operating_expenses", "interest_expended", "depreciation", "profit_before_tax",
            "other_expenses", "other_income", "cost_of_materials_consumed", "purchases_of_stock_in_trade", "changes_in_inventories")
    return {k: ({key: values[k]} if k in values else {}) for k in keys}


def test_reliance_ebitda_ebit_and_materials_cost_both_conventions() -> None:
    out = derive_income_rows(_raw(RELIANCE_Q1), ["Q1"])

    assert out["ebitda"]["Q1"] == pytest.approx(47517.0)          # operating, excl. other income
    assert out["ebit"]["Q1"] == pytest.approx(32417.0)
    assert out["ebitdaInclOther"]["Q1"] == pytest.approx(54067.0)  # operating + other income (6,550)
    assert out["ebitInclOther"]["Q1"] == pytest.approx(38967.0)
    assert out["materialsCost"]["Q1"] == pytest.approx(211364.0)   # materials + purchases + inventory change


def test_exceptional_items_do_not_leak_into_either_ebitda() -> None:
    """TCS FY2026: a one-off charge sits between expenses and PBT. PBT drops, EBITDA must not."""
    base = derive_income_rows(_raw(RELIANCE_Q1), ["Q1"])
    with_exceptional = derive_income_rows(_raw({**RELIANCE_Q1, "profit_before_tax": 30630.0 - 4500.0}), ["Q1"])

    assert with_exceptional == base
    assert base["ebitdaInclOther"]["Q1"] - base["ebitda"]["Q1"] == pytest.approx(6550.0)  # exactly other income


def test_legacy_period_without_the_xbrl_only_line_gets_no_derived_rows() -> None:
    values = {k: v for k, v in RELIANCE_Q1.items() if k != "other_expenses"}  # a Screener-era period

    out = derive_income_rows(_raw(values), ["Q1"])

    assert all(series == {} for series in out.values())


def test_missing_input_leaves_that_row_blank_not_zero() -> None:
    values = {k: v for k, v in RELIANCE_Q1.items() if k != "depreciation"}

    out = derive_income_rows(_raw(values), ["Q1"])

    assert out["ebitda"] == {} and out["ebitdaInclOther"] == {}
    assert out["materialsCost"]["Q1"] == pytest.approx(211364.0)  # doesn't need depreciation


def test_finalize_hides_non_financial_rows_for_banks_and_empty_added_rows() -> None:
    rows = [
        {"key": "earnings", "values": [None]},   # original row: kept even when empty
        {"key": "ebitda", "values": [1.0]},
        {"key": "employeeCost", "values": [None]},
        {"key": "materialsCost", "values": [0.0, 0.0]},   # a services company: all-zero row hidden
        {"key": "profitBeforeTax", "values": [5.0]},
        {"key": "taxExpense", "values": [None]},
    ]
    assert [r["key"] for r in finalize_income_rows(rows, is_financial=False)] == ["earnings", "ebitda", "profitBeforeTax"]
    assert [r["key"] for r in finalize_income_rows(rows, is_financial=True)] == ["earnings", "profitBeforeTax"]


def _canonical(conn: sqlite3.Connection, company: str, metric: str, fy: str, value: float) -> None:
    conn.execute(
        "INSERT INTO canonical_financials (company_id, metric_key, period_type, fiscal_year, quarter, statement_type, "
        "canonical_value, unit, reconciliation_reason, normalization_version, decided_at) "
        "VALUES (?, ?, 'annual', ?, NULL, 'consolidated', ?, 'INR_CRORE', 't', 'v1', '2026-01-01T00:00:00')",
        (company, metric, fy, value),
    )
    conn.commit()


@pytest.fixture
def conn(db_conn: sqlite3.Connection) -> sqlite3.Connection:
    register_company(db_conn, "INDCO", legal_name="Ind Co", display_name="Ind Co")
    for metric, value in RELIANCE_Q1.items():
        _canonical(db_conn, "INDCO", metric, "FY2025", value)
    return db_conn


def _income_rows(feed: dict) -> dict[str, list]:
    return {r["key"]: r["values"] for r in feed["METRICS"]["incomeStatement"]}


def test_charts_feed_income_statement_for_a_non_financial_company(conn: sqlite3.Connection) -> None:
    rows = _income_rows(build_charts_feed(conn, "INDCO"))

    assert rows["ebitda"] == [pytest.approx(47517.0)]
    assert rows["ebitdaInclOther"] == [pytest.approx(54067.0)]
    assert rows["profitBeforeTax"] == [30630.0]
    assert rows["materialsCost"] == [pytest.approx(211364.0)]
    assert "currentTax" not in rows  # no value for it: row hidden, not shown as dashes


def test_valuation_feed_has_the_same_rows(conn: sqlite3.Connection) -> None:
    rows = _income_rows(build_valuation_feed(conn, "INDCO"))

    assert rows["ebit"] == [pytest.approx(32417.0)]


def test_financial_companies_do_not_get_ebitda_rows(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE companies SET sector = 'Financial Services' WHERE company_id = 'INDCO'")
    conn.commit()

    rows = _income_rows(build_charts_feed(conn, "INDCO"))

    assert "ebitda" not in rows and "materialsCost" not in rows
    assert rows["profitBeforeTax"] == [30630.0]  # PBT stays for everyone


def test_us_gaap_period_derives_ebit_ebitda_and_other_income():
    from web.income_derivations import derive_income_rows

    k = (2024, 0)
    raw = {m: {} for m in (
        "total_revenue", "operating_expenses", "interest_expended", "depreciation", "other_income", "other_expenses",
        "cost_of_materials_consumed", "purchases_of_stock_in_trade", "changes_in_inventories", "cost_of_revenue",
        "selling_general_admin", "research_and_development", "depreciation_amortization", "operating_profit",
        "profit_before_tax")}
    raw["total_revenue"][k] = 1000.0
    raw["operating_profit"][k] = 300.0
    raw["cost_of_revenue"][k] = 600.0
    raw["depreciation_amortization"][k] = 50.0
    raw["profit_before_tax"][k] = 290.0
    raw["interest_expended"][k] = 20.0
    out = derive_income_rows(raw, [k])
    assert out["ebit"][k] == 300.0
    assert out["ebitda"][k] == 350.0
    assert out["expenses"][k] == 700.0
    assert out["otherIncome"][k] == 10.0  # 290 - 300 + 20
    assert out["ebitInclOther"][k] == 310.0
    assert out["ebitdaInclOther"][k] == 360.0
