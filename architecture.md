# Architecture

This document describes the current architecture of the Indian Equity Research
Assistant — a self-use, local-first Flask + SQLite application for researching
Indian (and a few non-Indian) listed companies, with an LLM research assistant
grounded in deterministically retrieved evidence.

For product/feature scope, see [README.md](README.md) and
[FeatureList.md](FeatureList.md). For running the app, see
[USER_GUIDE.md](USER_GUIDE.md).

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | Flask (server-rendered Jinja2, no SPA framework) |
| Database | SQLite (single file, `data/equity_research.db`) |
| LLM provider | Anthropic Claude (sonnet/haiku — Opus is registered but policy-disabled, `config/settings.py`'s `DISABLED_MODELS`), with a local Ollama fallback |
| Frontend | Server-rendered HTML + vanilla JS "islands" (no build step, no bundler, no npm) |
| Charts | matplotlib (server-rendered PNG) for legacy charts; client-side JS + JSON feeds for the interactive dashboards |
| Tests | pytest |

There is no separate frontend build pipeline — no `package.json`, no
webpack/vite. Pages are Jinja2 templates; interactivity is plain `<script>`
tags fetching JSON from Flask routes.

## High-level architecture

```
                         ┌─────────────────────────┐
                         │   Browser (Jinja2 HTML   │
                         │   + vanilla JS islands)  │
                         └────────────┬─────────────┘
                                      │ HTTP
                         ┌────────────▼─────────────┐
                         │   web/app.py (Flask)      │
                         │   routes, auth, sessions  │
                         └──────┬──────────────┬─────┘
                                │              │
              ┌─────────────────┘              └────────────────┐
              ▼                                                  ▼
  ┌───────────────────────┐                        ┌───────────────────────────┐
  │  Deterministic layer   │                        │   Research / AI layer      │
  │  companies/, ingestion/│                        │   research/, retrieval/    │
  │  normalization/,       │                        │   context/, llm/           │
  │  financials/, charts/  │                        │                            │
  └───────────┬────────────┘                        └──────────────┬─────────────┘
              │                                                    │
              ▼                                                    ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                    storage/ (sqlite3, schemas/sqlite_schema.sql)        │
  │                    data/equity_research.db                              │
  └────────────────────────────────────────────────────────────────────────┘

  main.py — CLI entry point, calls the same modules as web/app.py
```

The guiding split (see `research/assistant.py`'s module docstring): **retrieval
never calls the LLM**. Everything under `companies/`, `ingestion/`,
`normalization/`, `financials/`, `retrieval/` is deterministic Python/SQL —
the LLM is only ever handed a compact, pre-computed `Evidence` block and asked
to reason over it, never to fetch or calculate numbers itself.

## Backend

### Module map

| Package | Responsibility |
|---|---|
| `main.py` | CLI entry point (argparse) — ingest, analyze, ask, serve, admin commands. Thin wrapper over the same modules the web app uses. |
| `web/` | Flask app: routes, templates, auth, session, JSON feed endpoints for the JS-driven tabs. |
| `companies/` | Company registry (`registry.py`), lifecycle/archive rules (`lifecycle.py`), NSE bulk-import (`nse_import.py`). |
| `ingestion/` | File-format detection (`detector.py`), the ingest pipeline (`pipeline.py`) that runs a raw file through a source adapter → normalization → reconciliation, and validation (`validation.py`). |
| `sources/` | Source adapters — one per data provider: company financials (`screener.py`, `yfinance_financials.py`, `proprietary.py`), and non-company macro data (`macro.py`'s generic CSV convention, plus source-specific parsers for shapes that don't fit it — `rbi_indicators.py`/`rbi_dbie_tables.py`/`rbi_bank_infrastructure.py`, `iitm_rainfall.py`) — each turns a raw file/API response into `NormalizedObservation`/`MacroNormalizedObservation` rows. |
| `normalization/` | Canonicalizes raw labels into the shared metric vocabulary (`financials.py`), company identifiers (`companies.py`), fiscal periods (`periods.py`), and units/currency (`units.py`). |
| `financials/` | Deterministic math over `canonical_financials`: YoY/CAGR (`calculations.py`), ROA/ROE/vendor-reported ratios (`ratios.py`), and the human-readable text report (`report.py`) both the CLI's `analyze` command and the LLM evidence retrieval are built from. |
| `retrieval/` | `structured_search.py` — turns `financials/`'s calculations into typed `Evidence` for the LLM. Retrieval only, no LLM calls. |
| `research/` | The three LLM call sites: `assistant.py` (Q&A), `insights.py` (Key Insights summaries), `signals_report.py` (full Signals investigation reports) — plus `evidence.py` (the `Evidence`/citation model), `documents.py` (extracts `MANAGEMENT_STATEMENT` evidence from uploaded/linked Docs-tab PDFs), and `macro_evidence.py` (the third evidence source — India-wide macro/regulatory data; a narrow, deliberate exception to "retrieval never calls the LLM," since an LLM call picks which macro series/date-range apply before the deterministic fetch runs). |
| `context/` | The **Context Optimizer** — `optimizer.py` (dedup, value-scoring, token-budget compression of an `Evidence` list), `reuse.py` (reuse-before-recompute: returns a fresh, near-duplicate prior investigation instead of a new LLM call — now used by both `research/assistant.py`'s Q&A path and `research/signals_report.py`'s full reports), and `graph.py`/`graph_neo4j.py` (knowledge-graph traversal: surfaces a sector-peer *different* company's relevant prior investigation, via `config/knowledge_graph_seed.py`'s curated domain relationships — pure Python/SQLite by default, or a real Neo4j graph when `GRAPH_BACKEND=neo4j`, with automatic fallback to SQLite if Neo4j isn't reachable). |
| `llm/` | The **Model Router + Fallback layer** — `hardness.py` (task-complexity classifier), `router.py` (fallback chain across models/providers), `capability_registry.py` (static model metadata; which models are policy-disabled is read from `config/settings.py`'s `DISABLED_MODELS`), `providers/` (Anthropic + local Ollama), `observability.py` (per-call logging/cost tracking). The tier→model policy itself (`TIER_PREFERRED_MODEL`, `TIER_MIN_REASONING_STRENGTH`, `DISABLED_MODELS`) lives in `config/settings.py`, not scattered across these modules — edit that one file to change routing. |
| `charts/` | matplotlib chart generation for legacy server-rendered PNGs (`financial_charts.py`). |
| `storage/` | `database.py` (connection + schema init/migrations), `repositories.py` (all SQL — every other module goes through this, nothing else touches sqlite directly). |
| `schemas/` | `sqlite_schema.sql` — the full DDL. |
| `scripts/` | One-off bulk-import scripts for the various data workbooks (`scripts/import_*.py`). |

### Data ingestion flow

```
raw file (data/raw/<COMPANY>/...)
      │
      ▼
ingestion/detector.py   — which source adapter handles this file/format?
      │
      ▼
sources/<adapter>.py    — parse into NormalizedObservation rows
      │
      ▼
normalization/*.py      — canonical metric keys, periods, units, company id
      │
      ▼
storage/repositories.py — insert_financial_observations() (raw, per-source, kept forever)
      │
      ▼
storage/repositories.py — reconcile() → canonical_financials (one chosen value
                           per company/metric/period, by source trust_rank —
                           config/settings.py's DEFAULT_SOURCES)
      │
      ▼
financials/, retrieval/, charts/  — everything downstream reads only
                                     canonical_financials, never raw observations
```

Every insert is additive — conflicting/duplicate observations are never
overwritten; `reconciliation_log` records which source won and why, so the
full provenance trail survives.

### Research / AI layer (Context Optimization + Model Routing + Fallback)

This is the layer added to control LLM cost and add resilience. All three LLM
call sites (`research/assistant.py`, `research/insights.py`,
`research/signals_report.py`) follow the same pipeline:

```
Evidence retrieval (retrieval/structured_search.py + research/documents.py)
      │  — deterministic, always the same output for the same ingested data
      ▼
context/reuse.py         — is there a fresh, near-duplicate prior
                            (Signals reports only)   investigation already answering this? If so,
                            return it directly — zero LLM calls.
      │  (no reusable hit)
      ▼
llm/hardness.py           — classify the question's complexity
                            (QUICK / STANDARD / DEEP), deterministic
                            keyword/heuristic classifier, no LLM call
      │
      ▼
context/optimizer.py      — dedupe the Evidence list, score each line
                            (relevance × freshness × confidence ÷ token
                            cost), trim to the tier's token budget only if
                            it's actually over budget
      │
      ▼
context/graph.py          — (Signals reports only) is a sector-peer
                            company's prior investigation relevant here,
                            via a direct or curated-domain-edge metric
                            match? If so, appended as its own labeled
                            "Related prior investigations" block —
                            never merged into Evidence, always cited as
                            [INFERENCE] only
      │
      ▼
llm/router.py              — pick a model for this tier (preferred cloud
                            model → weaker cloud models → local Ollama),
                            skipping any candidate too weak for the tier;
                            on failure (rate limit/quota/outage/auth), fall
                            back to the next candidate automatically
      │
      ▼
llm/providers/{anthropic_provider,local_provider}.py  — the actual API call
      │
      ▼
llm/observability.py       — one log line + one llm_call_log row per call:
                            model/provider used, fallback used, tokens
                            in/out, context tokens before/after
                            optimization, estimated cost, latency
      │
      ▼
Answer / report text, returned to web/app.py or main.py
```

Design boundaries (deliberately kept separate, each independently
replaceable):

- **Context Optimizer** (`context/`) answers "what does this task need?" —
  never touches a model or provider. Within it, **reuse**
  (`context/reuse.py`, exact-scope match) and **knowledge graph**
  (`context/graph.py`, cross-company relationship match) are separate
  mechanisms answering different questions — "has this exact question been
  answered?" vs. "is a *different* company's reasoning relevant here?" — and
  a graph hit is never treated as Evidence about the question's own
  companies (see below).
- **Hardness Evaluator** (`llm/hardness.py`) answers "how hard is this?" —
  a pure function of the question text and evidence volume.
- **Model Router** (`llm/router.py`) answers "which available model handles
  it?" — capability-based (`llm/capability_registry.py`), not
  `if model == "..."` checks scattered through the codebase.
- **Model Provider** (`llm/providers/`) answers "how do we talk to that
  model?" — one module per provider (Anthropic cloud, local Ollama), same
  `generate()` shape, so a new provider only has to implement that shape.

`research/insights.py` and `research/signals_report.py`'s default model
stays pinned (no cross-tier fallback beyond the pinned model) to preserve
today's quality bar for those two features; only `research/assistant.py`
auto-routes across tiers by default. All three get the fallback-on-failure
and observability logging regardless of whether they're pinned or auto-routed.

### Web layer (`web/app.py`)

Single-file Flask app (`create_app()` factory), organized by feature area:

- **Auth**: `users` table, `werkzeug.security` password hashing, Flask
  `session` cookie (`session["user_id"]`), `g.user` populated in
  `before_request`. One seeded admin account (`admin`/`admin`) so the app is
  usable with zero setup. Admin-only routes gate on `g.user["is_admin"]`.
- **Company pages** (`/companies/<id>`): multi-tab company view (Overview,
  Financials, Valuation, Charts, Docs, Notes, Threads) — most tabs are
  server-rendered on page load; Financials/Valuation/Charts/Docs fetch their
  data from `*.json` feed endpoints (`valuation_feed.py`, `charts_feed.py`,
  `docs_feed.py`) and render client-side.
- **Research** (`/`, `/research/ask`, `/research/thread/generate`,
  `/research/thread/<id>`): the Ask-AI and Signals-investigation entry
  points, calling `research/assistant.py` / `research/signals_report.py`.
- **Investigations** (`/investigations`): list view over `generated_reports`.
- **Watchlist**, **Admin** (company metadata edits + raw-file import,
  the only web-UI path besides Docs uploads that writes ingested data),
  **Chat** (`/chat`), **Settings** (theme).
- **DB per-request**: `g.db` opened in `before_request`/closed in
  `teardown_appcontext`, via `storage.database.get_connection()`.

### CLI (`main.py`)

Thin argparse wrapper calling the same modules as the web app — `init`,
`status`, `seed-companies`, `add-company`, `import-nse-companies`,
`ingest`, `ingest-yfinance`, `list-companies`, `archive-company`,
`restore-company`, `analyze`, `ask`, `watchlist-add/remove`,
`list-watchlist`, `serve` (launches the Flask dev server). Useful for
bulk/scripted ingestion and quick terminal Q&A without the browser.

## Frontend

No SPA framework, no build step — Jinja2 templates rendered server-side,
with small vanilla-JS modules ("islands") that fetch JSON from Flask feed
endpoints and render/update the DOM directly.

### Templates (`web/templates/`)

| Template | Purpose |
|---|---|
| `base.html` | Shared shell (nav, header, theme, flash messages) every page extends. |
| `_header.html` | Top nav bar partial, includes the company search typeahead. |
| `_ask_ai.html` | Shared "Ask AI" modal/panel partial, included from company + research pages. |
| `landing.html`, `about.html` | Marketing/info pages. |
| `login.html`, `signup.html` | Auth pages. |
| `index.html` / `research.html` | Home page — the Research/Ask-AI entry point (`/`). |
| `company.html` | The multi-tab company page (Overview, Financials, Valuation, Charts, Docs, Notes, Threads). |
| `research_thread.html` | A single generated Signals report/thread view. |
| `investigations.html` | List of all generated reports. |
| `watchlist.html` | Pinned companies/threads. |
| `admin.html` | Company metadata editing + raw-file import. |
| `chat.html` | Freeform chat entry point (`/chat`). |
| `settings.html` | Theme preference. |

### JS modules (`web/static/js/`) — one per interactive tab, no shared framework

| Module | Backs |
|---|---|
| `header_search.js` | Header company typeahead against `/companies/search.json`. |
| `valuation_dashboard.js` | Financials tab's facts-only valuation tables (historical actuals/ratios/CAGR/sparklines only, no assumptions). |
| `valuation_dashboard_interactive.js` | Valuation Model tab — same data feed, but with adjustable required-return/growth/price assumptions and a recomputed 10-year projection. |
| `charts_overlay.js` | Charts tab — pick any attributes from `charts_feed.py` and overlay them on one time series (annual/quarterly granularity, trailing-window range, dual y-axis). |
| `docs_timeline.js` | Docs tab — fiscal-year-grouped document archive backed by `docs_feed.py`. |
| `notes_panel.js` | Notes tab — master-detail rail with a rich-text (contenteditable) compose/edit box, server-sanitized on save. |
| `threads_panel.js` | Delete affordance for the company page's Threads tab. |

Several of these (`valuation_dashboard.js`, `docs_timeline.js`,
`notes_panel.js`) started as visual prototypes built in Claude Design and
were ported to plain JS against this app's real data feeds — noted in each
file's header comment.

### Static assets (`web/static/`)

- `classical/styles.css` — the app's single CSS theme.
- `brand/` — logo assets.
- `fonts/` — self-hosted font files.
- `data/` — static JSON fixtures used by a couple of ported dashboards.

## Data model (SQLite, `schemas/sqlite_schema.sql`)

22 tables, grouped by concern:

- **Reference data**: `sources` (trust-ranked data providers), `metrics_dictionary`, `metric_aliases`.
- **Companies**: `companies`, `company_identifier_history`, `company_index_membership`, `company_list_column_settings`.
- **Financial data**: `financial_observations` (raw, per-source, never overwritten), `canonical_financials` (reconciled, one row per company/metric/period), `reconciliation_log` (audit trail of which source won and why), `macro_observations` (RBI + IITM rainfall series real and ingested — ~53K rows; MOSPI/IMD/IRDA registered, no files ingested yet).
- **Documents**: `documents` (Docs-tab uploads/links), `document_chunks` (FTS5-ready, not yet populated — no chunking pipeline exists yet).
- **Research/investigations**: `generated_reports` (persisted Signals reports), `research_thread_evidence`, `research_thread_followups`, `company_insights` (Key Insights history).
- **LLM observability**: `llm_call_log` — one row per `llm/router.py` call or `context/reuse.py` reuse hit (model/provider, fallback, tokens, cost, context-optimization accounting).
- **User content**: `company_notes`, `company_note_attachments`, `watchlist_items`.
- **Auth**: `users`.

Notable migration pattern (`storage/database.py`): `CREATE TABLE IF NOT
EXISTS` handles new tables; a handful of `_migrate_*` functions handle
`ALTER TABLE`-shaped changes (new columns, or full rebuild+copy for changes
SQLite's `ALTER TABLE` can't express, like relaxing `NOT NULL`) against an
existing database, run unconditionally and idempotently on every `init_db()`.

## Known gaps / not yet built

Everything below is a real gap today, not a hypothetical — grouped by the
area it affects. Nothing here is silently broken; each is either an
unexercised code path, a deliberately deferred feature, or a known
pre-existing test failure.

### Data ingestion & sources

- **No automated/recurring ingestion** — every ingest is a manual CLI/Admin-tab
  run against a single file. No scheduled job, no NSE/BSE filing scraper.
- **Multi-source reconciliation is unexercised** — `storage/repositories.py`'s
  `reconcile()` generalizes to picking the best of several sources by
  `trust_rank`, but in practice every company today has exactly one active
  source per metric, so the conflict-resolution path never actually runs
  against real conflicting data.
- **Macro data (RBI/IMD/MOSPI) isn't reconciled against company financials** —
  each series has exactly one provider by design (`config/settings.py`), so
  there's no cross-checking logic for it, unlike company financials.
- **MFIN source is reference PDFs only** — produces no `macro_observations`
  rows, unlike the other macro sources.

### Documents / Docs tab

- **No chunking, no full-text search** — `document_chunks` /
  `document_chunks_fts` exist in the schema (FTS5-ready) but nothing ever
  writes to them; document evidence is extracted straight into the prompt on
  every call instead.
- **No caching of downloaded PDF bytes** — a linked (non-uploaded) document is
  re-fetched over HTTP and re-parsed on every single question that touches it.
- **No automated document ingestion** — everything in the Docs tab is
  manually added via the upload/link form; there's no official-source pull
  pipeline.
- **No multi-company document attribution** — document evidence only backs
  single-company questions/reports today.

### Research / AI layer (Context Optimizer + Model Router)

- **No semantic/embedding-based matching** — `context/reuse.py`'s "is this the
  same question?" check is plain word-overlap (Jaccard similarity),
  deliberately conservative. It will miss genuine near-duplicates phrased
  differently rather than risk a false-positive reuse.
- ~~Reuse only covers Signals reports~~ — no longer true: every `/research/ask`,
  `/chat`, and per-company Ask AI answer is now persisted to `generated_reports`
  too (not just full Signals reports), and `research/assistant.py::answer_question()`
  checks `context/reuse.py` first, same as `research/signals_report.py` always did.
- **`research/insights.py` and `research/signals_report.py` don't auto-route
  by hardness** — both stay pinned to one fixed model (a deliberate choice to
  avoid changing their existing answer quality). Only `research/assistant.py`
  auto-routes across QUICK/STANDARD/DEEP tiers today, so Insights and Signals
  reports don't get tiering's cost savings yet.
- **No Anthropic prompt caching** (`cache_control`) — the same company's
  evidence block is rebuilt and resent in full on every call; nothing is
  cached provider-side even though large portions repeat across questions
  for the same company.
- **Local Ollama fallback is untested against a real Ollama instance** — the
  test suite mocks the HTTP layer; the fallback path has never been exercised
  against an actually running local model.
- **The Neo4j backend (`context/graph_neo4j.py`) is likewise untested against
  a real server** — its tests mock the driver/session and verify the Cypher
  parameters and result-scoring logic, not that the Cypher actually executes
  correctly against a live Neo4j instance. Worth a manual smoke test the
  first time it's pointed at a real server.
- **Cross-company reasoning-pattern reuse exists but is intentionally narrow**
  (`context/graph.py`) — it only connects a company to its *sector peers*
  (`companies.basic_industry`/`macro_economic_sector`) and only bridges
  through the ~6 hand-curated causal edges in
  `config/knowledge_graph_seed.py` (repo rate/CASA → NIM, NPAs → ROE,
  advances → NIM, deposits → CASA). It's real code, not a placeholder, but
  it will miss any relationship not in that seed list — extending it is a
  config-file edit, not a code change. No embeddings. Two backends share
  this limitation identically: the default (`context/graph.py`) computes
  every "edge" live from existing SQLite tables plus that static seed
  list, so there's nothing to keep in sync or go stale; the optional
  `GRAPH_BACKEND=neo4j` backend (`context/graph_neo4j.py`) persists the
  same edges as real Neo4j nodes/relationships instead — inspectable and
  visualizable in Neo4j Browser, rebuilt with a full idempotent resync
  before every traversal rather than incrementally maintained. Neo4j isn't
  managed by this app (no Docker lifecycle code) — start/stop it yourself,
  same as the Ollama fallback.
- **The graph doesn't pull live macro data** — seed edges name macro
  variables (e.g. `rbi_repo_rate`) descriptively for keyword matching and
  the LLM prompt; they don't query `macro_observations` for the actual
  current repo-rate value. A natural next step, not yet built.
- **Graph hits aren't logged to `llm_call_log`** — unlike a reuse hit
  (`context/reuse.py`, which does get an `llm_call_log` row via
  `observability.record_reuse`), a knowledge-graph match today is only
  visible inside the rendered prompt itself, not in the observability table.
- ~~No UI for `llm_call_log`~~ — no longer true: `/admin/usage` (profile menu →
  Usage, admin-only) summarizes total spend/tokens/calls plus breakdowns by
  task and by model. Still missing: per-investigation cost shown inline on
  the Investigations tab itself.

### Frontend

- **No shared component system** — each tab's JS module
  (`web/static/js/*.js`) is independent; there's no shared framework, so
  patterns (loading states, error handling) are duplicated per module rather
  than centralized.
- **A few modules are direct ports of Claude Design prototypes**
  (`valuation_dashboard.js`, `docs_timeline.js`, `notes_panel.js`) — worth
  re-checking against the current feed shape if `valuation_feed.py` /
  `docs_feed.py` / the notes routes ever change independently.

### Testing / CI

- **5 pre-existing failures in `tests/test_web.py`** — template/copy
  assertions out of sync with the current templates (home page copy, the
  `ANTHROPIC_API_KEY` banner text, the legacy `/research` redirect, the
  embedded `company_id` JSON on the research page). These predate the
  Context Optimizer / Model Router work and were verified via `git stash` to
  fail identically on a clean checkout — not something introduced or fixed
  by that work.
- **No CI pipeline** — no `.github/workflows` or other CI config in this
  repo; tests only run locally, on demand.

## Key design principles

1. **Deterministic Calculation Layer** — the LLM never computes a number.
   Every FACT/CALCULATION in an answer must restate a line Python already
   computed; the LLM only interprets and narrates.
2. **Evidence & Citations** — every LLM claim carries a `[FACT]` /
   `[CALCULATION]` / `[MANAGEMENT_STATEMENT]` / `[INFERENCE]` tag tracing
   back to a specific retrieved `Evidence` line.
3. **Retrieval never calls the LLM** — `retrieval/`, `research/documents.py`
   are pure Python/SQL; the Context Optimizer and Model Router operate
   entirely between retrieval and the single LLM call.
4. **Source provenance & reconciliation** — raw observations are never
   overwritten; a trust-ranked reconciliation step decides what's canonical,
   and that decision is auditable.
5. **Local-first, self-use** — single SQLite file, no external services
   required beyond the Anthropic API (optional local Ollama fallback), no
   deployment target, admin account seeded automatically.
6. **Cost-aware LLM execution** — hardness-based model routing, cloud→cloud→
   local fallback, context deduplication/budgeting, and reuse-before-recompute
   are all inspectable via `llm_call_log`, not invisible.
