"""Company classification helpers shared by the feeds and the company page."""

from __future__ import annotations

from storage.db_types import Row

_FINANCIAL_SECTOR = "financial services"


def is_financial_company(company: Row | None) -> bool:
    """Banks and other financials -- the only companies the Bank Ratios
    section (credit-deposit, advances/deposits, interest coverage, ...) is
    meaningful for. Both classification schemes in `companies` are checked:
    `sector` (yfinance-style, US and many Indian rows) and
    `macro_economic_sector` (Indian macro taxonomy); either being
    "Financial Services" qualifies."""
    if company is None:
        return False
    keys = company.keys()
    for column in ("sector", "macro_economic_sector"):
        if column in keys and (company[column] or "").strip().lower() == _FINANCIAL_SECTOR:
            return True
    return False
