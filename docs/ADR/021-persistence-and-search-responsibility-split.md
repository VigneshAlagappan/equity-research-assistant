# ADR-021 — Persistence and Search Responsibility Split (Postgres / S3 / Qdrant / Neo4j)

**Status:** Accepted (partially implemented — see Implementation Status)
**Date:** 2026-09-13
**Supersedes:** ADR-001 (SQLite Source of Truth) and ADR-020 (SQLite-to-Server Migration) for any deployment with `DATABASE_BACKEND=postgres` — both are the correct historical record of the SQLite-only era and stay unmodified; this ADR is the authoritative statement of the current, actually-deployed architecture.

## Context

ADR-001 and ADR-020 describe Signal's original SQLite-everything model and a hypothetical future server-database migration. That migration has since actually happened: `DATABASE_BACKEND=postgres` is live in production (Neon), Postgres full-text search over `document_chunks` is fully backfilled and live, and an S3-backed `DocumentStore` abstraction exists for annual reports/filings/transcripts. What was missing was an explicit statement of **which store owns which responsibility** now that four storage/search technologies (Postgres, S3, Qdrant, Neo4j) coexist — without that, it's easy for a future change to put the wrong kind of data in the wrong place (e.g. an earlier bug this session: full-text-search functions querying a table that only exists in one backend).

## Decision

### Storage responsibilities

**Postgres is the structured system of record.** Users, companies, investigation/thread *metadata* (not full content — see below), ownership/visibility, status, timestamps, S3 object keys, versions, canonical financial facts (`canonical_financials`), macro observations, and scheduler/audit metadata all live here. `financial_observations` (raw feed) is the one deliberate long-term exception — excluded from Postgres due to free-tier storage economics at the time, kept SQLite-only; this is a capacity decision, not a responsibility-boundary one, and should be revisited if/when it matters.

**S3 is the authoritative store for large/raw artifacts.** Annual reports, filings, transcripts, presentations, and full investigation/thread content (see below) live here via `storage/document_store.py`'s `DocumentStore` abstraction. Raw XBRL source files stay on local/container disk for now (lower priority, never re-read at query time) — a known, accepted gap, not an oversight.

**Investigations and threads:** full content lives in S3, keyed by `s3_key` on the `investigations`/`generated_reports` row. Postgres holds only metadata, an `abstract` (short preview), `version`, and derived summary fields needed for list-view performance (e.g. `strongest_verdict`, computed once at persist time instead of via a live JOIN over every investigation's hypotheses). `investigation_id`/`thread_id` remain logical identities, never S3 paths. List pages read Postgres only; opening an investigation/thread resolves `s3_key` from Postgres, then fetches from S3.

**Public investigations:** not yet implemented (no `visibility`/`owner_id` concept exists in the schema today — this is net-new product surface, scoped separately from this persistence migration, not bundled into it).

### Search responsibilities

**FTS5 is no longer the production keyword-search dependency.** `storage/fact_store_pg.py::search_document_chunks()` (Postgres `tsvector`/GIN, `to_tsquery`/`ts_rank`) is what production (`DATABASE_BACKEND=postgres`) actually uses. FTS5 (`document_chunks_fts`, `storage/repositories.py::search_document_chunks()`) remains defined and functional for local/`DATABASE_BACKEND=sqlite` development only — never removed, per "may remain only for local/legacy SQLite compatibility."

**Postgres search** handles exact keyword search: company names, tickers, titles, abstracts, tags, investigation/thread metadata, structured filtering. Simple `ILIKE`/`tsvector` queries, same tooling as `document_chunks`.

**Qdrant** handles semantic search: annual reports/filings/transcripts/document chunks today (`signal_document_chunks` collection, live); semantic search over full investigations is a documented future extension (payload-filter or second collection — not yet built).

**Neo4j stays relationship/graph-only.** Confirmed via audit: every node type (`Company`, `Concept`, `Investigation{thread_id}`, `Claim`, `Evidence`, `TimePeriod`) carries only small identifying properties, never full document or investigation text. No change needed — already matches this responsibility.

**S3 is never searched directly.** On ingest/update: raw/full artifact → S3, metadata → Postgres, searchable semantic chunks → Qdrant. This was already the case for documents; now also true for investigations.

**Hybrid retrieval** (`retrieval/hybrid_search.py`) already implements the target shape — Postgres/FTS5 keyword leg + Qdrant semantic leg → Reciprocal Rank Fusion → ranked evidence — and needs no architectural change, only the keyword leg's backend (already described above).

## Implementation Status (as of 2026-09-13)

| Item | Status |
|---|---|
| Postgres FTS backfill (`document_chunks.search_vector`) | Done — 95,067/95,067 rows, verified in production |
| Investigations → S3 metadata split | Done — `investigations.s3_key`/`abstract`/`version`/`strongest_verdict`/`visibility`/`owner_id`, full content in S3, `web/app.py::investigate_view()` reads S3 with fallback for pre-migration rows |
| Threads (`generated_reports`) → S3 metadata split | Done — full report (markdown + evidence + followups) written to S3 on every save (`web/app.py::_persist_generated_report_s3()`), `s3_key`/`abstract`/`version`/`visibility`/`owner_id` on the row. **Deliberate deviation from a pure split**: `report_markdown` (and `research_thread_evidence`/`research_thread_followups`) stay populated in Postgres too, not just S3 — `context/graph.py`'s sector-peer bridging and `context/graph_neo4j.py`'s `sync_graph()` both read `report_markdown` for every historical report in a loop (one on a live planning path, not a background job), and `report_markdown` is `NOT NULL` in the schema (relaxing it needs a full SQLite table rebuild). S3 is authoritative for reads (`research_thread()` prefers it), Postgres keeps a redundant copy specifically to avoid an N-fetch-per-row cost on those two hot paths. Revisit if/when that tradeoff no longer holds. |
| LLM-generated abstracts | Done — `research/abstracts.py::generate_abstract()` (QUICK tier, ~100 words preferred/300 max, professional tone), wired into both persist paths for every future investigation/report; historical rows backfilled via `scripts/backfill_case_s3_artifacts.py` (10 investigations, 17 reports — 3 pre-existing soft-deleted reports correctly excluded). Skips the LLM call for text under 400 characters (a deterministic short answer doesn't need paraphrasing) and falls back to plain truncation if every provider is unavailable — never blocks a persist. |
| Public investigations (`visibility`/`owner_id` columns) | Columns exist (default `'private'`, nullable `owner_id`), but no publish workflow — still net-new feature, not started |
| Semantic search over full investigations (Qdrant) | Not started |
| Raw XBRL files → S3 | Not started (accepted gap, low priority) |
| SQLite/Postgres compatibility tests (ADR-020's migration-approach step 4) | Done — `tests/test_backend_compatibility.py`, exercises `company_repository`, `investigation_repository`/`repositories.save_investigation`, and `indicator_repository` (incl. the `IS ?` → `IS NOT DISTINCT FROM %s` NULL-safety translation) against both backends with identical call sites. The SQLite half runs unconditionally; the Postgres half requires `NEON_TEST_URL` pointed at a dedicated Neon branch (never the production `NEON` connection string) and is skipped, not failed, when unset. |

## A real pre-existing bug found and fixed along the way

While backfilling historical abstracts, discovered that `llm/observability.py::record()`/`record_reuse()` — called by **every** LLM call site in `research/*.py` (investigation generation/evaluation/synthesis, the assistant, signals reports, key insights) — passed whatever connection the caller had straight into `insert_llm_call_log()`, an audit-log function that's SQLite-only forever (same category as `ingestion/batch_log.py`'s `BatchRun`, fixed earlier). Under `DATABASE_BACKEND=postgres`, this crashed (`AttributeError: 'psycopg2.extensions.connection' object has no attribute 'execute'`) on every single LLM call — meaning the entire "Ask AI" / Deep Dive investigation feature was very likely broken in production before this fix, independent of anything else in this ADR. Fixed with the same pattern as `BatchRun`: use the caller's connection when it's already SQLite, open a dedicated one otherwise.

## Consequences

- Investigations/threads created before this ADR now all have `s3_key` set via the backfill script — there is no remaining pre-migration data on either table.
- The normalized hypothesis/evidence/thread-evidence/followups tables are still written on every new investigation/report (unchanged) — the S3 artifact is built from the same in-memory data at persist time (investigations) or immediately after the existing save calls (threads), never a separate re-read, so the two can never diverge for a newly-created row.
- Every abstract-generation call is a real (small) LLM cost. Acceptable given QUICK tier + the 400-character skip threshold keep it cheap, but worth monitoring via `llm_call_log` (`task_name IN ('report_abstract')`) if volume grows significantly.
