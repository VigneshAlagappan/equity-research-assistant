# Phase 0 investigation — the balance-sheet mapping was already fixed; the DB just hadn't caught up

Before writing any PDF pipeline code, this branch (`nse-pdf-backfill`, off
`feature-v3`) re-checked the feasibility report's Section 6 hint directly:
"the real coverage difference found is not PDF-vs-XBRL-as-filed, but
XBRL-as-filed vs. what's actually in this repo's own database." That hint
turned out to be exactly right, and the situation has moved further since
the report was written.

## What was checked

1. **`sources/nse_xbrl.py`'s own field-mapping.** It is NOT a fixed
   dict that only pulls P&L tags. It walks every element under a known
   fin namespace, generically, for BOTH the `OneD`/`FourD` duration
   contexts (P&L) and the `OneI` **instant** context (balance sheet) — see
   its module docstring's own "Balance-sheet facts... live under `OneI`"
   section, and `_extract_context_values()`'s single unified pass. This
   generic-extraction design, plus the balance-sheet row in
   `normalization/financials.py`'s `DEFAULT_METRIC_ALIASES` (`Assets`,
   `Deposits`, `Advances`, `Investments`, `Borrowings`, `Capital`,
   `ReservesAndSurplus`, `EquityShareCapital`, `OtherEquity`, ...,
   `source="nse"`) predates this branch entirely — `git log -S` on both
   files traces it to commit `8425f00` ("UI improved and XBRL support"),
   well before the feasibility spike ever ran. `tests/test_nse_xbrl_adapter.py`
   already has a passing, real-filing-shaped test
   (`test_balance_sheet_facts_come_from_the_instant_context`) proving
   this end to end.
2. **Real production Neon**, read-only (same `db_readonly.py` pattern the
   spike used), queried today (2026-09-19/20):
   - HDFCBANK/ICICIBANK/RELIANCE/TCS **do** now have real
     `nse`-sourced, `nse-xbrl-v1`-parsed balance-sheet facts
     (`total_assets`, `deposits`, `advances`, `investments`,
     `borrowings`, `reserves`, `equity_share_capital`) in
     `canonical_financials`, for both `quarterly` and `annual`
     `period_type`, both `standalone` and `consolidated`.
   - `financial_observations.retrieved_at` for these rows is
     **2026-09-19**, i.e. essentially the last day or two before this
     branch started — not something this branch produced.
   - This isn't limited to the 4 spike companies: `count(DISTINCT
     company_id)` with an `nse`-sourced balance-sheet fact is **1,356**,
     and the same `retrieved_at ≈ today` pattern shows up across an
     essentially-random sample of tickers pulled from that count (checked
     ~40 of them). This has every hallmark of a scheduled/batch XBRL
     re-ingestion job running in production, independent of this branch,
     which picked up the balance-sheet facts for free because the mapping
     that reads them was already correct — this branch did not write to
     production and does not explain this data.

## What this means for scope

The feasibility report's Section 7 finding ("zero balance-sheet facts for
any quarter, any company") was **true when it was written** and is **no
longer true today** — not because of anything built in this branch, but
because normal production XBRL ingestion has since run and the
already-correct mapping did its job. The 2019+ balance-sheet gap the
report was most worried about is now, empirically, mostly closed by
XBRL alone.

**Coverage is still uneven, though** — real, but a different and much
smaller problem than "zero coverage": for a spot-checked company like
HDFCBANK, `total_assets` shows up for only a handful of quarters per
fiscal year (e.g. Q1 FY2022, Q2 FY2023, Q4 FY2024/2025/2026, plus every
annual `FY20xx` row back to FY2004) — not literally every quarter. This
looks like a **backfill-breadth** gap (only some periods' XBRL files have
been fetched/ingested so far), not a mapping or extraction gap. Closing
it further is plausibly just a matter of running the existing
`scripts/fetch_nse_xbrl.py` + XBRL ingest path over more historical
quarters — a much lower-risk fix than PDF/OCR, and out of scope for this
branch (it touches the existing XBRL fetch cadence, not PDF).

## What's still genuinely in scope for this branch

Per the task's own conditional, the PDF pipeline remains the right tool
for exactly two cases XBRL cannot cover at all, not for the general
2019+ balance-sheet gap:

1. **Pre-2019 periods**, where XBRL genuinely doesn't exist (confirmed
   independently again here, unchanged from the report: XBRL is a
   placeholder before ~2018-2019 depending on company).
2. **Any period/company where raw XBRL itself is missing a field** (not
   just unmapped) — not separately re-verified company-by-company here
   (out of budget), but the adapter's design already handles this
   gracefully: an unmapped/absent tag is simply not in `values{}`, so
   nothing is silently guessed.

Phases 1-4 below were built with this narrowed, still-real scope in mind:
the discovery/date-window adapter and the table-aware PDF extractor are
general-purpose (they don't hardcode "pre-2019 only"), but the
recommended near-term use is (a) pre-2019 balance sheet backfill, where
XBRL has nothing at all, and (b) a small, explicit, human-reviewed set of
2019+ periods/metrics where a spot-check shows XBRL is missing a field —
never a blanket "PDF for every quarter" run, which Phase 0 shows is
mostly unnecessary now.
