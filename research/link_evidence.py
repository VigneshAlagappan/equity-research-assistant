"""Data-first link checks (docs/L5_MVP_TASK_PLAN.md, option A): for a hypothesis's
causal chain, test each LINK against the company's own reported financials --
no LLM, no retrieval.

A chain step such as "Input cost per vehicle falls" names a measurable concept
(material cost as a share of revenue) and a direction (down). When both ends of a
link map to concepts we can compute from `canonical_financials`, the link is
checked: did the cause concept move as the chain says, and did the effect concept
move as the chain says, over the last few reported fiscal years?

  both moved as stated        -> a SUPPORTING item (a consistent-movement check)
  either moved the other way  -> a CONTRADICTING item
  either flat / no data / the step names no measurable concept -> nothing

That is an association over a handful of annual points, never proof of causation
(ADR-009), and the evidence label says "computed" and shows the numbers so a
reader can judge it. Concept matching is keyword-based and deliberately narrow: vague phrases
("cost base", "fixed cost", "discounting") are NOT mapped, because a wrong
proxy produces misleading evidence and no evidence is better than that. Some
matches are proxies (e.g. "commodity prices" is checked through material cost as a share of
revenue); a proxy is labelled as such. Banks and other financials are skipped:
their cost structure is a different taxonomy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from research.hypothesis_evaluator import EvidenceItem

WINDOW_YEARS = 4  # most recent fiscal years considered
MIN_POINTS = 3
LEVEL_THRESHOLD = 0.02  # relative change counted as movement for level concepts
RATIO_THRESHOLD_PP = 0.3  # percentage-point change counted as movement for ratio concepts

_UP = ("rise", "rises", "rising", "rose", "increase", "increases", "increased", "increasing", "up", "expand",
       "expands", "expanded", "expansion", "improve", "improves", "improved", "higher", "grow", "grows", "growth",
       "surge", "accelerate", "accelerates", "strengthen", "strengthens", "recover", "recovers", "gain", "gains")
_DOWN = ("fall", "falls", "falling", "fell", "decline", "declines", "declined", "declining", "down", "compress",
         "compresses", "compressed", "compression", "decrease", "decreases", "decreased", "lower", "shrink", "shrinks",
         "drop", "drops", "deteriorate", "deteriorates", "weaken", "weakens", "soften", "softens", "softening",
         "slow", "slows", "slowdown", "contract", "contracts", "erode", "erodes")
_WORD = re.compile(r"[a-z&]+")

Series = dict[int, float]  # fiscal year (int) -> value


@dataclass(frozen=True)
class Concept:
    key: str
    label: str
    patterns: tuple[str, ...]  # regexes on the lower-cased step text, most specific concepts listed first
    kind: str  # "ratio" (pp of revenue) or "level" (relative change)
    proxy: bool
    compute: Callable[["Loader"], Series]


class Loader:
    """Annual canonical series for one company. `get(metric)` -> {year: value}."""

    def __init__(self, fetch: Callable[[str], Series], is_us: bool) -> None:
        self._fetch, self.is_us = fetch, is_us
        self._cache: dict[str, Series] = {}

    def get(self, metric: str) -> Series:
        if metric not in self._cache:
            self._cache[metric] = self._fetch(metric)
        return self._cache[metric]


def _share(numerator: Series, revenue: Series) -> Series:
    return {y: numerator[y] / revenue[y] * 100.0 for y in numerator if revenue.get(y)}


def _sum(*series: Series) -> Series:
    years = set().union(*[set(s) for s in series]) if series else set()
    return {y: sum(s.get(y, 0.0) for s in series) for y in years if any(y in s for s in series)}


def _material_share(l: Loader) -> Series:
    revenue = l.get("total_revenue")
    if l.is_us:
        return _share(l.get("cost_of_revenue"), revenue)
    parts = [l.get(m) for m in ("cost_of_materials_consumed", "purchases_of_stock_in_trade", "changes_in_inventories")]
    if not (parts[0] or parts[1]):
        return {}
    return _share(_sum(*parts), revenue)


def _employee_share(l: Loader) -> Series:
    return _share(l.get("employee_benefit_expense"), l.get("total_revenue"))


def _other_cost_share(l: Loader) -> Series:
    if l.is_us:
        return _share(_sum(l.get("selling_general_admin"), l.get("research_and_development")), l.get("total_revenue"))
    return _share(l.get("other_expenses"), l.get("total_revenue"))


def _finance_cost_share(l: Loader) -> Series:
    return _share(l.get("interest_expended"), l.get("total_revenue"))


def _operating_margin(l: Loader) -> Series:
    """EBITDA margin using the same derivation the Financials tab shows."""
    from web.income_derivations import derive_income_rows

    metrics = ("total_revenue", "operating_expenses", "interest_expended", "depreciation", "other_income",
               "other_expenses", "cost_of_materials_consumed", "purchases_of_stock_in_trade", "changes_in_inventories",
               "cost_of_revenue", "selling_general_admin", "research_and_development", "depreciation_amortization",
               "operating_profit", "profit_before_tax")
    raw = {m: l.get(m) for m in metrics}
    years = sorted(raw["total_revenue"])
    out = derive_income_rows(raw, years)
    return _share(out["ebitda"], raw["total_revenue"])


def _gross_margin(l: Loader) -> Series:
    share = _material_share(l)
    return {y: 100.0 - v for y, v in share.items()}


def _net_margin(l: Loader) -> Series:
    return _share(l.get("net_profit"), l.get("total_revenue"))


# Order matters: the first matching concept wins, so specific phrases come before generic ones.
CONCEPTS: tuple[Concept, ...] = (
    Concept("gross_margin", "gross margin", (r"gross margin",), "ratio", False, _gross_margin),
    Concept("net_margin", "net margin", (r"net (profit )?margin",), "ratio", False, _net_margin),
    Concept("operating_margin", "operating (EBITDA) margin",
            (r"operating (profit )?margin", r"ebitda margin", r"operating profitability", r"\bmargins?\b"),
            "ratio", False, _operating_margin),
    Concept("material_cost", "material cost as % of revenue",
            (r"raw material", r"material cost", r"input cost", r"cost of (goods|materials|revenue)", r"\bcogs\b",
             r"commodity", r"steel", r"procurement cost"),
            "ratio", True, _material_share),
    Concept("employee_cost", "employee cost as % of revenue",
            (r"employee", r"wage", r"labou?r cost", r"personnel", r"headcount", r"manpower"), "ratio", False, _employee_share),
    Concept("other_cost", "other operating cost as % of revenue",
            (r"other expense", r"\bopex\b", r"operating (expense|cost)", r"overhead", r"sg&a", r"\br&d\b",
             r"selling (and|&) distribution"),
            "ratio", True, _other_cost_share),
    Concept("finance_cost", "finance cost as % of revenue",
            (r"finance cost", r"interest cost", r"interest expense", r"borrowing cost"), "ratio", False, _finance_cost_share),
    Concept("revenue", "revenue", (r"revenue", r"net sales", r"\bsales\b", r"demand", r"volume", r"top.?line"),
            "level", True, lambda l: l.get("total_revenue")),
    Concept("net_profit", "net profit", (r"net profit", r"\bprofit\b", r"earnings", r"profitability"),
            "level", False, lambda l: l.get("net_profit")),
)


def match_concept(step: str) -> Concept | None:
    text = (step or "").lower()
    for concept in CONCEPTS:
        if any(re.search(p, text) for p in concept.patterns):
            return concept
    return None


def stated_direction(step: str) -> int | None:
    """+1 / -1 from the step's wording, None when absent or contradictory."""
    words = set(_WORD.findall((step or "").lower()))
    up, down = bool(words & set(_UP)), bool(words & set(_DOWN))
    if up == down:
        return None
    return 1 if up else -1


def realized_direction(series: Series, kind: str) -> tuple[int, int, int, float, float] | None:
    """(direction, first_year, last_year, first_value, last_value) over the last
    WINDOW_YEARS reported years, direction 0 = flat; None when too few points."""
    years = sorted(series)[-WINDOW_YEARS:]
    if len(years) < MIN_POINTS:
        return None
    first, last = series[years[0]], series[years[-1]]
    if kind == "ratio":
        moved = abs(last - first) >= RATIO_THRESHOLD_PP
    else:
        moved = first != 0 and abs(last / first - 1.0) >= LEVEL_THRESHOLD
    direction = (1 if last > first else -1) if moved else 0
    return direction, years[0], years[-1], first, last


def _fmt(concept: Concept, first: float, last: float) -> str:
    if concept.kind == "ratio":
        return f"{first:.1f}% -> {last:.1f}%"
    return f"{first:,.0f} -> {last:,.0f}"


def check_links(steps: list[str], loader: Loader, company_label: str) -> list[tuple[str, EvidenceItem]]:
    """[(stance, item)] for each testable link; stance is 'supporting' or 'contradicting'."""
    out: list[tuple[str, EvidenceItem]] = []
    usable = [s for s in steps if isinstance(s, str) and s.strip()]
    for link in range(len(usable) - 1):
        cause_step, effect_step = usable[link], usable[link + 1]
        cause, effect = match_concept(cause_step), match_concept(effect_step)
        cause_dir, effect_dir = stated_direction(cause_step), stated_direction(effect_step)
        if cause is None or effect is None or cause_dir is None or effect_dir is None or cause.key == effect.key:
            continue
        try:
            cause_real = realized_direction(cause.compute(loader), cause.kind)
            effect_real = realized_direction(effect.compute(loader), effect.kind)
        except Exception:  # noqa: BLE001 -- a missing metric just means no check, never an error
            continue
        if cause_real is None or effect_real is None or cause_real[0] == 0 or effect_real[0] == 0:
            continue
        stated_ok = cause_real[0] == cause_dir and effect_real[0] == effect_dir
        proxy = " (proxy)" if (cause.proxy or effect.proxy) else ""
        arrow = lambda d: "up" if d > 0 else "down"  # noqa: E731
        cy, ey = cause_real, effect_real
        label = (f"{company_label}: computed check of link {link}{proxy} — {cause.label} {arrow(cause_dir)}, "
                 f"{effect.label} {arrow(effect_dir)}")
        value = (f"{cause.label}: {_fmt(cause, cy[3], cy[4])} (FY{cy[1]}–FY{cy[2]}); "
                 f"{effect.label}: {_fmt(effect, ey[3], ey[4])} (FY{ey[1]}–FY{ey[2]}). "
                 f"Actual: {cause.label} {arrow(cy[0])}, {effect.label} {arrow(ey[0])}"
                 f"{' — both as the chain states' if stated_ok else ' — differs from the chain'}.")
        item = EvidenceItem(
            kind="CALCULATION", label=label, value=value,
            citation="canonical_financials, annual (computed; association over a few reported years, not causation)",
            chain_step=link, source_tier="CALCULATED",
        )
        out.append(("supporting" if stated_ok else "contradicting", item))
    return out


def link_items_for_hypothesis(conn, hypothesis, statement_type: str, fact_store) -> list[tuple[str, EvidenceItem]]:
    """Run the checks for each company the hypothesis names (non-financial only)."""
    from companies.registry import get_company
    from web.company_kind import is_financial_company

    results: list[tuple[str, EvidenceItem]] = []
    steps = list(getattr(hypothesis, "chain_steps", None) or [])
    if len(steps) < 2:
        return results
    for company_id in getattr(hypothesis, "companies", None) or []:
        company = get_company(conn, company_id)
        if company is None or is_financial_company(company):
            continue
        is_us = (company["currency"] or "INR") == "USD"

        def fetch(metric: str, _cid=company_id) -> Series:
            series: Series = {}
            for row in fact_store.get_canonical_series(conn, _cid, metric, "annual", statement_type):
                try:
                    series[int(str(row["fiscal_year"]).removeprefix("FY"))] = float(row["canonical_value"])
                except (ValueError, TypeError):
                    continue
            return series

        results.extend(check_links(steps, Loader(fetch, is_us), company_id))
    return results
