# Pending List

Snapshot as of 2026-09-02, after the database-portability refactor, the
Configurable Indicator Framework, and the Golden Research Loop validation
(see `architecture.md` and `SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`).
Nothing below is uncommitted or blocking — this is the backlog, not open work.

## Configurable Indicator Framework — deliberately deferred (not bugs)

- **Feedback loop** (Agree / Disagree / Not Sure, spec section 11) —
  design-only by the original spec; an `indicator_feedback` table keyed off
  `evaluation_id` is the intended shape, noted in `indicators/evaluation.py`'s
  docstring as an extension point, not built.
- **Critical (red) classification tier** — vocabulary slot reserved, unused.
  Only `positive` / `observation` / `warning` are live.
- **9 of the 11 anticipated indicator families** — only `shareholding` and
  `financial_trajectory` are seeded. Not yet built: promoter pledging, debt/
  refinancing, revenue/profitability, operating performance, valuation,
  governance, corporate actions, cash flow, capital allocation.
- **Audit-trail browsing UI** — `indicator_evaluations` is real, queryable,
  append-only data, but no Admin/Tools panel renders it yet.

## Golden Research Loop validation — real gaps, ranked by priority

1. **Missing financial metrics in `canonical_financials`** — no NIM, CASA,
   cost-of-funds, or segment-level credit-cost data. The most common
   `missing_evidence` item across all five golden-loop runs, and the main
   reason verdicts land at PARTIALLY_SUPPORTED rather than SUPPORTED.
2. **Uneven document ingestion coverage** — IDFC First Bank has 9 documents
   → 124 knowledge-graph claims (richest result of the five); ICICI and Axis
   have zero documents ingested. Investigation quality varies by company for
   data-coverage reasons, not pipeline capability.
3. **IndusInd Bank's ingested annual data ends at FY2013** — the "early
   warning" golden question concerns 2024-25, which is currently
   unanswerable. Not an architecture gap: the point-in-time (`as_of`)
   evidence-scoping capability built to support exactly this kind of
   question is implemented and proven (`research/temporal.py`); a re-run
   will work as soon as more recent IndusInd data is ingested.
4. **Charts never attach to an investigation record** — `charts/
   financial_charts.py` exists but nothing under `research/` calls it, and
   there's no schema column on `investigations`/`investigation_hypotheses`
   to hold a chart reference.
5. **Indicator rule coverage was thin against the golden-loop banks** — the
   5 existing rules (2 families) didn't fire on most of the five test
   companies (professionally managed banks, small profit moves), so
   `IndicatorEvidenceCapability` contributed little evidence in this
   particular validation despite being wired into the investigation
   pipeline. Expected to matter more once more indicator families exist
   (see above) and/or on companies whose indicators actually trigger.

## Roadmap additions — directional only, not authorized for build (2026-09-25)

These are intentionally **not** part of the current implementation and must
not be built until a separate, explicit implementation request is made.

### Mutual Fund / Fund House Research

Expand research coverage beyond individual listed companies to mutual funds
and fund houses: fund/fund-house data, portfolio holdings, holding changes,
fund characteristics, fact-based fund analysis, comparative analysis,
exposure analysis, relevant historical trends. Directional only — do not
build mutual fund functionality as part of the current company research
platform.

### Watchlist — "Important Now"

A highly selective intelligence layer at the top of the Watchlist that
surfaces exceptional developments materially affecting followed companies:
major corporate actions, M&A/significant share swaps, material financial
events, significant regulatory developments, major management/governance
events, triggered Signals indicators, important findings from active
investigations, highly relevant macro events with defensible causal
relationships. Must **not** become another news feed — stays a small,
high-signal exception layer, and shows nothing when no item meets the
materiality threshold. Each eventual item should explain: what happened,
which company is affected, why Signals considers it important, when it
happened, supporting source/evidence, and offer the ability to open or
initiate an investigation. Do not implement now.

### Watchlist Priority Monitoring

Monitor Watchlist companies more frequently than the broader supported
universe (conceptually: identify geography → identify applicable
authoritative sources → check for changes → acquire → preserve source →
persist → classify → surface). Target ~15–30 min cadence for Watchlist
companies where technically/legally/economically appropriate, vs.
lower-frequency scheduled or on-demand retrieval for the broader universe;
frequency must eventually be configurable and respect source-specific rate
limits. Must be geography-agnostic in architecture (India → NSE/BSE/etc.,
US → SEC/exchange sources, future markets → their own authoritative
sources) with a common monitoring framework and geography-specific source
adapters — do not design around NSE alone.

Current behavior (already fine to keep/extend, not the deferred part):
accessing a company/Watchlist entry may trigger an on-demand lookup of
recent announcements/disclosures (~latest 24-48h where supported), routed
through the normal durable ingestion/provenance pipeline — not a second
ingestion path. UI must not imply this is complete historical coverage.
Future evolution is "Scheduler → same ingestion pipeline" (vs. today's
"User action → ingestion pipeline"); do not build a second ingestion
architecture when scheduled monitoring is introduced. Do not build
scheduled monitoring yet.

Relates to but stays decoupled from Important Now: Watchlist Priority
Monitoring concerns *acquisition* (frequent monitoring → new event
detected → classification → materiality assessment → Important Now
candidate); Important Now concerns *intelligence/prioritization*. Do not
couple their implementation prematurely.

### Watchlist Announcements & Financial-filing ingestion pipeline

Status: Future Roadmap — implementation spec ready, deferred by explicit
user decision (2026-09-26)

The Watchlist tab (`web/templates/watchlist.html`) was restructured
2026-09-26 into a Grid/Feed activity workspace (company rail, News/
Announcements/Financial/Investigations panels — see `docs/FeatureList.md`).
That pass shipped News (from `company_news`) and Investigations (from
`research_cases`) with real data; the Announcements and Financial &
filing-updates panels render their honest empty state ("not yet
available"), since no ingestion pipeline exists yet to populate them.

A full design/implementation spec for that pipeline already exists —
`design_handoff_watchlist/README.md` (repo root) — covering:
- An `AnnouncementSource` adapter interface + country registry (NSE for
  IN now, SEC EDGAR for US and BSE later), wrapping
  `sources/nse_filing_documents.py::fetch_announcements_raw()` (module-
  qualified — a second, differently-typed function of the same name
  exists in `sources/nse_pdf_filings.py`, a real naming collision to
  avoid).
- A new classifier (category: `financial`/`announcement`, subtype:
  `corporate_action`/`m_and_a`/`governance`/`regulatory`/`other`) — reuses
  `nse_filing_documents.py::classify_announcement_row()`'s desc/
  `attchmntText`-pattern-matching style, not its literal output vocabulary
  (that function's `DOCUMENT_TYPES` don't map to this feature's taxonomy).
- 3 new tables: `announcements_raw`, `announcements`,
  `announcement_fetch_log` (the last backs a 15-minute on-demand-refresh
  throttle), following the `raw_objects`/`documents` provenance
  conventions already established elsewhere in this codebase.
- `ingest_announcements(conn, company_id, trigger='on_demand')` in
  `ingestion/pipeline.py`, using `storage/raw_object_store.py::
  store_raw_object()` for hash-deduped raw storage and publishing a
  `DatasetIngestedEvent`, mirroring `ingest_sec_edgar_company()`'s shape.

Do not build this until a separate, explicit implementation request is
made. When picked up: extend `web/watchlist_feed.py::
list_watchlist_activity()` to source real announcement/financial rows
instead of the current always-empty lists, and update
`docs/FeatureList.md`'s Watchlist row accordingly.

## Status

Git is clean — all work described above (and everything that produced this
list) is committed. This file tracks backlog only; update or re-generate it
after picking any item up.
