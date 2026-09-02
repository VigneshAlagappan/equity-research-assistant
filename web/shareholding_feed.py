"""Builds the Shareholding-tab JSON feed (see web/static/js/shareholding_panel.js)
from real data: storage.repositories.list_shareholding_history's per-quarter
aggregate percentages (promoter / public / employee-trust, plus the FII /
DII / Government / Public-non-institutional Table I rollup where that
quarter's SHP XBRL parsed) and list_shareholding_holders_all's
individually-named holders, grouped into the four buckets a Screener-style
"Major Holders" panel shows (sources.nse_shareholding.classify_public_category
puts each named public-side holder into fii/dii/public; promoter-side
holders are always the "promoter" bucket).

Ported from a Claude Design prototype (claude.ai/design, "Signals.dc.html"),
then reshaped at the user's request into a wide table (shareholding_panel.js:
quarters as columns, one row per bucket that expands into its named
holders) instead of the prototype's one-quarter-at-a-time cards. The
prototype's Insider Roster / Insider Transactions / Insider Sentiment
sub-tabs and its "top 5 of 1,842" holder-universe counts are UI chrome this
app has no data source for: NSE's SHP filing doesn't disclose insider-
trading activity, and this app only ever knows about the holders NSE named
individually, never a total universe size. shareholding_panel.js ports the
sub-tab affordance (Insider Sentiment kept locked, matching the prototype's
own gating) but only Major Holders ever gets real content, and every holder
count below is "named holders on file", never a fabricated denominator.

Not every quarter has a category breakdown or named holders -- only
"2025-10-31"-taxonomy filings do (see sources/nse_shareholding.py). Where
fii_percent/dii_percent are NULL for a quarter, that quarter's fii, dii,
AND public buckets all report percent=None -- "public" here means the
institutional residual (public_holding_percent minus FII minus DII), which
is exactly as undisclosed as FII/DII itself that quarter. An earlier
version fell back to the whole, undivided public_holding_percent instead;
with every quarter shown in one table row that reads as a single series
silently switching definitions mid-stream -- a fake ~60-70pp "Change" the
moment a later quarter's breakdown kicks in. promoter_holding_percent is
unaffected (always disclosed, every quarter).
"""

from __future__ import annotations

from storage.db_types import DBConnection
from collections import defaultdict

from sources.nse_shareholding import classify_public_category
from storage.repositories import list_shareholding_history, list_shareholding_holders_all

_BUCKETS = ("promoter", "fii", "dii", "public")
_BUCKET_LABELS = {"promoter": "Promoters", "fii": "FII", "dii": "DII", "public": "Public"}


def _round2(x: float | None) -> float | None:
    return round(x, 2) if x is not None else None


def _bucket_percents(obs: dict) -> dict[str, float | None]:
    fii = obs.get("fii_percent")
    dii = obs.get("dii_percent")
    if fii is not None and dii is not None:
        public = (obs.get("public_non_institutional_percent") or 0) + (obs.get("government_percent") or 0) + (obs.get("employee_trust_percent") or 0)
    else:
        # This quarter's SHP XBRL didn't parse a category breakdown at all
        # (older taxonomy) -- "public" here means the institutional residual
        # (public_holding_percent minus FII minus DII), which this quarter
        # simply doesn't have a value for either. Leaving it unset (like
        # fii/dii) rather than falling back to the whole, undivided
        # public_holding_percent: that fallback used to read as a genuine
        # -60-70pp swing the moment a later quarter's breakdown kicked in --
        # same series, two different definitions of "public" stitched
        # together -- which was invisible one quarter at a time but glaring
        # once the Major Holders table put every quarter in one row. A
        # multi-regime company shows "--" for fii/dii/public until its
        # filings start carrying the breakdown; promoter_holding_percent
        # (always disclosed) is unaffected.
        public = None
    return {
        "promoter": _round2(obs.get("promoter_holding_percent")),
        "fii": _round2(fii),
        "dii": _round2(dii),
        "public": _round2(public),
    }


def build_shareholding_feed(conn: DBConnection, company_id: str) -> dict:
    """Every quarter on file, each carrying that quarter's own bucket
    percentages and named holders -- shareholding_panel.js reshapes this
    into a holder x quarter matrix itself (it already has every quarter's
    holder list; no per-holder trend/delta needs computing here, since the
    table shows every quarter as its own column rather than one at a time)."""
    history = list_shareholding_history(conn, company_id)  # oldest first
    if not history:
        return {"quarters": []}

    valid_periods = {(h["fiscal_year"], h["quarter"]) for h in history}
    holder_rows = [
        h for h in list_shareholding_holders_all(conn, company_id)
        if (h["fiscal_year"], h["quarter"]) in valid_periods
    ]

    # (fiscal_year, quarter) -> bucket -> [holder row]
    by_period_bucket: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for h in holder_rows:
        bucket = "promoter" if h["side"] == "promoter" else classify_public_category(h["category"])
        by_period_bucket[(h["fiscal_year"], h["quarter"])][bucket].append(h)

    quarters_out = []
    for obs in history:
        period = (obs["fiscal_year"], obs["quarter"])
        percents = _bucket_percents(obs)

        buckets_out = []
        for bucket in _BUCKETS:
            holders = sorted(
                by_period_bucket[period].get(bucket, []),
                key=lambda h: (h["percent_of_shares"] is None, -(h["percent_of_shares"] or 0)),
            )
            holders_out = [
                {"name": h["holder_name"], "category": h["category"], "percent": _round2(h["percent_of_shares"])}
                for h in holders
            ]
            buckets_out.append({
                "key": bucket,
                "label": _BUCKET_LABELS[bucket],
                "percent": percents[bucket],
                "holders": holders_out,
                "holder_count": len(holders_out),
            })

        quarters_out.append({
            "fiscal_year": obs["fiscal_year"],
            "quarter": obs["quarter"],
            "label": f"{obs['quarter']} {obs['fiscal_year']}",
            "submission_date": obs.get("submission_date"),
            "source_url": obs.get("source_url"),
            "num_shareholders": obs.get("num_shareholders"),
            "buckets": buckets_out,
        })

    quarters_out.reverse()  # newest first -- latest quarter's provenance line shown first
    return {"quarters": quarters_out}
