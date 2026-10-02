"""Macro -> sector/company candidate-edge pilot (read-only analysis).

Asks of each CANDIDATE edge (a macro driver and something it is believed to
affect): across the data on file, does the effect series move with the cause
series in the expected direction, at what lag, and is that stronger than chance?
It produces direction, best lag, sample size and an adjusted p-value -- nothing
else. It never labels an edge "causal": a statistical association over a short
history (13-17 quarters of Indian company data, 8-9 years of RBI series) is
evidence for investigation, not proof (ADR-009), and the report says so for
every row.

Method (numpy only, no LLM):
  * Both series are transformed to changes (YoY %, or YoY difference for rates)
    so a shared trend cannot masquerade as a relationship.
  * Effect series are z-scored per company and pooled (a "panel") so several
    peers contribute to one test; the cause leads the effect by `lag` periods.
  * The lag with the largest |Pearson r| is reported, and its significance is
    judged against a circular-shift permutation null that repeats the same lag
    search -- so p already accounts for having searched over lags, and for
    autocorrelation in the cause series.
  * Classification: DIRECTION_SUPPORTED / DIRECTION_CONTRADICTED when the
    adjusted p < 0.05 and the sign matches / opposes the expectation;
    NOT_DETECTED otherwise; INSUFFICIENT_DATA below 12 distinct periods.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

MIN_PERIODS = 12
ALPHA = 0.05


# ------------------------------------------------------------------
# Period helpers and transforms (periods are integer indices)
# ------------------------------------------------------------------

def month_index(date_str: str) -> int | None:
    try:
        return int(date_str[0:4]) * 12 + int(date_str[5:7]) - 1
    except (ValueError, IndexError):
        return None


def quarter_index_from_month(month_idx: int) -> int:
    return (month_idx // 12) * 4 + (month_idx % 12) // 3


def fiscal_quarter_index(fiscal_year: str, quarter: str) -> int | None:
    """March-year-end (India) fiscal quarter -> calendar quarter index of its end.
    FY2024 Q1 ends June 2023, Q4 ends March 2024."""
    try:
        fy = int(fiscal_year.removeprefix("FY"))
        q = int(quarter.removeprefix("Q"))
    except ValueError:
        return None
    end_month, end_year = {1: (6, fy - 1), 2: (9, fy - 1), 3: (12, fy - 1), 4: (3, fy)}.get(q, (None, None))
    if end_month is None:
        return None
    return end_year * 4 + (end_month - 1) // 3


def aggregate_mean(points: list[tuple[str, float]], freq: str) -> dict[int, float]:
    """Mean of observations per month ("M") or calendar quarter ("Q") bucket."""
    buckets: dict[int, list[float]] = {}
    for date_str, value in points:
        m = month_index(date_str)
        if m is None or value is None:
            continue
        key = m if freq == "M" else quarter_index_from_month(m)
        buckets.setdefault(key, []).append(float(value))
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def yoy(series: dict[int, float], periods: int, kind: str) -> dict[int, float]:
    """kind='pct' -> % change vs `periods` earlier; kind='diff' -> difference."""
    out = {}
    for t, v in series.items():
        prev = series.get(t - periods)
        if prev is None:
            continue
        if kind == "pct":
            if prev != 0:
                out[t] = (v / prev - 1.0) * 100.0
        else:
            out[t] = v - prev
    return out


def ratio(numerator: dict[int, float], denominator: dict[int, float]) -> dict[int, float]:
    return {t: numerator[t] / denominator[t] for t in numerator if t in denominator and denominator[t]}


# ------------------------------------------------------------------
# The test
# ------------------------------------------------------------------

@dataclass
class EdgeTestResult:
    n_pairs: int
    n_periods: int
    best_lag: int | None
    r_best: float | None
    r_by_lag: dict[int, float | None] = field(default_factory=dict)
    p_adjusted: float | None = None
    sign_matches: bool | None = None
    classification: str = "INSUFFICIENT_DATA"


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 5:
        return None
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return None
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))


def lagged_panel_test(
    cause: dict[int, float], effects: dict[str, dict[int, float]], lags: list[int], expected_sign: int,
    n_perm: int = 2000, seed: int = 0,
) -> EdgeTestResult:
    """Pooled lagged correlation with a lag-search-aware circular-shift permutation p-value."""
    pts_t: list[int] = []
    pts_y: list[float] = []
    for series in effects.values():
        if len(series) < 5:
            continue
        values = np.array(list(series.values()), dtype=float)
        std = values.std()
        if std == 0:
            continue
        mean = values.mean()
        for t, v in series.items():
            pts_t.append(t)
            pts_y.append((v - mean) / std)
    periods = sorted(set(pts_t))
    if not cause or len(periods) < MIN_PERIODS:
        return EdgeTestResult(n_pairs=len(pts_t), n_periods=len(periods), best_lag=None, r_best=None)

    t_arr = np.array(pts_t)
    y_arr = np.array(pts_y)
    cmin, cmax = min(cause), max(cause)
    length = cmax - cmin + 1
    cause_arr = np.full(length, np.nan)
    for t, v in cause.items():
        cause_arr[t - cmin] = v

    def r_by_lag(c: np.ndarray) -> dict[int, float | None]:
        out: dict[int, float | None] = {}
        for k in lags:
            idx = t_arr - k - cmin
            ok = (idx >= 0) & (idx < length)
            x = c[idx[ok]]
            keep = ~np.isnan(x)
            out[k] = _pearson(x[keep], y_arr[ok][keep]) if keep.sum() >= 5 else None
        return out

    observed = r_by_lag(cause_arr)
    valid = {k: r for k, r in observed.items() if r is not None}
    if not valid:
        return EdgeTestResult(n_pairs=len(pts_t), n_periods=len(periods), best_lag=None, r_best=None, r_by_lag=observed)
    best_lag = max(valid, key=lambda k: abs(valid[k]))
    stat = abs(valid[best_lag])

    rng = np.random.default_rng(seed)
    guard = max(lags) + 4
    candidates = [s for s in range(length) if guard <= s <= length - guard]
    if len(candidates) < 20:
        candidates = list(range(1, length))
    exceed = 0
    for s in rng.choice(candidates, size=n_perm, replace=True):
        null = r_by_lag(np.roll(cause_arr, int(s)))
        nulls = [abs(r) for r in null.values() if r is not None]
        if nulls and max(nulls) >= stat:
            exceed += 1
    p_adj = (1 + exceed) / (1 + n_perm)

    r_best = valid[best_lag]
    sign_ok = (r_best > 0) == (expected_sign > 0)
    if p_adj < ALPHA:
        classification = "DIRECTION_SUPPORTED" if sign_ok else "DIRECTION_CONTRADICTED"
    else:
        classification = "NOT_DETECTED"
    return EdgeTestResult(
        n_pairs=int(len(pts_t)), n_periods=len(periods), best_lag=best_lag, r_best=r_best, r_by_lag=observed,
        p_adjusted=p_adj, sign_matches=sign_ok, classification=classification,
    )


# ------------------------------------------------------------------
# Candidate edges (hand-written from domain knowledge, NOT LLM output)
# ------------------------------------------------------------------

BANKS = ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK"]
AUTOS = ["MARUTI", "M&M", "HEROMOTOCO", "EICHERMOT", "BAJAJAUTO"]
IT = ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"]


@dataclass(frozen=True)
class MacroSpec:
    series: str
    transform: str  # "pct" | "diff"


@dataclass(frozen=True)
class Candidate:
    edge_id: str
    cause: MacroSpec
    expected_sign: int  # +1 / -1
    mechanism: str
    #: "macro": effect is another macro series (monthly); "company": a quarterly panel
    kind: str
    effect_macro: MacroSpec | None = None
    effect_companies: tuple[str, ...] = ()
    effect_metric: str = ""  # canonical metric key, or "margin" (net_profit / total_revenue)
    lags: tuple[int, ...] = ()


CANDIDATES: list[Candidate] = [
    # --- macro -> macro, monthly (lags in months) ---
    Candidate("repo_to_base_rate", MacroSpec("policy_repo_rate", "diff"), +1, "Policy rate passes through to banks' base lending rate", "macro",
              effect_macro=MacroSpec("base_rate", "diff"), lags=tuple(range(0, 13))),
    Candidate("repo_to_gsec10", MacroSpec("policy_repo_rate", "diff"), +1, "Policy rate anchors the 10Y G-sec yield", "macro",
              effect_macro=MacroSpec("10_year_g_sec_yield_fbil", "diff"), lags=tuple(range(0, 13))),
    Candidate("repo_to_tbill91", MacroSpec("policy_repo_rate", "diff"), +1, "Policy rate drives short-term T-bill yields", "macro",
              effect_macro=MacroSpec("91_day_treasury_bill_primary_yield", "diff"), lags=tuple(range(0, 13))),
    Candidate("repo_to_bank_credit", MacroSpec("policy_repo_rate", "diff"), -1, "Higher borrowing cost slows credit growth", "macro",
              effect_macro=MacroSpec("bank_credit", "pct"), lags=tuple(range(0, 13))),
    Candidate("repo_to_m3", MacroSpec("policy_repo_rate", "diff"), -1, "Tighter policy slows broad-money growth", "macro",
              effect_macro=MacroSpec("m3", "pct"), lags=tuple(range(0, 13))),
    Candidate("us10y_to_gsec10", MacroSpec("dgs10", "diff"), +1, "US yields spill over into Indian G-sec yields", "macro",
              effect_macro=MacroSpec("10_year_g_sec_yield_fbil", "diff"), lags=tuple(range(0, 13))),
    Candidate("dxy_to_usdinr", MacroSpec("dtwexbgs", "pct"), +1, "A stronger dollar weakens the rupee", "macro",
              effect_macro=MacroSpec("usd_inr_reference_rate", "pct"), lags=tuple(range(0, 13))),
    Candidate("oil_to_usdinr", MacroSpec("dcoilwtico", "pct"), +1, "India imports oil: dearer crude raises dollar demand", "macro",
              effect_macro=MacroSpec("usd_inr_reference_rate", "pct"), lags=tuple(range(0, 13))),
    Candidate("vix_to_usdinr", MacroSpec("vixcls", "pct"), +1, "Risk-off episodes weaken the rupee", "macro",
              effect_macro=MacroSpec("usd_inr_reference_rate", "pct"), lags=tuple(range(0, 13))),
    # --- macro -> company panels, quarterly (lags in quarters) ---
    Candidate("repo_to_bank_interest_expense", MacroSpec("policy_repo_rate", "diff"), +1, "Higher policy rate raises banks' funding cost", "company",
              effect_companies=tuple(BANKS), effect_metric="interest_expended", lags=tuple(range(0, 7))),
    Candidate("repo_to_bank_interest_income", MacroSpec("policy_repo_rate", "diff"), +1, "Higher policy rate raises banks' lending yields", "company",
              effect_companies=tuple(BANKS), effect_metric="interest_earned", lags=tuple(range(0, 7))),
    Candidate("repo_to_auto_revenue", MacroSpec("policy_repo_rate", "diff"), -1, "Dearer vehicle finance dampens auto demand", "company",
              effect_companies=tuple(AUTOS), effect_metric="total_revenue", lags=tuple(range(0, 7))),
    Candidate("bankcredit_to_auto_revenue", MacroSpec("bank_credit", "pct"), +1, "Credit availability supports vehicle purchases", "company",
              effect_companies=tuple(AUTOS), effect_metric="total_revenue", lags=tuple(range(0, 7))),
    Candidate("usdinr_to_it_revenue", MacroSpec("usd_inr_reference_rate", "pct"), +1, "A weaker rupee lifts IT services' rupee revenue", "company",
              effect_companies=tuple(IT), effect_metric="total_revenue", lags=tuple(range(0, 7))),
    Candidate("oil_to_indigo_margin", MacroSpec("dcoilwtico", "pct"), -1, "Fuel is an airline's largest variable cost", "company",
              effect_companies=("INDIGO",), effect_metric="margin", lags=tuple(range(0, 7))),
    Candidate("oil_to_asianpaints_margin", MacroSpec("dcoilwtico", "pct"), -1, "Crude derivatives are key paint inputs", "company",
              effect_companies=("ASIANPAINT",), effect_metric="margin", lags=tuple(range(0, 7))),
]


def run_candidate(candidate: Candidate, macro_points: dict[str, list[tuple[str, float]]],
                  company_series: dict[tuple[str, str], dict[int, float]], n_perm: int = 2000) -> EdgeTestResult:
    """macro_points: series_key -> [(date, value)]; company_series: (company, metric) -> {quarter_idx: value}."""
    freq = "M" if candidate.kind == "macro" else "Q"
    periods_per_year = 12 if freq == "M" else 4

    def transformed(spec: MacroSpec) -> dict[int, float]:
        base = aggregate_mean(macro_points.get(spec.series, []), freq)
        return yoy(base, periods_per_year, spec.transform)

    cause = transformed(candidate.cause)
    if candidate.kind == "macro":
        effects = {candidate.effect_macro.series: transformed(candidate.effect_macro)}
    else:
        effects = {}
        for company in candidate.effect_companies:
            if candidate.effect_metric == "margin":
                base = ratio(company_series.get((company, "net_profit"), {}), company_series.get((company, "total_revenue"), {}))
                effects[company] = yoy(base, 4, "diff")
            else:
                effects[company] = yoy(company_series.get((company, candidate.effect_metric), {}), 4, "pct")
    return lagged_panel_test(cause, effects, list(candidate.lags), candidate.expected_sign, n_perm=n_perm)


def format_report(results: list[tuple[Candidate, EdgeTestResult]], generated_on: str) -> str:
    def cell(v, fmt):
        return "n/a" if v is None else format(v, fmt)

    lines = [
        "# Macro edge pilot results", "",
        f"Generated {generated_on} by `python -m scripts.macro_edge_pilot` (read-only; no database writes, no LLM). "
        "Candidate edges are hand-written from domain knowledge. **Nothing here is a causal claim**: each row is "
        "direction + lag + significance of an association over a short history (ADR-009: connectivity and "
        "correlation are evidence for investigation, not proof).", "",
        "Method: both series as YoY changes; effect series z-scored per company and pooled; cause leads effect by the "
        "lag; best lag by |r|; p-value from a circular-shift permutation test that repeats the lag search "
        "(so it already accounts for lag-picking and autocorrelation). Classified at p < 0.05; INSUFFICIENT_DATA "
        f"below {MIN_PERIODS} distinct periods.", "",
        "| Edge | Expected | Periods | Pairs | Best lag | r | p (adj.) | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cand, res in results:
        unit = "mo" if cand.kind == "macro" else "qtr"
        lag = "n/a" if res.best_lag is None else f"{res.best_lag} {unit}"
        lines.append(
            f"| {cand.edge_id} | {'+' if cand.expected_sign > 0 else '−'} | {res.n_periods} | {res.n_pairs} | {lag} | "
            f"{cell(res.r_best, '+.2f')} | {cell(res.p_adjusted, '.3f')} | {res.classification} |"
        )
    lines += ["", "## Mechanisms", ""]
    lines += [f"- **{c.edge_id}**: {c.mechanism} ({c.cause.series} → "
              f"{c.effect_macro.series if c.effect_macro else ', '.join(c.effect_companies) + ' ' + c.effect_metric})"
              for c, _ in results]
    return "\n".join(lines) + "\n"
