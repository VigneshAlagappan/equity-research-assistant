from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import scripts.backfill_diluted_eps as backfill
from companies.registry import register_company
from storage.repositories import get_canonical_series
from tests.test_nse_xbrl_adapter import _NS_CAPMKT, _make_xbrl


@pytest.fixture
def raw_dir(tmp_path: Path, db_conn: sqlite3.Connection, monkeypatch) -> Path:
    register_company(db_conn, "INFY", "Infosys Limited", "Infosys")
    root = tmp_path / "raw"
    nse = root / "INFY" / "nse"
    nse.mkdir(parents=True)
    src = _make_xbrl(
        tmp_path,
        {
            "RevenueFromOperations": "399570000000",
            "ProfitLossForPeriod": "72490000000",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "17.87",
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "17.80",
        },
        namespace=_NS_CAPMKT,
    )
    shutil.copy(src, nse / "2023-09-30_standalone_1.xml")
    monkeypatch.setattr(backfill.app_settings, "RAW_DIR", root)
    return root


def test_backfills_only_diluted_eps_and_is_idempotent(db_conn: sqlite3.Connection, raw_dir: Path) -> None:
    detail = backfill.backfill_company(db_conn, "INFY")
    assert "inserted=1" in detail

    diluted = get_canonical_series(db_conn, "INFY", "diluted_eps", "quarterly", "standalone")
    assert [r["canonical_value"] for r in diluted] == [pytest.approx(17.80)]
    # nothing else from the file was inserted (no eps / net_profit rows were created)
    assert get_canonical_series(db_conn, "INFY", "eps", "quarterly", "standalone") == []
    assert get_canonical_series(db_conn, "INFY", "net_profit", "quarterly", "standalone") == []

    again = backfill.backfill_company(db_conn, "INFY")
    assert "inserted=0" in again
    assert len(get_canonical_series(db_conn, "INFY", "diluted_eps", "quarterly", "standalone")) == 1


def test_filings_before_fy2022_are_skipped(db_conn: sqlite3.Connection, raw_dir: Path) -> None:
    (raw_dir / "INFY" / "nse" / "2023-09-30_standalone_1.xml").rename(raw_dir / "INFY" / "nse" / "2019-09-30_standalone_1.xml")

    detail = backfill.backfill_company(db_conn, "INFY")

    assert "files=0" in detail and "inserted=0" in detail


def test_income_statement_metrics_backfill_loads_expense_lines_and_is_idempotent(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    register_company(db_conn, "RELI", "Reliance Test", "Reliance Test")
    nse = tmp_path / "raw" / "RELI" / "nse"
    nse.mkdir(parents=True)
    src = _make_xbrl(
        tmp_path,
        {
            "RevenueFromOperations": "3118500000000",
            "FinanceCosts": "83370000000",
            "EmployeeBenefitExpense": "77170000000",
            "OtherExpenses": "452520000000",
            "CurrentTax": "46710000000",
            "DeferredTax": "29580000000",
        },
        namespace=_NS_CAPMKT,
    )
    shutil.copy(src, nse / "2023-09-30_consolidated_1.xml")
    monkeypatch.setattr(backfill.app_settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(backfill, "ACTIVE_METRICS", backfill.INCOME_STATEMENT_METRICS)

    detail = backfill.backfill_company(db_conn, "RELI")

    assert "inserted=5" in detail  # finance, employee, other expenses, current tax, deferred tax -- not revenue
    values = {
        m: get_canonical_series(db_conn, "RELI", m, "quarterly", "consolidated")[0]["canonical_value"]
        for m in ("interest_expended", "employee_benefit_expense", "other_expenses", "current_tax", "deferred_tax")
    }
    assert values == {
        "interest_expended": pytest.approx(8337.0), "employee_benefit_expense": pytest.approx(7717.0),
        "other_expenses": pytest.approx(45252.0), "current_tax": pytest.approx(4671.0), "deferred_tax": pytest.approx(2958.0),
    }
    assert get_canonical_series(db_conn, "RELI", "total_revenue", "quarterly", "consolidated") == []
    assert "inserted=0" in backfill.backfill_company(db_conn, "RELI")
