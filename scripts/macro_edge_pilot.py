"""Run the macro candidate-edge pilot (research/macro_edge_pilot.py) against the
macro_observations and canonical_financials already on file and write the
results report. Read-only: no database writes, no LLM, no network except the
database itself.

  python -m scripts.macro_edge_pilot [--out docs/MACRO_EDGE_PILOT.md] [--perm 2000]
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from research.macro_edge_pilot import (  # noqa: E402
    CANDIDATES, fiscal_quarter_index, format_report, run_candidate,
)
from storage.backend_bootstrap import open_db  # noqa: E402


def _rows(conn, sql: str, params: tuple) -> list:
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def load_macro(conn, series_keys: set[str]) -> dict[str, list[tuple[str, float]]]:
    return {
        key: [(r["period"], r["value"]) for r in _rows(
            conn, "SELECT period, value FROM macro_observations WHERE series_key = %s AND region IS NULL", (key,))]
        for key in series_keys
    }


def load_company_quarterly(conn, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict[int, float]]:
    out: dict[tuple[str, str], dict[int, float]] = {}
    for company, metric in pairs:
        series: dict[int, float] = {}
        for r in _rows(
            conn,
            "SELECT fiscal_year, quarter, canonical_value FROM canonical_financials WHERE company_id = %s "
            "AND metric_key = %s AND period_type = 'quarterly' AND statement_type = 'consolidated'",
            (company, metric),
        ):
            idx = fiscal_quarter_index(r["fiscal_year"], r["quarter"])
            if idx is not None:
                series[idx] = r["canonical_value"]
        out[(company, metric)] = series
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="docs/MACRO_EDGE_PILOT.md")
    parser.add_argument("--perm", type=int, default=2000)
    args = parser.parse_args()

    conn = open_db()
    series_keys = {c.cause.series for c in CANDIDATES} | {c.effect_macro.series for c in CANDIDATES if c.effect_macro}
    pairs = {
        (company, metric)
        for c in CANDIDATES if c.kind == "company"
        for company in c.effect_companies
        for metric in (("net_profit", "total_revenue") if c.effect_metric == "margin" else (c.effect_metric,))
    }
    macro = load_macro(conn, series_keys)
    company = load_company_quarterly(conn, pairs)
    empty = sorted(k for k, v in macro.items() if not v)
    if empty:
        print("warning: no macro data for", empty, flush=True)
    results = []
    for cand in CANDIDATES:
        res = run_candidate(cand, macro, company, n_perm=args.perm)
        results.append((cand, res))
        print(f"{cand.edge_id:34s} periods={res.n_periods:3d} lag={res.best_lag} r={res.r_best} p={res.p_adjusted} {res.classification}", flush=True)
    Path(args.out).write_text(format_report(results, date.today().isoformat()))
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
