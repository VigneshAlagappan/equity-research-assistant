# Scheduled / Periodic Jobs — Plan

Every recurring job requested so far, what already exists to build on vs.
what's a genuine gap. Company-list execution batches (Nifty 500, USA XBRL
fetch) live separately in `NIFTY500_USA_XBRL_BATCHES.md` — this file is
scheduling policy and gap analysis only.

## 1. Financials — quarterly (India + USA)

- **India**: ready. `scripts/fetch_nse_xbrl.py` + `sources/nse_xbrl.py` —
  the pipeline already running for Nifty 50 (13 of 50 done as of this
  writing) and planned for Nifty 500. Re-running per company is idempotent
  (already-downloaded files skipped, already-ingested observations not
  reprocessed).
- **USA**: gap. `sources/yfinance_financials.py` is annual-only by its own
  documented design — yfinance's quarterly frames use calendar-quarter
  boundaries that don't line up with a company's actual fiscal quarters,
  and that per-company mapping was never built. Scheduling the existing
  pilot would only ever produce annual data.
- **Cadence note**: run ~6–8 weeks after each quarter-end, not right at it
  (SEBI gives companies 45/60 days to file).

**Verdict — India: ready to schedule. USA: gap, needs the fiscal-quarter mapping built first.**

## 2. Historical price data — weekly (India + USA)

- **India**: ready, and already exceeds the ask. `scripts/
  fetch_daily_prices.py` runs *daily* (upserts the last 5 trading days,
  self-healing) across the full NSE 500 universe via `company_index_
  membership`; `scripts/backfill_price_history.py` did the one-time deep
  backfill. Both via `sources/yfinance_prices.py`. Daily already satisfies
  weekly — no new job needed.
- **USA**: gap. `sources/yfinance_prices.py` itself is ticker-agnostic
  (yfinance covers US tickers fine), but `fetch_daily_prices.py`'s
  universe query is NSE-500-specific — it never loops over the 12 US
  companies on file. Needs a US-scoped sibling script or a parameterized
  universe query.

**Verdict — India: ready (already running, daily). USA: gap, needs a US-scoped fetch script.**

## 3. Uploaded PDF analysis — quarterly (transcripts, concall presentations, annual report docs)

- **Document typing already exists.** `documents.document_type` already
  distinguishes `transcript`, `investor_presentation` (concall
  presentation), `annual_report`, `financial_result`, `xbrl`,
  `concall_recording`, `ai_summary`, `announcement` — the Docs tab's
  upload UI already maps to these types. No separate "annual report
  *presentation*" type exists distinct from `annual_report` itself — would
  need a new type or a convention (e.g. reuse `investor_presentation` for
  these too) if that distinction matters.
- **No recurring trigger today.** Processing pending documents
  (`ingestion/coordinator.py`'s `discover_pending_documents` /
  `process_all_pending_documents`) is strictly button-driven from the
  Admin Ingest queue's Documents tab — nothing scans or fires on a
  schedule. And what "processing" does today is limited: the Knowledge
  Builder extraction step (Step 2A) isn't built yet, so a processed
  document just gets its `file_hash` refreshed and a `processed` mark, not
  real content extraction.
- **No automated source for new PDFs.** There's no NSE/BSE
  corporate-announcements scraper anywhere in `sources/` — only
  `nse_fetch.py` for XBRL financial filings. "Quarterly ingestion" here
  necessarily means *processing what a human has manually uploaded by
  then*, not an automated fetch of new transcripts/presentations from
  their original source.

**Verdict — gap on three fronts: (i) an "annual report presentation" type decision, (ii) a scheduled trigger for the existing process-all action, (iii) real content extraction (Knowledge Builder) isn't built — and there's no fetch source at all, so this can only ever process manually-uploaded files, never auto-discover new ones.**

## 4. Analytics + insights — monthly (Nifty 50, USA, and Macro — India and USA)

- **Per-company generation exists, human-triggered only.** The "Generate
  key insights" button (`/companies/<id>/insights/generate`) calls
  `research/insights.py`'s `generate_key_insights` — one Anthropic LLM
  call per company, on click. The separate cross-company "System
  Insights" feature explicitly documents itself as *not* auto-scheduled,
  by design — "generation is trigger-button-initiated, same
  user-controls-when-it-runs discipline the existing Key Insights
  'Generate' button already follows."
- **No batch variant exists for either.** Both are strictly one-company
  (or one cross-company run), one click, at a time today.
- **Macro insights don't exist at all yet — bigger gap than the company
  side.** `research/system_insights.py`'s "System Insights" is
  cross-*company* (synthesized from `knowledge_claims` about companies in
  the Knowledge Graph), not macro-economic. There's no equivalent function
  anywhere that reads India macro data (`sources/rbi_*`) or US macro data
  (`sources/fred.py`) and synthesizes an insight from it — the Tools tab's
  "Macro" panel (`web/app.py`'s `tools()`, `active_panel="macro"`) is a
  data-browsing view, not an insight generator. A monthly macro-insights
  job needs that generation function built first (what does a "macro
  insight" even look like — one narrative per indicator? per
  country/region? correlated across a few series? — is a real design
  question, not just a scheduling one), not just a batch wrapper around
  something that already exists like the company side has.
- **A reusable batching pattern exists** — `scripts/fetch_daily_prices.py`
  reads a ticker universe from the DB, loops in batches with a pause
  between batches and between individual calls, idempotent/safe to
  interrupt. A monthly Nifty-50-and-USA insights job would follow this
  exact shape: read the company list, loop calling
  `generate_key_insights` per company, paced (LLM calls, unlike price
  fetches, cost real money and tokens per call — pacing here is about
  cost/rate control, not just courtesy to an external API).

**Verdict — company insights (Nifty 50 + USA): gap, but a shallow one — the per-company generation function is reusable as-is, only the batch-loop script and scheduler trigger need building. Macro insights (India + USA): deeper gap — the generation function itself doesn't exist, and needs a design decision (what a "macro insight" is) before any code. Also worth deciding up front: 62 companies × monthly LLM calls, plus whatever the macro job turns out to cost, is real recurring spend — confirm that's wanted before automating either.**

## 5. Macro data ingestion — weekly

- **Mixed: one live source, the rest file-based.** `sources/fred.py` is a
  genuine live fetch — pulls FRED's public CSV endpoint directly, no API
  key, no file staging (`main.py`'s `ingest-fred` command already wraps
  it, currently one series at a time). Everything else
  (`sources/rbi_dbie_tables.py`, `rbi_indicators.py`,
  `rbi_bank_infrastructure.py`, `iitm_rainfall.py`, and the general
  `sources/macro.py`) is a bespoke parser over a file a human already
  downloaded from RBI's DBIE site or similar and staged under
  `data/raw/_macro/` — none of them make an HTTP request.
- **No `scripts/` loop exists yet even for the live source.** FRED is
  fetchable live today, but only one series per CLI invocation — nothing
  loops over a configured list of series the way `fetch_daily_prices.py`
  loops over tickers.

**Verdict — FRED: ready to schedule once a `scripts/` loop over your series list is written (small, well-precedented addition). RBI/IITM/DBIE sources: gap — either build real fetchers against those sites, or accept that "weekly" for those specifically still means a human downloads the file first, and the "job" is just re-running ingestion over whatever's already staged.**

## 6. DB sharding — daily

- **The script itself is ready today, no new code needed.**
  `scripts/db_shard.py` splits `data/equity_research.db` into ≤50MB parts
  under `data/db_shards/` for git storage (GitHub hard-blocks any file
  over 100MB, and the live db is already well past the 50MB warning
  threshold — currently ~300MB, so ~6 parts). It uses SQLite's own online
  backup API (`Connection.backup()`), which snapshots safely even while
  the app or an ingestion job is writing concurrently — it never locks or
  copies the live file directly. Companion `scripts/db_unshard.py`
  reassembles it (used after a fresh clone/pull, refuses to overwrite an
  existing db unless `--force`d). Directly closes the gap flagged
  earlier — right now the db and the raw XBRL archive exist only on this
  machine, not in git at all.
- **What it does NOT do: commit or push.** `db_shard.py` only writes
  shard files to disk — verified, no `git` calls anywhere in it. For a
  daily job to actually achieve "this data is backed up / shared via git,"
  something still has to `git add data/db_shards/` + commit + push after
  every run. That's a materially different kind of action than the other
  five jobs in this file: those are read-only pulls from an external
  source into local files/db; this one would push potentially-sensitive
  ingested financial data to a remote repo, unattended, every day. Per
  this session's own working rules, an automated push to a shared remote
  needs its own explicit authorization — not something to fold into "run
  it daily" without a separate decision on exactly that.

**Verdict — sharding step: ready to schedule as-is (mechanical, safe, no new code). Commit+push step: needs an explicit decision first (what remote, what branch, whether it's really meant to run unattended) — don't wire that part up on the strength of "daily" alone.**

## Summary

| Job | Cadence | India/live-source | USA / other |
|---|---|---|---|
| Financials | Quarterly | Ready | Gap (fiscal-quarter mapping) |
| Price history | Weekly | Ready (already daily) | Gap (no US-scoped script) |
| Doc analysis (transcripts/concalls) | Quarterly | Partial (typing exists, no trigger, no extraction, no fetch source) | Same |
| Analytics/insights — companies | Monthly | Gap (no batch script; generation fn reusable) | Same |
| Analytics/insights — macro | Monthly | Gap (generation fn doesn't exist yet) | Same |
| Macro data | Weekly | FRED ready; RBI/IITM/DBIE gap | N/A (US macro not in scope here) |
| DB sharding | Daily | Ready (shard step only) | Commit+push needs a separate explicit decision |

Price history (India), financials (India), and the DB sharding step are
ready to actually put on a schedule today. Everything else needs real
implementation work first — not just a cron entry. And even for sharding,
"ready" is the local file-writing part only — turning that into an actual
git backup still needs the commit+push decision above made explicitly.
