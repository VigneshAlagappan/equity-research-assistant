-- Global Equity Research Assistant — Postgres/Neon schema (companion to
-- schemas/sqlite_schema.sql; checkpoint 1 of the SQLite -> Postgres migration).
--
-- This file ports the 42 tables from sqlite_schema.sql that are moving to
-- Postgres in this checkpoint. It deliberately excludes:
--   - 7 pure audit/observability log tables staying SQLite-only forever:
--     batch_job_runs, batch_job_items, dataset_events, llm_call_log,
--     retrieval_diagnostics, reconciliation_log, worker_processing_log.
--   - ingestion_queue_items (discovery/status tracking only -- the real gate,
--     documents.processing_status, IS ported below).
--   - document_chunks_fts (the FTS5 virtual table + its shadow tables) --
--     full-text search reimplementation via Postgres tsvector/GIN is
--     deferred to a separate later task. document_chunks itself (the real
--     table holding chunk text) IS ported below.
--
-- Layer order: sources -> companies -> documents -> financial_observations
--   -> canonical_financials -> document_chunks
-- Raw observations are never overwritten; canonical_financials records the
-- reconciliation decision separately (see README: Source / Provenance & Reconciliation).
--
-- Translation notes vs. the SQLite original:
--   - `INTEGER PRIMARY KEY [AUTOINCREMENT]` -> `INTEGER GENERATED ALWAYS AS
--     IDENTITY PRIMARY KEY`. SQLite's plain `INTEGER PRIMARY KEY` already
--     autoincrements as a rowid alias when a caller inserts without
--     specifying the id, so both forms get the same IDENTITY treatment here
--     to keep existing insert-without-id call sites working once wired up.
--   - TEXT/REAL/INTEGER/BLOB column types are kept as-is (BLOB -> BYTEA,
--     Postgres's equivalent binary type); no column was upgraded to
--     DATE/TIMESTAMP -- every date/datetime in this app is stored as an
--     ISO-8601 TEXT string and compared/formatted as such throughout the
--     Python codebase, so the column types stay TEXT here too.
--   - PRAGMA statements are dropped (Postgres enforces foreign keys by
--     default; no equivalent pragma is needed).
--   - CHECK / UNIQUE / FOREIGN KEY / CREATE INDEX statements are ported
--     directly -- verified individually against real Postgres/Neon syntax.
--   - No query-level SQL (INSERT OR IGNORE/REPLACE, ON CONFLICT) is touched
--     here -- that conversion is a later checkpoint, entirely in storage/*.py.

-- ============================================================
-- Sources & reconciliation priority
-- ============================================================

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,       -- screener | nse | bse | investor_relations | macro
  name TEXT,
  trust_rank INTEGER,               -- default reconciliation priority (lower = preferred)
  description TEXT
);

-- ============================================================
-- Company Master & Lifecycle
-- ============================================================

CREATE TABLE IF NOT EXISTS companies (
  company_id TEXT PRIMARY KEY,          -- stable internal id, e.g. "HDFCBANK"
  legal_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  nse_symbol TEXT,
  bse_code TEXT,
  isin TEXT,
  country TEXT NOT NULL DEFAULT 'IN',      -- ISO 3166-1 alpha-2, e.g. "IN", "US" -- drives currency/exchange defaults, the Companies list filter, and live_quote.py's ticker-suffix logic
  currency TEXT NOT NULL DEFAULT 'INR',    -- ISO 4217, e.g. "INR", "USD" -- drives unit localization (normalization/financials.py) and price/financials display formatting
  fiscal_year_end_month INTEGER NOT NULL DEFAULT 3, -- 1-12, the calendar month this company's fiscal year closes in (3 = March, India's default; 12 = December, the common US default) -- drives normalization/periods.py's fiscal-year/quarter parsing
  website TEXT,                            -- not from an ingested source file; web-searched and entered manually
  valuation_model_file TEXT,               -- filename under web/static/data/ for a ported Claude Design valuation dashboard, if any
  macro_economic_sector TEXT,              -- NSE classification, broadest level, e.g. "Financial Services"
  sector TEXT,                             -- NSE classification, e.g. "Financial Services", "Chemicals"
  industry TEXT,                           -- NSE classification, e.g. "Banks", "Finance"
  basic_industry TEXT,                     -- NSE classification, most granular, e.g. "Private Sector Bank"
  status TEXT NOT NULL DEFAULT 'active',   -- active | archived
  listed_date TEXT,
  archived_at TEXT,
  archive_reason TEXT,                     -- delisted|acquired|merged|renamed|duplicate|manual
  predecessor_company_id TEXT REFERENCES companies(company_id),
  successor_company_id TEXT REFERENCES companies(company_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_identifier_history (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  identifier_type TEXT NOT NULL,   -- nse_symbol | bse_code | isin | legal_name
  identifier_value TEXT NOT NULL,
  effective_from TEXT,
  effective_to TEXT
);

CREATE INDEX IF NOT EXISTS idx_identifier_history_company
  ON company_identifier_history(company_id, identifier_type);

-- ============================================================
-- Metric vocabulary (lookup table, not hardcoded columns)
-- ============================================================

CREATE TABLE IF NOT EXISTS metrics_dictionary (
  metric_key TEXT PRIMARY KEY,             -- net_profit, gnpa, segment_revenue_tractors, ...
  display_name TEXT,
  category TEXT,                           -- income_statement|balance_sheet|cash_flow|ratio|bank|manufacturing|...
  applicable_sectors TEXT,                 -- JSON list, NULL = universal
  default_unit TEXT
);

-- Row labels aren't standardized across vendors/sectors (bank sheets say
-- "Interest Earned" instead of "Sales") -- mapping goes through this alias
-- table rather than hardcoded row positions, so a new alias is a data edit,
-- not a code change (README: Ingestion Approach by Source -> Screener).
CREATE TABLE IF NOT EXISTS metric_aliases (
  alias_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source TEXT NOT NULL REFERENCES sources(source_id),
  raw_label TEXT NOT NULL,          -- exact vendor row label, e.g. "Interest Earned"
  metric_key TEXT NOT NULL REFERENCES metrics_dictionary(metric_key),
  UNIQUE(source, raw_label)
);

-- ============================================================
-- Documents & Chunks (narrative documents; large binaries stay on filesystem)
-- ============================================================

CREATE TABLE IF NOT EXISTS documents (
  document_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT REFERENCES companies(company_id),
  source TEXT REFERENCES sources(source_id),
  document_type TEXT,               -- annual_report|investor_presentation|transcript|announcement|financial_result|xbrl|concall_recording|ai_summary
  fiscal_year TEXT,
  quarter TEXT,
  published_at TEXT,
  retrieved_at TEXT,
  raw_file_path TEXT,               -- points into data/documents/... ; NULL when source_url is a plain link (no uploaded file)
  file_hash TEXT,
  storage_object_key TEXT,          -- storage/document_store.py DocumentStore key (mirrors schemas/sqlite_schema.sql)
  content_hash TEXT,                -- sha256 via the active DocumentStore backend (mirrors schemas/sqlite_schema.sql)
  source_url TEXT,
  parser_version TEXT,
  added_by_user TEXT,               -- NULL = officially sourced; set = manually added via the Docs tab, by whom
  -- Settings/Admin -> Ingest queue (ingestion/coordinator.py): whether this
  -- document has been "registered" as ready for future knowledge extraction
  -- (Step 2A, not built yet -- Step 1 processing just marks it processed).
  -- pending | processing | processed | failed | skipped
  processing_status TEXT NOT NULL DEFAULT 'pending',
  processed_at TEXT,
  error_message TEXT               -- why processing_status='failed', for retry to show/act on
);

CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id, document_type);

-- ============================================================
-- Financial Observations (raw, per-source, pre-reconciliation) --
-- deliberately EXCLUDED from this Postgres schema (2026-09-11): the single
-- largest table (869K rows, 269MB on Postgres -- more than half of Neon
-- free tier's 512MB cap), and confirmed no live-facing feature reads it
-- directly (research/web/financials/context all read canonical_financials
-- instead) -- only the SQLite-side reconciliation pipeline touches it, to
-- produce canonical_financials. Same "stays SQLite-only, never ported"
-- treatment as the 8 audit-log tables, added here after the fact once
-- Neon's storage cap made keeping it not worth the cost. Stays in
-- schemas/sqlite_schema.sql and storage/repositories.py exactly as before.
-- ============================================================

-- ============================================================
-- Canonical (reconciled) financials
--
-- reconciliation_log (the audit trail of considered/chosen observations) is
-- NOT ported in this checkpoint -- it stays SQLite-only (pure audit log,
-- nothing reads it to gate a fetch/reprocessing decision).
-- ============================================================

CREATE TABLE IF NOT EXISTS canonical_financials (
  canonical_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL,
  metric_key TEXT NOT NULL,
  period_type TEXT NOT NULL,
  fiscal_year TEXT NOT NULL,
  quarter TEXT,
  statement_type TEXT,
  canonical_value REAL NOT NULL,
  unit TEXT NOT NULL,
  chosen_observation_id INTEGER,    -- no FK: financial_observations is excluded from this schema (see above)
  reconciliation_reason TEXT,       -- "official filing preferred over screener"
  normalization_version TEXT,
  decided_at TEXT NOT NULL,
  UNIQUE(company_id, metric_key, period_type, fiscal_year, quarter, statement_type)
);

-- ============================================================
-- Macro observations (non-company data: RBI, IMD, MOSPI, ...)
--
-- Mirrors financial_observations' shape (raw, per-source, append-only —
-- never overwritten, same as financial_observations) but keyed by
-- series_key/region instead of company_id, since these series aren't
-- scoped to a company. README: Data Layers -> Non-company sources.
-- ============================================================

CREATE TABLE IF NOT EXISTS macro_observations (
  observation_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  series_key TEXT NOT NULL,                -- repo_rate, rainfall_index, credit_growth_yoy, ...
  region TEXT,                             -- NULL = all-India/national; else e.g. "Maharashtra"
  period_type TEXT NOT NULL,               -- annual | monthly
  period TEXT NOT NULL,                    -- "2015" (annual) or "2015-06" (monthly)
  value REAL NOT NULL,
  unit TEXT NOT NULL,
  source TEXT NOT NULL REFERENCES sources(source_id),
  source_file TEXT,
  source_url TEXT,
  retrieved_at TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_obs_lookup ON macro_observations(series_key, region, period);

-- ============================================================
-- Bank-level infrastructure/transaction snapshots (RBI's monthly
-- ATM/card-acceptance and NEFT/RTGS bulletins under
-- data/raw/_macro/rbi/MoneyAndBanks/ATM*.XLSX, NEFTRTGS*.XLSX --
-- sources/rbi_bank_infrastructure.py). Deliberately NOT
-- macro_observations: this is bank x metric x period, not one flat
-- series x period the way every other macro source is -- a single
-- series_key per (bank, metric) pair would work but would bury ~700+
-- narrow series inside a table meant for economy-wide indicators, and
-- make "compare banks" queries awkward.
-- ============================================================

CREATE TABLE IF NOT EXISTS bank_infrastructure_observations (
  observation_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bank_name TEXT NOT NULL,
  metric TEXT NOT NULL,          -- e.g. "atms_crms_onsite", "neft_inward_amount_crore"
  period_type TEXT NOT NULL,     -- "monthly" -- these bulletins are always one calendar month
  period TEXT NOT NULL,          -- "YYYY-MM"
  value REAL NOT NULL,
  unit TEXT NOT NULL,
  source TEXT NOT NULL REFERENCES sources(source_id),
  source_file TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_infra_lookup ON bank_infrastructure_observations(bank_name, metric, period);

-- ============================================================
-- Document chunks (AI index)
--
-- The full-text search layer (SQLite's FTS5 virtual table
-- document_chunks_fts + its shadow tables) is deliberately NOT ported in
-- this checkpoint -- a Postgres tsvector/GIN reimplementation is a separate,
-- later task. This table (the real chunk text) IS ported.
-- ============================================================

CREATE TABLE IF NOT EXISTS document_chunks (
  chunk_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id INTEGER REFERENCES documents(document_id),
  company_id TEXT,
  section_heading TEXT,
  page_number INTEGER,
  chunk_index INTEGER,
  text TEXT NOT NULL,
  embedding BYTEA,                  -- unused legacy column, left in place; the real semantic
                                     -- layer (retrieval/vector_store.py) indexes vectors in the
                                     -- VectorStore, not here -- this table + FTS5 stay the
                                     -- rebuildable source chunks/keyword index, never the vector
                                     -- store's storage.
  -- Semantic-indexing status (retrieval/semantic_indexer.py) -- lets a
  -- backfill/re-index be idempotent (a chunk already 'indexed' under the
  -- current embedding_model is skipped) without needing to query the vector
  -- store just to find out. pending (default) | indexed | failed.
  embedding_status TEXT NOT NULL DEFAULT 'pending',
  embedding_model TEXT,
  embedded_at TEXT,
  created_at TEXT,
  -- Postgres tsvector/GIN full-text search replacement for SQLite's FTS5
  -- `document_chunks_fts` virtual table -- backfilled via
  -- `to_tsvector('english', text)`, kept current on insert by
  -- storage/repositories_pg.py::replace_document_chunks(), queried by
  -- storage/fact_store_pg.py::search_document_chunks() via `ts_rank`.
  search_vector tsvector
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector
  ON document_chunks USING GIN (search_vector);

-- ============================================================
-- Watchlist (single shared list -- no per-user model yet,
-- README: Web UI Implementation Sequence, step 16)
-- ============================================================

CREATE TABLE IF NOT EXISTS watchlist_items (
  item_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_type TEXT NOT NULL,      -- company | thread
  item_ref TEXT NOT NULL,       -- company_id or thread_id
  pinned_at TEXT NOT NULL,
  UNIQUE(item_type, item_ref)
);

-- A rolling 7-week cache of Google News RSS lookups (web/news.py) -- link,
-- source, and published time only, same "never the article content, just
-- an outbound pointer to it" scope web/news.py's own docstring already
-- commits to; nothing here is scraped article text. Exists so the News
-- page (web/templates/news.html) can show a merged multi-company feed from
-- one fast local query instead of a live RSS fetch per company on every
-- page view (infeasible outright across this app's full company registry
-- -- thousands of companies). Populated incidentally: whenever ANY existing
-- news lookup runs (the Watchlist row teaser, the Overview tab's news
-- section, or the News page itself), its results are upserted here too.
-- Rows older than 7 weeks (by `published_at`, falling back to `fetched_at`
-- when a feed item had no publish date) are pruned on each write -- see
-- storage/repositories.py::save_company_news.
CREATE TABLE IF NOT EXISTS company_news (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  title TEXT NOT NULL,
  link TEXT NOT NULL,
  source TEXT,
  published_at TEXT,       -- ISO timestamp from the feed; NULL if the feed gave no date
  fetched_at TEXT NOT NULL,  -- when this app first saw the item -- the retention fallback
  UNIQUE(company_id, link)
);
CREATE INDEX IF NOT EXISTS idx_company_news_company ON company_news(company_id, published_at DESC);

-- ============================================================
-- Generated Signals reports (research/signals_report.py, via
-- /research/thread/generate) -- full multi-section investigations, as
-- opposed to the short tagged answers from /research/ask which are never
-- persisted. company_ids is a JSON array (e.g. '["HDFCBANK", "ICICIBANK"]')
-- -- filtered in Python (web/app.py), not SQL, since a report only ever
-- names a handful of companies and this avoids a separate junction table.
-- ============================================================

CREATE TABLE IF NOT EXISTS generated_reports (
  thread_id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  company_ids TEXT NOT NULL,     -- JSON array of company_id
  statement_type TEXT NOT NULL,
  report_markdown TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  question_embedding TEXT,       -- JSON array of floats (context/reuse.py's semantic
                                  -- reuse-matching layer) -- NULL when the embedding
                                  -- provider was unavailable at save time; reuse
                                  -- matching then falls back to word-overlap only for
                                  -- this report, same graceful-degradation spirit as
                                  -- retrieval/hybrid_search.py
  question_embedding_model TEXT, -- which model produced it, so a later model/provider
                                  -- change can't silently compare incompatible vectors
  hidden_at TEXT,                -- reversible (Cases list "Hide"/"Unhide") -- ported from
                                  -- storage/database.py's _migrate_case_visibility_columns,
                                  -- SQLite added these via ALTER TABLE rather than in the
                                  -- original CREATE TABLE; Postgres gets them directly here
  deleted_at TEXT                -- permanent-looking in the UI ("archived forever"), but the
                                  -- row itself is never actually DELETEd, same "never truly
                                  -- destroy data" stance as archived companies
);

-- ============================================================
-- The deterministic Evidence (research/evidence.py) that actually grounded
-- one generated_reports row -- the real retrieval output the LLM was given,
-- not anything the LLM produced itself, so the Investigations evidence rail
-- can render real source/value/citation rows instead of parsing them back
-- out of report_markdown prose. sort_order preserves retrieval order.
-- ============================================================

CREATE TABLE IF NOT EXISTS research_thread_evidence (
  thread_id TEXT NOT NULL REFERENCES generated_reports(thread_id),
  sort_order INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- FACT | CALCULATION | MANAGEMENT_STATEMENT | INFERENCE
  company_id TEXT NOT NULL,
  label TEXT NOT NULL,
  value TEXT NOT NULL,
  citation TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_thread_evidence_thread_id
  ON research_thread_evidence(thread_id, sort_order);

-- ============================================================
-- Follow-up question suggestions the LLM appended to a generated_reports
-- row (research/signals_report.py parses these out of its own response,
-- see SIGNALS_SYSTEM_PROMPT's ===FOLLOWUP_QUESTIONS=== marker) -- persisted
-- so the Follow-up research rail's buttons are real, re-clickable
-- suggestions instead of dead UI.
-- ============================================================

CREATE TABLE IF NOT EXISTS research_thread_followups (
  thread_id TEXT NOT NULL REFERENCES generated_reports(thread_id),
  sort_order INTEGER NOT NULL,
  followup_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_thread_followups_thread_id
  ON research_thread_followups(thread_id, sort_order);

-- ============================================================
-- LLM-generated key insights (Overview tab) -- every generate/regenerate
-- inserts a new row, kept against generated_at rather than overwriting, so
-- a company's insights have history; user-triggered via a button, never
-- regenerated automatically. See research/insights.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS company_insights (
  insight_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  insight_text TEXT NOT NULL,
  statement_type TEXT NOT NULL,
  generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_insights_company_id
  ON company_insights(company_id, generated_at);

-- System Insights (Tools tab) -- distinct from company_insights above:
-- company_insights is one free-text blob per company, generated on request,
-- grounded only in canonical_financials. system_insights is cross-company,
-- generated in a batch (research/system_insights.py), grounded in the
-- Knowledge Graph's knowledge_claims (source_claim_ids is provenance), and
-- carries a user-controlled status the company_insights table has no
-- equivalent of -- same "status TEXT NOT NULL DEFAULT 'x' -- a | b | c"
-- shape documents.processing_status already uses.
CREATE TABLE IF NOT EXISTS system_insights (
  insight_id TEXT PRIMARY KEY,
  company_ids TEXT NOT NULL,          -- JSON array of company_id
  insight_text TEXT NOT NULL,
  source_claim_ids TEXT,              -- JSON array of knowledge_claims.claim_id (provenance)
  status TEXT NOT NULL DEFAULT 'new', -- new | retained | archived
  generated_at TEXT NOT NULL,
  status_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_system_insights_status ON system_insights(status, generated_at);

-- ============================================================
-- Personal notes (Notes tab) -- user-authored, dated, editable; unlike
-- company_insights this is never LLM-generated, just a running log the user
-- keeps for themselves against a company. note_text holds rich-text HTML
-- from the contenteditable editor (web/static/js/notes_panel.js), always
-- passed through web/rich_text.py's sanitize_note_html() before it's
-- written here -- this column is trusted-safe-to-render precisely because
-- every write path enforces that, not because of anything in the schema.
-- ============================================================

CREATE TABLE IF NOT EXISTS company_notes (
  note_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  note_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_company_notes_company_id
  ON company_notes(company_id, created_at);

-- ============================================================
-- Files attached to a note (the Notes tab editor's paperclip button) --
-- same never-overwrite, on-disk-plus-row convention as `documents`, stored
-- under data/documents/<company_id>/note_attachments/ instead of mixing
-- with financial-document uploads. Only attachable to a note that's already
-- been saved (has a note_id) -- the compose-a-new-note flow disables the
-- paperclip until the first save.
-- ============================================================

CREATE TABLE IF NOT EXISTS company_note_attachments (
  attachment_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  note_id INTEGER NOT NULL REFERENCES company_notes(note_id),
  filename TEXT NOT NULL,
  raw_file_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  uploaded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_note_attachments_note_id
  ON company_note_attachments(note_id);

-- ============================================================
-- Index membership (Admin tab) -- which market indices (Nifty 50, Sensex,
-- ...) a company belongs to. Many-to-many; index_name is constrained to
-- index_definitions (below) at the application layer, not a real FK (SQLite
-- can rename a referenced row without touching dependents, but a real FK
-- would block the rename until every membership row was updated first).
-- ============================================================

CREATE TABLE IF NOT EXISTS company_index_membership (
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  index_name TEXT NOT NULL,
  PRIMARY KEY (company_id, index_name)
);

-- ============================================================
-- Sector / Industry / Index-tag vocabularies (Admin tab: "Sectors,
-- Industries & Tags") -- editable lookup tables an admin can add/rename/
-- delete from directly, rather than sector/industry being pure freeform
-- text on `companies` (the "+ Add new..." escape hatch on a company's own
-- row still works and just adds a row here too) and index tags being a
-- hardcoded Python list. Seeded on first run from whatever's already in use
-- (storage/database.py's _seed_sectors_and_industries/_seed_index_definitions)
-- so nothing already-assigned silently disappears from a dropdown.
-- Renaming updates every company/membership row using the old name in the
-- same transaction (storage/repositories.py's rename_*); these are plain
-- TEXT primary keys, not INTEGER ids, specifically so a rename is a single
-- UPDATE ... WHERE name = ? on both this table and its dependents, not an
-- id lookup + two separate updates.
-- ============================================================

CREATE TABLE IF NOT EXISTS sectors (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industries (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_definitions (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

-- ============================================================
-- Company list column configuration (Admin tab) -- which optional columns
-- are available at all on the Companies list. The Companies list itself
-- additionally lets a visitor temporarily narrow further, per-browser
-- (localStorage, not stored server-side) -- this table is only the
-- admin-controlled superset. column_key values are fixed at the
-- application layer (storage/repositories.py's COMPANY_LIST_COLUMNS), not
-- user-defined.
-- ============================================================

CREATE TABLE IF NOT EXISTS company_list_column_settings (
  column_key TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1
);

-- ============================================================
-- Company Overview-tab ratio grid configuration (Admin tab) -- which ratios
-- from the fixed catalog (storage/repositories.py's OVERVIEW_RATIO_CATALOG)
-- appear on a company's Overview tab (web/templates/company.html,
-- web/static/js/valuation_dashboard.js). Same shape/reasoning as
-- company_list_column_settings above: ratio_key values are fixed at the
-- application layer, not user-defined -- adding a genuinely new ratio is a
-- one-entry addition to the catalog in code, which then shows up here
-- automatically (enabled by default) for an admin to toggle, no schema
-- change needed per ratio.
-- ============================================================

CREATE TABLE IF NOT EXISTS overview_ratio_settings (
  ratio_key TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1
);

-- ============================================================
-- Knowledge Builder (Step 2A, research/knowledge_builder.py) -- structured
-- research knowledge extracted from a processed document (Admin -> Ingest
-- queue), grounded and provenanced. Plain SQL storage only -- no Neo4j at
-- this stage (that's Step 2B, a separate later step). Every extraction is
-- additive: a new quarter's management statement becomes a NEW claim row,
-- never an UPDATE to a previous one -- same "never overwrite" discipline
-- financial_observations already follows.
--
-- knowledge_entities  -- Company/Product/Segment/Risk/... named things
-- knowledge_claims    -- one extracted statement, with its own provenance
--                         (document, company, fiscal period, speaker,
--                         claim_type, extraction_confidence)
-- knowledge_relationships -- typed edges between two entities, optionally
--                         traced back to the claim that asserted them
-- knowledge_evidence  -- the supporting quote for one claim, traceable to
--                         its source document
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_entities (
  entity_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_type TEXT NOT NULL,        -- Company | ManagementPerson | Product | Segment | Industry |
                                     -- Strategy | Risk | Opportunity | Metric | MacroFactor | Regulation
  name TEXT NOT NULL,
  company_id TEXT REFERENCES companies(company_id),  -- NULL for an entity not tied to one company
  created_at TEXT NOT NULL,
  UNIQUE(entity_type, name, company_id)
);

CREATE TABLE IF NOT EXISTS knowledge_claims (
  claim_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(document_id),
  company_id TEXT REFERENCES companies(company_id),
  claim_type TEXT NOT NULL,         -- FACT | CALCULATION | MANAGEMENT_OPINION | PREDICTION |
                                     -- INFERENCE | CORRELATION | CAUSATION
  category TEXT,                    -- strategy | guidance | risk | opportunity | fact | competitive | regulatory | other
  claim_text TEXT NOT NULL,
  speaker TEXT,                     -- e.g. "CEO"; NULL if not attributable to a specific person
  fiscal_year TEXT,
  quarter TEXT,
  extraction_confidence REAL,       -- 0-1, the model's own stated confidence
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_claims_company ON knowledge_claims(company_id, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_knowledge_claims_document ON knowledge_claims(document_id);

CREATE TABLE IF NOT EXISTS knowledge_relationships (
  relationship_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  claim_id INTEGER REFERENCES knowledge_claims(claim_id),  -- the claim this relationship was asserted in, if any
  source_entity_id INTEGER NOT NULL REFERENCES knowledge_entities(entity_id),
  relationship_type TEXT NOT NULL,  -- OFFERS | OPERATES_IN | COMPETES_WITH | SUPPLIES | DEPENDS_ON |
                                     -- MAY_AFFECT | DRIVES | EXPOSED_TO (config/knowledge_ontology.py)
  target_entity_id INTEGER NOT NULL REFERENCES knowledge_entities(entity_id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_relationships_source ON knowledge_relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_relationships_target ON knowledge_relationships(target_entity_id);

CREATE TABLE IF NOT EXISTS knowledge_evidence (
  evidence_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  claim_id INTEGER NOT NULL REFERENCES knowledge_claims(claim_id),
  document_id INTEGER NOT NULL REFERENCES documents(document_id),
  quote TEXT,                       -- the supporting excerpt from the source document
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_evidence_claim ON knowledge_evidence(claim_id);

-- ============================================================
-- Hypothesis-driven investigations (Steps 2E-2H, research/investigation.py)
-- -- the full "generate competing hypotheses -> gather evidence -> evaluate
-- each independently -> rank/synthesize" loop. Distinct from
-- generated_reports (research/signals_report.py's single narrative report,
-- Q&A-shaped) -- an investigation is structured around multiple named,
-- independently-evaluated hypotheses, not one answer. Every table here is
-- write-once/append-only per investigation, same "never overwrite, the
-- decision is auditable" discipline as reconciliation_log.
-- ============================================================

CREATE TABLE IF NOT EXISTS investigations (
  investigation_id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  company_ids TEXT NOT NULL,        -- JSON array of company_id (display order, as asked)
  statement_type TEXT NOT NULL,
  strongest_explanation TEXT,       -- Step 2H's synthesis narrative
  unanswered_questions TEXT,        -- JSON array
  additional_evidence_needed TEXT,  -- JSON array
  generated_at TEXT NOT NULL,
  as_of TEXT,                       -- ISO date: point-in-time evidence cutoff, NULL = "everything known today"
  hidden_at TEXT,                   -- ported from storage/database.py's
                                     -- _migrate_case_visibility_columns -- see
                                     -- generated_reports above for the same columns' reasoning
  deleted_at TEXT
);

-- One investigation <-> many companies. `investigations.company_ids` above
-- stays the ordered, as-asked list (it is what the investigation view
-- renders); this join table is the *queryable* association, so
-- "every investigation that touches company X" is an indexed lookup rather
-- than a JSON LIKE scan over every row. A cross-company investigation
-- (e.g. "HDFC Bank vs ICICI Bank") gets one row per company and is still a
-- single investigation record — it appears under each company's
-- Investigations section without the underlying record being duplicated.
CREATE TABLE IF NOT EXISTS investigation_companies (
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  position INTEGER NOT NULL DEFAULT 0,  -- the company's index in company_ids, so ordering survives the join
  PRIMARY KEY (investigation_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_investigation_companies_company ON investigation_companies(company_id);

CREATE TABLE IF NOT EXISTS investigation_hypotheses (
  hypothesis_id TEXT PRIMARY KEY,
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  statement TEXT NOT NULL,
  mechanism TEXT,
  chain_steps TEXT,            -- JSON array of short causal-stage labels (Step 2E), cause -> observed effect;
                                -- NULL/empty for hypotheses generated before this column existed (falls back
                                -- to rendering `mechanism` prose instead — see web/templates/investigation.html)
  category TEXT NOT NULL,     -- financial|operational|competitive|strategic|management|regulatory|macro|industry
  rationale TEXT,
  unknowns TEXT,               -- JSON array
  generation_order INTEGER NOT NULL,  -- the order Step 2E produced them in
  verdict TEXT,                -- SUPPORTED|PARTIALLY_SUPPORTED|REFUTED|INSUFFICIENT_EVIDENCE (Step 2G)
  confidence_basis TEXT,       -- Step 2G's own explanation of the verdict
  confidence_score INTEGER,    -- Step 2G's own 0-100 evidence-strength estimate; NULL if evaluation never ran
                                -- or predates this column
  synthesis_rank INTEGER,      -- Step 2H's final ranking (1 = strongest); NULL until synthesized
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_investigation_hypotheses_investigation ON investigation_hypotheses(investigation_id);

CREATE TABLE IF NOT EXISTS investigation_hypothesis_evidence (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  hypothesis_id TEXT NOT NULL REFERENCES investigation_hypotheses(hypothesis_id),
  stance TEXT NOT NULL,   -- supporting | contradicting | missing (Step 2G)
  kind TEXT NOT NULL,     -- FACT|CALCULATION|MANAGEMENT_OPINION|PREDICTION|INFERENCE|CORRELATION|CAUSATION
  label TEXT NOT NULL,
  value TEXT,
  citation TEXT
);
CREATE INDEX IF NOT EXISTS idx_investigation_hypothesis_evidence_hypothesis ON investigation_hypothesis_evidence(hypothesis_id);

-- ============================================================
-- Stock actions (Admin tab) -- discrete corporate events that change a
-- company's outstanding share count: splits, bonus issues, rights issues.
-- Raw records only for now -- no split-adjustment of historical shares/EPS/
-- price series and no chart markers yet (a documented follow-up, not built
-- here); this table just gives every action a durable, auditable home.
-- action_type: split | bonus | rights. ratio_from/ratio_to describe shares
-- held before/after (a 1:2 split and a 1-for-1 bonus are the same
-- share-count math, stored the same way -- ratio_from=1, ratio_to=2).
-- subscription_price only applies to a rights issue, the one type that
-- involves real cash rather than a pure share-count change.
-- ============================================================

CREATE TABLE IF NOT EXISTS stock_actions (
  action_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  action_type TEXT NOT NULL,        -- split | bonus | rights
  action_date TEXT NOT NULL,        -- ISO date (YYYY-MM-DD), the ex-date
  ratio_from REAL NOT NULL,         -- shares held before, e.g. 1
  ratio_to REAL NOT NULL,           -- shares held after, e.g. 2
  subscription_price REAL,          -- rights issues only; NULL for split/bonus
  source TEXT,
  source_url TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_actions_company ON stock_actions(company_id, action_date);

-- ============================================================
-- Corporate actions, raw feed -- NSE's corporates-corporateActions listing
-- (Bonus, Dividend, Split, Face-Value Split, Rights), fetched and stored
-- verbatim with zero interpretation (see sources/nse_corporate_actions.py).
-- `subject` is NSE's own freeform text -- classifying it into a type is a
-- separate ingestion-layer concern, deliberately not done at fetch time,
-- same "raw feed, decide how to process it separately" split this app
-- already uses for financial_observations vs canonical_financials.
-- Independent of stock_actions above: that table is hand-curated
-- share-count-adjustment ratios feeding indicator math; this one is the
-- full auto-fetched history (including dividends, which don't fit
-- stock_actions' ratio_from/ratio_to shape at all).
-- ============================================================

CREATE TABLE IF NOT EXISTS corporate_actions_raw (
  raw_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  subject TEXT NOT NULL,            -- NSE's raw free-text label, verbatim (may carry leading/trailing whitespace)
  ex_date TEXT NOT NULL,            -- ISO date
  record_date TEXT,                 -- ISO date, nullable -- some older actions carry "-" and use bc_start/end_date instead
  face_value REAL,
  bc_start_date TEXT,               -- book-closure window, when this action used one instead of a record date
  bc_end_date TEXT,
  raw_json TEXT NOT NULL,           -- the full NSE row, verbatim, for anything not modeled in columns above
  source TEXT NOT NULL DEFAULT 'nse',
  retrieved_at TEXT NOT NULL,
  processed_at TEXT,                -- NULL until a future ingestion step classifies this row (not built yet)
  UNIQUE(company_id, ex_date, subject)
);
CREATE INDEX IF NOT EXISTS idx_corp_actions_raw_unprocessed ON corporate_actions_raw(company_id) WHERE processed_at IS NULL;

-- Processed/display-ready rows, produced by ingestion/corporate_actions.py
-- classifying corporate_actions_raw.subject -- see that module for the
-- keyword rules. classifier_version lets a future rule change be
-- re-applied to history (re-run ingestion) without re-fetching from NSE.
CREATE TABLE IF NOT EXISTS corporate_actions (
  action_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_id INTEGER NOT NULL REFERENCES corporate_actions_raw(raw_id),
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  action_type TEXT NOT NULL,        -- bonus | dividend | split | fv_split | rights | other
  subject TEXT NOT NULL,
  ex_date TEXT NOT NULL,
  record_date TEXT,
  face_value REAL,
  classifier_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(raw_id)
);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_company ON corporate_actions(company_id, ex_date);

-- ============================================================
-- Shareholding pattern (SEBI LODR Reg 31) -- an independent domain from
-- financial_observations/canonical_financials: NSE's corporate-share-
-- holdings-master listing gives one row per quarterly submission
-- (aggregate promoter/public/employee-trust %), and each submission's own
-- linked XBRL adds individually-named holders on top (not every
-- sub-category is named -- see sources/nse_shareholding.py's module
-- docstring). Single-source (NSE only) today, so neither table here is
-- routed through metric_aliases/reconciliation -- upserted directly,
-- keyed on the natural (company, period[, holder]) identity.
-- ============================================================

CREATE TABLE IF NOT EXISTS shareholding_observations (
  observation_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  fiscal_year TEXT NOT NULL,
  quarter TEXT NOT NULL,
  promoter_holding_percent REAL,
  public_holding_percent REAL,
  employee_trust_percent REAL,
  -- Institutional breakdown of the public_holding_percent total above --
  -- Screener-style FII/DII/Government/Public(non-institutional) split,
  -- read off the SAME SHP XBRL's own category-rollup contexts (Table I,
  -- CategoryOfShareholdersAxis) rather than hand-aggregated here -- see
  -- sources/nse_shareholding.py's parse_shp_category_breakdown(). Only
  -- populated where that XBRL parses (same taxonomy-version gap as the
  -- named-holder tables); NULL for an older filing, not a wrong zero.
  fii_percent REAL,
  dii_percent REAL,
  government_percent REAL,
  public_non_institutional_percent REAL,
  num_shareholders INTEGER,
  source TEXT NOT NULL DEFAULT 'nse',
  source_url TEXT,                  -- the SHP xbrl link, for provenance
  submission_date TEXT,
  retrieved_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  -- When the per-quarter detail fetch (fetch_shareholding_detail(), one
  -- extra HTTP call per quarter beyond the master listing above) last ran
  -- for this quarter -- set on ANY successful fetch, whether or not it
  -- found a named-holder/FII-DII breakdown to parse, so a repeat
  -- scripts/batch_fetch_nse.py "Run now" can skip a quarter it already
  -- has, rather than re-fetching every quarter NSE's listing returns on
  -- every single click. NULL both for "not tried yet" and for a company
  -- whose row predates this column.
  detail_fetched_at TEXT,
  UNIQUE(company_id, fiscal_year, quarter)
);
CREATE INDEX IF NOT EXISTS idx_shareholding_company ON shareholding_observations(company_id, fiscal_year, quarter);

-- One row per individually-named shareholder disclosed in a submission's
-- SHP XBRL -- promoter individuals/HUF and promoter-group bodies corporate
-- on the "promoter" side; named institutional holders (mutual funds, FPIs,
-- insurers, pension funds, and similar) on the "public" side. Retail /
-- aggregate-only sub-categories never produce a row here, by taxonomy
-- design.
CREATE TABLE IF NOT EXISTS shareholding_holders (
  holder_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  fiscal_year TEXT NOT NULL,
  quarter TEXT NOT NULL,
  side TEXT NOT NULL,                -- promoter | public
  category TEXT NOT NULL,            -- e.g. "Individuals / HUF", "Mutual Funds / UTI"
  holder_name TEXT NOT NULL,
  num_shares REAL,
  percent_of_shares REAL,
  source TEXT NOT NULL DEFAULT 'nse',
  source_url TEXT,
  submission_date TEXT,
  retrieved_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (side IN ('promoter', 'public')),
  UNIQUE(company_id, fiscal_year, quarter, side, holder_name)
);
CREATE INDEX IF NOT EXISTS idx_shareholding_holders_lookup ON shareholding_holders(company_id, fiscal_year, quarter, side, percent_of_shares);

-- ============================================================
-- Users -- sign-up is email-based (no verification, self-use system).
-- The one seeded admin account logs in by username instead of email, so
-- it's a separate nullable column rather than a fake "admin@..." email.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email TEXT UNIQUE,
  username TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  theme TEXT NOT NULL DEFAULT 'schwab',  -- light | white | green | dark | schwab -- storage/repositories.py's VALID_THEMES
  created_at TEXT NOT NULL,
  CHECK (email IS NOT NULL OR username IS NOT NULL)
);

-- ============================================================
-- Configurable Indicator Framework (indicators/*.py)
--
-- Indicators are deterministic, rule-based factual patterns ("promoter
-- holding declined more than X pp"), NOT LLM output and NOT inferences --
-- they sit next to Evidence in this app's Fact -> Evidence -> Inference ->
-- Hypothesis -> Conclusion separation. The rules themselves are Python
-- (indicators/rules.py: trigger logic, required facts, explanation
-- template, version) and deliberately are NOT rows here -- same reasoning
-- as company_list_column_settings/overview_ratio_settings above, where the
-- catalog lives in code and only the toggles live in the database.
--
-- indicator_rule_config -- the ONLY user-editable layer. One row per
--   (user, rule, scope) override; a NULL column means "inherit", so an
--   override of just `classification` never freezes the threshold it
--   didn't touch. Resolution is per-field most-specific-wins
--   (company > sector > global-user-default > the Python rule's own
--   default) -- indicators/config.py::resolve_effective_config. A user
--   changing anything here never modifies or duplicates the system rule.
-- indicator_evaluations -- append-only audit trail, same spirit as
--   reconciliation_log: what fired, on which facts, under which effective
--   configuration, at which version, when. Never updated in place. A
--   re-evaluation whose result_hash matches that rule's most recent row
--   for the same (user, company) appends nothing -- refreshing a company
--   page is not a new auditable event, a *changed* result is.
--
-- A future indicator_feedback table (Agree | Disagree | Not Sure, spec
-- section 11) would hang off indicator_evaluations(evaluation_id); it is
-- deliberately not built in this increment.
-- ============================================================

CREATE TABLE IF NOT EXISTS indicator_rule_config (
  config_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(user_id),
  rule_id TEXT NOT NULL,             -- indicators/rules.py registry key, not an FK
  scope_type TEXT NOT NULL,          -- global | sector | company
  scope_value TEXT NOT NULL DEFAULT '',  -- '' for global; a sectors.name; a companies.company_id
  enabled INTEGER,                   -- NULL = inherit; 0/1 otherwise
  classification TEXT,               -- NULL = inherit; positive | observation | warning
  thresholds_json TEXT,              -- NULL = inherit; JSON object of per-threshold overrides
  updated_at TEXT NOT NULL,
  CHECK (scope_type IN ('global', 'sector', 'company')),
  CHECK (classification IS NULL OR classification IN ('positive', 'observation', 'warning')),
  UNIQUE(user_id, rule_id, scope_type, scope_value)
);
CREATE INDEX IF NOT EXISTS idx_indicator_rule_config_user ON indicator_rule_config(user_id, rule_id);

CREATE TABLE IF NOT EXISTS indicator_evaluations (
  evaluation_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  user_id INTEGER,                   -- NULL = evaluated with system defaults only (signed-out view)
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,        -- bumped in code whenever trigger logic changes
  classification TEXT NOT NULL,      -- the EFFECTIVE classification, after user config
  severity TEXT NOT NULL,            -- low | medium | high
  explanation TEXT NOT NULL,         -- rendered from the rule's own factual template
  facts_json TEXT NOT NULL,          -- the input fact values the rule actually fired on
  effective_config_json TEXT NOT NULL,  -- resolved enabled/classification/thresholds + per-field source
  scope_applied TEXT NOT NULL,       -- most specific scope that contributed, e.g. "company:HDFCBANK"
  period_label TEXT,                 -- e.g. "Q2 FY2026", "FY2024" -- NULL when not period-shaped
  provenance TEXT,                   -- source table/url the facts came from
  result_hash TEXT NOT NULL,         -- rule + version + facts + effective config -> dedupe key
  evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_indicator_evaluations_company
  ON indicator_evaluations(company_id, evaluated_at);
