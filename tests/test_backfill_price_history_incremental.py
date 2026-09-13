"""scripts/backfill_price_history.py::run_price_history_backfill() --
incremental deepening (gap-fetch via fetch_daily_bars's new `end` param)
and per-run time budgeting/resume, added to support pulling up to N years
of history gradually across scheduled runs instead of one long pull."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from companies.registry import seed_companies
from scripts.backfill_price_history import run_price_history_backfill
from sources.yfinance_prices import PriceBar
from storage.company_repository import tag_companies_index
from storage.price_database import init_price_db
from storage.price_repository import get_price_history, upsert_daily_bars


@pytest.fixture
def price_conn(tmp_path: Path):
    conn = init_price_db(db_path=tmp_path / "prices.db")
    yield conn
    conn.close()


def _bars(*dates_and_closes: tuple[str, float]) -> list[PriceBar]:
    return [
        PriceBar(trade_date=d, open=c, high=c + 1, low=c - 1, close=c, volume=1000)
        for d, c in dates_and_closes
    ]


def test_company_with_no_prior_data_gets_full_pull_no_end(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK"], "Nifty 500")
    calls = []

    def _fetch(symbol, period=None, start=None, end=None, country="IN"):
        calls.append({"symbol": symbol, "start": start, "end": end})
        return _bars(("2020-01-02", 100.0))

    monkeypatch.setattr("scripts.backfill_price_history.fetch_daily_bars", _fetch)

    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
    )

    assert len(calls) == 1
    assert calls[0]["end"] is None, "a company with nothing on file must get the full pull, no bounded end"
    assert calls[0]["start"] is not None


def test_company_with_partial_coverage_gets_gap_fetch_only(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK"], "Nifty 500")
    # Pretend a previous --years 3 run already landed data back to 2023-01-01.
    upsert_daily_bars(price_conn, [
        dict(company_id="HDFCBANK", trade_date="2023-01-01", open_=90.0, high=91.0, low=89.0, close=90.0, volume=500),
        dict(company_id="HDFCBANK", trade_date="2026-09-10", open_=100.0, high=101.0, low=99.0, close=100.0, volume=500),
    ])

    calls = []

    def _fetch(symbol, period=None, start=None, end=None, country="IN"):
        calls.append({"symbol": symbol, "start": start, "end": end})
        return _bars(("2019-01-02", 80.0))

    monkeypatch.setattr("scripts.backfill_price_history.fetch_daily_bars", _fetch)

    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
    )

    assert len(calls) == 1
    twenty_years_ago = date(date.today().year - 20, date.today().month, date.today().day).isoformat()
    assert calls[0]["start"] == twenty_years_ago
    assert calls[0]["end"] == "2023-01-01", "must fetch only the missing OLDER gap, bounded by what's already on file"

    # The gap-filled bar landed alongside what was already there -- existing rows untouched.
    history = get_price_history(price_conn, "HDFCBANK", "2019-01-01", "2026-12-31")
    assert {row["trade_date"] for row in history} == {"2019-01-02", "2023-01-01", "2026-09-10"}


def test_fully_covered_company_is_skipped_no_fetch_call(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK"], "Nifty 500")
    twenty_years_ago = date(date.today().year - 20, date.today().month, date.today().day).isoformat()
    upsert_daily_bars(price_conn, [
        dict(company_id="HDFCBANK", trade_date=twenty_years_ago, open_=10.0, high=11.0, low=9.0, close=10.0, volume=100),
    ])

    calls = []
    monkeypatch.setattr(
        "scripts.backfill_price_history.fetch_daily_bars",
        lambda *a, **k: calls.append(k) or [],
    )

    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
    )

    assert calls == [], "a company already covered back to the target must never be fetched"


def test_time_budget_defers_remaining_companies(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)  # HDFCBANK, ICICIBANK
    tag_companies_index(db_conn, ["HDFCBANK", "ICICIBANK"], "Nifty 500")

    call_order = []

    def _fetch(symbol, period=None, start=None, end=None, country="IN"):
        call_order.append(symbol)
        return _bars(("2020-01-02", 100.0))

    monkeypatch.setattr("scripts.backfill_price_history.fetch_daily_bars", _fetch)
    # A budget of 0 seconds must expire before the very first company is processed.
    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
        time_budget_seconds=0,
    )

    assert call_order == [], "a zero time budget must defer every company, none fetched this run"


def test_resume_next_run_reaches_previously_deferred_company(db_conn, price_conn, monkeypatch) -> None:
    seed_companies(db_conn)
    tag_companies_index(db_conn, ["HDFCBANK", "ICICIBANK"], "Nifty 500")

    fetched = []

    def _fetch(symbol, period=None, start=None, end=None, country="IN"):
        fetched.append(symbol)
        return _bars(("2020-01-02", 100.0))

    monkeypatch.setattr("scripts.backfill_price_history.fetch_daily_bars", _fetch)

    # "This week": budget only large enough for the first company (sleeps between
    # companies dominate the elapsed time in the real implementation; a budget of
    # 0 defers everything, so instead simulate "already reached company 1" by
    # giving an ample budget once, then a zero budget on a fresh, empty run to
    # prove nothing is silently skipped forever -- the real resume guarantee is
    # that an unfetched company's coverage state is simply unchanged, so the
    # very next unrestricted run reaches it.
    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
        time_budget_seconds=0,
    )
    assert fetched == []

    # "Next week": no budget -- both companies, still uncovered, get fetched.
    run_price_history_backfill(
        main_conn=db_conn, price_conn=price_conn, index_name="Nifty 500", years=20, job_name="test_ph",
    )
    assert set(fetched) == {"HDFCBANK", "ICICIBANK"}
