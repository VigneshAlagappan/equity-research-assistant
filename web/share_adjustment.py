"""Restates per-share history onto the latest share basis, so EPS, book
value, dividend and sales-per-share stay comparable across splits and
bonus issues (what data vendors do): a 1:1 bonus or a Rs10 -> Rs2 face-value
split multiplies the share count, so every earlier period's per-share figure
is divided by that factor and its share count multiplied by it.

Only structural changes (bonus, face-value split, split) are applied -- never
today's diluted count, which would also fold in shares issued later for
cash/ESOPs/acquisitions and understate old EPS.

Factors come from the classified Corporate Actions feed (NSE `subject` text).
Two guards keep this from ever making numbers worse:
  * an action whose ratio can't be parsed is skipped, not guessed;
  * an action is applied only if the company's own shares_outstanding series
    actually jumps across it -- a flat series means the filing values were
    already restated to the new basis, and dividing again would double-adjust.
"""

from __future__ import annotations

import re
from datetime import date

from storage.company_repository import select_corporate_actions
from storage.db_types import DBConnection

_PER_SHARE_KEYS = ("eps", "diluted_eps", "book_value", "dividend_per_share", "sales_per_share")
_BONUS = re.compile(r"bonus\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)
_FV = re.compile(r"from\s*(?:rs|re)\.?\s*(\d+(?:\.\d+)?).*?to\s*(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.I | re.S)
_FV_SHORT = re.compile(r"(?:rs|re)\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*to\s*(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.I)


def share_multiplier(action_type: str, subject: str) -> float | None:
    """How many times larger the share count became: "Bonus 1:1" -> 2.0
    (1 new per 1 held), "Bonus 1:2" -> 1.5, "From Rs 10 To Rs 2" -> 5.0.
    None when the subject can't be parsed."""
    if action_type == "bonus":
        m = _BONUS.search(subject)
        if m and float(m.group(2)) > 0:
            return (float(m.group(1)) + float(m.group(2))) / float(m.group(2))
        return None
    if action_type in ("fv_split", "split"):
        m = _FV.search(subject) or _FV_SHORT.search(subject)
        if m and float(m.group(2)) > 0 and float(m.group(1)) > float(m.group(2)):
            return float(m.group(1)) / float(m.group(2))
    return None


def restate_to_latest_share_basis(
    conn: DBConnection,
    company_id: str,
    period_ends: dict[tuple[int, int], date],
    raw: dict[str, dict[tuple[int, int], float]],
) -> set[str]:
    """Mutates `raw` in place (per-share series divided, shares_outstanding
    multiplied, by the cumulative factor of every later applied action) and
    returns the set of raw metric keys that were actually changed."""
    ordered = sorted(period_ends, key=lambda pk: period_ends[pk])
    shares = raw.get("shares_outstanding", {})
    factors: dict[tuple[int, int], float] = {pk: 1.0 for pk in ordered}

    for row in select_corporate_actions(conn, company_id):
        if row["action_type"] not in ("bonus", "fv_split", "split") or not row["ex_date"]:
            continue
        mult = share_multiplier(row["action_type"], row["subject"])
        if mult is None or mult <= 1.0:
            continue
        ex = date.fromisoformat(row["ex_date"])
        before = [pk for pk in ordered if period_ends[pk] < ex]
        after = [pk for pk in ordered if period_ends[pk] >= ex]
        if not before:
            continue
        s_before = next((shares[pk] for pk in reversed(before) if shares.get(pk)), None)
        s_after = next((shares[pk] for pk in after if shares.get(pk)), None)
        if s_before and s_after and s_after / s_before < 1 + (mult - 1) * 0.5:
            continue  # share series doesn't jump: filings are already on the new basis
        for pk in before:
            factors[pk] *= mult

    changed: set[str] = set()
    for key in _PER_SHARE_KEYS:
        series = raw.get(key, {})
        for pk, f in factors.items():
            if f != 1.0 and pk in series:
                series[pk] = series[pk] / f
                changed.add(key)
    for pk, f in factors.items():
        if f != 1.0 and pk in shares:
            shares[pk] = shares[pk] * f
            changed.add("shares_outstanding")
    return changed
