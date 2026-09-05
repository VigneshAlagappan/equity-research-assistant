"""SECEdgarAdapter — pulls a US company's quarterly *and* annual financial
statements directly from the SEC's own structured XBRL data (10-K/10-Q),
via the free, public, no-API-key `data.sec.gov/api/xbrl/companyfacts`
endpoint. This is the USA equivalent of sources/nse_fetch.py's NSE XBRL
pipeline — the regulator's own tagged filing data, not a scrape and not a
secondary provider — and is what actually closes the "Financials — USA"
gap SCHEDULED_JOBS.md has flagged all along: sources/yfinance_financials.py
is annual-only specifically because yfinance's quarterly frames use
calendar-quarter boundaries that don't line up with a company's real fiscal
quarters. SEC's own `end` dates ARE the company's real reporting dates, so
this derives the fiscal-quarter label from those real dates (using the same
"fiscal year is named for the calendar year it ends in" convention
web/charts_feed.py's _period_date_range already uses for India), instead of
trusting yfinance's calendar-quarter framing.

Trust tier: same as NSE/BSE (config/settings.py's DEFAULT_SOURCES,
trust_rank 0) — this is the regulator's own filing data, not a secondary
provider like yfinance (trust_rank 3).

## What this does NOT attempt

XBRL's `us-gaap` taxonomy has 300-500+ possible tags per company, and which
one a given company actually uses for "revenue" or "loans" varies by
industry and filer (see this module's own _CONCEPT_MAP / metric_aliases
seed data for the exact list this covers). Coverage is deliberately a
curated core set (income statement, balance sheet, EPS, shares outstanding,
cash flow, plus a handful of bank-specific concepts), not exhaustive —
consistent with this app's "real gap, not silently guessed" rule, an
unmapped concept is just absent, never approximated from a nearby tag. Bank
holding companies in particular often skip the generic `Revenues` tag
entirely (interest income/expense nets differently in a bank's income
statement) — expect thinner coverage for banks/insurers than for a typical
industrial or tech company.

## The duration-vs-instant, single-quarter-vs-YTD problem

A balance-sheet concept (Assets, StockholdersEquity, ...) is an "instant" —
one value as of a date, no ambiguity. An income-statement/cash-flow concept
(NetIncomeLoss, EPS, ...) is a "duration" — and critically, **one 10-Q
filing reports the SAME concept over multiple overlapping spans**: this
quarter alone (~91 days) AND year-to-date-so-far (~182/273 days), tagged
with the identical fy/fp/accn, distinguished only by which (start, end)
span is attached. Naively taking "whatever value has this fy/fp" risks
silently picking the 6-month cumulative figure as if it were one quarter's
— see _classify_span_days()/_extract_periods() below, which classify by
actual elapsed days instead of trusting fy/fp alone.

SEC also never asks a filer for a standalone Q4 — only Q1-Q3 get a 10-Q;
the 10-K reports the full year. Q4 standalone (to match this app's Q1-Q4
convention) is therefore *derived*, not reported: FY minus (Q1+Q2+Q3) — a
deterministic arithmetic step from already-ingested facts (same spirit as
web/charts_feed.py's fill_missing, never a cross-source guess), computed
in _derive_q4_observations() only when all four inputs are present.
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from config import settings
from companies.registry import get_company
from normalization.financials import build_observations_from_periods
from sources.base import NormalizedObservation
from storage.db_types import DBConnection

logger = logging.getLogger(__name__)

PARSER_VERSION = "sec-edgar-v1"

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# One companyfacts request per company (every concept comes from the same
# JSON) -- no per-request pacing constant needed in this module itself.
# A future batch-loop script covering many companies at once (mirroring
# scripts/batch_fetch_nse.py's own REQUEST_DELAY_SECONDS between companies)
# should still pace itself against SEC's fair-access policy (<=10 req/s),
# same discipline sources/nse_fetch.py already applies to NSE.


class SECFetchError(Exception):
    pass


def _headers() -> dict[str, str]:
    # SEC requires every request carry an identifying User-Agent (fair-access
    # policy, not an API key) -- config.settings.SEC_EDGAR_USER_AGENT
    # defaults to a placeholder; set the SEC_EDGAR_USER_AGENT env var to your
    # own "Company Name contact@example.com" before running this on a
    # schedule against real traffic, not just a one-off test.
    return {"User-Agent": settings.SEC_EDGAR_USER_AGENT}


_ticker_cik_cache: dict[str, int] | None = None


def _load_ticker_cik_map() -> dict[str, int]:
    global _ticker_cik_cache
    if _ticker_cik_cache is not None:
        return _ticker_cik_cache
    try:
        resp = requests.get(_TICKER_MAP_URL, headers=_headers(), timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SECFetchError(f"Failed to fetch SEC ticker->CIK map: {exc}") from exc
    data = resp.json()
    _ticker_cik_cache = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
    return _ticker_cik_cache


def get_cik_for_ticker(ticker: str) -> int | None:
    """SEC's own free ticker->CIK directory (~10,400 entries, refreshed
    periodically) -- cached in-process for the life of this run since it's
    the same file for every company a batch job resolves."""
    return _load_ticker_cik_map().get(ticker.upper())


def fetch_company_facts(cik: int) -> dict:
    """The raw companyfacts JSON for one CIK -- every XBRL-tagged fact this
    company has ever reported, across every filing. Can be several MB for a
    large, long-listed filer (Apple's is ~3.8MB)."""
    url = _COMPANY_FACTS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
    except requests.RequestException as exc:
        raise SECFetchError(f"Failed to fetch company facts for CIK {cik}: {exc}") from exc
    if resp.status_code == 404:
        raise SECFetchError(f"No SEC XBRL company facts for CIK {cik} (never filed, or CIK wrong)")
    resp.raise_for_status()
    return resp.json()


# ============================================================
# Period classification -- see module docstring's "duration-vs-instant"
# section for why this exists at all.
# ============================================================

def _classify_span_days(days: int) -> str | None:
    if 80 <= days <= 100:
        return "quarter"
    if 350 <= days <= 380:
        return "annual"
    return None  # a YTD-cumulative (2-quarter/3-quarter) span -- not a standalone period, ignored


def _fiscal_period_for_end_date(end_date: date, fiscal_year_end_month: int) -> tuple[str, str]:
    """(fiscal_year_label, quarter_label) for a period ending on end_date,
    given this company's fiscal_year_end_month -- the inverse of web/
    charts_feed.py's _period_date_range. Fiscal year is named for the
    calendar year it ends in (this app's existing FYnnnn convention);
    quarter is which 3-month block, counting from the fiscal year's own
    start month, end_date falls into. Month-based (not exact-day) on
    purpose -- real fiscal calendars (52/53-week, "last Saturday of the
    month") land within a few days of month-end, not exactly on it."""
    if end_date.month <= fiscal_year_end_month:
        fy = end_date.year
    else:
        fy = end_date.year + 1
    fy_start_month = (fiscal_year_end_month % 12) + 1
    months_elapsed = (end_date.month - fy_start_month) % 12
    quarter_num = min(months_elapsed // 3 + 1, 4)
    return f"FY{fy}", f"Q{quarter_num}"


def _extract_periods(
    rows: list[dict], fiscal_year_end_month: int, *, instant: bool
) -> tuple[dict[tuple[str, str | None], float], dict[tuple[str, str | None], float]]:
    """rows is one XBRL concept's raw {start?, end, val, filed, form, ...}
    list. Returns (quarterly_period_values, annual_period_values), each
    shaped for normalization.financials.build_observations_from_periods
    -- {(fiscal_year, quarter_or_None): value}. Ties (the same real-world
    period reported in more than one filing, e.g. as a prior-year
    comparative) are resolved by keeping whichever was filed most recently,
    same "latest wins" reconciliation rule storage/repositories.py's
    insert_shareholding_observations already documents elsewhere."""
    quarterly: dict[tuple[str, str | None], float] = {}
    annual: dict[tuple[str, str | None], float] = {}
    quarterly_filed: dict[tuple[str, str | None], str] = {}
    annual_filed: dict[tuple[str, str | None], str] = {}

    for r in rows:
        if "end" not in r or "val" not in r:
            continue
        try:
            end_date = date.fromisoformat(r["end"])
        except ValueError:
            continue

        if instant:
            span_class = "instant"
        else:
            if "start" not in r:
                continue
            try:
                start_date = date.fromisoformat(r["start"])
            except ValueError:
                continue
            span_class = _classify_span_days((end_date - start_date).days)
            if span_class is None:
                continue

        fy_label, quarter = _fiscal_period_for_end_date(end_date, fiscal_year_end_month)
        filed = r.get("filed", "")

        if instant:
            key = (fy_label, quarter)
            if key not in quarterly_filed or filed > quarterly_filed[key]:
                quarterly[key] = r["val"]
                quarterly_filed[key] = filed
            # An instant also stands in for its own fiscal year (e.g. shares
            # outstanding as of fiscal year-end IS the annual figure too) --
            # only when the quarter is the company's own Q4 (year-end).
            if quarter == "Q4":
                akey = (fy_label, None)
                if akey not in annual_filed or filed > annual_filed[akey]:
                    annual[akey] = r["val"]
                    annual_filed[akey] = filed
        elif span_class == "quarter":
            key = (fy_label, quarter)
            if key not in quarterly_filed or filed > quarterly_filed[key]:
                quarterly[key] = r["val"]
                quarterly_filed[key] = filed
        elif span_class == "annual":
            key = (fy_label, None)
            if key not in annual_filed or filed > annual_filed[key]:
                annual[key] = r["val"]
                annual_filed[key] = filed

    return quarterly, annual


def _derive_q4_observations(
    quarterly: dict[tuple[str, str | None], float], annual: dict[tuple[str, str | None], float]
) -> dict[tuple[str, str | None], float]:
    """Q4 standalone = FY (10-K, direct) minus (Q1+Q2+Q3 standalone,
    already-fetched 10-Q figures) -- SEC never asks a filer for a
    standalone Q4 report, only the full year, so this is the one metric
    this adapter computes rather than reads. Only emitted when all four
    inputs already exist for the same fiscal year; never guessed from a
    partial set."""
    derived: dict[tuple[str, str | None], float] = {}
    fiscal_years = {fy for (fy, q) in quarterly if q in ("Q1", "Q2", "Q3")}
    for fy in fiscal_years:
        if (fy, "Q4") in quarterly:
            continue  # already have a direct figure (the instant-metric case above)
        q1, q2, q3 = quarterly.get((fy, "Q1")), quarterly.get((fy, "Q2")), quarterly.get((fy, "Q3"))
        fy_total = annual.get((fy, None))
        if None in (q1, q2, q3, fy_total):
            continue
        derived[(fy, "Q4")] = fy_total - q1 - q2 - q3
    return derived


# ============================================================
# metric_key -> XBRL concept resolution. Ordered fallback lists (first
# concept the company actually reports wins) -- see normalization/
# financials.py's DEFAULT_METRIC_ALIASES for the (source="sec_edgar",
# raw_label=<concept name>, metric_key=...) rows these names resolve
# through; add a concept there, not here, to extend coverage for a metric
# already in this map.
# ============================================================

_CONCEPT_MAP: dict[str, list[str]] = {
    "total_revenue": [
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
        "RevenuesNetOfInterestExpense",
    ],
    "operating_profit": ["OperatingIncomeLoss"],
    "profit_before_tax": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "tax": ["IncomeTaxExpenseBenefit"],
    "net_profit": ["NetIncomeLoss", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "interest_earned": ["InterestAndDividendIncomeOperating", "InterestIncomeOperating", "InterestAndFeeIncomeLoansAndLeases"],
    "interest_expended": ["InterestExpense", "InterestExpenseOperating"],
    "total_assets": ["Assets"],
    "total_shareholders_funds": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "shares_outstanding": ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
    "deposits": ["Deposits"],
    "advances": ["LoansAndLeasesReceivableNetReportedAmount", "LoansAndLeasesReceivableNetOfDeferredIncome", "LoansReceivableNet"],
    "net_cash_from_operating_activities": ["NetCashProvidedByUsedInOperatingActivities"],
    "net_cash_from_investing_activities": ["NetCashProvidedByUsedInInvestingActivities"],
    "net_cash_from_financing_activities": ["NetCashProvidedByUsedInFinancingActivities"],
}

# Balance-sheet-style concepts (a point-in-time snapshot, "end" date only,
# no "start") -- everything else in _CONCEPT_MAP is a duration concept.
_INSTANT_CONCEPTS = frozenset({
    "Assets", "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "CommonStockSharesOutstanding", "CommonStockSharesIssued", "Deposits",
    "LoansAndLeasesReceivableNetReportedAmount", "LoansAndLeasesReceivableNetOfDeferredIncome",
    "LoansReceivableNet",
})

# Per-unit concepts (already dollars-per-share or a share count) -- must
# NOT be divided by _UNIT_DIVISOR the way an aggregate dollar figure is.
# Mirrors sources/yfinance_financials.py's own _PER_UNIT_ROW_LABELS split.
_PER_UNIT_CONCEPTS = frozenset({
    "EarningsPerShareDiluted", "EarningsPerShareBasic",
    "CommonStockSharesOutstanding", "CommonStockSharesIssued",
})

_UNIT_DIVISOR = 1_000_000  # raw USD -> this app's USD_MILLION "big" convention


class SECEdgarAdapter:
    source_id = "sec_edgar"

    def __init__(self, conn: DBConnection):
        self._conn = conn

    def fetch(self, company_id: str, cik: int, *, currency: str = "USD") -> list[NormalizedObservation]:
        """Fetch and normalize this company's quarterly + annual financials
        from SEC's own XBRL data. Returns [] (not an error) if this CIK has
        no us-gaap facts at all -- same "absence isn't an error" rule
        sources/yfinance_financials.py's fetch() follows."""
        facts = fetch_company_facts(cik)
        usgaap = facts.get("facts", {}).get("us-gaap", {})
        if not usgaap:
            logger.warning("SEC EDGAR: no us-gaap facts for CIK %s (company_id=%s)", cik, company_id)
            return []

        company = get_company(self._conn, company_id)
        fiscal_year_end_month = company["fiscal_year_end_month"] if company is not None else 12
        source_file = f"sec_edgar:CIK{cik:010d}"
        observations: list[NormalizedObservation] = []

        for metric_key, concept_names in _CONCEPT_MAP.items():
            present = [c for c in concept_names if c in usgaap and usgaap[c].get("units")]
            if not present:
                continue
            # Merge rows from EVERY present concept name, not just the
            # first -- a company can (and does) migrate which XBRL tag it
            # uses for the same real-world line item over the years, e.g.
            # Apple reported revenue under "Revenues" through fiscal 2018
            # (ASC 606 adoption), then switched to
            # "RevenueFromContractWithCustomerExcludingAssessedTax" for
            # every filing since. Picking only the first tag that has *any*
            # data would have silently returned 11 stale rows (Apple's
            # abandoned "Revenues" tag, last updated 2018) while ignoring
            # 117 current rows sitting right there under the newer tag --
            # a real bug caught by testing this against Apple's actual
            # filing history, not a hypothetical. _extract_periods' own
            # per-period "latest filed wins" dedup already resolves the
            # rare case where two tags both report the same real period.
            concept_name = present[0]  # for row_label/alias resolution only -- any present alias resolves to the same metric_key
            rows: list[dict] = []
            for name in present:
                unit_key = next(iter(usgaap[name]["units"]))
                rows.extend(usgaap[name]["units"][unit_key])
            instant = concept_name in _INSTANT_CONCEPTS
            quarterly, annual = _extract_periods(rows, fiscal_year_end_month, instant=instant)

            divisor = 1 if concept_name in _PER_UNIT_CONCEPTS else _UNIT_DIVISOR
            # Q4 = FY - (Q1+Q2+Q3) is only ever valid for a flow/duration
            # concept (net profit over a period) -- for an instant/balance-
            # sheet concept (shares outstanding, deposits, ...) Q4 is
            # already the direct year-end snapshot (populated above whenever
            # an instant reading's own date falls in Q4), and subtracting
            # cumulative flows from a point-in-time balance would be
            # nonsense. Guarded here, not just left to "Q4 already present
            # so _derive_q4_observations no-ops" -- that's true in the
            # normal case, but this makes the invariant explicit rather
            # than incidental.
            if not instant:
                quarterly = {**quarterly, **_derive_q4_observations(quarterly, annual)}

            if quarterly:
                observations.extend(
                    build_observations_from_periods(
                        self._conn, company_id=company_id, source=self.source_id, source_file=source_file,
                        parser_version=PARSER_VERSION, period_type="quarterly", statement_type="consolidated",
                        row_label=concept_name,
                        period_values={k: v / divisor for k, v in quarterly.items()},
                        currency=currency,
                    )
                )
            if annual:
                observations.extend(
                    build_observations_from_periods(
                        self._conn, company_id=company_id, source=self.source_id, source_file=source_file,
                        parser_version=PARSER_VERSION, period_type="annual", statement_type="consolidated",
                        row_label=concept_name,
                        period_values={k: v / divisor for k, v in annual.items()},
                        currency=currency,
                    )
                )

        if not observations:
            logger.warning("SEC EDGAR: no mapped concepts found for CIK %s (company_id=%s)", cik, company_id)
        return observations
