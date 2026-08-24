-- Indian Equity AI Research Assistant (POC) — SQLite schema
--
-- Layer order: sources -> companies -> documents -> financial_observations
--   -> canonical_financials -> reconciliation_log -> document_chunks (+FTS5)
-- Raw observations are never overwritten; canonical_financials records the
-- reconciliation decision separately (see README: Source / Provenance & Reconciliation).

PRAGMA foreign_keys = ON;

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
  id INTEGER PRIMARY KEY,
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
  alias_id INTEGER PRIMARY KEY,
  source TEXT NOT NULL REFERENCES sources(source_id),
  raw_label TEXT NOT NULL,          -- exact vendor row label, e.g. "Interest Earned"
  metric_key TEXT NOT NULL REFERENCES metrics_dictionary(metric_key),
  UNIQUE(source, raw_label)
);

-- ============================================================
-- Documents & Chunks (narrative documents; large binaries stay on filesystem)
-- ============================================================

CREATE TABLE IF NOT EXISTS documents (
  document_id INTEGER PRIMARY KEY,
  company_id TEXT REFERENCES companies(company_id),
  source TEXT REFERENCES sources(source_id),
  document_type TEXT,               -- annual_report|investor_presentation|transcript|announcement|financial_result|xbrl|concall_recording|ai_summary
  fiscal_year TEXT,
  quarter TEXT,
  published_at TEXT,
  retrieved_at TEXT,
  raw_file_path TEXT,               -- points into data/documents/... ; NULL when source_url is a plain link (no uploaded file)
  file_hash TEXT,
  source_url TEXT,
  parser_version TEXT,
  added_by_user TEXT                -- NULL = officially sourced; set = manually added via the Docs tab, by whom
);

CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id, document_type);

-- ============================================================
-- Financial Observations (raw, per-source, pre-reconciliation)
-- ============================================================

CREATE TABLE IF NOT EXISTS financial_observations (
  observation_id INTEGER PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  metric_key TEXT NOT NULL REFERENCES metrics_dictionary(metric_key),
  period_type TEXT NOT NULL,               -- annual | quarterly
  fiscal_year TEXT NOT NULL,               -- e.g. FY2025
  quarter TEXT,                            -- Q1..Q4, NULL for annual
  statement_type TEXT,                     -- consolidated | standalone
  value REAL NOT NULL,
  unit TEXT NOT NULL,                      -- INR_CRORE, INR_LAKH, PERCENT, RATIO, NUMBER
  currency TEXT NOT NULL DEFAULT 'INR',
  source TEXT NOT NULL REFERENCES sources(source_id),
  source_document_id INTEGER REFERENCES documents(document_id),
  source_file TEXT,
  source_url TEXT,
  retrieved_at TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  normalization_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_lookup
  ON financial_observations(company_id, metric_key, fiscal_year, quarter);

-- ============================================================
-- Canonical (reconciled) financials & reconciliation audit trail
-- ============================================================

CREATE TABLE IF NOT EXISTS canonical_financials (
  canonical_id INTEGER PRIMARY KEY,
  company_id TEXT NOT NULL,
  metric_key TEXT NOT NULL,
  period_type TEXT NOT NULL,
  fiscal_year TEXT NOT NULL,
  quarter TEXT,
  statement_type TEXT,
  canonical_value REAL NOT NULL,
  unit TEXT NOT NULL,
  chosen_observation_id INTEGER REFERENCES financial_observations(observation_id),
  reconciliation_reason TEXT,       -- "official filing preferred over screener"
  normalization_version TEXT,
  decided_at TEXT NOT NULL,
  UNIQUE(company_id, metric_key, period_type, fiscal_year, quarter, statement_type)
);

CREATE TABLE IF NOT EXISTS reconciliation_log (
  log_id INTEGER PRIMARY KEY,
  canonical_id INTEGER REFERENCES canonical_financials(canonical_id),
  observation_id INTEGER REFERENCES financial_observations(observation_id),
  considered_at TEXT,
  was_chosen INTEGER,               -- 0/1
  note TEXT
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
  observation_id INTEGER PRIMARY KEY,
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
  observation_id INTEGER PRIMARY KEY,
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
-- Document chunks + full-text search (AI index)
-- ============================================================

CREATE TABLE IF NOT EXISTS document_chunks (
  chunk_id INTEGER PRIMARY KEY,
  document_id INTEGER REFERENCES documents(document_id),
  company_id TEXT,
  section_heading TEXT,
  page_number INTEGER,
  chunk_index INTEGER,
  text TEXT NOT NULL,
  embedding BLOB,                   -- NULL until a semantic layer is added
  created_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
  USING fts5(text, content='document_chunks', content_rowid='chunk_id');

-- ============================================================
-- Watchlist (single shared list -- no per-user model in the POC,
-- README: Web UI Implementation Sequence, step 16)
-- ============================================================

CREATE TABLE IF NOT EXISTS watchlist_items (
  item_id INTEGER PRIMARY KEY,
  item_type TEXT NOT NULL,      -- company | thread
  item_ref TEXT NOT NULL,       -- company_id or thread_id
  pinned_at TEXT NOT NULL,
  UNIQUE(item_type, item_ref)
);

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
  generated_at TEXT NOT NULL
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
-- One row per llm/router.py route() call -- observability for the Context
-- Optimization + Model Routing + Fallback layer (llm/observability.py).
-- attempts_json records every candidate model tried, in order, including
-- ones skipped as too weak for the task and ones that failed over --
-- estimated_cost_usd is a rough per-call estimate from a static price
-- table, not a billed-amount reconciliation.
-- ============================================================

CREATE TABLE IF NOT EXISTS llm_call_log (
  call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  task_name TEXT NOT NULL,        -- assistant_qa | key_insights | signals_report | macro_retrieval_plan
  company_ids TEXT,               -- comma-separated company_id list
  question TEXT,
  thread_id TEXT,
  complexity_tier TEXT NOT NULL,
  complexity_level INTEGER NOT NULL,
  complexity_reason TEXT,
  model_used TEXT NOT NULL,
  provider_used TEXT NOT NULL,
  fallback_used INTEGER NOT NULL DEFAULT 0,
  attempts_json TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms REAL,
  stop_reason TEXT,
  -- Context Optimizer (context/optimizer.py) accounting -- tokens before/after
  -- dedup+budgeting and how many evidence lines were dropped to fit budget.
  context_tokens_before INTEGER,
  context_tokens_after INTEGER,
  context_items_dropped INTEGER,
  -- Reuse-before-recompute (context/reuse.py) -- set when this call was
  -- answered from a prior fresh investigation instead of a new LLM call,
  -- in which case model_used/provider_used above read "reused"/"cache" and
  -- input_tokens/output_tokens/estimated_cost_usd are all 0.
  reuse_hit INTEGER NOT NULL DEFAULT 0,
  reused_thread_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_call_log_created_at ON llm_call_log(created_at);

-- ============================================================
-- LLM-generated key insights (Overview tab) -- every generate/regenerate
-- inserts a new row, kept against generated_at rather than overwriting, so
-- a company's insights have history; user-triggered via a button, never
-- regenerated automatically. See research/insights.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS company_insights (
  insight_id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id TEXT NOT NULL REFERENCES companies(company_id),
  insight_text TEXT NOT NULL,
  statement_type TEXT NOT NULL,
  generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_insights_company_id
  ON company_insights(company_id, generated_at);

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
  note_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
-- Users -- sign-up is email-based (no verification, README: self-use POC).
-- The one seeded admin account logs in by username instead of email, so
-- it's a separate nullable column rather than a fake "admin@..." email.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE,
  username TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  theme TEXT NOT NULL DEFAULT 'schwab',  -- light | white | green | dark | schwab -- storage/repositories.py's VALID_THEMES
  created_at TEXT NOT NULL,
  CHECK (email IS NOT NULL OR username IS NOT NULL)
);
