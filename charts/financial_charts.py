"""Matplotlib charts built from canonical_financials — wired into `analyze`
(README: Implementation Sequence, step 4) for both the CLI and the web viewer.

Same principle as the text report: every chart is built straight from
canonical_financials via the same repository/ratio functions the report
uses, so a chart and its report line can never disagree. A metric or ratio
with no data for this company just means that chart is skipped — never a
fabricated/empty plot.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless — no display available in a CLI or web worker

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from financials.calculations import MissingDataError
from financials.ratios import roa_for_company, roe_for_company
from storage.db_types import DBConnection
from storage.repositories import get_canonical_series

# Same palette as web/templates/base.html's design tokens, so a chart and the
# page it's embedded in read as one system rather than two different tools.
_INK = "#1e2a32"
_MUTED = "#5b6b72"
_GRID = "#dcdfe0"
_TEAL = "#2d6a6e"
_AMBER = "#9a6a00"
_FACE = "#ffffff"

# Per-line colors for any chart with more than one series, chosen for hue
# separation under color-vision deficiency, not just visual variety — blue,
# amber, and teal cluster too close together in the blue-green range for
# deuteranopia/protanopia, so this spreads across blue / amber / vermillion
# (red-orange) / plum instead, each far enough apart in hue to stay
# distinguishable even with red-green color blindness. Never the only
# signal, either: _MARKERS below gives every line a second, color-independent
# shape so the charts hold up in grayscale/print too (WCAG: don't encode
# information in color alone).
_LINE_COLORS = ["#2f5f8f", _AMBER, "#a8501f", "#6b4a63"]
_MARKERS = ["o", "s", "^", "D"]  # circle, square, triangle, diamond

_FIGSIZE = (7.5, 4.2)
_DPI = 130


def _style_axes(ax: plt.Axes, title: str) -> None:
    ax.set_title(title, fontsize=12, fontweight="bold", color=_INK, loc="left", pad=12)
    ax.set_facecolor(_FACE)
    ax.grid(True, axis="y", color=_GRID, linewidth=0.8, zorder=0)
    for spine_name, spine in ax.spines.items():
        spine.set_visible(spine_name == "bottom")
        if spine_name == "bottom":
            spine.set_color(_GRID)
    ax.tick_params(axis="both", colors=_MUTED, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f"{v:,.0f}"))


def plot_metric_trend(
    conn: DBConnection,
    company_id: str,
    metric_key: str,
    title: str,
    statement_type: str | None = "consolidated",
) -> plt.Figure | None:
    """Single metric's annual series as a line chart. None if there's no data."""
    series = get_canonical_series(conn, company_id, metric_key, "annual", statement_type)
    if not series:
        return None

    years = [row["fiscal_year"] for row in series]
    values = [row["canonical_value"] for row in series]
    unit = series[0]["unit"]

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    ax.plot(years, values, color=_TEAL, linewidth=2, marker="o", markersize=5, zorder=3)
    _style_axes(ax, f"{title} ({unit})")
    fig.tight_layout()
    return fig


def plot_ratio_trend(
    conn: DBConnection, company_id: str, statement_type: str | None = "consolidated"
) -> plt.Figure | None:
    """ROA and ROE together, both percent — same fiscal years the text report computes them for."""
    net_profit_years = [
        row["fiscal_year"] for row in get_canonical_series(conn, company_id, "net_profit", "annual", statement_type)
    ]

    years: list[str] = []
    roa_values: list[float] = []
    roe_values: list[float] = []
    for fiscal_year in net_profit_years:
        try:
            roa_result = roa_for_company(conn, company_id, fiscal_year, statement_type=statement_type)
            roe_result = roe_for_company(conn, company_id, fiscal_year, statement_type=statement_type)
        # ValueError too: roa()/roe() raise it for a degenerate (<=0) denominator, which
        # real ingested data can produce (e.g. a genuine 0.0 total_assets for early years).
        except (MissingDataError, ValueError):
            continue  # no prior-year balance sheet figure to average against
        years.append(fiscal_year)
        roa_values.append(roa_result.value)
        roe_values.append(roe_result.value)

    if not years:
        return None

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    ax.plot(years, roa_values, color=_LINE_COLORS[0], linewidth=2, marker=_MARKERS[0], markersize=5, label="ROA", zorder=3)
    ax.plot(years, roe_values, color=_LINE_COLORS[1], linewidth=2, marker=_MARKERS[1], markersize=5, label="ROE", zorder=3)
    _style_axes(ax, "ROA vs ROE (%)")
    ax.legend(frameon=False, fontsize=9, labelcolor=_INK, loc="upper left")
    fig.tight_layout()
    return fig


def plot_indexed_comparison(
    conn: DBConnection,
    company_ids: list[str],
    metric_key: str,
    title: str,
    statement_type: str | None = "consolidated",
) -> plt.Figure | None:
    """One metric, multiple companies, rebased to 100 at their common overlapping
    start year — a fair like-for-like growth comparison instead of each
    company's own raw values plotted on whatever scale it happens to be at
    (a bank with 10x the balance sheet size dwarfs the other's line
    otherwise). Only years every company has data for are plotted — the
    overlap, not each company's full separate history at a different length,
    the exact "not strictly like-for-like" gap a real comparison answer
    flagged as its own caveat before this existed.
    """
    per_company_series: dict[str, dict[str, float]] = {}
    for company_id in company_ids:
        series = get_canonical_series(conn, company_id, metric_key, "annual", statement_type)
        if series:
            per_company_series[company_id] = {row["fiscal_year"]: row["canonical_value"] for row in series}

    if len(per_company_series) < 2:
        return None  # need at least 2 companies with data for a comparison

    common_years = set.intersection(*(set(values.keys()) for values in per_company_series.values()))
    if len(common_years) < 2:
        return None  # not enough overlap to show a trend

    ordered_years = sorted(common_years)  # "FY2024" < "FY2025" sorts correctly as text
    anchor_year = ordered_years[0]

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    for i, (company_id, values_by_year) in enumerate(per_company_series.items()):
        anchor_value = values_by_year[anchor_year]
        if anchor_value == 0:
            continue  # can't index off a zero base
        indexed = [values_by_year[year] / anchor_value * 100 for year in ordered_years]
        ax.plot(
            ordered_years, indexed, color=_LINE_COLORS[i % len(_LINE_COLORS)],
            linewidth=2, marker=_MARKERS[i % len(_MARKERS)], markersize=5, label=company_id, zorder=3,
        )

    if not ax.lines:
        plt.close(fig)
        return None

    _style_axes(ax, f"{title} — indexed to 100 at {anchor_year}")
    ax.legend(frameon=False, fontsize=9, labelcolor=_INK, loc="upper left")
    fig.tight_layout()
    return fig


def plot_ratio_comparison(
    conn: DBConnection,
    company_ids: list[str],
    ratio: str,  # "roa" | "roe"
    statement_type: str | None = "consolidated",
) -> plt.Figure | None:
    """One ratio (already a percent, so no rebasing needed — it's comparable
    across companies as-is), multiple companies, restricted to the fiscal
    years every company has a computable value for."""
    compute = {"roa": roa_for_company, "roe": roe_for_company}[ratio]

    per_company_values: dict[str, dict[str, float]] = {}
    for company_id in company_ids:
        fiscal_years = [
            row["fiscal_year"]
            for row in get_canonical_series(conn, company_id, "net_profit", "annual", statement_type)
        ]
        values_by_year: dict[str, float] = {}
        for fiscal_year in fiscal_years:
            try:
                result = compute(conn, company_id, fiscal_year, statement_type=statement_type)
            # ValueError too: roa()/roe() raise it for a degenerate (<=0) denominator, which
            # real ingested data can produce (e.g. a genuine 0.0 total_assets for early years).
            except (MissingDataError, ValueError):
                continue
            values_by_year[fiscal_year] = result.value
        if values_by_year:
            per_company_values[company_id] = values_by_year

    if len(per_company_values) < 2:
        return None

    common_years = set.intersection(*(set(values.keys()) for values in per_company_values.values()))
    if len(common_years) < 2:
        return None

    ordered_years = sorted(common_years)

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    for i, (company_id, values_by_year) in enumerate(per_company_values.items()):
        ax.plot(
            ordered_years, [values_by_year[year] for year in ordered_years],
            color=_LINE_COLORS[i % len(_LINE_COLORS)],
            linewidth=2, marker=_MARKERS[i % len(_MARKERS)], markersize=5, label=company_id, zorder=3,
        )
    _style_axes(ax, f"{ratio.upper()} (%) — common period")
    ax.legend(frameon=False, fontsize=9, labelcolor=_INK, loc="upper left")
    fig.tight_layout()
    return fig


_COMPARISON_CHART_BUILDERS = [
    ("net_profit", lambda conn, ids, st: plot_indexed_comparison(conn, ids, "net_profit", "Net Profit", st)),
    ("total_assets", lambda conn, ids, st: plot_indexed_comparison(conn, ids, "total_assets", "Total Assets", st)),
    ("roa", lambda conn, ids, st: plot_ratio_comparison(conn, ids, "roa", st)),
    ("roe", lambda conn, ids, st: plot_ratio_comparison(conn, ids, "roe", st)),
]


def build_comparison_charts(
    conn: DBConnection, company_ids: list[str], statement_type: str | None = "consolidated"
) -> dict[str, plt.Figure]:
    """Every comparison chart with enough overlapping data across company_ids,
    keyed by chart_key. Empty/insufficient-overlap charts are omitted."""
    charts: dict[str, plt.Figure] = {}
    for chart_key, builder in _COMPARISON_CHART_BUILDERS:
        figure = builder(conn, company_ids, statement_type)
        if figure is not None:
            charts[chart_key] = figure
    return charts


def plot_advances_vs_deposits(
    conn: DBConnection, company_id: str, statement_type: str | None = "consolidated"
) -> plt.Figure | None:
    """Advances (loans) vs Deposits, annual — directly serves the "loan/deposit
    growth" trend for a single-company deep dive."""
    advances = get_canonical_series(conn, company_id, "advances", "annual", statement_type)
    deposits = get_canonical_series(conn, company_id, "deposits", "annual", statement_type)
    if not advances and not deposits:
        return None

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    unit = (advances or deposits)[0]["unit"]
    if advances:
        ax.plot(
            [r["fiscal_year"] for r in advances], [r["canonical_value"] for r in advances],
            color=_LINE_COLORS[0], linewidth=2, marker=_MARKERS[0], markersize=5, label="Advances", zorder=3,
        )
    if deposits:
        ax.plot(
            [r["fiscal_year"] for r in deposits], [r["canonical_value"] for r in deposits],
            color=_LINE_COLORS[1], linewidth=2, marker=_MARKERS[1], markersize=5, label="Deposits", zorder=3,
        )
    _style_axes(ax, f"Advances vs Deposits ({unit})")
    ax.legend(frameon=False, fontsize=9, labelcolor=_INK, loc="upper left")
    fig.tight_layout()
    return fig


# chart_key -> builder. Order here is display order for both the CLI's saved
# filenames and the web viewer's chart grid.
_CHART_BUILDERS = [
    ("net_profit", lambda conn, cid, st: plot_metric_trend(conn, cid, "net_profit", "Net Profit", st)),
    ("total_assets", lambda conn, cid, st: plot_metric_trend(conn, cid, "total_assets", "Total Assets", st)),
    ("roa_roe", plot_ratio_trend),
    ("advances_vs_deposits", plot_advances_vs_deposits),
]


def build_company_charts(
    conn: DBConnection, company_id: str, statement_type: str | None = "consolidated"
) -> dict[str, plt.Figure]:
    """Every chart with data for this company, keyed by chart_key. Empty charts are omitted, not returned blank."""
    charts: dict[str, plt.Figure] = {}
    for chart_key, builder in _CHART_BUILDERS:
        figure = builder(conn, company_id, statement_type)
        if figure is not None:
            charts[chart_key] = figure
    return charts


def save_charts(figures: dict[str, plt.Figure], output_dir: Path) -> dict[str, Path]:
    """Save each figure as a PNG under output_dir. Closes every figure (Agg backend doesn't free memory otherwise)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for chart_key, figure in figures.items():
        path = output_dir / f"{chart_key}.png"
        figure.savefig(path, facecolor=_FACE)
        plt.close(figure)
        paths[chart_key] = path
    return paths


def figure_to_base64_png(figure: plt.Figure) -> str:
    """Render a figure to a base64 PNG string (for a data: URI) and close it."""
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=_FACE)
    plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
