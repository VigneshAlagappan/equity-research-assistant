"""EBITDA / EBIT / materials-cost rows for non-financial companies, derived
from NSE XBRL (Ind-AS general taxonomy) line items.

Neither IFRS nor US GAAP defines EBITDA/EBIT, and the XBRL taxonomy has no
tag for them, so both common conventions are provided, clearly labelled:

  operating (excl. other income)
      EBITDA = revenue from operations - (total expenses - finance costs - depreciation)
      EBIT   = EBITDA - depreciation
  incl. other income
      EBITDA / EBIT above + other income

Exceptional items are excluded from BOTH (they sit between "expenses" and
profit before tax; e.g. TCS FY2026's one-off ~Rs 4,500 Cr charge), so the two
differ by exactly other income. PBT is shown as its own row and does include
them.

"Total expenses" in the filing already INCLUDES finance costs and depreciation
(Reliance Q1 FY27: materials + purchases + inventory change + employee +
finance + depreciation + other = Expenses to the rupee), hence the add-backs.

Only computed for periods that carry the XBRL-only `other_expenses` line:
canonical `operating_expenses` means "total expenses incl. finance and
depreciation" in XBRL periods but Screener's narrower operating cost in older
legacy periods, and mixing the two would be wrong. Materials cost is a COGS
PROXY (materials + purchases + inventory change): Ind-AS Schedule III
classifies expenses by nature, so there is no SG&A tag and "other expenses"
mixes factory overheads with selling/admin costs.

Works on series keyed by anything (annual year ints or (year, quarter) tuples).
"""

from __future__ import annotations

from typing import Hashable

Series = dict[Hashable, float]

DERIVED_KEYS = ("ebitda", "ebitdaInclOther", "ebit", "ebitInclOther", "materialsCost")


def derive_income_rows(raw: dict[str, Series], keys: list[Hashable]) -> dict[str, Series]:
    out: dict[str, Series] = {k: {} for k in DERIVED_KEYS}
    other_expenses = raw.get("other_expenses", {})
    for k in keys:
        if k not in other_expenses:
            continue
        rev = raw["total_revenue"].get(k)
        opex = raw["operating_expenses"].get(k)
        fin = raw["interest_expended"].get(k)
        dep = raw["depreciation"].get(k)
        other_income = raw["other_income"].get(k)
        if None not in (rev, opex, fin, dep):
            ebitda = rev - (opex - fin - dep)
            out["ebitda"][k] = ebitda
            out["ebit"][k] = ebitda - dep
            if other_income is not None:
                out["ebitdaInclOther"][k] = ebitda + other_income
                out["ebitInclOther"][k] = ebitda - dep + other_income
        parts = [raw[m].get(k) for m in ("cost_of_materials_consumed", "purchases_of_stock_in_trade", "changes_in_inventories")]
        if parts[0] is not None or parts[1] is not None:
            out["materialsCost"][k] = sum(p for p in parts if p is not None)
    return out


#: Income Statement rows added alongside the original six. Shown only when they
#: hold at least one value, and never for financial companies (banks/NBFCs use
#: a different taxonomy with no materials / employee / other-expense lines, and
#: EBITDA is not a meaningful bank measure).
NON_FINANCIAL_ONLY_KEYS = frozenset(
    ("ebitda", "ebitdaInclOther", "ebit", "ebitInclOther", "materialsCost", "employeeCost", "otherExpenses",
     "currentTax", "deferredTax")
)
ADDED_KEYS = NON_FINANCIAL_ONLY_KEYS | {"profitBeforeTax", "taxExpense"}


def finalize_income_rows(rows: list[dict], is_financial: bool) -> list[dict]:
    """Drop added rows that don't apply (financial company) or hold no non-zero value;
    the original rows are never touched."""
    kept = []
    for r in rows:
        if r["key"] in NON_FINANCIAL_ONLY_KEYS and is_financial:
            continue
        if r["key"] in ADDED_KEYS and not any(v for v in r["values"]):  # all blank or all zero
            continue
        kept.append(r)
    return kept
