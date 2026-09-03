# Architecture

This document describes the current architecture of **Signals**, an Equity AI
Research Assistant — a self-use, local-first Flask + SQLite application for
researching listed companies, with a primary focus on the US and India markets,
with an LLM research assistant grounded in deterministically retrieved evidence.

For product/feature scope, see [README.md](README.md) and
[FeatureList.md](FeatureList.md). For running the app, see
[USER_GUIDE.md](USER_GUIDE.md).

### The four-layer split

The guiding division of responsibility across the whole system, each layer
answering a different question and never doing another layer's job:

- **SQLite knows the facts** — `canonical_financials`, `macro_observations`,
  `knowledge_claims`, and everything else under [Data model](#data-model-sqlite-schemassqlite_schemasql)
  is the one source of truth. Nothing downstream invents a fact SQLite
  doesn't already have.
- **The Knowledge Graph knows the relationships** — `context/knowledge_graph.py`
  / `context/graph_neo4j.py` answer "what is connected to what, when was it
  true, and what evidence supports that relationship," projected from
  SQLite, never a second source of truth for the facts themselves (see
  [Ingestion Coordinator & Knowledge Builder](#ingestion-coordinator--knowledge-builder-admin--ingest)).
- **FTS5 and the Vector DB are retrieval indexes over the same authoritative
  text, not two different truths** — `document_chunks`/`documents` (SQLite)
  hold the authoritative textual evidence (annual reports, transcripts,
  investor presentations, regulatory filings, macro/research reports —
  anything eligible under [Document Retrieval](#document-retrieval-retrievaldocument_searchpy));
  `document_chunks_fts` (FTS5/BM25) is the exact/lexical retrieval index over
  it, and the `VectorStore` (`retrieval/vector_store.py`, Qdrant by default)
  is the semantic retrieval index over it — see
  [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy).
  **Invariant: FTS5, the Vector DB, and Neo4j are retrieval/projection
  structures. They must never silently become competing sources of truth.**
  Every one of them is fully rebuildable from SQLite's `documents`/
  `document_chunks` at any time (`python main.py vector-backfill`,
  `python main.py replay-events --worker chunk_indexer`) — losing any of
  them loses a retrieval path, never a fact.
- **The LLM reasons about what they mean** — every call site
  (`research/assistant.py`, `insights.py`, `signals_report.py`,
  `knowledge_builder.py`) is handed a compact, pre-computed evidence block
  and asked to interpret/narrate it, never to fetch or calculate a number
  itself (see [Key design principles](#key-design-principles) #1 and #3).
- **The Planner decides what to investigate next** — `research/investigation.py`
  (the hypothesis-driven investigation pipeline: `research/hypothesis_generator.py`
  generates hypotheses, `research/investigation_planner.py` gathers evidence per
  hypothesis, `research/hypothesis_evaluator.py` evaluates each one, and
  `research/research_synthesis.py` ranks and synthesizes the findings),
  reachable from the Research tab's "Run structured investigation" button
  (`/investigate/generate`, `/investigate/<id>`). For a question, it generates
  several competing hypotheses, then per hypothesis runs an
  Orchestrator-controlled evidence-sufficiency loop — an
  `INSUFFICIENT_EVIDENCE` verdict triggers one more gap-targeted retrieval
  pass and re-evaluation, bounded by 4 termination controls (evidence
  sufficiency, `MAX_EVIDENCE_ITERATIONS`, a wall-clock deadline, and a
  no-new-evidence check) — before ranking and synthesizing the findings. See
  [Golden Research Loop validation](#golden-research-loop-validation) for
  what this closed (cross-company association, point-in-time `as_of`
  scoping, indicator evidence) and [Known gaps](#ingestion-coordinator-knowledge-builder-research-knowledge-graph--document-retrieval)
  for what's still open (no cost/token budget control; a retry re-runs the
  same broad evidence-gathering pass rather than targeting one capability).
  Every hypothesis, its evidence, verdict, and rank persists to
  `investigations`/`investigation_hypotheses`/`investigation_hypothesis_evidence`
  and stays individually queryable — distinct from `research/signals_report.py`'s
  one-narrative Signals reports.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | Flask (server-rendered Jinja2, no SPA framework) |
| Database | SQLite (single file, `data/equity_research.db`) |
| Knowledge graph | SQLite by default (`context/graph.py`, pure Python traversal over existing tables) — optionally a real Neo4j graph instead (`context/graph_neo4j.py`, `GRAPH_BACKEND=neo4j`), with automatic fallback to SQLite if Neo4j isn't reachable. Not managed by this app (no Docker lifecycle code) — start/stop it yourself, same as the Ollama fallback. |
| Document retrieval | FTS5/BM25 (SQLite, always on) + semantic/vector search — Qdrant by default (`retrieval/vector_store_qdrant.py`, `VECTOR_STORE_BACKEND=qdrant`), not managed by this app (same as Neo4j/Ollama), with automatic fallback to FTS5-only if unreachable. Embeddings: `sentence-transformers` on-device by default (`EMBEDDING_PROVIDER=local`, zero API cost), Voyage AI optional (`EMBEDDING_PROVIDER=voyage`, needs `VOYAGE_API_KEY`). |
| LLM provider | Anthropic Claude (sonnet/haiku — Opus is registered but policy-disabled, `config/settings.py`'s `DISABLED_MODELS`), with a local Ollama fallback |
| Frontend | Server-rendered HTML + vanilla JS "islands" (no build step, no bundler, no npm) |
| Charts | matplotlib (server-rendered PNG) for legacy charts; client-side JS + JSON feeds for the interactive dashboards |
| Tests | pytest |

There is no separate frontend build pipeline — no `package.json`, no
webpack/vite. Pages are Jinja2 templates; interactivity is plain `<script>`
tags fetching JSON from Flask routes.

## High-level architecture

The consolidated view below is the single source of truth for how every
subsystem in this document fits together — every box and label names a real
module/table/table-group already described elsewhere in this file; nothing
here is aspirational. The more detailed pipeline diagrams further down (data
ingestion, the Ingestion Coordinator, Document Retrieval, the Configurable
Indicator Framework, evidence retrieval → LLM routing, the Golden Research
Loop's hypothesis-driven investigation flow) each zoom into one box below.

```
                           ┌──────────────────────────────────────────────────────────────┐
                           │ Browser — Jinja2 server-rendered HTML + vanilla JS "islands" │
                           │ (no SPA framework, no build step)                            │
                           └──────────────────────────────────────────────────────────────┘
                                                           │ HTTP
                                                           │
             ┌─────────────────────────────────────────────┐      ┌────────────────────────────────────┐
             │ web/app.py (Flask)                          │      │ main.py (CLI)                      │
             │ routes . auth/session . JSON feed endpoints │      │ same modules, terminal entry point │
             └─────────────────────────────────────────────┘      └────────────────────────────────────┘
                                    │                                                │
                                    ▼                                                ▼
 ┌────────────────────────────────────────────────────┐    ┌───────────────────────────────────────────────────────┐
 │ DETERMINISTIC LAYER (never calls an LLM)           │    │ RESEARCH / AI LAYER (evidence-grounded reasoning)     │
 │                                                    │    │                                                       │
 │ ingestion/   detect -> sources/ -> normalization/  │    │ retrieval/  structured_search.py, document_search.py  │
 │ companies/   registry, lifecycle, stock_actions    │    │ context/    optimizer, reuse, graph.py/graph_neo4j.py │
 │ financials/  YoY/CAGR, ratios, text report         │    │ research/   assistant, insights, signals_report,      │
 │ indicators/  rules, Global->Sector->Company config │    │             knowledge_builder, investigation.py       │
 │ analytics/   cross-company pattern scans (Tools)   │    │ llm/        hardness -> router -> providers -> obs.   │
 │ charts/      matplotlib PNG charts                 │    │                                                       │
 └────────────────────────────────────────────────────┘    └───────────────────────────────────────────────────────┘
                            │                                                          │
                            ▼                                                          ▼
                     ┌──────────────────────────────────────────────────────────────────────────┐
                     │ storage/  —  db_types . repositories . fact_store . company_repository . │
                     │              indicator_repository . investigation_repository             │
                     │ the only layer in the codebase that knows SQLite exists                  │
                     └──────────────────────────────────────────────────────────────────────────┘
                                                           │
                                                           ▼
                     ┌─────────────────────────────────────────────────────────────────────────┐
                     │ data/equity_research.db  (SQLite, 46 tables, schemas/sqlite_schema.sql) │
                     │ the one source of truth every other layer reads and writes through      │
                     └─────────────────────────────────────────────────────────────────────────┘
                                                          │
                                                          ▼
                     ┌──────────────────────────────────────────────────────────────────────────┐
                     │ Knowledge Graph — projected from SQLite, never a 2nd source of truth     │
                     │ SQLite traversal by default (context/graph.py); optionally a real Neo4j  │
                     │ graph (GRAPH_BACKEND=neo4j), automatic fallback to SQLite if unreachable │
                     └──────────────────────────────────────────────────────────────────────────┘


                         ↑ fetched by ingestion/sources/            ↑ called by llm/providers/
                      ┌─────────────────────────────┐        ┌─────────────────────────────────┐
                      │ EXTERNAL DATA SOURCES       │        │ EXTERNAL LLM PROVIDERS          │
                      │ NSE/BSE XBRL, Screener.in,  │        │ Anthropic Claude API (primary), │
                      │ Yahoo Finance, RBI/IMD/FRED │        │ local Ollama (fallback)         │
                      └─────────────────────────────┘        └─────────────────────────────────┘
```

Notes on what the diagram compresses for readability (each is exact
elsewhere in this document, not simplified away here):

- **`indicators/`** sits in the Deterministic layer because it is, by
  design, pure rule evaluation with zero LLM calls — see [Configurable
  Indicator Framework](#configurable-indicator-framework-indicators).
- **`research/investigation.py`** (the hypothesis-driven investigation
  pipeline) is the one module that spans both layers in practice: it's
  LLM-orchestrated (Research/AI layer) but, per [Golden Research Loop
  validation](#golden-research-loop-validation), now also reads
  `indicators/`'s deterministic output as evidence — shown here under
  `research/` since the orchestration and every LLM call live there.
- **The Knowledge Graph** is drawn once, fed from SQLite — in reality both
  `context/graph.py` (sector-peer traversal) and `context/knowledge_graph.py`
  (cross-entity claim traversal) maintain it, and both are called
  from the Research/AI layer, not from storage directly; the arrow from
  `data/equity_research.db` represents "projected from," not a literal
  runtime call path.
- **`main.py`** and **`web/app.py`** are peers calling the same two layers,
  not a hierarchy — the CLI is not routed through the Flask app.

The guiding split (see `research/assistant.py`'s module docstring): **retrieval
never calls the LLM**. Everything under `companies/`, `ingestion/`,
`normalization/`, `financials/`, `indicators/`, `retrieval/` is deterministic Python/SQL —
the LLM is only ever handed a compact, pre-computed `Evidence` block and asked
to reason over it, never to fetch or calculate numbers itself.

## Backend

### Module map

| Package | Responsibility |
|---|---|
| `main.py` | CLI entry point (argparse) — ingest, analyze, ask, serve, admin commands. Thin wrapper over the same modules the web app uses. |
| `web/` | Flask app: routes, templates, auth, session, JSON feed endpoints for the JS-driven tabs. |
| `companies/` | Company registry (`registry.py` — country/currency/fiscal-year-end are per-company, not global), lifecycle/archive rules (`lifecycle.py`), NSE bulk-import (`nse_import.py`, India-only — no US equivalent yet, see Known gaps), discrete stock-action records (`stock_actions.py` — splits/bonus/rights issues, raw events only). |
| `ingestion/` | File-format detection (`detector.py`), the ingest pipeline (`pipeline.py`) that runs a raw file through a source adapter → normalization → reconciliation, validation (`validation.py`), and the Ingest-queue orchestration layer (`coordinator.py` — discovers unprocessed financial/macro files and documents, dispatches each to the existing pipeline below or to `research/knowledge_builder.py`; see its own section). Also the **Event Bus** (`event_bus.py`, `events.py` — see [Dataset-centric ingestion: the event bus](#dataset-centric-ingestion-the-event-bus-ingestionevent_buspy) below) and its **workers** (`ingestion/workers/`: `chunk_indexer_worker.py`, `financial_derivation.py`, `knowledge_builder_worker.py`), plus `batch_log.py` (audit logging for `scripts/`' bulk-fetch batch runs). |
| `sources/` | Source adapters — one per data provider. Company financials: `screener.py` (India, Screener.in exports), `yfinance_financials.py` (US and other non-Indian tickers, live-fetched via Yahoo Finance), `proprietary.py` (hand-prepared workbooks), `nse_xbrl.py`/`xbrl_generic.py` (NSE XBRL filings — now the trust_rank-0 source of truth for a validated reporting period), `nse_shareholding.py` (NSE shareholding-pattern XLS), `nse_fetch.py` (shared NSE download/parse plumbing the two above build on), `yfinance_prices.py` (daily OHLCV, feeds the separate price-history subsystem below, not `financial_observations`). Non-company macro data: `macro.py`'s generic CSV convention (India: `rbi`/`imd`/`iitm`/`mospi`/`irda`), source-specific parsers for shapes that don't fit it (`rbi_indicators.py`/`rbi_dbie_tables.py`/`rbi_bank_infrastructure.py`, `iitm_rainfall.py`), and `fred.py` (US — FRED, live-fetched, the US counterpart to the RBI/IMD/IITM adapters). Each turns a raw file/API response into `NormalizedObservation`/`MacroNormalizedObservation` rows, behind the common `base.py::SourceAdapter` interface. |
| `normalization/` | Canonicalizes raw labels into the shared metric vocabulary (`financials.py` — also localizes each metric's default unit to the company's `currency`, e.g. `INR_CRORE`→`USD_MILLION`), company identifiers (`companies.py`), fiscal periods (`periods.py` — parametrized by each company's `fiscal_year_end_month`, not a single global calendar), and units/currency (`units.py`). |
| `financials/` | Deterministic math over `canonical_financials`: YoY/CAGR (`calculations.py`), ROA/ROE/vendor-reported ratios (`ratios.py`), and the human-readable text report (`report.py`) both the CLI's `analyze` command and the LLM evidence retrieval are built from. |
| `analytics/` | Cross-company pattern scans for the Tools tab (`patterns.py` — e.g. `detect_yoy_spikes()`, the same "significant YoY move" definition the Configurable Indicator Framework's `financial_trajectory` rule family reuses). No per-user configuration, no LLM call — a scan, not a rule engine. |
| `indicators/` | The **Configurable Indicator Framework** — deterministic, rule-based factual patterns over existing facts (never an LLM-generated insight). `framework.py` (`IndicatorRule`/`RULE_REGISTRY`/`TriggeredIndicator` shapes), `rules.py` (the seeded `shareholding` and `financial_trajectory` rule families), `config.py` (pure Global→Sector→Company override resolution), `evaluation.py` (`evaluate_company_indicators()`, the engine), `settings.py` (the Settings page's read/write model). See [Configurable Indicator Framework](#configurable-indicator-framework-indicators) below. |
| `retrieval/` | `structured_search.py` — turns `financials/`'s calculations into typed `Evidence` for the LLM. `document_search.py` — FTS5 keyword search over `research/document_chunker.py`'s indexed chunks, returning typed `DocumentPassage` results. `embedding_provider.py`/`embedding_provider_local.py`/`embedding_provider_voyage.py` — the `EmbeddingProvider` abstraction (local sentence-transformers default, Voyage AI opt-in), independent of the vector store. `vector_store.py`/`vector_store_qdrant.py` — the `VectorStore` abstraction (Qdrant the only concrete backend; the sole module allowed to import `qdrant_client`). `semantic_search.py` — embeds a query, searches the VectorStore, hydrates hits into `DocumentPassage`. `hybrid_search.py` — Reciprocal-Rank-Fusion combination of FTS5 + semantic results, with graceful degradation and retrieval diagnostics. `observability.py` — logs each hybrid retrieval call. Retrieval only, no LLM calls, in any of them. |
| `research/` | Four LLM call sites: `assistant.py` (Q&A), `insights.py` (Key Insights summaries), `signals_report.py` (full Signals investigation reports), and `knowledge_builder.py` (structured knowledge extraction from a document — its own section below) — plus `evidence.py` (the `Evidence`/citation model), `documents.py` (extracts `MANAGEMENT_STATEMENT` evidence from uploaded/linked Docs-tab PDFs, and exposes `document_text()`/`document_pages()`, shared with `knowledge_builder.py`/`document_chunker.py`), `document_chunker.py` (no LLM call, purely mechanical page-scoped chunking + FTS5 indexing), and `macro_evidence.py` (the third evidence source — macro/regulatory data spanning both India and US sources, attributed per-series to `"INDIA"` or `"USA"`; a narrow, deliberate exception to "retrieval never calls the LLM," since an LLM call picks which macro series/date-range apply before the deterministic fetch runs). |
| `context/` | The **Context Optimizer** — `optimizer.py` (dedup, value-scoring, token-budget compression of an `Evidence` list), `reuse.py` (reuse-before-recompute: returns a fresh, near-duplicate prior investigation instead of a new LLM call — now used by both `research/assistant.py`'s Q&A path and `research/signals_report.py`'s full reports), `graph.py`/`graph_neo4j.py` (sector-peer knowledge-graph traversal: surfaces a *different* company's relevant prior investigation, via `config/knowledge_graph_seed.py`'s curated domain relationships), and `knowledge_graph.py` (the Research Knowledge Graph — a distinct, cross-*entity* traversal over the Knowledge Builder's `knowledge_claims`/`knowledge_relationships`, its own section below). Both graphs are pure Python/SQLite by default, or the same real Neo4j instance when `GRAPH_BACKEND=neo4j` (sharing `Company` nodes between the two), with automatic fallback to SQLite if Neo4j isn't reachable. |
| `llm/` | The **Model Router + Fallback layer** — `hardness.py` (task-complexity classifier), `router.py` (fallback chain across models/providers), `capability_registry.py` (static model metadata; which models are policy-disabled is read from `config/settings.py`'s `DISABLED_MODELS`), `providers/` (Anthropic + local Ollama), `observability.py` (per-call logging/cost tracking). The tier→model policy itself (`TIER_PREFERRED_MODEL`, `TIER_MIN_REASONING_STRENGTH`, `DISABLED_MODELS`) lives in `config/settings.py`, not scattered across these modules — edit that one file to change routing. |
| `charts/` | matplotlib chart generation for legacy server-rendered PNGs (`financial_charts.py`). |
| `config/` | `settings.py` (paths, source trust order, LLM/model-tiering policy, repo-relative path helpers), `knowledge_graph_seed.py` (curated sector-peer causal edges — `context/graph.py`'s vocabulary), `knowledge_ontology.py` (the fixed `ENTITY_TYPES`/`RELATIONSHIP_TYPES`/`CLAIM_TYPES` vocabulary `research/knowledge_builder.py`'s extraction validates against, kept distinct from `STRUCTURAL_NODE_TYPES` — Claim/Evidence/Document/TimePeriod, never something the model extracts by name — plus `CANONICAL_HOME`, an explicit map of which subsystem owns each concept's real value). |
| `storage/` | `database.py` (connection + schema init/migrations), `repositories.py` (general-purpose SQL — reference data, financials, documents, Knowledge Builder, generated reports, LLM observability, the event store), `db_types.py` (`DBConnection`/`Row` — the backend-agnostic types every other module now type-hints against), `fact_store.py` (`FactStore` — the DI seam `research/`/`context/`/`indicators/` call through instead of importing `repositories.py` directly), `company_repository.py` (companies/stock-actions SQL), `indicator_repository.py` (indicator config + audit-trail SQL), `investigation_repository.py` (the `investigation_companies` join table). Together with `price_database.py`/`price_repository.py`/`price_store.py` below, the only place in the codebase that knows SQLite exists — see [Storage layer and database portability](#storage-layer-and-database-portability-storagedb_typespy) below. |
| `storage/price_database.py`, `price_repository.py`, `price_store.py` | A second, parallel storage stack for daily OHLCV price history (`daily_prices`, `schemas/price_schema.sql`), deliberately kept in its own file (`data/price_history.db`, `config/settings.py`'s `PRICE_DB_PATH`) rather than `equity_research.db` — see [Price history](#price-history-storageprice_py-schemasprice_schemasql) below. |
| `schemas/` | `sqlite_schema.sql` — the main DDL (46 tables). `price_schema.sql` — the separate `daily_prices` price-history DDL (its own db file, not part of the 46). |
| `scripts/` | One-off/bulk scripts: data-workbook imports (`import_*.py`, `parse_equity_analysis_workbook.py`), NSE XBRL/shareholding batch fetchers (`batch_fetch_nse.py`, `fetch_nse_xbrl.py`, `fetch_nse_shareholding.py`, `xbrl_diagnostic.py`), backfills (`backfill_company_websites.py`, `backfill_sector_industry.py`, `backfill_price_history.py`), the daily price job (`fetch_daily_prices.py`), and db sharding (`db_shard.py`/`db_unshard.py` — see [USER_GUIDE.md](USER_GUIDE.md#13-database-sharding-git-storage)). |

### Storage layer and database portability (`storage/db_types.py`)

`storage/repositories.py` was already "the only place that touches SQL" for
most of the app; this pass makes that literally true for the *type system*
too, not just for which function runs a query. `storage/db_types.py` defines
two backend-agnostic aliases — `DBConnection` (a `Protocol` matching DB-API
2.0's `execute`/`executemany`/`executescript`/`commit`/`cursor`/`close`) and
`Row` (a `Mapping[str, Any]` alias) — and every function outside `storage/`
that takes a `conn` parameter, or reads back a row, is now typed against
these instead of `sqlite3.Connection`/`sqlite3.Row` directly (a mechanical,
non-behavioral change across roughly 40 files in `research/`, `context/`,
`web/`, `retrieval/`, `sources/`, `financials/`, `ingestion/`, `analytics/`,
`charts/`, `llm/`). sqlite3's own connection/row objects already satisfy
both shapes structurally, so nothing here changes behavior; what it buys is
that none of those modules needs `import sqlite3` just to type-hint a
parameter, and it's the same "interface, not concrete implementation"
principle `storage/fact_store.py`'s `FactStore` already applies one level up
(*which function* runs a query) — taken one level further down (*what
connection/row shape* a function is even allowed to assume).

Two new repository modules absorb raw SQL that used to live inline in
business-logic modules, following the discipline `storage/repositories.py`
already set for everything else:

- **`storage/company_repository.py`** — every statement
  `companies/registry.py`, `companies/lifecycle.py`, and
  `companies/stock_actions.py` need, plus the one-off backfill/batch scripts
  under `scripts/` (website and sector/industry backfills, daily-price
  fetch, price-history backfill, the NSE batch-fetch script, the
  equity-analysis-workbook importer) — those callers now hold
  validation/business rules only and never call `conn.execute(...)`
  themselves.
- A handful of new functions on **`storage/repositories.py`** itself
  (`get_document`, `get_metric_dictionary_entry`, `get_metric_key_for_alias`,
  `seed_metric_vocabulary`), absorbing SQL that used to live inline in
  `ingestion/coordinator.py`, `ingestion/workers/*.py`, `financials/ratios.py`,
  and `normalization/financials.py`.

Net effect: `storage/*.py` and `schemas/sqlite_schema.sql` are now the only
places in the codebase that know SQLite exists — swapping the backend means
replacing `storage/`'s implementations, not touching business logic
anywhere else. `scripts/db_shard.py`/`db_unshard.py` are a deliberate
exception, not a gap: they're SQLite-file-splitting utilities with no
portability story of their own, and stay that way on purpose.

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

### Ingestion Coordinator & Knowledge Builder (Admin → Ingest)

A second entry point into ingestion, alongside the CLI's `ingest` command
and Admin's Import Data panel — `ingestion/coordinator.py`, surfaced as the
Admin tab's **Ingest** panel. Where the flow above assumes you already know
which file to ingest, the coordinator answers "what's sitting unprocessed
right now?" for both financial/macro files under `data/raw/` and documents
already added via the Docs tab, tracks their status, and dispatches each to
the right *existing* pipeline — it introduces no parallel ingestion logic
of its own.

```
Admin → Ingest UI
      │
      ▼
ingestion/coordinator.py  — discover (filesystem scan + ingestion/detector.py
      │                      for files; the documents table itself for docs)
      │                      → track status (PENDING/NEEDS_REVIEW/PROCESSING/
      │                      PROCESSED/FAILED, content-hash keyed) → dispatch
      │
      ├───────────────────────────┬───────────────────────────────┐
      ▼                           ▼                                ▼
ingestion/pipeline.py       documents table                research/knowledge_builder.py
(ingest_file() /            (processing_status /            — one LLM call per document,
 ingest_macro_file() /       processed_at /                  extracting structured
 ingest_bank_infra...())     error_message columns —         knowledge (entities, claims,
 — the same pipeline the     the row's own identity,         relationships), grounded and
 CLI/Import Data panel       nothing duplicated)              provenanced, validated against
 already use, unchanged                                       config/knowledge_ontology.py
```

- **Idempotent, content-hash tracked** (`ingestion_queue_items`): a sha256
  of each discovered file is stored alongside its status. An unchanged,
  already-`PROCESSED` file is never reprocessed on rescan; a changed one is
  re-flagged `PENDING`; a `FAILED` row keeps its error message until
  explicitly retried rather than being silently reset by the next scan.
- **Paths are stored repo-relative**, not absolute (`config/settings.py`'s
  `to_repo_relative()`/`from_repo_relative()`, used by `ingestion_queue_items`,
  `documents.raw_file_path`, and `company_note_attachments.raw_file_path`
  alike) — an absolute path bakes in the repo folder's current name/location
  and silently breaks every stored reference if the repo is ever renamed or
  moved (as this one already has been once).
- **Knowledge Builder** (`research/knowledge_builder.py`) extracts entities,
  claims, and relationships from one document's text
  (`research/documents.py::document_text()`, shared with the Q&A evidence
  path), asking the model for structured JSON — every claim carries its own
  `claim_type` (FACT/CALCULATION/MANAGEMENT_OPINION/PREDICTION/INFERENCE/
  CORRELATION/CAUSATION), `category`, `speaker`, fiscal period, an
  `extraction_confidence`, and a supporting quote. Reuses the same Model
  Router + Fallback and `llm/observability.py` logging
  (`task_name="knowledge_extraction"`) every other LLM call site uses — this
  is a fourth call site alongside Q&A/Insights/Signals, not a separate stack.
- **Additive, never overwritten**: a new document's claims are always fresh
  `INSERT`s into `knowledge_claims`, never an `UPDATE` to a prior document's
  rows — the same discipline `financial_observations` already follows.
  Entities ARE deduped/shared across documents
  (`get_or_create_knowledge_entity`, keyed by `(entity_type, name,
  company_id)`) — the same "Product: iPhone" entity isn't re-created every
  time a new document mentions it.
- **A document with no extractable text succeeds with zero claims, not an
  error** — a non-PDF upload, or a link that doesn't look like a PDF
  (`document_text()` returning `None`); no LLM call is even attempted, same
  "absence isn't an error" rule `research/documents.py`'s own evidence path
  already follows. An extraction that *does* have text but fails (LLM
  unavailable, response truncated at the token limit, unparseable JSON)
  marks the document `failed` with the reason recorded — retryable the same
  way a failed financial file is.
- **Persisted in plain SQL first** (`knowledge_entities`/`knowledge_claims`/
  `knowledge_relationships`/`knowledge_evidence`) — projected into the graph
  by the next section, never the other way around.
- **The diagram above simplifies one thing**: the Knowledge Builder and
  Document Retrieval's chunking/indexing step are no longer *directly*
  called by `ingestion/coordinator.py` — both now run as independent workers
  subscribed to the event bus's `document` events (`ingestion/workers/
  knowledge_builder_worker.py`, `chunk_indexer_worker.py`), so a chunking
  failure can never undo an already-successful extraction or vice versa. See
  [Dataset-centric ingestion: the event bus](#dataset-centric-ingestion-the-event-bus-ingestionevent_buspy)
  immediately below.

### Dataset-centric ingestion: the event bus (`ingestion/event_bus.py`)

Both ingestion entry points above — the CLI/Import Data panel's
`ingestion/pipeline.py` and the Ingest queue's `ingestion/coordinator.py` —
publish one `DatasetIngestedEvent` (`ingestion/events.py`) on every
successful ingest, regardless of dataset type (company financials, macro,
shareholding, a document, ...). This is the layer that lets "ingestion" and
"everything that should happen after ingestion" stay decoupled: a worker
decides for itself whether an event is its concern, rather than the
ingestion code knowing in advance who needs to hear about it.

```
ingestion/pipeline.py            ┐
ingestion/coordinator.py         ┴──▶  event_bus.publish(DatasetIngestedEvent)
                                          │
                                          ├─▶ storage/repositories.py's
                                          │   dataset_events table (Event Store —
                                          │   the event is persisted before any
                                          │   worker runs)
                                          │
                                          ▼
                                  every registered worker, synchronously,
                                  each in its own try/except:
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
     financial_derivation.py   knowledge_builder_worker.py  chunk_indexer_worker.py
     (company_financials       (document events →           (document events →
      events → reconcile()     research/knowledge_          research/document_
      per changed key)         builder.py extraction)        chunker.py indexing)
```

- **In-process and DB-backed, not a message broker** — consistent with the
  app's modular-monolith shape; no new infrastructure. A worker is a plain
  `def run(conn, event) -> WorkerResult` registered via `register_worker(name,
  version, run)`; it inspects `event.dataset_type`/`event.scope` and returns
  `WorkerResult(status="skipped")` for anything not its concern — one calling
  convention for every worker, no separate relevance predicate to register.
- **The event carries a pointer, not the data** — `storage_reference`
  points a worker back at the table/row it should re-read, so replaying an
  event never needs to re-fetch or re-ingest source data.
- **Idempotent replay, not idempotent publish** — every real ingestion mints
  a fresh event (nothing to dedupe there); `replay()` is where it matters —
  it skips a worker for an event it already logged `ok`/`skipped` for
  (`UNIQUE(event_id, worker_name, worker_version)` on `worker_processing_log`),
  unless `--force`. Bumping a worker's `WORKER_VERSION` is the normal way to
  force reprocessing after a logic change, since the new version has no log
  row yet.
- **CLI surface**: `main.py replay-events` (re-dispatch stored events to
  registered workers — recovery/backfill/audit, filterable by `--event-id`/
  `--dataset-type`/`--worker`/`--since`, with `--force`), `list-batch-runs`/
  `show-batch-run` (audit log for `scripts/`'s bulk-fetch batch jobs —
  `batch_job_runs`/`batch_job_items`, a related but separate audit trail from
  the event store).
- **Backing tables**: `dataset_events` (the Event Store), `worker_processing_log`
  (one row per worker × event, `ok`/`skipped`/`failed` with an
  `output_reference`), `batch_job_runs`/`batch_job_items` (bulk-script audit
  log, `scripts/batch_fetch_nse.py` and similar — logged via
  `ingestion/batch_log.py`, not itself a worker/event-bus concept).

### Research Knowledge Graph (`context/knowledge_graph.py`)

Answers a cross-entity, cross-*company* question plain per-company SQL
doesn't do well: "which claims, from ANY company, are connected to this
entity?" — e.g. every company's claims touching the `Risk` entity "Interest
Rate Volatility," not just one company's own. Same backend-choice and
graceful-degradation shape as `context/graph.py`'s sector-peer traversal:
SQLite by default (`storage/repositories.py::find_knowledge_claims_about_entity()`,
a real join, not a stub), a real Neo4j graph when `GRAPH_BACKEND=neo4j`
(`context/graph_neo4j.py::sync_knowledge_graph()`/`find_claims_about_entity()`),
with automatic fallback to SQLite if unreachable.

- **Company nodes are shared with the sector-peer graph, not duplicated** —
  a `knowledge_entities` row of type `Company` merges into the *exact same*
  `(:Company {id: ...})` node `context/graph_neo4j.py::sync_graph()` already
  creates for sector-peer relationships, via a common `:KGNode{kg_key}` tag
  every node gets. Every non-Company entity gets its own `(:Entity {id:
  entity_id})` node, keyed by the SQL primary key (not by name), so two
  different companies' same-named "Growth" strategy entities never collide.
- **Full idempotent resync before every query**, not incremental sync —
  same philosophy `sync_graph()` already uses for sector-peer data: SQLite
  stays the source of truth, cheap to fully rebuild at today's scale.
- **`Claim --VALID_DURING--> TimePeriod`, `Claim --SUPPORTED_BY--> Evidence`,
  `Claim --ABOUT--> (every entity its relationships touch)`, and `Company
  --STATES--> Claim`** are added on top of the claim's own extracted
  relationship (e.g. `MacroFactor --MAY_AFFECT--> Metric`,
  `Company --EXPOSED_TO--> Risk`) — every graph edge traces back to the
  claim and evidence quote that asserted it, never invented by the
  traversal itself.
- **`STATES` links from the claim's company, not a resolved `ManagementPerson`
  node** — the spec's own example shows `Management --STATES--> Claim`;
  today's edge is the coarser but always-available `Company --STATES-->
  Claim` (a `speaker` string like "CEO" is stored on the `Claim` node
  itself, not resolved to a specific `ManagementPerson` entity node).

### Document Retrieval (`retrieval/document_search.py`)

Answers a different question from the Knowledge Builder/Research Knowledge
Graph above: not "what structured claim did this document make" but "where
was something *similar* discussed?" — a keyword-relevance search over the
document's own raw text, for when what's needed is the passage itself, not
an LLM's summary of it.

```
documents table (raw_file_path / source_url)
      │
      ▼
research/documents.py::document_pages()      — same PDF/link resolution as
      │                                         document_text(), but keeping
      │                                         page boundaries intact
      ▼
research/document_chunker.py                 — fixed-size, page-scoped
      │  chunk_and_index_document()             chunks (1500 chars, 150
      │                                         overlap) — no LLM call,
      │                                         purely mechanical
      ▼
document_chunks (+ document_chunks_fts, FTS5) — SQLite is the search index
      │                                          itself here, not just the
      │                                          source of truth for it
      ▼
retrieval/document_search.py::search_documents()  — FTS5 MATCH, ranked by
                                                     bm25 (`ORDER BY rank`),
                                                     optionally scoped to
                                                     one company
```

- **FTS5 keyword search** — `document_chunks_fts` was unpopulated for a
  while after it was first written; this is now the exact/lexical half of
  [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy)
  below, never replaced by the semantic/vector half added alongside it
  (feature spec section 1: "Vector search is additive. Do not replace
  FTS5.").
- **Query sanitization**: raw user input is tokenized to plain alphanumeric
  words and each one individually double-quoted before hitting FTS5's
  `MATCH` (`storage/repositories.py::_sanitize_fts_query()`) — FTS5 treats
  hyphens/colons/quotes as query operators, so an arbitrary question passed
  straight through can raise a syntax error instead of just finding
  nothing; sanitizing keeps every token literal while still ANDing across
  them (FTS5's default multi-term behavior).
- **Chunking runs as part of "processing" a document** (Ingest queue →
  `ingestion/coordinator.py::process_documents()`, alongside the Knowledge
  Builder's extraction) — best-effort: a chunking failure is logged but never undoes
  an already-successful knowledge extraction, same graceful-degradation
  spirit as the Neo4j/Ollama fallbacks elsewhere. Re-processing a document
  *replaces* its chunks rather than accumulating duplicates — unlike
  `knowledge_claims`, a chunk is a mechanical index over the document's
  *current* text, not a historical claim, so there's no provenance reason
  to keep a stale chunk set around.
- **Wired into both Q&A and the Investigation Planner** — see
  [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy):
  `research/investigation_planner.py`'s `document_search` capability, and
  `research/assistant.py`'s `answer_question()` (additively, alongside the
  existing whole-document evidence), both now route through the Hybrid
  Retriever rather than FTS5 alone.

### Hybrid Document Retrieval (`retrieval/hybrid_search.py`)

Additive semantic/vector layer over the FTS5 index above — same evidence
(`document_chunks`), a second, independent way to find it:

```
Research Document -> Text Extraction -> Document Chunking (research/document_chunker.py)
      |
      +-- FTS5 / BM25 Index (document_chunks_fts, unchanged)
      |
      +-- Embedding Generation -> VectorStore -> Semantic Search
             (retrieval/embedding_provider.py)   (retrieval/vector_store.py)

FTS5 Results + Vector Results -> retrieval/hybrid_search.py (Reciprocal Rank
Fusion) -> Ranked Top-K Evidence -> Planner / research/assistant.py
```

- **`EmbeddingProvider`** (`retrieval/embedding_provider.py`) — `embed_text`/
  `embed_batch`, config-selected (`EMBEDDING_PROVIDER`): `"local"` (default)
  is `sentence-transformers/all-MiniLM-L6-v2` running on-device
  (`retrieval/embedding_provider_local.py`) — zero API cost, no key, so the
  test suite and CI never need one; `"voyage"` is Voyage AI's hosted API
  (`retrieval/embedding_provider_voyage.py`, Anthropic's commonly-recommended
  embeddings partner — Claude has no first-party embeddings endpoint), gated
  on `VOYAGE_API_KEY` and never the default. Independent of the vector
  store: it doesn't import or know about one.
- **`VectorStore`** (`retrieval/vector_store.py`) — `upsert`/`delete_document`/
  `search`/`health_check`, backend-independent by the same
  interface-plus-concrete-implementation pattern `storage/` uses for SQLite
  and `context/graph.py`/`context/graph_neo4j.py` use for the knowledge
  graph. `VECTOR_STORE_BACKEND="qdrant"` (default) is the only concrete
  implementation shipped (`retrieval/vector_store_qdrant.py`, the ONE module
  in this codebase allowed to import `qdrant_client` — enforced by
  `tests/test_vector_store_architecture.py`), talking to a local/Dockerized
  Qdrant instance not started/stopped by this app (same optional-infra
  pattern as `GRAPH_BACKEND=neo4j`); `"none"` disables the layer entirely.
  Never a source of truth — every vector is rebuildable from
  `documents`/`document_chunks` at any time.
- **Graceful degradation (section 10)**: `retrieval/hybrid_search.py`
  catches `VectorStoreUnavailable`/`EmbeddingProviderUnavailable`, logs the
  degradation (`retrieval_diagnostics`, below), and returns FTS5-only
  results — research keeps working with zero vector infrastructure running.
  A failed embedding attempt for one chunk marks that chunk's
  `document_chunks.embedding_status='failed'` and never touches its
  already-indexed FTS5 row.
- **Ranking**: Reciprocal Rank Fusion (`k=60` default,
  `HYBRID_RETRIEVAL_RRF_K`) over both legs' ranked results — deterministic,
  no LLM judgment call. A passage found by both methods is deduplicated to
  one entry (`retrieval_source="both"`) with a naturally higher fused score
  than a single-method match — the "confidence boost" of independent
  agreement, not a hand-tuned weight.
- **Automatic maintenance going forward**: `ingestion/workers/
  embedding_indexer_worker.py` subscribes to the same `document`
  `DATASET_INGESTED` event `chunk_indexer_worker` does (registered right
  after it, so it always sees that event's freshly-written chunks) — every
  future document ingestion embeds and upserts automatically, through the
  existing ingestion pipeline, not a parallel one.
- **One-time backfill**: `python main.py vector-backfill [--company-id ...]
  [--limit N] [--force]` — idempotent (a chunk already indexed under the
  current embedding model is skipped; re-running costs nothing extra),
  reusing the exact same `retrieval/semantic_indexer.py::
  embed_and_index_document_chunks()` the worker calls, not a second
  implementation.
- **Observability**: `retrieval_diagnostics` (one row per hybrid retrieval
  call) — candidate counts per method, embedding/vector-store/keyword
  latency, degradation flag+reason, and a compact per-passage summary
  (ids/ranks/scores, never raw text) — the retrieval-layer counterpart to
  `llm_call_log`.
- **What never gets embedded**: structured facts (`financial_observations`,
  `macro_observations`, `bank_infrastructure_observations`, shareholding/
  indicator data) have no path into `document_chunks` at all — they're
  ingested through an entirely separate pipeline
  (`ingestion/pipeline.py::ingest_file()`/`ingest_macro_file()`) that never
  touches the `documents` table, so the embedding indexer structurally never
  sees them (proved in `tests/test_embedding_indexer_worker.py`).
- See [SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md](SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md)
  for a worked comparison of FTS5-only vs semantic-only vs hybrid retrieval
  against Signal's real documents.

### Configurable Indicator Framework (`indicators/`)

A package, peer to `financials/`/`analytics/`/`research/`, for
deterministic, rule-based **indicators** — factual patterns worth noticing
("promoter holding fell 2.1pp over the last two quarters", "net profit grew
22% year over year") — explicitly NOT an LLM-generated insight, conclusion,
prediction, or recommendation. An indicator is a *computed fact* in this
app's Fact → Evidence → Inference → Hypothesis → Conclusion separation, so
no LLM call exists anywhere on this path, by construction.

```
canonical facts (canonical_financials, shareholding_observations, ...)
      │
      ▼
indicators/rules.py             — IndicatorRule.evaluate(conn, company_id,
      │  RULE_REGISTRY             thresholds, fact_store) -> RuleOutcome |
      │                            None, via the same FactStore seam
      │                            research/ already uses
      ▼
indicators/config.py            — resolve_effective_config(): system
      │  resolve_effective_config() default, then Global → Sector → Company
      │                            overrides, resolved per FIELD, not
      │                            whole-row
      ▼
indicators/evaluation.py        — evaluate_company_indicators(): run every
      │  evaluate_company_indicators() enabled rule with per-rule failure
      │                            isolation, append a deduped audit row
      │                            per newly-triggered/changed indicator
      ▼
storage/indicator_repository.py — indicator_rule_config (mutable, user-
                                   owned), indicator_evaluations
                                   (append-only audit trail)
```

- **Rules are Python code, not database rows.** Each `IndicatorRule`
  (`indicators/framework.py`, a frozen dataclass) declares a stable
  `rule_id`, `name`, `family`, `description`, `version`, `required_facts`, a
  `default_classification` — one of a fixed three-value vocabulary,
  `positive`/`observation`/`warning` (`red` is a defined-but-unused
  constant, reserved for a future "critical" classification), a
  `default_severity`, one or more `ThresholdSpec`s (each declaring its own
  key/label/unit/min/max, so the Settings form doesn't have to guess a
  range), and an `evaluate(conn, company_id, thresholds, fact_store)`
  callable. Rules register into `RULE_REGISTRY` (a plain `dict[rule_id,
  IndicatorRule]`) via `register_rule()` — the same "registry of named,
  versioned, pluggable things" pattern `ingestion/detector.py`'s
  `ADAPTER_CLASSES` and `ingestion/event_bus.py`'s worker registry already
  establish. Adding a rule is a `register_rule(...)` call in
  `indicators/rules.py`, never a framework change. A user's configuration
  never modifies or duplicates the rule object itself — only thresholds,
  classification, and enabled/disabled are configurable, and those live
  entirely in `indicator_rule_config`.
- **Two seeded families as of this session**: `shareholding` (promoter
  holding decline/increase between the two most recent quarters on file,
  read via `fact_store.list_shareholding_history`) and
  `financial_trajectory` (YoY moves in `net_profit`/`total_assets` from
  `canonical_financials`, computed by
  `financials/calculations.py::yoy_growth_for_metric`). That computation,
  and its default threshold (`analytics.patterns.DEFAULT_YOY_THRESHOLD_PERCENT`),
  are deliberately shared with `analytics/patterns.py`'s pre-existing
  `detect_yoy_spikes()` — so there is exactly one definition of "a
  significant YoY move" in the codebase even though the two surfaces render
  it differently. `detect_yoy_spikes()` itself is unchanged: it remains the
  Tools tab's cross-company, no-per-user-configuration scan it always was,
  not migrated into the rule engine.
- **Configuration is separate from rules, at Global → Sector → Company
  scope, resolved most-specific-wins, per FIELD (not whole-row)** — a new
  `indicator_rule_config` table keyed `(user_id, rule_id, scope_type,
  scope_value)`, where a NULL field means "inherit" (never "off"), resolved
  by the pure function `resolve_effective_config()` in `indicators/config.py`.
  Per-field resolution means a user who overrides *classification* for one
  company still inherits whatever *threshold* they configured globally,
  rather than that company override silently freezing today's global
  threshold too. `EffectiveConfig.sources` records which scope supplied
  each field — exactly what the Settings UI shows as "overridden" and what
  the audit trail stores as `scope_applied`.
- **Evaluation engine** —
  `indicators/evaluation.py::evaluate_company_indicators(conn, company_id,
  user_id, fact_store, persist)`: resolves each rule's effective
  configuration, skips disabled rules, runs each enabled rule's `evaluate()`
  with per-rule failure isolation (a raising rule is logged and skipped,
  never allowed to blank the whole section — the same discipline
  `ingestion/event_bus.py` applies to workers), and persists one audit row
  to the new `indicator_evaluations` table per *newly-triggered or changed*
  indicator — deduped by a `result_hash` fingerprint against that rule's
  most recent stored row for the same (company, user), so viewing a company
  page repeatedly doesn't inflate the trail; rules that don't trigger are
  never recorded. Append-only, same discipline `reconciliation_log` already
  follows. No LLM call exists anywhere on this path.
- **UI surfaces**: a new "Indicator Rules" section on the user Settings page
  (browse/search the rule catalog, see the system default alongside the
  user's effective configuration, enable/disable a rule, change its
  classification, adjust its thresholds, apply an override at a chosen
  scope, see what's overridden, and reset it back to inherited) — routes
  `POST /settings/indicators` and `/settings/indicators/reset` in
  `web/app.py`, built on `indicators/settings.py`'s read/write model. And a
  new "Indicators" section on every company page: three `<details>`-
  disclosure columns (Positive / Observations / Warnings, via
  `group_by_classification()`) — only the Warnings column auto-opens, and
  only when it's non-empty, this app's established real-estate-efficient
  disclosure convention.
- **Deliberately deferred** (not built in this pass): the Critical/red
  classification; most of the other indicator families the design
  anticipates (pledging, debt, valuation, governance, cash flow, capital
  allocation, ...); a user feedback loop (Agree/Disagree/Not Sure) and any
  adaptive personalization on top of it (`indicators/evaluation.py`'s
  module docstring records the intended shape — an `indicator_feedback`
  table keyed off the `evaluation_id` every triggered indicator already
  carries, *suggesting* a configuration change for approval rather than
  applying one — as an extension point, not something built here); and an
  audit-trail browsing UI (`indicator_evaluations` is real, queryable data,
  but nothing renders it yet — see [Known gaps → Configurable Indicator
  Framework](#configurable-indicator-framework)). Feeding indicators into
  the hypothesis/investigation workflow — the other item the framework's
  own spec named as future work — was partially built in this same
  session; see [Golden Research Loop validation](#golden-research-loop-validation)
  below.

### Research / AI layer (Context Optimization + Model Routing + Fallback)

This is the layer added to control LLM cost and add resilience. The three
Q&A/Insights/Signals call sites (`research/assistant.py`, `research/insights.py`,
`research/signals_report.py`) follow the same pipeline below — the fourth call
site, `research/knowledge_builder.py`, deliberately does not: it has no
Evidence retrieval, no Reuse check, no Context Optimizer pass, and no
knowledge-graph lookup to make (a document's own text is already everything
it needs), just a direct call through `llm/router.py`/`llm/observability.py`
like every other site, described in its own section above.

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

### Golden Research Loop validation

[`SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`](SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md)
(repo root) is the benchmark document for this: a validation exercise that
ran the hypothesis-driven investigation pipeline above end-to-end against five real research
questions, on the live database and a live Anthropic API — a re-runnable
score meant to be compared against after future changes, not a one-off
spot-check. Score: **8/10** — 4 of 5 investigations passed outright, 1
partial (blocked by a real, non-architectural data-coverage gap: IndusInd
Bank's ingested annual data ends at FY2013, years before the deterioration
the test question asks about — not a capability gap in the pipeline itself).
The exercise surfaced, and this session fixed, five genuine, generic
architecture gaps — changes that benefit every future investigation, not
one-off patches for the five test questions:

1. **Cross-company investigation persistence** — `investigations.company_ids`
   was a JSON blob with no real relational association, and company pages
   had no Investigations section at all. Fixed with a new
   `investigation_companies` join table (`storage/investigation_repository.py`):
   every investigation gets one row per associated company (a single-company
   investigation gets one row, a cross-company one gets several), plus
   `select_investigations_for_company()` to list a company's investigations
   through that join. One investigation record now genuinely appears under
   every relevant company's Investigations section without duplicating the
   record — `research/investigation.py`'s persistence path writes the join
   rows alongside the investigation itself, and the company page
   (`web/app.py`, `web/templates/company.html`'s new Investigations section)
   reads through it, showing a cross-company investigation's other companies
   as an "also ..." kicker linking to the one shared `/investigate/<id>`
   view rather than a per-company copy.
2. **Point-in-time (`as_of`) evidence scoping** (`research/temporal.py`) —
   previously no retrieval path had any temporal-cutoff concept at all, so a
   "no look-ahead bias" historical investigation ("could Signals have
   detected X before it became obvious?") was architecturally impossible to
   do correctly: evidence retrieval always pulled the full history up to
   today regardless of the question's framing. `as_of` is a plain ISO date,
   threaded generically through
   `research/capabilities.py::default_capabilities(as_of=...)` and applied
   uniformly across every evidence-gathering capability rather than as a
   one-off filter for a single investigation — fiscal periods visible only
   if their end date is on or before the cutoff, macro observations by
   period, documents/passages/knowledge claims by publication date, with an
   undated item excluded under a cutoff (fails closed: it cannot be shown to
   predate it). Enforced in retrieval, not asked for in a prompt, because an
   "as of 2013" question whose evidence block contains 2024 figures has
   already leaked the answer. The cutoff is recorded on `investigations.as_of`
   so a historical conclusion states the information set it was reached
   under.
3. **Deterministic indicators as investigation input**
   (`research/indicator_evidence.py`, plus a new `IndicatorEvidenceCapability`
   in `research/capabilities.py`) — the piece connecting the Configurable
   Indicator Framework above to the existing investigation pipeline: a
   company's currently-triggered indicators (via `indicators/evaluation.py`,
   read-only — `persist=False`, since an investigation reading a company's
   indicators must never write to the same audit trail a company-page view
   writes to) are now available both as hypothesis-generation context
   (alongside a company's sector and known knowledge-graph entities) and
   as per-hypothesis CALCULATION evidence during evidence gathering (citing
   the rule id, version, and provenance so the line is reproducible) — closing the loop
   the indicator framework's own spec described ("indicators may later
   become inputs to Signals' hypothesis/investigation workflow") but
   explicitly left unbuilt when that framework shipped. Disabled entirely
   under an `as_of` cutoff rather than leaking post-cutoff findings, since
   indicator rules evaluate only against the latest facts on file and have
   no historical mode.
4. **Hypothesis-generation robustness fixes**
   (`research/hypothesis_generator.py`) — two defects real load was silently
   hitting: `MAX_TOKENS` was raised from 3072 to 8192 (a 6-hypothesis
   response is a genuinely large JSON object; real golden-loop runs measured
   2155-2221 output tokens even before a multi-clause question truncated
   outright and failed the whole investigation), and the JSON parser now
   decodes with `strict=False` so a literal control character inside a
   quoted string (an unescaped newline inside one response's "mechanism"
   field, observed in a real run) no longer fails an entire investigation —
   the same tolerance `research/knowledge_builder.py`'s parser already had.
   Both are generation-failure fixes: hypothesis generation failing is the
   one failure `run_investigation()` cannot degrade past, since there is
   then nothing left to investigate.
5. **Neo4j sync de-duplication** (`context/graph_neo4j.py`,
   `context/knowledge_graph.py`) — the knowledge-graph read path was
   re-running a full, idempotent resync of the whole knowledge-claim graph
   to Neo4j on nearly every call (`research/investigation_planner.py` calls
   the knowledge-graph capability up to 6 times per hypothesis, so a real
   6-hypothesis investigation triggered ~36 full resyncs and spent most of
   its wall clock on them — risking lock contention when investigations run
   concurrently). Fixed with an in-process fingerprint of the SQLite
   knowledge tables (row counts plus the highest claim id — the
   `knowledge_*` tables are append-only, so this is a sufficient change
   signal) cached between calls: a sync is skipped when the fingerprint is
   unchanged since the last one, and a fresh process always syncs at least
   once, so this can never serve another process's stale idea of the data.
   `find_claims_about_entity()` also gained the same `as_of` filtering as
   every other capability (point 2 above), on both the SQLite and Neo4j
   paths, so switching `GRAPH_BACKEND` cannot change what a point-in-time
   investigation is allowed to see.

### Web layer (`web/app.py`)

Single-file Flask app (`create_app()` factory), organized by feature area:

- **Auth**: `users` table, `werkzeug.security` password hashing, Flask
  `session` cookie (`session["user_id"]`), `g.user` populated in
  `before_request`. One seeded admin account (`admin`/`admin`) so the app is
  usable with zero setup. Admin-only routes gate on `g.user["is_admin"]`.
- **Company pages** (`/companies/<id>`): multi-tab company view (Overview,
  Key Insights, Indicators, Charts, Financials, Valuation Model, Docs,
  Notes, Shareholding Pattern, Investigations, Threads, Commentary, News) —
  most tabs are server-rendered on page load; Financials/Valuation/Charts/Docs
  fetch their data from `*.json` feed endpoints (`valuation_feed.py`,
  `charts_feed.py`, `docs_feed.py`) and render client-side. Indicators
  (`indicators/*.py`, see [Configurable Indicator
  Framework](#configurable-indicator-framework-indicators)) and
  Investigations (the hypothesis-driven investigation pipeline, see [Golden
  Research Loop validation](#golden-research-loop-validation)) are the two
  newest tabs.
- **Research** (`/`, `/research/ask`, `/research/thread/generate`,
  `/research/thread/<id>`): the Ask-AI and Signals-investigation entry
  points, calling `research/assistant.py` / `research/signals_report.py`.
- **Investigations** (`/investigations`): list view over `generated_reports`.
- **Watchlist**, **Settings** (`/settings` — theme, plus an Administration
  group that now hosts what used to be the standalone Admin page: company
  metadata edits, raw-file import, stock actions, vocabulary
  (sectors/industries/tags), and the Ingest queue — the discovery/processing
  entry points that write ingested data, alongside Docs-tab uploads),
  **Chat** (`/chat`). `/admin` itself (`web/app.py:752`) is a redirect-only
  stub now — it 302s `/admin?panel=<sub>` to `/settings?panel=admin-<sub>`
  so old bookmarks still land somewhere correct, but the panels themselves
  are built by `_build_admin_settings_context()` and rendered inside
  `settings.html`, not a template of their own.
- **Tools** (`/tools`): site-level (cross-company) panels, distinct from a
  single company's own tabs — `macro` (FRED/RBI series charts), `analytics`
  (`analytics/patterns.py`'s cross-company scans), and `insights`
  (`research/insights.py`'s Key Insights surfaced at the site level rather
  than for one company), switched via `?panel=` and backed by
  `_tools_macro_context()`/`_tools_analytics_context()`/`_tools_insights_context()`.
- **DB per-request**: `g.db` opened in `before_request`/closed in
  `teardown_appcontext`, via `storage.database.get_connection()`.

### CLI (`main.py`)

Thin argparse wrapper calling the same modules as the web app — `init`,
`status`, `seed-companies`, `add-company`, `import-nse-companies`,
`ingest`, `ingest-yfinance`, `ingest-fred` (ingest one US macro series live
from FRED, e.g. `FEDFUNDS`/`DGS10`), `list-companies`, `archive-company`,
`restore-company`, `add-stock-action`/`list-stock-actions` (record/list a
split, bonus, or rights issue), `analyze`, `ask`, `watchlist-add/remove`,
`list-watchlist`, `list-batch-runs`/`show-batch-run` (audit log for
`scripts/`'s bulk-fetch batch jobs), `vector-backfill` (one-time/idempotent
embedding backfill over already-processed documents — section 11; the same
`retrieval/semantic_indexer.py` function every future document ingestion
also calls), and `replay-events` (re-dispatch stored
`DatasetIngestedEvent`s to registered workers) — both tied to the event bus,
see [Dataset-centric ingestion: the event
bus](#dataset-centric-ingestion-the-event-bus-ingestionevent_buspy) — and
`serve` (launches the Flask dev server). Useful for bulk/scripted ingestion
and quick terminal Q&A without the browser.

### Price history (`storage/price_*.py`, `schemas/price_schema.sql`)

Daily OHLCV bars for NSE 500 companies, in a `daily_prices` table
(`company_id`, `trade_date`, `open`/`high`/`low`/`close`/`volume`, `source`,
`fetched_at`, PK `(company_id, trade_date)`) — but kept in its own SQLite
file, `data/price_history.db` (`config/settings.py`'s `PRICE_DB_PATH`),
separate from `equity_research.db`.

```
sources/yfinance_prices.py  — fetch_daily_bars()
      │
      ▼
storage/price_repository.py — upsert_daily_bar()/upsert_daily_bars()
                               (ON CONFLICT(company_id, trade_date) DO UPDATE)
      │
      ▼
data/price_history.db  — daily_prices, separate file from equity_research.db
      │
      ▼
web/app.py:1683 — GET /companies/<company_id>/price-feed.json
```

- **Why a separate db file**: `schemas/price_schema.sql`'s header comment
  gives the rationale directly — this data is cheaply regenerable from
  yfinance at any time, so the file is gitignored (the blanket `*.db` rule)
  and never git-shard-committed the way `equity_research.db` is
  (`scripts/db_shard.py` stays `equity_research.db`-only). There's also no
  cross-db foreign key to `companies` — SQLite can't enforce one anyway —
  so referential integrity is procedural: `storage/price_repository.py`'s
  writers only ever receive `company_id`s the caller already read out of
  the main db's `company_index_membership`.
- **Populated by two scripts, both module-invoked** (not a `main.py`
  subcommand — `python -m scripts.backfill_price_history` /
  `python -m scripts.fetch_daily_prices`, run as modules so their
  `storage`/`sources` imports resolve against the repo root):
  `scripts/backfill_price_history.py` for a one-time/occasional full
  historical pull (`--period 1y/5y/10y/max`), and
  `scripts/fetch_daily_prices.py`, meant to run daily, which upserts a
  trailing 5-day window per company rather than just "yesterday" — a
  missed run (weekend, transient failure) self-heals on the next run
  instead of leaving a gap, since upserts make re-fetching overlapping
  days free. Both are batched with a pause between groups, gentler on
  yfinance's soft rate limits than one continuous loop over ~500 tickers.
  See `USER_GUIDE.md` §12/13 for the operator-facing version.
- **Read back via `/companies/<company_id>/price-feed.json`**
  (`web/app.py:1683`) — takes `period` (`1y`/`5y`/`10y`/`max`), resolves it
  to a date range, and returns parallel `dates`/`open`/`high`/`low`/`close`/
  `volume` arrays read straight off `daily_prices` for that company and
  window. Backs the Charts tab's price overlay.
- **`PriceStore`** (`storage/price_store.py`) is the same DI shape as
  `storage/fact_store.py`'s `FactStore` (architecture guardrail #3) — a
  frozen dataclass of plain callables matching the repository functions'
  signatures, so every consumer (the two scripts above, the price-feed
  route) takes an optional `price_store` parameter defaulting to
  `default_price_store()` rather than importing the SQLite functions
  directly.
- **Price/volume only, never a valuation input** — same posture as
  `web/live_quote.py`'s live quote (not authoritative for any valuation
  math, display only): `daily_prices` never feeds `financial_observations`,
  `canonical_financials`, or any FACT/CALCULATION in a generated report —
  yfinance is approved as a source here for price/volume history only,
  never for financial-statement facts.

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
| `_sidebar_shell.html` | Shared sidebar-layout partial (nav rail + content pane) used by the multi-panel pages (`settings.html`, `tools.html`, ...) so each doesn't reimplement the same shell. |
| `_signals_topbar.html` | Shared top toolbar partial for Signals-report/investigation pages (report title, status, actions). |
| `landing.html`, `about.html` | Marketing/info pages. |
| `login.html`, `signup.html` | Auth pages. |
| `index.html` / `research.html` | Home page — the Research/Ask-AI entry point (`/`). |
| `company.html` | The multi-tab company page (Overview, Financials, Valuation, Charts, Docs, Notes, Threads). |
| `research_thread.html` | A single generated Signals report/thread view. |
| `investigations.html` | List of all generated reports (the hypothesis-driven investigation pipeline). |
| `investigation.html` | A single investigation's detail view (`/investigate/<id>`) — distinct from `investigations.html`'s list. |
| `watchlist.html` | Pinned companies/threads. |
| `chat.html` | Freeform chat entry point (`/chat`). |
| `settings.html` | Theme preference, plus the Administration group (the former `admin.html` panels — see below). |
| `tools.html` | Site-level Tools tabs (`/tools`) — macro/analytics/insights panels, see the Web layer bullet above. |
| `docs.html` | Docs reference page (`/docs`, `web/app.py:1998`) — sources/XBRL documentation sections, unrelated to a company's own Docs tab. |
| `usage.html` | LLM call-log audit view (`/admin/usage`) — see [Known gaps → Research / AI layer](#research--ai-layer-context-optimizer--model-router). |

`admin.html` was retired along with the standalone `/admin` page — its
content was absorbed into `settings.html`'s Administration group (see the
Web layer bullet above); the file no longer exists in `web/templates/`.

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
| `shareholding_panel.js` | Shareholding Pattern tab — Screener-style wide table (rows = Promoters/FII/DII/Public and their named holders, columns = every quarter on file) fed by `web/shareholding_feed.py`'s `/companies/<id>/shareholding-feed.json`. |
| `company_picker.js` | Generic, reusable company search-and-select widget for plain HTML forms — same `/companies/search.json` typeahead as `header_search.js`, but fills a hidden form field instead of navigating. |
| `local_time.js` | Rewrites every `[data-utc]` timestamp client-side from the server's UTC into the viewer's local timezone; runs once at load. |

Several of these (`valuation_dashboard.js`, `docs_timeline.js`,
`notes_panel.js`, `shareholding_panel.js`) started as visual prototypes
built in Claude Design and were ported to plain JS against this app's real
data feeds — noted in each file's header comment.

### Static assets (`web/static/`)

- `classical/styles.css` — the app's single CSS theme.
- `brand/` — logo assets.
- `fonts/` — self-hosted font files.
- `data/` — static JSON fixtures used by a couple of ported dashboards.

## Data model (SQLite, `schemas/sqlite_schema.sql`)

46 tables, grouped by concern:

- **Reference data**: `sources` (trust-ranked data providers), `metrics_dictionary`, `metric_aliases`.
- **Companies**: `companies` (per-company `country`/`currency`/`fiscal_year_end_month`, not global), `company_identifier_history`, `company_index_membership`, `company_list_column_settings`, `overview_ratio_settings` (global `ratio_key` → `enabled` toggle — which ratios the company page's Overview tab shows, Admin-configurable, same shape/spirit as `company_list_column_settings` but for the Overview tab instead of the company list), `stock_actions` (discrete corporate events — splits/bonus/rights issues — recorded as raw events only; no split-adjustment of historical shares/EPS/price series yet), `sectors`/`industries`/`index_definitions` (Admin-editable lookup vocabularies backing the sector/industry/index-tag dropdowns, seeded from whatever's already in use).
- **Financial data**: `financial_observations` (raw, per-source, never overwritten), `canonical_financials` (reconciled, one row per company/metric/period), `reconciliation_log` (audit trail of which source won and why), `macro_observations` (India: RBI + IITM rainfall series real and ingested — 158,759 rows (IITM 116,187 + RBI 42,572); MOSPI/IMD/IRDA registered, no files ingested yet. US: FRED, live-fetched per series on demand, no bulk/scheduled pull yet), `bank_infrastructure_observations` (RBI's monthly bank×metric ATM/NEFT/RTGS bulletins — a separate shape from `macro_observations`' flat series). Daily OHLCV price/volume history lives separately, in its own db file — see [Price history](#price-history-storageprice_py-schemasprice_schemasql) below.
- **Ingestion tracking**: `ingestion_queue_items` — the Admin → Ingest panel's discovery/status tracking for financial/macro files under `data/raw/` (content-hash keyed); orchestration metadata only, never the source of truth for parsed data itself.
- **Event bus & batch audit**: `dataset_events`, `worker_processing_log`, `batch_job_runs`, `batch_job_items` — the Event Store, per-worker processing log, and bulk-script audit trail behind ingestion's event-driven layer; see [Dataset-centric ingestion: the event bus](#dataset-centric-ingestion-the-event-bus-ingestionevent_buspy) for the full shape of each.
- **Documents**: `documents` (Docs-tab uploads/links; `processing_status`/`processed_at`/`error_message` track the Ingest queue's state for each one), `document_chunks` + `document_chunks_fts` (page-scoped chunks, FTS5-indexed by `research/document_chunker.py`; `embedding_status`/`embedding_model`/`embedded_at` track semantic-indexing state per chunk — the actual vectors live in the VectorStore, not this table, which stays the rebuildable authoritative source — see [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy)).
- **Shareholding Pattern**: `shareholding_observations` (one row per company/fiscal_year/quarter — promoter/public/employee-trust holding percentages, plus an FII/DII/Government/public-non-institutional breakdown read off the SHP XBRL's own category-rollup contexts rather than hand-aggregated), `shareholding_holders` (one row per named holder within a category — `side` promoter/public, `category`, `holder_name`, `num_shares`/`percent_of_shares`, sourced from NSE filings). Backs the company page's Shareholding Pattern tab, rendered by `web/static/js/shareholding_panel.js` against `web/shareholding_feed.py`'s `/companies/<id>/shareholding-feed.json` — not otherwise described elsewhere in this doc.
- **Knowledge Builder**: `knowledge_entities` (deduped named things — Company/Product/Risk/ManagementPerson/...), `knowledge_claims` (one extracted statement per row, with its own provenance — document, fiscal period, speaker, `claim_type`, confidence — additive, never overwritten), `knowledge_relationships` (typed edges between two entities, optionally traced to the claim that asserted them), `knowledge_evidence` (the supporting quote for one claim). SQLite is the source of truth for all four; `context/knowledge_graph.py`/`context/graph_neo4j.py` project them into the same Neo4j graph the sector-peer traversal uses, sharing `Company` nodes rather than duplicating them — see [Research Knowledge Graph](#research-knowledge-graph-contextknowledge_graphpy).
- **Research/investigations**: `generated_reports` (persisted Signals reports), `research_thread_evidence`, `research_thread_followups`, `company_insights` (Key Insights history, per-company); `system_insights` (the site-level counterpart — one row per cross-company insight, `company_ids` a JSON array, `source_claim_ids` tracing provenance back into `knowledge_claims`, `status` new/retained/archived — generated from the [`/tools` Insights panel](#web-layer-webapppy), not a single company page); The hypothesis-driven investigation pipeline's `investigations` (one row per structured investigation, including `as_of` — the point-in-time cutoff it ran under, if any — see [Golden Research Loop validation](#golden-research-loop-validation)), `investigation_hypotheses`, `investigation_hypothesis_evidence`; and `investigation_companies` — the investigation↔company join table a company page's Investigations section queries through (`storage/investigation_repository.py`), so a cross-company investigation is one row, listed under every company it covers, never duplicated.
- **Configurable Indicator Framework**: `indicator_rule_config` (per-user Global/Sector/Company overrides, keyed `(user_id, rule_id, scope_type, scope_value)`, a NULL field meaning "inherit"), `indicator_evaluations` (append-only audit trail of triggered indicators, deduped by `result_hash`). The rules themselves are Python (`indicators/rules.py`), not rows — see [Configurable Indicator Framework](#configurable-indicator-framework-indicators) above.
- **LLM observability**: `llm_call_log` — one row per `llm/router.py` call or `context/reuse.py` reuse hit (model/provider, fallback, tokens, cost, context-optimization accounting) — covers all four LLM call sites, including `research/knowledge_builder.py` (`task_name="knowledge_extraction"`).
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

- **No automated/recurring ingestion** — the Admin → Ingest panel
  (`ingestion/coordinator.py`) makes *discovering and triggering* ingestion a
  one-click action instead of hand-typing a CLI command per file, but
  discovery only runs when that panel is loaded or "Refresh Pending Files"
  is clicked — there's still no scheduled job, no filesystem watcher, no
  NSE/BSE filing scraper pushing new files in on its own.
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
- **No US company sector/industry classification** — `companies.sector`/
  `industry`/`macro_economic_sector`/`basic_industry` are NSE's own 4-level
  taxonomy; there's no GICS-equivalent importer for US companies, so those
  columns stay `NULL` for them (`register_company()` already tolerates this).
  `financials/ratios.py`'s bank/NBFC sector-tagging heuristic and its
  GNPA/CASA terminology likewise stay India-vocabulary-biased — a US bank's
  ratios compute fine, but under India's regulatory naming, not a US
  equivalent like "NPL ratio".
- **No bulk US company-master importer** — `companies/nse_import.py` bulk-
  registers from an NSE export; there's no parallel importer for a US index
  constituent list. Registering a US company is one-at-a-time today
  (`add-company --country US` / `ingest-yfinance`).
- **`sources/yfinance_financials.py` doesn't consult a company's
  `fiscal_year_end_month`** — it labels US fiscal years by calendar close
  year (`FY{period_end.year}`) regardless of the row on `companies`. The
  underlying data is correct either way; only the `FY` label can be
  cosmetically off for a company with a non-calendar fiscal year (e.g.
  Apple's September close).
- **`sources/fred.py` is per-series, on-demand** — same model as
  `ingest-yfinance`, not a scheduled/bulk pull (see "No automated/recurring
  ingestion" above, which applies here too).
- **Ticker-suffix and index-tag logic (`web/live_quote.py`, `web/app.py`) is
  a 2-way IN/US hardcoded branch**, not a general N-country lookup table —
  intentional given the app's stated US+India focus, but a third market
  would need real generalization, not another `if`.

### Documents / Docs tab

- **Q&A still doesn't use chunking/full-text search** — `document_chunks`/
  `document_chunks_fts` are populated now (`research/document_chunker.py`),
  but `research/documents.py::get_document_evidence()` (the Q&A
  evidence path) still extracts a document's full text straight into the
  prompt on every call, not a retrieved/ranked subset of chunks. Distinct
  from the Knowledge Builder (`research/knowledge_builder.py`),
  too — that extracts *structured claims* once, persisted to
  `knowledge_claims`, not a general-purpose searchable index of the raw
  text; chunking, claim extraction, and the Q&A evidence path are three
  separate things, and none of them closes the others' gaps.
- **No caching of downloaded PDF bytes** — a linked (non-uploaded) document is
  re-fetched over HTTP and re-parsed on every single question that touches it.
- **No automated document ingestion** — everything in the Docs tab is
  manually added via the upload/link form; there's no official-source pull
  pipeline.
- **No multi-company document attribution** — document evidence only backs
  single-company questions/reports today.

### Ingestion Coordinator, Knowledge Builder, Research Knowledge Graph & Document Retrieval

- **No chunking for long documents *sent to the extraction model*** — a
  document's text is capped at `MAX_CHARS_FOR_EXTRACTION` (40,000
  characters) before `research/knowledge_builder.py` sends it to the model;
  a longer annual report gets its first ~40K characters extracted, not the
  whole thing. `research/document_chunker.py` *does* chunk the
  full document for search — the two gaps are different: extraction is
  still single-pass and length-capped, search indexes everything.
- **Entity resolution is name-string matching, not identity resolution** — a
  `MAX_CHARS_FOR_EXTRACTION` (40,000 characters) before being sent to the
  model; a longer annual report gets its first ~40K characters extracted,
  not the whole thing. A real multi-pass/chunked extraction is future work,
  not built.
- **Entity resolution is name-string matching, not identity resolution** — a
  real company can end up with two separate `Company`-type entity rows: one
  from the model naming it in the extracted text (e.g. "SBFC Finance
  Limited"), one from the `COMPANY` placeholder resolving to the internal
  `company_id` (e.g. "SBFCFINANCE"). Both rows are individually correct and
  correctly scoped; they're just not unified into one canonical entity.
  Worth resolving before anything downstream assumes exactly one `Company`
  entity per company.
- **The Research Knowledge Graph (`context/knowledge_graph.py`) only answers
  single-hop "what's connected to this entity" queries** — real multi-hop
  reasoning (e.g. "which companies have a claim `ABOUT` a `Risk` that
  `MAY_AFFECT` a `Metric` another company also has a claim about") isn't
  built; `find_claims_about_entity()` returns one entity's directly-connected
  claims and their immediate neighbors, not a chain across several hops.
  Genuinely graph-shaped multi-hop traversal is future work, not attempted
  here — `research/investigation_planner.py` queries the graph the
  same single-hop way everything else does.
- **Not wired into Q&A or Signals reports yet** — `research/assistant.py`/
  `signals_report.py` don't query the Research Knowledge Graph at all; a
  question can't yet be answered from a cross-company claim connection the
  way it can from `canonical_financials` or a sector-peer investigation.
  Building that integration point is a later step, not attempted when the
  Research Knowledge Graph itself shipped.
- **Investigation Orchestrator (`research/investigation.py`) has
  the iterative evidence-sufficiency loop the guardrails call for, but it's
  narrower than a full Planner-controlled loop** — an `INSUFFICIENT_EVIDENCE`
  verdict does trigger one gap-targeted retry (bounded by 4 termination
  controls: evidence sufficiency, `MAX_EVIDENCE_ITERATIONS`, a wall-clock
  deadline, a no-new-evidence check — see [The four-layer
  split](#the-four-layer-split) and [Golden Research Loop
  validation](#golden-research-loop-validation)), but two things are still
  missing: no cost/token budget is one of those termination controls, and a
  retry re-runs the same broad evidence-gathering pass across every capability
  rather than targeting just the one capability the named evidence gap
  actually points at. The Knowledge Builder itself still only extracts and
  persists what a document already states — it's `research/investigation.py`
  that does the hypothesis generation/planning/evaluation/synthesis
  reasoning, not the Knowledge Builder.
- **No UI to browse extracted claims** — `knowledge_claims`/
  `knowledge_entities`/`knowledge_relationships` are real, queryable data
  (`storage/repositories.py::list_knowledge_claims_for_company()` etc.), but
  reaching them today means a direct SQL query or that repository function —
  no company-page tab or Admin view renders them yet.
- **Knowledge extraction cost isn't broken out from other LLM calls in the
  UI** — it *is* logged to `llm_call_log` (`task_name="knowledge_extraction"`),
  so `/admin/usage`'s task breakdown already includes it, but there's no
  Ingest-queue-specific cost view (e.g. "$X spent processing these N pending
  documents").
- **~~Document search is keyword-only, not semantic~~ — resolved.**
  `retrieval/hybrid_search.py` now runs FTS5 AND embedding/vector semantic
  search together (RRF-fused) for both the Investigation Planner's
  `document_search` capability and `research/assistant.py`'s Q&A path — see
  [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy).
  What remains genuinely unbuilt: `retrieval/vector_store_qdrant.py` has only
  been exercised against a mocked `qdrant_client` in this environment, not a
  live Qdrant server (the Docker container available here has no published
  host port) — see
  [SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md](SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md)'s
  disclosure for exactly what that gap is and how to close it.
- **Chunking is fixed-size, not paragraph/section-aware** — 1500 characters
  with 150 overlap, page-scoped; a chunk boundary can land mid-paragraph or
  mid-table. `document_chunks.section_heading` exists in the schema but is
  never populated — no heading-detection logic exists. This limits both
  FTS5 and semantic chunk quality equally; not specific to the vector layer.
- **~~`retrieval/document_search.py` isn't wired into any evidence path~~ —
  resolved** — see [Hybrid Document Retrieval](#hybrid-document-retrieval-retrievalhybrid_searchpy)
  above. `search_documents()` (FTS5-only) itself is still directly callable
  and tested; what changed is that the Planner's/Q&A's evidence paths are
  now bound to the hybrid composition rather than nothing.

### Configurable Indicator Framework

- **Only two rule families exist** — `shareholding` and
  `financial_trajectory` (`indicators/rules.py`) prove the registry shape;
  the design's other anticipated families (pledging, debt, valuation,
  governance, cash flow, capital allocation, ...) are unregistered. Adding
  one is a `register_rule(...)` call, not a framework change.
- **No Critical/red classification** — `CLASSIFICATIONS` is fixed at
  `positive`/`observation`/`warning`; `red` is a defined-but-unused constant
  reserved for a future fourth tier.
- **No user feedback loop, and no adaptive personalization built on one** —
  no Agree/Disagree/Not Sure verdict on a triggered indicator.
  `indicators/evaluation.py`'s module docstring records the intended shape
  (an `indicator_feedback` table keyed off `evaluation_id`, *suggesting* a
  configuration change for approval rather than applying one silently) as
  an extension point, not something built here.
- **No UI to browse the indicator audit trail** — `indicator_evaluations`
  is real, queryable data
  (`storage/indicator_repository.py::select_indicator_evaluations()`), but
  nothing renders it; a company page shows only the currently-triggered
  set, not its history. Same shape as "No UI to browse extracted claims"
  above.
- **Feeding indicators into the investigation workflow was partially
  built**, not left as a pure gap — see [Golden Research Loop
  validation](#golden-research-loop-validation)'s "Deterministic indicators
  as investigation input": a company's currently-triggered indicators are
  now available to hypothesis generation/planning as evidence, but nothing
  else the framework's own spec anticipated on top of that (e.g. no
  push/alerting on a newly-triggered warning) is built.

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
  (`valuation_dashboard.js`, `docs_timeline.js`, `notes_panel.js`,
  `shareholding_panel.js`) — worth
  re-checking against the current feed shape if `valuation_feed.py` /
  `docs_feed.py` / the notes routes ever change independently.

### Testing / CI

- **Full suite green** — `.venv/bin/python -m pytest -q` passes 738/738 as
  of this audit. The 5 `tests/test_web.py` template/copy
  failures noted in earlier revisions of this doc (home page copy, the
  `ANTHROPIC_API_KEY` banner text, the legacy `/research` redirect, the
  embedded `company_id` JSON on the research page) have since been fixed,
  not just gone stale — re-run the suite rather than trusting this count.
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
   and that decision is auditable. The same discipline now spans three
   systems: `financial_observations` (trust-ranked reconciliation),
   `ingestion_queue_items` (content-hash tracked — an unchanged processed
   file is never silently reprocessed, a failed one never silently reset),
   and `knowledge_claims` (every extraction is a fresh, additive row, never
   an update to a prior document's claims).
5. **Local-first, self-use** — single SQLite file, no external services
   required beyond the Anthropic API (optional local Ollama fallback), no
   deployment target, admin account seeded automatically.
6. **Cost-aware LLM execution** — hardness-based model routing, cloud→cloud→
   local fallback, context deduplication/budgeting, and reuse-before-recompute
   are all inspectable via `llm_call_log`, not invisible.
