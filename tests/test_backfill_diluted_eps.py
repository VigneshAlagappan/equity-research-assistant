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
