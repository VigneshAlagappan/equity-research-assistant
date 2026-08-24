"""Chart generation tests — a chart with no underlying data must be skipped
(None), never a fabricated empty plot; a chart with data must actually render."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import pytest

from charts.financial_charts import (
    build_comparison_charts,
    build_company_charts,
    figure_to_base64_png,
    plot_advances_vs_deposits,
    plot_indexed_comparison,
    plot_metric_trend,
    plot_ratio_comparison,
    plot_ratio_trend,
    save_charts,
)
from companies.registry import seed_companies
from ingestion.pipeline import ingest_file
from storage.database import utcnow_iso
from tests.test_screener_adapter import _make_screener_workbook

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _insert_canonical(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    fiscal_year: str,
    value: float,
    unit: str = "INR_CRORE",
    statement_type: str = "consolidated",
) -> None:
    """Directly populate canonical_financials for a synthetic company_id —
    canonical_financials has no FK to companies (unlike financial_observations),
    so comparison-chart tests can control exactly which fiscal years each side
    of a comparison has, without round-tripping through a full Screener
    workbook per company."""
    conn.execute(
        """
        INSERT INTO canonical_financials (
            company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
            canonical_value, unit, chosen_observation_id, reconciliation_reason,
            normalization_version, decided_at
        ) VALUES (?, ?, 'annual', ?, NULL, ?, ?, ?, NULL, 'test fixture', 'v1', ?)
        """,
        (company_id, metric_key, fiscal_year, statement_type, value, unit, utcnow_iso()),
    )
    conn.commit()


@pytest.fixture
def ingested_conn(tmp_path: Path, db_conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_companies(db_conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(db_conn, file_path, company_id="HDFCBANK", source_id="screener")
    return db_conn


def test_plot_metric_trend_returns_none_without_data(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert plot_metric_trend(db_conn, "HDFCBANK", "net_profit", "Net Profit") is None


def test_plot_metric_trend_renders_with_data(ingested_conn: sqlite3.Connection) -> None:
    figure = plot_metric_trend(ingested_conn, "HDFCBANK", "net_profit", "Net Profit")
    try:
        assert figure is not None
        assert isinstance(figure, plt.Figure)
        ax = figure.axes[0]
        assert "Net Profit" in ax.get_title(loc="left")
        line = ax.get_lines()[0]
        assert list(line.get_ydata()) == [17000.0, 20500.0]
    finally:
        plt.close(figure)


def test_plot_ratio_trend_returns_none_without_enough_history(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    assert plot_ratio_trend(db_conn, "HDFCBANK") is None


def test_plot_ratio_trend_renders_roa_and_roe(ingested_conn: sqlite3.Connection) -> None:
    figure = plot_ratio_trend(ingested_conn, "HDFCBANK")
    try:
        assert figure is not None
        ax = figure.axes[0]
        assert len(ax.get_lines()) == 2  # ROA + ROE
        legend_labels = {t.get_text() for t in ax.get_legend().get_texts()}
        assert legend_labels == {"ROA", "ROE"}
    finally:
        plt.close(figure)


def test_plot_advances_vs_deposits_renders_both_series(ingested_conn: sqlite3.Connection) -> None:
    figure = plot_advances_vs_deposits(ingested_conn, "HDFCBANK")
    try:
        assert figure is not None
        ax = figure.axes[0]
        legend_labels = {t.get_text() for t in ax.get_legend().get_texts()}
        assert legend_labels == {"Advances", "Deposits"}
    finally:
        plt.close(figure)


def test_build_company_charts_omits_charts_with_no_data(db_conn: sqlite3.Connection) -> None:
    seed_companies(db_conn)
    charts = build_company_charts(db_conn, "HDFCBANK")
    assert charts == {}


def test_build_company_charts_returns_all_four_for_full_fixture(ingested_conn: sqlite3.Connection) -> None:
    charts = build_company_charts(ingested_conn, "HDFCBANK")
    try:
        assert set(charts.keys()) == {"net_profit", "total_assets", "roa_roe", "advances_vs_deposits"}
    finally:
        for figure in charts.values():
            plt.close(figure)


def test_save_charts_writes_png_files(tmp_path: Path, ingested_conn: sqlite3.Connection) -> None:
    charts = build_company_charts(ingested_conn, "HDFCBANK")
    output_dir = tmp_path / "charts_out"
    paths = save_charts(charts, output_dir)

    assert set(paths.keys()) == set(charts.keys())
    for chart_key, path in paths.items():
        assert path == output_dir / f"{chart_key}.png"
        assert path.read_bytes().startswith(_PNG_MAGIC)


def test_figure_to_base64_png_is_valid_png(ingested_conn: sqlite3.Connection) -> None:
    figure = plot_metric_trend(ingested_conn, "HDFCBANK", "net_profit", "Net Profit")
    encoded = figure_to_base64_png(figure)
    decoded = base64.b64decode(encoded)
    assert decoded.startswith(_PNG_MAGIC)


# ------------------------------------------------------------------
# Comparison charts — multiple companies, one chart, rebased to a common
# anchor over their overlapping period (not each company's own full history
# on its own scale).
# ------------------------------------------------------------------


def test_plot_indexed_comparison_rebases_to_100_at_common_start(db_conn: sqlite3.Connection) -> None:
    # COMPA starts at 200 -> 300 -> 400 (50% then 33% growth).
    # COMPB starts at 1000 -> 1500 -> 1800 (50% then 20% growth) — a very
    # different absolute scale, which is exactly why raw values wouldn't
    # compare meaningfully on one chart without rebasing.
    for year, value in [("FY2023", 200), ("FY2024", 300), ("FY2025", 400)]:
        _insert_canonical(db_conn, "COMPA", "net_profit", year, value)
    for year, value in [("FY2023", 1000), ("FY2024", 1500), ("FY2025", 1800)]:
        _insert_canonical(db_conn, "COMPB", "net_profit", year, value)

    figure = plot_indexed_comparison(db_conn, ["COMPA", "COMPB"], "net_profit", "Net Profit")
    try:
        assert figure is not None
        ax = figure.axes[0]
        assert "indexed to 100 at FY2023" in ax.get_title(loc="left")
        lines_by_label = {line.get_label(): line for line in ax.get_lines()}
        assert set(lines_by_label) == {"COMPA", "COMPB"}
        assert list(lines_by_label["COMPA"].get_ydata()) == pytest.approx([100.0, 150.0, 200.0])
        assert list(lines_by_label["COMPB"].get_ydata()) == pytest.approx([100.0, 150.0, 180.0])
    finally:
        plt.close(figure)


def test_plot_indexed_comparison_restricts_to_overlapping_years(db_conn: sqlite3.Connection) -> None:
    """COMPA has two extra early years COMPB doesn't — the chart must anchor
    at the first year *both* have data for for a fair like-for-like window,
    not COMPA's own longer, unmatched history."""
    for year, value in [("FY2017", 50), ("FY2018", 80), ("FY2019", 200), ("FY2020", 300)]:
        _insert_canonical(db_conn, "COMPA", "net_profit", year, value)
    for year, value in [("FY2019", 1000), ("FY2020", 1500)]:
        _insert_canonical(db_conn, "COMPB", "net_profit", year, value)

    figure = plot_indexed_comparison(db_conn, ["COMPA", "COMPB"], "net_profit", "Net Profit")
    try:
        assert figure is not None
        ax = figure.axes[0]
        assert "indexed to 100 at FY2019" in ax.get_title(loc="left")
        lines_by_label = {line.get_label(): line for line in ax.get_lines()}
        assert list(lines_by_label["COMPA"].get_ydata()) == pytest.approx([100.0, 150.0])  # 200 -> 300
        assert list(lines_by_label["COMPB"].get_ydata()) == pytest.approx([100.0, 150.0])  # 1000 -> 1500
    finally:
        plt.close(figure)


def test_plot_indexed_comparison_none_with_only_one_company(db_conn: sqlite3.Connection) -> None:
    _insert_canonical(db_conn, "COMPA", "net_profit", "FY2024", 100)
    assert plot_indexed_comparison(db_conn, ["COMPA"], "net_profit", "Net Profit") is None


def test_plot_indexed_comparison_none_without_year_overlap(db_conn: sqlite3.Connection) -> None:
    _insert_canonical(db_conn, "COMPA", "net_profit", "FY2020", 100)
    _insert_canonical(db_conn, "COMPB", "net_profit", "FY2024", 100)
    assert plot_indexed_comparison(db_conn, ["COMPA", "COMPB"], "net_profit", "Net Profit") is None


def test_plot_indexed_comparison_none_with_single_overlapping_year(db_conn: sqlite3.Connection) -> None:
    """One shared year isn't enough to show a trend line."""
    _insert_canonical(db_conn, "COMPA", "net_profit", "FY2023", 100)
    _insert_canonical(db_conn, "COMPA", "net_profit", "FY2024", 150)
    _insert_canonical(db_conn, "COMPB", "net_profit", "FY2024", 500)
    assert plot_indexed_comparison(db_conn, ["COMPA", "COMPB"], "net_profit", "Net Profit") is None


def test_plot_ratio_comparison_uses_common_years_no_rebasing(ingested_conn: sqlite3.Connection) -> None:
    """ROA/ROE are already percent, comparable across companies without
    rebasing — unlike plot_indexed_comparison, values are plotted as-is.
    ICICIBANK is synthetic canonical data (not a real ingested company) —
    HDFCBANK's own data comes from the ingested_conn fixture, which only
    covers FY2023/FY2024 and has no FY2022 total_assets, so HDFCBANK's own
    ROA is only computable for FY2024 (needs the prior year too) — add
    FY2022 so FY2023 becomes computable and there's a real overlap to test."""
    _insert_canonical(ingested_conn, "HDFCBANK", "total_assets", "FY2022", 2000000)

    _insert_canonical(ingested_conn, "ICICIBANK", "net_profit", "FY2023", 15000)
    _insert_canonical(ingested_conn, "ICICIBANK", "net_profit", "FY2024", 18000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_shareholders_funds", "FY2022", 180000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_shareholders_funds", "FY2023", 200000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_shareholders_funds", "FY2024", 225000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_assets", "FY2022", 1800000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_assets", "FY2023", 2000000)
    _insert_canonical(ingested_conn, "ICICIBANK", "total_assets", "FY2024", 2250000)

    figure = plot_ratio_comparison(ingested_conn, ["HDFCBANK", "ICICIBANK"], "roa")
    try:
        assert figure is not None
        ax = figure.axes[0]
        assert "ROA (%)" in ax.get_title(loc="left")
        legend_labels = {t.get_text() for t in ax.get_legend().get_texts()}
        assert legend_labels == {"HDFCBANK", "ICICIBANK"}
    finally:
        plt.close(figure)


def test_plot_ratio_comparison_none_with_only_one_company(ingested_conn: sqlite3.Connection) -> None:
    assert plot_ratio_comparison(ingested_conn, ["HDFCBANK"], "roa") is None


def test_build_comparison_charts_omits_metrics_without_overlap(db_conn: sqlite3.Connection) -> None:
    _insert_canonical(db_conn, "COMPA", "net_profit", "FY2020", 100)
    _insert_canonical(db_conn, "COMPB", "net_profit", "FY2024", 100)  # no overlap
    charts = build_comparison_charts(db_conn, ["COMPA", "COMPB"])
    assert charts == {}


def test_build_comparison_charts_includes_net_profit_with_overlap(db_conn: sqlite3.Connection) -> None:
    for year, value in [("FY2023", 100), ("FY2024", 150)]:
        _insert_canonical(db_conn, "COMPA", "net_profit", year, value)
        _insert_canonical(db_conn, "COMPB", "net_profit", year, value * 10)
    charts = build_comparison_charts(db_conn, ["COMPA", "COMPB"])
    try:
        assert "net_profit" in charts
    finally:
        for figure in charts.values():
            plt.close(figure)
