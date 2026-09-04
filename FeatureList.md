# Feature List & Backlog

Split out of `README.md`'s [MVP Delivery Plan](README.md#mvp-delivery-plan)
so that document stays focused on architecture and scoping decisions, not a
running status list. This file tracks what's actually been built and what's
still open — check it (plus `git log`/`git status`) before asking "what's
pending?" again; it decays fast, so trust the code over old prose here if
the two disagree.

For the reasoning behind building the UI wireframe-first, see README.md's
[MVP Delivery Plan](README.md#mvp-delivery-plan). For the current, accurate
technical picture (module map, data model, known gaps), see
[architecture.md](architecture.md). For the latest backlog snapshot — after
the database-portability refactor, the Configurable Indicator Framework, and
the Golden Research Loop validation — see [pendingList.md](pendingList.md);
it isn't duplicated here.

Legend: ✅ done · 🟡 partial · ⬜ not started

---

## Deployment model

**Current design target: a single individual user, running locally.** This
isn't an oversight to fix later in the features below — it's the deliberate
scope this phase was built to (see [architecture.md's Key design principles
#5](architecture.md#key-design-principles), "Local-first, self-use"): one
SQLite file (`data/equity_research.db`), one seeded admin account, a Flask
dev server (`main.py serve`) with no production WSGI/process manager in
front of it, no connection pooling, no per-request rate limiting, no
horizontal scaling story. Every "Available" row above assumes this
single-user, single-process context.

**Future phase (not started): a server deployment planned for 1000+
concurrent sessions.** Getting there is a distinct, sizeable body of work —
not a config flag — and is tracked as its own Pending item below rather than
implied by anything currently shipped.

## Available

| Area | Feature | Status | Notes |
|---|---|---|---|
| Nav / shell | 7-link nav (Research / Companies / Tools / Investigations / Watchlist / Docs / About) + separate profile/account menu (Settings, Usage if admin, Log in/out) | ✅ | All routes render, active-state highlighting per section (`web/templates/_header.html`). |
| Research | Query box + example cards on Research home | ✅ | Claude-style auto-growing textarea (Enter submits, Shift+Enter newline). |
| Research | Live Q&A (`/research/ask`, `/chat`) | 🟡 | `research/assistant.py::answer_question()` gives a real grounded answer + charts for any question — company-scoped (auto-detected by substring match on the registry, no chip needed) and/or macro-scoped (see Macro evidence row below); company detection is a real ticker/name substring match, so a common English word that happens to be a real ticker (e.g. "AVG") can false-positive. Every answer is auto-saved to `generated_reports` (shows up in Investigations/Threads) and checked against `context/reuse.py` first — a near-duplicate question is served from the saved answer at zero cost instead of a fresh LLM call. Still one `answer_html` blob (now real Markdown → HTML, not escaped literal punctuation) + images, not the structured key-finding/confidence/evidence-*table* shape README step 11 originally specified. No computed confidence for a real answer. |
| Research | Macro/regulatory evidence (`research/macro_evidence.py`) | ✅ | A third evidence source alongside Financials/Docs, for India-wide macro questions (rainfall, repo rate, credit growth, ...) with or without a company named. An LLM call (`_plan_retrieval`, cheapest allowed tier) picks which catalog series and what year range apply, given the full list of ingested series with their date coverage — falls back to a keyword/regex heuristic if that call fails or doesn't parse. Every model-named series is validated against the real catalog before use, so a hallucinated series_key is silently dropped. |
| Research | Full "Signals" report generation (`research/signals_report.py`, `/research/thread/generate`) | ✅ | Structured multi-section report (Key Finding, Hypothesis, What We Still Don't Know, etc.) from the LLM, grounded in financial evidence + any uploaded-document `MANAGEMENT_STATEMENT` evidence. Same FACT/CALCULATION/INFERENCE tagging discipline as the short-answer path. |
| Research | Investigation persistence (`generated_reports`, `research_thread_evidence`, `research_thread_followups` tables) | ✅ | Every generated Signals report is saved (`thread_id`, question, company_ids, markdown, timestamp) and re-readable, not recomputed. The deterministic `Evidence` list that actually grounded the report (real retrieval output, not LLM text) is persisted row-by-row and rendered as a real evidence rail (capped at 20 rows shown, with a "+N more" note for anything beyond that). The LLM also appends 2-4 follow-up question suggestions after a `===FOLLOWUP_QUESTIONS===` marker (best-effort parsed, same pattern as `extract_report_meta`), persisted and rendered as real clickable buttons that kick off a new report via `/research/thread/generate` — fulfills README [step 12](README.md#web-ui-implementation-sequence). Differs from the originally spec'd schema shape (one evidence/followups table per thread_id rather than the exact 4-table design sketched in README), and follow-ups are LLM-suggested, not computed from a fixed template. |
| Research → Documents | Uploaded/linked documents as evidence (`research/documents.py`) | ✅ | A company's Docs-tab entries (uploaded PDF or a pasted PDF URL) get extracted and fed into both the short-answer and Signals-report evidence blocks as `MANAGEMENT_STATEMENT` lines (whole-document, `get_document_evidence()`) — additively alongside the targeted-passage hybrid retrieval below (`get_document_passage_evidence()`), not replaced by it. No download cache. Non-PDF docs (recordings, plain links) are silently skipped, not errored. |
| Research → Documents | Hybrid (FTS5 + semantic/vector) document retrieval (`retrieval/hybrid_search.py`) | 🟡 | Page-scoped chunking + FTS5 (`research/document_chunker.py`) plus a Qdrant-backed vector layer (`retrieval/vector_store.py`, `embedding_provider.py`), Reciprocal-Rank-Fusion combined, wired into Q&A, Signals reports, and the Investigation Planner (see [architecture.md's Hybrid Document Retrieval](architecture.md#hybrid-document-retrieval-retrievalhybrid_searchpy)). Degrades gracefully to FTS5-only if Qdrant/the embedding model isn't reachable. Partial only because `vector_store_qdrant.py` has been exercised against a mocked `qdrant_client`, not a live Qdrant server, in this environment — see [SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md](SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md). |
| Research | Hypothesis-driven investigation (`research/investigation.py`, `/investigate/generate`, `/investigate/<id>`) | 🟡 | "Run structured investigation" button on the Research tab's live-answer result. Generates several competing hypotheses, routes each to SQL/macro/documents/knowledge-graph evidence through an injectable `PlannerCapabilities`/`FactStore` seam (`research/capabilities.py`, `storage/fact_store.py`), evaluates each independently, then ranks/synthesizes — persisted per-hypothesis with its own evidence/verdict/rank (`investigations`/`investigation_hypotheses`/`investigation_hypothesis_evidence`), individually queryable, distinct from the single-narrative Signals report. Listed on the Investigations tab under its own "Structured investigations" section. The evidence-sufficiency loop (Orchestrator-controlled, not the LLM) is implemented — an `INSUFFICIENT_EVIDENCE` verdict triggers one gap-driven retry, bounded by 4 termination controls (sufficiency, max iterations, wall-clock timeout, no-new-evidence). Partial only in that a real cost/token budget isn't one of those controls yet, and the retry re-runs the same broad evidence-gathering pass rather than targeting one specific capability for the named gap. Validated end-to-end against 5 real research questions (score: 8/10) — that pass also added cross-company investigation association (`investigation_companies`), point-in-time (`as_of`) evidence scoping, and Configurable-Indicator-Framework evidence as investigation input. Full results and remaining gaps: [SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md](SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md) (also tracked in `pendingList.md`). |
| Investigations | List real + example threads | ✅ | Generated Signals reports (newest first) ahead of the 3 fixture example threads; opens the real report page. |
| Companies | Company Overview + Financials (real data) | ✅ | Wired to `companies/registry.py` + `financials/report.py`, real numbers/charts, not mocked. |
| Companies | Live NSE price badge (`web/live_quote.py`) | ✅ | Real-time quote via `yfinance`, 60s in-memory cache, graceful `None` fallback to the last ingested price on any fetch failure. Display-only — never feeds valuation math. |
| Companies | Overview tab — AI-generated key insights (`research/insights.py`, `company_insights` table) | ✅ | User-triggered "Generate insights" button; every generate/regenerate inserts a new row (kept as history, not overwritten), never auto-run. |
| Companies | Commentary tab | ⬜ | Honest "not yet available" placeholder — blocked on README [step 7](README.md#implementation-sequence) (Investor Relations / document pipeline) for real attributed pull-quotes. |
| Companies | Notes tab (`company_notes` table) | ✅ | Real per-company free-text notes: add/edit/delete, master-detail rail UI (`notes_panel.js`), backed by real routes — not the append-only-log the schema comment originally described. |
| Companies | Docs tab — real document timeline (`web/docs_feed.py`, `docs_timeline.js`) | ✅ | Fiscal-year/quarter grid built from real ingested periods (`list_company_periods`) plus whatever documents have been manually added via the Add form (`documents` table) — real periods with empty slots for a freshly-ingested company, not fabricated sample rows. Still no data-provider integration; every doc row is added by hand. |
| Companies | Admin: raw-file import per company | ✅ | Admin → Import Data panel and a per-company shortcut button run uploads through the same `ingest_file()` pipeline the CLI uses. `screener`, `nse` (NSE XBRL), and `proprietary` are registered adapters today; no `bse` adapter yet. Parse/adapter failures are caught and flashed, not 500s. |
| Companies | Indicators tab (`indicators/`, Settings → Indicator Rules) | ✅ | The Configurable Indicator Framework — deterministic, rule-based factual patterns (no LLM call anywhere on this path), e.g. "promoter holding fell 2.1pp" or "net profit grew 22% YoY". Two seeded rule families (`shareholding`, `financial_trajectory`), Global→Sector→Company-scoped threshold/classification overrides on `/settings`, and a 3-column (Positive/Observations/Warnings) disclosure on every company page. See [architecture.md's Configurable Indicator Framework](architecture.md#configurable-indicator-framework-indicators) for the full design. Deliberately deferred items (Critical/red tier, 9 of 11 planned rule families, a feedback loop, an audit-trail browsing UI) are tracked in `pendingList.md`, not here. |
| Companies | Threads tab | ✅ | Reads `research_thread_companies`-equivalent (generated reports naming this company) — same list layout as Investigations. |
| Watchlist | Pin/unpin companies + threads | ✅ | Real `watchlist_items` table + repository CRUD + CLI commands; page reads real pinned rows. Pin buttons live only on detail pages (Company page / Research-thread page), not the Companies list. |
| Watchlist | Per-company "Latest news (24h)" | ✅ | Real headline/link/source metadata from Google News' public RSS feed (not scraped HTML, not article content), lazy-fetched on first expand, 15-min in-memory cache. |
| Accounts | Sign-up, login, seeded admin | ✅ | Email-based sign-up (no verification), Werkzeug password hashing, signed-cookie sessions. Every tab is browsable signed out; only `/admin*` and `/settings` require auth. |
| Accounts | Per-user theming | ✅ | 4 themes (Light/White/Green/Dark) picked on `/settings`, stored per-user, applied via `data-theme` on `<html>`. No auto `prefers-color-scheme` detection. |
| Site | Unified header + profile menu | ✅ | One shared `_header.html` partial across every page (was two hand-synced copies). |
| Site | About page | ✅ | Public, grounded in the actual evidence-labeling scheme rather than aspirational copy. |
| Site | Self-hosted fonts, shared palette | ✅ | No Google Fonts CDN dependency (unreliable in testing). |
| Data pipeline | Screener ingestion (5 companies) | ✅ | HDFCBANK, IDFCFIRSTB, JIOFIN, SBFCFINANCE, POONAWALLAFIN — all real Screener exports, all Financial Services. Scoped to the `screener` adapter specifically; see the NSE XBRL row below for the database's much broader real coverage. |
| Data pipeline | NSE XBRL ingestion (`sources/nse_xbrl.py`, the `nse` adapter) | 🟡 | Real — runs through the same `ingest_file()` pipeline the CLI/Admin Import Data panel use, not a one-off script. Now the trust_rank-0 source of truth for a validated reporting period (see [Source / Provenance](README.md#source--provenance--reconciliation)). Brought real canonical-financials coverage to many more companies than the original 5-company Screener set, including non-bank names (see "Notes on data honesty" below). Nifty 50 coverage in progress, Nifty 500 planned — see `SCHEDULED_JOBS.md` for current coverage/cadence. No BSE adapter yet. |
| Data pipeline | Macro ingestion — RBI (`sources/macro.py`, `sources/rbi_*.py`) | ✅ | Real data ingested: repo rate + dozens of other RBI indicators (weekly/monthly/quarterly), ~42.6K observations. `imd`/`mospi`/`irda` sources are registered but have no files ingested yet — `imd`'s CSV-per-series pipeline works end-to-end, just unused (no rainfall files dropped in; IITM below covers that domain instead). |
| Data pipeline | Macro ingestion — IITM rainfall (`sources/iitm_rainfall.py`) | ✅ | Real long-period regional/subdivisional rainfall data ingested (1813-2016+, ~116.2K observations) from IITM Pune's fixed-width text publications — a different format/parser from the CSV convention the other macro sources use, since this isn't a per-series-CSV export. |
| Cost / observability | Usage page (`/admin/usage`) | ✅ | Admin-only page (profile menu) summarizing every LLM call ever logged (`llm_call_log`): total spend/tokens/calls, breakdown by task and by model, and a recent-calls table. Backed by `storage.repositories.get_llm_usage_summary()`. |
| Cost / observability | Model tiering policy (`config/settings.py`) | ✅ | Which model each hardness tier prefers (`TIER_PREFERRED_MODEL`), the minimum reasoning strength a fallback candidate needs (`TIER_MIN_REASONING_STRENGTH`), and which models are fully disabled regardless of tier/pin (`DISABLED_MODELS`) are all edited in one settings file rather than scattered across `llm/router.py`/`llm/hardness.py`/`llm/capability_registry.py`. Opus is disabled by default (cost-control policy) — every call this app makes today is Sonnet or below. |
| Data pipeline | US macro ingestion — FRED (`sources/fred.py`, `ingest-fred`) | ✅ | Live-fetched per series (Fed funds rate, Treasury yields, CPI, unemployment, GDP, ...) via FRED's public CSV endpoint, no API key — the US counterpart to RBI/IITM above. Per-series, on-demand, same model as `ingest-yfinance` (no scheduled/bulk pull). |
| Research | US macro evidence attribution | ✅ | `research/macro_evidence.py` spans both India (rbi/imd/iitm/mospi/irda) and US (fred) sources, attributing each matched series to `"INDIA"` or `"USA"` in the cited evidence rather than a single hardcoded `"INDIA"` label. |
| Data pipeline | Per-company fiscal year (`companies.fiscal_year_end_month`) | ✅ | Was a single global Apr-Mar assumption (`normalization/periods.py`); now per-company (defaults to 3/March for `--country IN`, 12/calendar-year for `--country US` via `add-company`, overridable). Screener (India-only) still relies on the March-close default. |
| Data pipeline | Multi-currency unit rescaling fix | ✅ | `web/valuation_feed.py`/`charts_feed.py` previously only rescaled `INR_LAKH`; a `USD_THOUSAND` series would have rendered 1000x too large next to a `USD_MILLION` one. Generalized to a small per-unit rescale table covering both currencies' unit families, plus `USD_BILLION` added to the valid-units set. |
| Infra | Database portability (`storage/db_types.py`) | ✅ | `storage/*.py` + `schemas/sqlite_schema.sql` are now the only places in the codebase that know SQLite exists — every other module (~40 files) type-hints `conn`/row parameters against backend-agnostic `DBConnection`/`Row` protocols instead of importing `sqlite3` directly. Mechanical, non-behavioral. New `storage/company_repository.py` and `storage/investigation_repository.py` absorbed SQL that used to live inline in `companies/`, `research/`, and one-off `scripts/`. See [architecture.md's Storage layer and database portability](architecture.md#storage-layer-and-database-portability-storagedb_typespy). |

---

## Pending

| Priority | Feature | Status | Blocked on / notes |
|---|---|---|---|
| High | Structured research-thread response for the short-answer path (README [step 11](README.md#web-ui-implementation-sequence)) | 🟡 open | The full Signals-report path (`/research/thread/generate`) now has a real evidence table and follow-ups (see Available table above); `/research/ask`'s short `answer_html` blob still doesn't — no key finding/confidence/methodology split, no evidence table, no follow-ups for a quick question that isn't escalated to a full report. Confidence still isn't computed anywhere (bounded by evidence count / FACT-vs-INFERENCE ratio, not LLM self-report). |
| High | BSE ingestion + a real multi-source reconciliation test (README [step 6](README.md#implementation-sequence)) | 🟡 partial | NSE XBRL ingestion is now real (see the Data pipeline row above) — this item has narrowed. BSE still has no adapter. Per [architecture.md's Known gaps](architecture.md#data-ingestion--sources), multi-source reconciliation itself remains unexercised: every company today still has exactly one active source per metric, so `reconcile()`'s conflict-resolution path has never run against real conflicting data. |
| High | Investor Relations + document pipeline, full version (README [step 7](README.md#implementation-sequence)) | 🟡 partial | `research/documents.py`'s direct PDF extraction (whole-document, no cache) still runs alongside the newer retrieval layer, not replaced by it. Page-scoped chunking + FTS5 + Qdrant-backed semantic search (`retrieval/hybrid_search.py`, RRF-fused) are now wired additively into Q&A (`research/assistant.py`), Signals reports (`research/signals_report.py`), and the Investigation Planner (see [architecture.md's Hybrid Document Retrieval](architecture.md#hybrid-document-retrieval-retrievalhybrid_searchpy)). Still missing: the Commentary tab's attributed pull-quote extraction. |
| Medium | Hypothesis chains & cross-metric charts (README [step 13](README.md#web-ui-implementation-sequence)) | ⬜ not started | New capability (causal step chains, indexed cross-domain series), not a reshaping of an existing one. Real macro data now exists (RBI + IITM rainfall) to build it against — the blocker now is the mechanism itself: no `indexed_series()` helper, no chain-of-steps schema, no UI for it. |
| Medium | Drop real data into `data/raw/_macro/imd\|mospi\|irda` (README [step 8](README.md#implementation-sequence)) | 🟡 partial | RBI and IITM (rainfall) are real, ingested, and wired into the research assistant (see Available table). `imd`/`mospi`/`irda` are registered sources with working pipelines but no files dropped in yet. |
| Medium | Add a non-financial-sector company (README [step 10](README.md#implementation-sequence)) | 🟡 partial | No longer true that every registered company is Financial Services — NSE XBRL ingestion (see Data pipeline row above) brought in real data for non-financial companies too (e.g. ADANIPORTS, ADANIENT both have real promoter-holding/net-profit data — `SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md` §6.4). What's unverified: whether sector-specific metrics (segment revenue, volume, realization) made it in too, or just the generic metrics indicators check — the schema's sector-generality claim isn't confirmed either way yet. |
| Low | Pin/unpin from the Companies list | ⬜ not started | Today only available from a company's own detail page or the Research-thread page. |
| Low | Comparison charts beyond 4 companies | ⬜ not started | `_LINE_COLORS`/`_MARKERS` in `charts/financial_charts.py` are 4-element lists; a 5th company would cycle back and become ambiguous. Untested beyond 2 in practice. |
| Low | Pricing page | ⬜ not started | No pricing model exists (self-use project, no billing/plans) — nav link was removed rather than left dead. |
| Low | System theme auto-detection | ⬜ not started | Per-user theming ships 4 explicit themes; no `prefers-color-scheme`-driven "System" option yet. |
| Medium | US company sector/industry classification | ⬜ not started | `companies.sector`/`industry`/`macro_economic_sector`/`basic_industry` are NSE's own 4-level taxonomy; no GICS-equivalent importer exists for US companies, so those columns stay `NULL` for them. `financials/ratios.py`'s bank/NBFC heuristic and GNPA/CASA terminology also stay India-vocabulary-biased. |
| Medium | Bulk US company-master importer | ⬜ not started | `companies/nse_import.py` bulk-registers from an NSE export; no parallel importer exists for a US index constituent list. Registering a US company is one-at-a-time today (`add-company --country US` / `ingest-yfinance`). |
| Low | `yfinance` fiscal-year labeling doesn't consult `fiscal_year_end_month` | ⬜ not started | `sources/yfinance_financials.py` labels US fiscal years by calendar close year (`FY{period_end.year}`), not the company's actual `fiscal_year_end_month`. Data is correct either way; only the `FY` label can be cosmetically off for a non-calendar US fiscal year (e.g. Apple's September close). |
| Low | N-country ticker-suffix / index-tag generalization | ⬜ not started | `web/live_quote.py`/`web/app.py`'s ticker-suffix and index-tag logic is a 2-way IN/US hardcoded branch — matches the app's stated US+India focus, not a general lookup table a third market would need. |
| Future phase | Multi-user server deployment, 1000+ concurrent sessions | ⬜ not started | Out of scope for the current [single-user, local-first design](#deployment-model) — a distinct future phase, not a natural extension of what's shipped. Would need, at minimum: a production WSGI server/process manager in front of Flask (`main.py serve` is a dev server); SQLite replaced or fronted for concurrent multi-writer access (single-file `data/equity_research.db`, no connection pooling today); real multi-tenant auth/authorization (today: one seeded admin, no per-user data isolation model); per-user/per-session LLM cost and rate controls (today's Model Router/Fallback and `llm_call_log` observability assume one user's traffic); and horizontal scaling for the Flask app itself. None of this is designed against yet. |

Two more backlog areas exist beyond this table and are tracked in
[pendingList.md](pendingList.md) rather than duplicated here: the
Configurable Indicator Framework's deliberately-deferred items (feedback
loop, Critical/red tier, the remaining 9 of 11 planned rule families,
audit-trail browsing UI) and the Golden Research Loop validation's ranked
gaps (missing NIM/CASA/cost-of-funds data, uneven document ingestion
coverage, IndusInd data ending FY2013, no charts attached to investigations,
thin indicator rule coverage on the golden-loop banks).

---

### Notes on data honesty

- `companies/registry.py`'s `SEED_COMPANIES` constant (HDFCBANK, ICICIBANK) is
  deliberate test/CLI scaffolding, not a description of the real database —
  in the test suite specifically, ICICIBANK is kept without ingested data on
  purpose (see `tests/test_structured_search.py`). **The real database has
  moved well past the original 5-company Screener set** this note used to
  describe: NSE XBRL ingestion (see the Data pipeline row above) now provides
  real canonical financials for ICICIBANK itself, and for several more
  companies — `SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`'s data table
  records real, measured canonical-financials coverage into FY2027 for
  HDFCBANK, ICICIBANK, IDFCFIRSTB, AXISBANK, and KOTAKBANK — and non-financial
  companies are registered too (see the "non-financial-sector company"
  Pending row). Verify against whatever `/companies` (or `python main.py
  status`) actually lists — this file decays fast and a fixed company
  count/list here goes stale quickly.
- Docs tab, Commentary tab, and macro data all follow the same rule: an empty
  or "not yet available" state is preferred over fabricated sample content
  once real data plumbing exists for that area.
