# ADR-022 — S3 Raw/Processed Object Store With a Postgres Lineage Catalog

**Status:** Accepted, phased implementation in progress. Rollout order: (1) Postgres `raw_objects` catalog + one source wired end-to-end as a proof of concept, additive and low-risk; (2) remaining sources' `raw/` persistence; (3) reconciliation job; (4) weekly DR backup (definitions-only, no deletion); (5) DR retention/cleanup (the one delete-capable piece — implemented last, behind extra review, per this ADR's own risk note); (6) service-identity/IAM separation and privilege tests (requires real AWS/Postgres/Qdrant/Neo4j account changes — flagged for explicit go-ahead before execution, not done silently); (7) existing-S3-key migration (flagged for explicit go-ahead — do not run against production without a dry run). This ADR's Implementation Status table is updated as each phase lands.
**Date:** 2026-09-13
**Relationship to ADR-021:** ADR-021 established that S3 is the authoritative store for large/raw artifacts (documents, investigations, threads) and that Postgres holds metadata + an `s3_key` pointer. This ADR is a superset: it adds a formal `raw/` vs `processed/` prefix layout, immutability/versioning, a dedicated lineage/state catalog table (richer than the `s3_key`/`abstract`/`version` columns ADR-021 added directly to `investigations`/`generated_reports`), and a reconciliation job. It does not reverse any ADR-021 decision.

## Context

Today, S3 usage is real but ad hoc. Confirmed via a full-codebase audit (2026-09-13):

**Two incompatible key namespaces already coexist in the same bucket/store.** `storage/document_store.py`'s `DocumentStore` (`LocalDocumentStore`/`S3DocumentStore`) is used two ways: (1) narrative documents (uploaded annual reports/filings/transcripts, note attachments, investor-relations downloads) where `key == documents.raw_file_path == documents.storage_object_key`, a repo-relative path rooted at `DOCUMENTS_DIR` (e.g. `data/documents/<COMPANY>/<timestamp>__<file>`); (2) generated JSON artifacts where `key` is a hand-built string, `f"investigations/{investigation_id}/v1.json"` (`research/investigation.py:325`) or `f"threads/{thread_id}/v1.json"` (`web/app.py:412`).

**Critical existing-behavior finding, independent of this refactor: investigation/thread S3 artifacts are NOT actually versioned today, despite the `v1` in the filename suggesting they are.** The key is always the literal string `v1.json` and `version=1` is hardcoded in every `update_investigation_s3_metadata()`/`update_generated_report_s3_metadata()` call (`storage/repositories.py:1475-1495`, `:2131-2146`) — there is no version increment logic anywhere. `S3DocumentStore.store()` is a plain `put_object` with no versioning or if-none-match guard. **A re-run that regenerates an investigation or report today silently overwrites the prior S3 artifact at the same key** — the opposite of this ADR's "raw objects are immutable, never overwrite" requirement, and worth being aware of as a live risk on its own, separate from whether this refactor happens.

**Hash columns exist but are never queried for dedup.** `documents.file_hash` and `documents.content_hash` are both computed and stored (two separate sha256 fields, one for RAW_DIR-style local files, one for whatever the active DocumentStore backend holds) but nothing anywhere checks them for uniqueness before insert — a genuine "compute the hash, then ignore it for dedup purposes" gap across the whole codebase.

**Raw persistence today is inconsistent across sources.** `sources/nse_fetch.py` already writes raw filing bytes to `RAW_DIR` (local disk, skip-if-exists caching) and `sources/investor_relations.py` already writes raw IR PDFs via `DocumentStore` under `DOCUMENTS_DIR` — both are migration candidates (already staged, wrong location/shape for the new layout). Eleven other sources have **no raw persistence at all** today — `sec_edgar.py`, `fred.py`, `rbi_indicators.py`, `iitm_rainfall.py`, `yfinance_prices.py`, `yfinance_financials.py`, `nse_shareholding.py`, `nse_corporate_actions.py`, `rbi_bank_infrastructure.py`, `rbi_dbie_tables.py`, `screener.py` — every one of these parses the network response in memory and only the derived/normalized values ever reach any store. For these, `raw/` persistence is 100% new code, not a migration.

**Retry/state infrastructure is real but per-source and none of it has "reconciled" or "quarantined."** Three separate, non-overlapping mechanisms exist: `ingestion_queue_items` (states `PENDING|NEEDS_REVIEW|PROCESSING|PROCESSED|FAILED|SKIPPED`, keyed by local `file_path`, financial-file staging only); `batch_job_runs`/`batch_job_items` (`ingestion/batch_log.py`, states `running|completed|failed` / `running|ok|failed`, multi-company batch runs — SQLite-only when this ADR was written, ported to Postgres 2026-09-13, see ADR-021); `documents.processing_status` (`pending|processing|processed|failed|skipped`, one flag per uploaded document). None of these is a multi-step lineage chain, and `reconciled`/`quarantined` are genuinely new vocabulary, not a rename of something that already exists.

**The `documents` table is already a closer match to the requested catalog shape than expected**: it already has `document_id, company_id, source, document_type, fiscal_year, quarter, published_at, retrieved_at, raw_file_path, file_hash, storage_object_key, content_hash, source_url, parser_version, added_by_user, processing_status, processed_at, error_message` — most of the fields this ADR's `raw_objects` proposal asks for, just scoped to narrative documents rather than every raw external fetch. Whether the new catalog is a new table or an extension/generalization of `documents` is an open design question (see "Revisit when" below), not settled by this ADR.

The requesting brief (2026-09-13) asks for a single bucket, a fixed prefix taxonomy, immutable/versioned raw objects, a Postgres control-plane catalog with full lineage, an explicit state machine, hash-based dedup, retry/replay/backfill support in every scheduled job, richer audit counters, and periodic S3↔Postgres reconciliation.

## Decision

### Bucket layout

One bucket (the existing `signals-app-documents-*` bucket), eight root prefixes:

```text
raw/companies/        -- filings, annual/quarterly PDFs, XBRL, disclosures, corporate actions, other company-originated source data (NSE XBRL, SEC EDGAR companyfacts, yfinance financials payloads, uploaded annual-report/transcript PDFs)
raw/market-data/       -- prices, volumes, indices, market snapshots (yfinance OHLCV, any future market-data source)
raw/macro/             -- FRED, RBI, IITM raw pulls/files
raw/regulatory/        -- corporate actions, shareholding pattern filings, other NSE regulatory feeds
processed/investigations/   -- full investigation JSON artifacts (today's `investigations/{id}/v1.json`-style content)
processed/threads/          -- full generated-report/thread JSON artifacts (today's `threads/{id}/v1.json`)
processed/generated-reports/ -- reserved for a future split if generated_reports content needs its own artifact separate from threads
dr/                          -- versioned disaster-recovery definition snapshots (see the Weekly DR backup section below)
```

Every externally fetched byte stream (an NSE filing, a SEC EDGAR JSON response, a FRED CSV row set, an RBI file, a yfinance response) is written to its `raw/` prefix **before** any downstream parsing/normalization touches it — parsing reads from the raw object, never directly from the network response in memory only, so a parser bug or a changed parsing library version can always be replayed against the original bytes.

**The raw/processed line is drawn by origin, not by file type.** A PDF is `raw/` when it originates externally — an annual report, a regulatory filing, a company disclosure — regardless of it being the same file format `processed/` content might superficially resemble. `processed/` is reserved exclusively for content Signal itself generates (an investigation's synthesis, a research report, a hypothesis writeup) — never a re-storage of source material, however lightly transformed. Parsing, normalization, reconciliation, and enrichment steps read from a raw object and write their *output* to Postgres/Qdrant/Neo4j (or, for Signal-generated artifacts, to `processed/`) — they never modify the raw object itself, and never write their intermediate results back into `raw/`.

### Immutability and deduplication

Raw objects are never overwritten or mutated in place. A re-fetch is resolved by content hash before anything is written:
- **Identical hash** (same content already cataloged for this `source`/`entity`/`object_type`/`period`) → no new object is written; the fetch is recorded as a duplicate and the existing catalog row/object is referenced, not re-uploaded. This is a genuine reuse-by-reference, not merely "skip and do nothing" — a caller asking "what raw object backs this period" always gets an answer, whether this was the first fetch or the fifth.
- **Different hash** (upstream content actually changed) → a new immutable object/version is written (new key with a version/timestamp component, or S3 native object versioning on the prefix — exact mechanism TBD during build, not settled by this ADR) and cataloged as a new `raw_objects` row, superseding-by-addition rather than overwrite.

Nothing in a normal job path calls `delete()` on a raw object — the only delete-capable code in this entire design is the DR-snapshot cleanup job (below), and that job is scoped to `dr/` only, never `raw/`.

### Postgres catalog (control plane)

A new table — provisional name `raw_objects` — is the single source of truth for what's in `raw/`, replacing the per-source ad hoc idempotency checks with one shared mechanism:

| Column | Purpose |
|---|---|
| `object_id` | Primary key, referenced by every downstream lineage record |
| `source` | e.g. `nse_xbrl`, `sec_edgar`, `fred`, `rbi`, `yfinance_prices`, `yfinance_financials`, `investor_relations` |
| `entity` | `company_id` (or null for a non-company source like a macro series) |
| `object_type` | e.g. `xbrl_filing`, `companyfacts`, `fred_series_csv`, `ohlcv_batch` |
| `period` | fiscal year/quarter, trade-date range, or series period, as applicable |
| `source_url` | the exact URL/endpoint fetched |
| `s3_key` | the immutable object's key under `raw/...` |
| `content_hash` | sha256 of the raw bytes — the dedup key |
| `fetched_at` | when the bytes were retrieved |
| `parser_version` | which parser/schema version last processed this object (if any) |
| `state` | `fetched` → `stored` → `validated` → `parsed` → `ingested` → `reconciled`, or `failed` / `quarantined` |
| `retry_count` | attempts so far at the current state transition |
| `last_error` | most recent failure detail, if any |

A separate lineage table (or a nullable `source_object_id` column on the existing derived tables — `financial_observations`, `canonical_financials`, `macro_observations`, Qdrant point payloads, Neo4j node properties — exact mechanism TBD during build) lets any derived record answer "which raw object(s) was this computed from."

### Deduplication

Every fetch computes `content_hash` before writing to S3. If an object with the same `(source, entity, object_type, period, content_hash)` already exists, the fetch is a no-op recorded as `skipped` (not `failed`, not silently dropped) in that run's audit counters — this replaces today's file-exists-on-disk check (NSE), `INSERT OR IGNORE` (corporate actions), and TTL-based skip (SEC EDGAR) with one uniform, hash-verified mechanism.

### State machine

`fetched` (bytes retrieved, not yet durably stored) → `stored` (S3 write confirmed + catalog row committed) → `validated` (basic shape/schema check passed) → `parsed` (normalized observations extracted) → `ingested` (written to the relevant Postgres/Qdrant/Neo4j store) → `reconciled` (canonical values recomputed, where applicable — mirrors the existing `financial_observations` → `canonical_financials` reconciliation step). `failed` and `quarantined` are terminal-until-retried states reachable from any step; `quarantined` specifically means "failed enough times, or failed validation badly enough, that it needs a human look before any further automatic retry." Critically, a `failed`/`quarantined` object is never deleted or silently dropped — the whole point of persisting to `raw/` before parsing is that a malformed or unparseable object stays inspectable and retryable rather than vanishing the moment its first parse attempt fails.

### Scheduled-job capabilities

Every job touching `raw/` must support, uniformly:
- **fetch** — the normal path.
- **retry** — re-attempt a `failed` object's next state transition without re-fetching from the network if the raw object already exists (`stored` or later).
- **replay-from-S3** — re-run parse/ingest/reconcile against an already-`stored` raw object, e.g. after a parser bug fix, without any new network call. Replay is addressable by object ID directly, and also supports practical bulk filtering by entity/company, source/type, and period/date range (e.g. "replay every `nse_xbrl` object for RELIANCE in FY2025", not just one object at a time).
- **historical backfill** — the existing `--years`/`--period` style bulk pull, now landing in `raw/` with full catalog rows rather than going straight to a derived table.

### Audit logging

Every run's summary counters gain: discovered, skipped (dedup hit), downloaded, processed, failed, retried, quarantined, replayed — alongside the existing `items_total`/`items_succeeded`/`items_failed` `batch_job_runs` columns.

### Reconciliation job

A new periodic job compares the S3 bucket's actual object listing against `raw_objects` rows: an S3 object with no catalog row (orphan) or a catalog row pointing at a missing S3 object (broken reference) is flagged for operator attention — never auto-deleted or auto-recreated silently.

### Infrastructure-as-code for schema/index definitions (Postgres, Qdrant, Neo4j) — added 2026-09-13

All three schema-bearing systems' definitions move into (or are confirmed to already live in) version-controlled `infrastructure/` files, with one documented disaster-recovery rebuild sequence spanning all three. Current state, confirmed by inspection:

- **Postgres/SQLite — already version-controlled, just not under `infrastructure/`.** `schemas/postgres_schema.sql`, `schemas/sqlite_schema.sql`, and `schemas/price_schema.sql` already are the real, applied DDL (idempotent `CREATE TABLE IF NOT EXISTS`, read and executed directly by `storage/database.py::init_db()`/`init_postgres_db()`). This ADR's requirement is largely already met in substance; the open question is purely cosmetic/organizational — move `schemas/` to `infrastructure/postgres/` (updating `config/settings.py`'s `SCHEMA_PATH`/`PRICE_SCHEMA_PATH` and the Postgres schema path in `storage/database.py`) or declare `schemas/` the de facto infrastructure directory and skip a rename. Either way, no DDL content needs to change.
- **Qdrant — genuinely missing today.** `retrieval/vector_store_qdrant.py::_ensure_collection()` creates the collection lazily, inline, on first upsert, sized to whatever embedding dimension the first batch happens to carry — there is no version-controlled collection/index definition file, and no payload index is ever created (Qdrant can filter unindexed payload fields, just without an index to speed it up). This ADR requires a new `infrastructure/qdrant/` definition (collection name, vector size/distance, and explicit payload indexes on `company_id`/`document_type`/etc.) plus a script that applies it idempotently (create-if-missing, same spirit as the SQL files' `IF NOT EXISTS`) — replacing today's implicit lazy-create.
- **Neo4j — partially covered by an existing rebuild mechanism, no schema file.** No `CREATE CONSTRAINT`/`CREATE INDEX` exists anywhere in `context/graph_neo4j.py` — every write is a plain `MERGE`, meaning no uniqueness constraint backs any node key today. However, `context/graph_neo4j.py::sync_graph()` already **is** a real, working full-rebuild-from-source mechanism (its own docstring: "does a full, idempotent rebuild (MERGE everywhere) from companies/generated_reports/research_thread_evidence plus the static seed edge list... simplest to just resync before every traversal") — this is a genuine positive to build on, not a gap to fill from scratch. This ADR requires adding an `infrastructure/neo4j/` constraints/index file (e.g. uniqueness constraints on `Company.id`, `Concept.key`, `Investigation.thread_id`, `Claim.id`, `Evidence.id`) applied once at rebuild time, and documenting `sync_graph()` itself as the disaster-recovery rebuild step.
- **The disaster-recovery rebuild sequence itself does not exist as a single document today** — Neo4j has an informal one (`sync_graph()`'s docstring), Qdrant and Postgres have none written down. This ADR requires one `infrastructure/DISASTER_RECOVERY.md` (or similar) stating, in order: (1) restore/recreate the Postgres database from the DDL files (+ latest data backup/shard restore, see `scripts/db_shard.py`/`db_unshard.py`), (2) apply Qdrant collection/index definitions and re-embed from Postgres-sourced document chunks, (3) apply Neo4j constraints/indexes and run `sync_graph()`. Order matters — Qdrant and Neo4j are both derived/rebuildable from Postgres (ADR-014's "rebuildable derived stores" principle), so Postgres must be restored first.

### Weekly definitions-only DR backup job + retention/cleanup — added 2026-09-13

A new scheduled job (`schedule` panel's Maintenance category, alongside `db_shard`) that backs up **schema/definitions only, never data** — the DR sequence above already establishes that Postgres table data, Qdrant vectors/chunks, and Neo4j projections are all rebuildable from immutable `raw/` S3 content, so backing up their contents too would be redundant storage of something already durable and recoverable. This is a different job from the existing `db_shard` (which backs up the *full* SQLite database file, split into <=50MB parts, for a completely different purpose — fitting under GitHub's file-size limit for git-tracked storage) — `db_shard`'s `hashlib.sha256`-per-part-plus-`checksum.sha256`-manifest pattern is reusable prior art for this job's own hash/version tracking, but the two jobs back up different things for different reasons and neither replaces the other.

**What gets captured, per store:**
- **Postgres**: DDL/schema (`schemas/postgres_schema.sql` et al., or wherever the infra-as-code section above lands them), migrations, indexes, constraints, enums, and required reference/config definitions (e.g. seed data like `sources`, `sectors`, `index_definitions` rows this app already seeds at `init_db()` time) — never `canonical_financials`/`documents`/`investigations` row data itself.
- **Qdrant**: the collection definition, vector dimension and embedding-model assumption, distance metric, payload schema, and payload/vector index definitions (the same `infrastructure/qdrant/` file the infra-as-code section above introduces) — never the vectors/chunks themselves.
- **Neo4j**: constraints, indexes, node/relationship schema definitions, and the initialization Cypher (the `infrastructure/neo4j/` file above, plus `sync_graph()`'s own MERGE statements as the "how to repopulate" reference) — never the graph's actual node/relationship data.

**Where it lands:** a versioned DR path under the same S3 bucket this whole ADR already covers (a natural fourth root prefix alongside `raw/`/`processed/`, e.g. `dr/<store>/<timestamp>/`) — distinct from `raw/` and `processed/` so retention/cleanup logic (below) can never accidentally touch source-of-truth raw data. Every snapshot's timestamp, hash/version, S3 key, status, and any error is recorded to Audit Log → Job Runs the same way every other scheduled job here already reports (`ingestion/batch_log.py`'s `BatchRun`/`batch_job_items` — this job is audited exactly like `db_shard`, `price_history_backfill_*`, etc., not a special case).

**Recovery sequence** (extends the Postgres → Qdrant → Neo4j order the infra-as-code section already established): recreate the stores (empty Postgres DB, Qdrant instance, Neo4j instance) → restore schema/config from the latest DR snapshot → reingest from immutable `raw/` S3 data → rebuild Postgres structured/canonical data → rebuild Qdrant vectors/indexes (re-embed from Postgres-sourced chunks) → rebuild Neo4j projections (`sync_graph()`) → validate (row counts, collection point counts, a representative Cypher query — same "LLM-based validation is not sufficient" principle ADR-020 already established for the SQLite→Postgres migration).

**Backup failures are visible and retryable**: a failed snapshot is a normal `batch_job_items` `'failed'` row with its error message, shows up in Audit Log like any other job failure, and a "Run now" re-triggers it — no separate failure-handling mechanism needed, this reuses the exact same audited-job infrastructure every other scheduled job already has.

**Retention/cleanup**, per store independently:
- Keep the latest 8 weekly snapshots.
- Keep 1 month-end snapshot for 12 months.
- Keep 1 year-end snapshot indefinitely, unless an admin explicitly purges it.
- A failed/incomplete snapshot is kept 30 days (for troubleshooting) then cleaned up.
- Cleanup only runs after the *current* backup validates successfully — a cleanup pass never fires off the back of a failed or unvalidated run, so a bad backup can never trigger deletion of the last good one.
- The newest successful snapshot for any store is never deleted, regardless of what the retention math would otherwise say — a hard floor under the retention rules above, not just a consequence of them.
- Cleanup only ever touches the `dr/` prefix (or whatever the DR root ends up named) — never `raw/`, never `processed/`. This is enforced by scoping the cleanup job's S3 `list_objects`/`delete_object` calls to that one prefix, the same "normal jobs never delete raw objects" boundary the rest of this ADR already establishes for `raw/`.
- Every deleted key, its retention reason (e.g. "9th-oldest weekly, beyond the 8-snapshot floor" / "failed snapshot past 30-day grace"), timestamp, and cleanup status is logged — same `BatchRun`/`batch_job_items` audit trail, one item per deleted key (or one item per store-cleanup-pass with a detail string listing keys, depending on how granular Audit Log should show this — an implementation choice, not settled here).
- Cleanup is idempotent: re-running it against a state where nothing is due for deletion is a no-op, not an error — same "safe to re-run" principle every batch job in this codebase already follows (`ingestion/batch_log.py`'s docstring, `scripts/backfill_sector_industry.py`'s per-company commit granularity, etc.).

### Ownership and access control — added 2026-09-13

**Current state, confirmed: there is no service-identity separation at all today.** Production uses exactly one IAM user (`signals-app-s3`, per `docs/USER_GUIDE.md`'s deployment instructions) whose single `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` pair is injected into the one container that runs everything — ingestion jobs, the web application, scheduled maintenance, and any admin action all share the identical S3 credential with (implicitly) full read/write/delete on the whole bucket. The same pattern holds for Postgres (one `NEON` connection string, one Postgres role, used by every code path from a company's "Run now" click to a scheduled financials fetch to a raw SQL admin query) and for Qdrant/Neo4j (one API key, one password, each). This ADR's ownership model is entirely new scaffolding, not a tightening of something partially separated already.

**Per-prefix ownership:**
- `raw/` — owned by ingestion/data-pipeline services. Runtime application/research code paths get **read-only** access; no normal application role can overwrite or delete a raw object, matching the immutability rule above at the credential level, not just the code level.
- `processed/` — owned by Signal's research/application services (the code paths that generate investigations/reports). Investigation/thread content is scoped to its owning user/tenant unless a future product policy explicitly makes it shareable — this ADR doesn't add sharing, it just doesn't want the storage layer to accidentally make private research globally readable.
- `dr/` — owned by the maintenance/DR service. Normal application and ingestion roles get no write/delete access here at all.

**Separate service identities** (distinct from today's single shared credential) for: ingestion, application runtime, scheduled maintenance/DR, and administration. Permissions restricted by prefix:
- ingestion → read/write `raw/`, no delete under normal operation.
- application/research → read `raw/` (needed to serve/replay evidence), read/write `processed/` within its authorization scope.
- maintenance/DR → write/manage `dr/`, read-only access to the schema/config sources it snapshots (it needs to read Postgres DDL/Qdrant collection config/Neo4j schema to back them up, but never needs to modify `raw/`/`processed/`).
- admin → the only identity with purge/recovery/access-control permissions — exceptional, not a role any automated job runs as.

**Database/service privilege separation**, same principle applied beyond S3:
- Runtime Postgres roles must not carry unrestricted DDL privileges — schema changes (migrations, new tables/columns) require a separate, higher-privileged role/deployment step, never something a running web request or scheduled job can do incidentally.
- Qdrant/Neo4j: normal runtime access (search, upsert points/nodes within the existing schema) stays separate from privileged administration (creating/altering collections, constraints, indexes) — the `infrastructure/` definitions above are applied by the privileged identity, not the one the app runs as day to day.

**Encryption and secrets**: S3 and database storage/connections use whatever platform-supported encryption is available (S3 server-side encryption, TLS to Postgres/Qdrant/Neo4j — Neon and Qdrant Cloud already require TLS by default in this app's current setup). Secrets never appear in source code, S3 object metadata, logs, or audit records — this is already this codebase's existing convention (`.env` is gitignored, `config/settings.py` reads from environment) and this ADR doesn't relax it, only extends it to the new service identities.

**Privileged-operation auditing**: schema/config changes, backup/restore, retention cleanup, access-control changes, and any raw-object deletion *attempt* (not just successful deletions — an attempt by a role that shouldn't have delete access is itself an auditable, suspicious event) are recorded the same way every other audited action in this app already is.

### What does NOT change

- `financial_observations` was ported to Postgres for real on 2026-09-13 (see ADR-021's "RESOLVED" section) — that happened independently of this ADR, which is about the **raw artifact layer**, not a re-litigation of where reconciled/canonical values live.
- `LocalDocumentStore`'s local-disk behavior (`DOCUMENT_STORE_BACKEND=local`, the default for local dev) is unaffected in spirit — the same prefix/key conventions apply there too, just resolved to a local path instead of an S3 key.

## Migration and risk (flagged, not yet executed)

- **Existing `processed/`-shaped content already in S3 under the OLD flat keys** (`threads/{id}/v1.json`, `investigations/{id}/v1.json`, and existing narrative documents under `documents.raw_file_path`-derived keys) needs either (a) a rename/copy migration to the new prefixed layout, or (b) a documented "old key format still resolves for pre-migration objects, new key format for everything after cutover" compatibility shim. Given ADR-021's own backfill script (`scripts/backfill_case_s3_artifacts.py`) already had to handle "pre-migration rows with no `s3_key`," a second migration wave here is a real, non-trivial operation against the same production data — **do not run any bulk rename/copy against the production bucket without a dry run and an explicit go-ahead**.
- **Fix the overwrite-in-place bug before or alongside adding immutability.** Because `investigations`/`generated_reports` artifacts today write to the exact same `v1.json` key every time (confirmed: `version=1` is hardcoded, never incremented), every prior regeneration of an investigation or report has already silently overwritten its predecessor in S3 — there is no way to recover an S3-side history that was never kept. This isn't something this ADR's rollout creates; it's a pre-existing, currently-live behavior this investigation surfaced. Worth an explicit decision on whether to fix it as a standalone, smaller change first (increment `version` and use it in the key) before the larger prefix/catalog rework, since the two are separable.
- **`raw/` is entirely new for most sources** (NSE XBRL filings currently land on local/container disk via `RAW_DIR`, not S3; SEC EDGAR/FRED/yfinance responses are parsed in-memory with nothing persisted raw today) — this is additive for those, not a migration, but it does mean every source's ingestion code path needs a new "write to S3 first" step added, which is real implementation surface, not a config flip.
- **A new Postgres table plus lineage columns on existing derived tables** is a schema migration on every backend (SQLite dev + Postgres production) — needs the same `CREATE TABLE IF NOT EXISTS` idempotent-migration discipline the rest of this codebase already follows, and should go through `tests/test_backend_compatibility.py`-style parity verification before being trusted in production.
- The `financial_observations`/Postgres gap this bullet used to warn about (ADR-021) is resolved as of 2026-09-13 — the table is real on Postgres now, so a lineage column pointing at its rows is no longer blocked on that.
- **The DR-snapshot cleanup job is the one piece of this whole ADR that deletes anything on a schedule** — every other job here (`raw/` fetch/backfill jobs, the reconciliation job) is explicitly delete-never. Scope its S3 permissions/IAM policy as narrowly as the code boundary (`dr/` prefix only) — a bug that widens its effective scope even slightly would be the one part of this design capable of silently destroying data, which is exactly the failure mode the "keep newest successful snapshot always," "cleanup only after validation," and "log every deleted key" rules exist to prevent. Treat any change to this job's deletion logic as needing extra review, proportionate to it being the sole exception to an otherwise delete-never system.

## Implementation Status (as of 2026-09-13)

| Item | Status |
|---|---|
| Bucket prefix layout (`raw/*`, `processed/*`) | Key-builder implemented (`storage/raw_object_store.py::build_raw_key()`, deterministic `raw/<prefix>/<source>/<entity>/<object_type>/<period>/<hash>.<ext>` shape) and wired for one source (`yfinance_prices`). Old `investigations/`/`threads/` key migration still not started |
| Raw-object immutability/versioning | **Done and tested** for the wired source: `storage/raw_object_store.py::store_raw_object()` never overwrites — dedup-by-hash reuses an existing object, changed content writes a new one at a new (hash-derived) key. The pre-existing `investigations`/`generated_reports` overwrite-in-place bug (Context) is untouched — separate fix, not yet done |
| `raw_objects` Postgres catalog table | **Done** — added to both `schemas/sqlite_schema.sql` and `schemas/postgres_schema.sql` as a genuinely new table (not a `documents` generalization — kept separate since most sources have no narrative-document concept at all), wholesale-swappable via `storage/backend_bootstrap.py` same as `company_repository`/`price_repository` |
| Lineage table on derived stores | **Done** — `raw_object_lineage` table + `storage/raw_object_repository.py::insert_lineage()`/`get_lineage_for_object()`/`get_lineage_for_record()`, tested including the multi-raw-object-per-derived-record case. Wired for one source (`daily_prices` rows now trace back to their `yfinance_prices` raw object) |
| Hash-based dedup | **Done and tested** for the wired source — `store_raw_object()` checks `find_duplicate()` before ever writing bytes; a re-fetch of unchanged content creates zero new S3 writes and zero new catalog rows. Still per-source ad hoc idempotency everywhere else (unchanged): NSE file-exists-on-disk cache, corporate-actions `INSERT OR IGNORE`, SEC EDGAR 24h TTL, shareholding `detail_fetched_at`, FRED existing-periods filter |
| State machine (`fetched`→...→`reconciled`, `failed`/`quarantined`) | Schema + repository done (`storage/raw_object_repository.py::update_raw_object_state()`, all 8 states, retry_count/last_error tracking, tested). Wired through `fetched→stored→ingested` for the one proof-of-concept source (OHLCV data has no separate `validated`/`parsed`/`reconciled` step of its own); `failed`/`quarantined` transitions not yet exercised by any real job |
| Per-job fetch/retry/replay-from-S3/backfill support | Partial — backfill exists per-source (e.g. `scripts/backfill_price_history.py`); retry exists at the batch-run level (`web/app.py::_resume_interrupted_batch_jobs()`); replay-from-S3 (by object ID or filtered by entity/source/period, `list_raw_objects()` already supports the filtering) and a uniform retry primitive built on the new catalog do not exist yet |
| Audit counters (discovered/skipped/downloaded/processed/failed/retried/quarantined/replayed) | Not started (`batch_job_runs`/`batch_job_items` today track `items_total`/`succeeded`/`failed` only) |
| S3↔Postgres catalog reconciliation job | **Done** — `scripts/reconcile_raw_objects.py::reconcile_raw_objects()` lists every `raw/` key the active `DocumentStore` backend actually has (new `DocumentStore.list_keys(prefix)` method, added to the Protocol + both `LocalDocumentStore`/`S3DocumentStore` implementations) and cross-checks against every `raw_objects` catalog row (new `list_all_raw_objects()`, unfiltered/unlimited, distinct from the replay-filtered `list_raw_objects()`), flagging orphaned S3 keys and broken catalog references. Report-only — never deletes or recreates anything, verified by a dedicated test. Registered as a new Weekly Maintenance-category scheduled job (`raw_object_reconciliation`), BatchRun-audited with one `batch_job_items` row per finding. 5 new tests (clean/orphan/broken/never-mutates/audited-per-finding) |
| Migration of existing `processed/`-shaped S3 objects to new prefixes | Not started — see Migration and risk above |
| Raw persistence for sources with none today | **6 of 13 done**, all four `raw/` prefixes covered, proving the pattern generalizes: <br>• `yfinance_prices` → `raw/market-data/` (`scripts/fetch_daily_prices.py::run_price_history_update()`) <br>• `fred` → `raw/macro/` (`ingestion/pipeline.py::ingest_fred_series()`; `sources/fred.py` split into fetch/parse halves, public function preserved as a thin composer) <br>• `sec_edgar` → `raw/companies/` (`ingestion/pipeline.py::ingest_sec_edgar_company()`; `SECEdgarAdapter.fetch()` gained an optional `facts=` param — default behavior unchanged. Written before the `financial_observations`/Postgres bug was fixed (ADR-021, 2026-09-13): if that insert had failed, the raw object would have stayed at `state='stored'`, not `'ingested'` — preserved and replayable, never lost, regardless) <br>• `nse_corporate_actions` → `raw/regulatory/` (`scripts/batch_fetch_nse.py::_run_corporate_actions()`) <br>• `nse_shareholding` → `raw/regulatory/` (`scripts/batch_fetch_nse.py::_run_shareholding()` — BOTH the master listing (1 raw object/company) and each quarter's XBRL detail (1 raw object/quarter), composing with the pre-existing `detail_fetched_at` idempotency, not replacing it) <br>• `yfinance_financials` → `raw/companies/` (`ingestion/pipeline.py::ingest_yfinance_company()` — a deliberate exception to the "split one fetch into raw+parse halves" pattern: reconstructing yfinance's own DataFrames from a serialized round-trip risked subtly different dtypes from a fresh live fetch, so this one issues a second yfinance call specifically for the raw artifact rather than reusing one fetch, disclosed as an accepted tradeoff since this path isn't behind a recurring scheduled job) <br>Each verified with 3 new tests (raw-before-ingest, dedup-on-identical-rerun, new-object-on-changed-content) — 18 new tests total across 6 sources. **A real test-isolation bug was found and fixed along the way**: a pre-existing FRED test (`tests/test_pipeline.py::test_ingest_fred_series_end_to_end`) and a pre-existing yfinance test (`tests/test_web.py`'s annual-only-company docs-feed test) both called functions that now write raw objects via `LocalDocumentStore`, but neither isolated `config.settings.BASE_DIR` — running them wrote real files into the actual repo (`raw/macro/fred/fedfunds/...`, caught and cleaned up mid-session). Both fixed with the same `monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)` pattern `tests/test_web.py`'s own `_build_app()` already established for exactly this class of bug. Full suite now 807 passed (was 764 before any ADR-022 work), same 2 pre-existing unrelated `test_web.py` failures. Remaining 7 with no raw persistence at all: `rbi_indicators`, `iitm_rainfall`, `rbi_bank_infrastructure`, `rbi_dbie_tables`, `screener`, plus 2 migration candidates (already staged elsewhere, not yet moved to the new layout): `nse_fetch` (`RAW_DIR` disk), `investor_relations` (`DocumentStore`/`DOCUMENTS_DIR`) |
| Postgres/SQLite DDL under version control | **Already done in substance** (`schemas/*.sql`) — only the `infrastructure/` directory convention/location is open |
| Qdrant collection/index definitions under version control | Not started — collection is created lazily/inline (`retrieval/vector_store_qdrant.py::_ensure_collection()`), no payload indexes exist at all |
| Neo4j constraints/indexes under version control | Not started — no `CREATE CONSTRAINT`/`CREATE INDEX` exists anywhere, MERGE-only. `sync_graph()`'s full rebuild-from-source mechanism already exists and is reusable as the DR rebuild step |
| One documented disaster-recovery rebuild sequence (Postgres → Qdrant → Neo4j) | Not started — Neo4j has an informal one in a docstring, Postgres and Qdrant have none written down |
| Weekly definitions-only DR backup job | Not started — closest prior art is `db_shard` (Maintenance category, Daily), which backs up full SQLite data for a different purpose (git file-size limit), not schema-only, not weekly, no retention policy |
| DR snapshot retention/cleanup (8 weekly / 12 monthly / indefinite yearly / 30-day failed-snapshot grace) | Not started |

## Consequences

### Positive
- One uniform dedup/idempotency mechanism replaces five-plus ad hoc per-source checks.
- Full replay-from-raw-bytes becomes possible for every source, not just the ones that happen to cache a file today.
- A real answer to "what Postgres/Qdrant/Neo4j record came from what raw fetch" where today there is none.

### Negative
- Meaningful new schema and code surface across every ingestion source — this is a multi-week build, not a config change.
- Every source's ingestion path needs touching, with real risk of behavior drift if not done source-by-source with parity checks.
- The existing S3 objects under the old flat key scheme need an explicit, carefully-sequenced migration decision before this can be "the" S3 layout rather than "a new S3 layout alongside the old one."

## Acceptance criteria

The task is complete only when these are demonstrably true (tested/verified), not merely when the corresponding code paths exist:

- [ ] Every newly fetched external artifact lands in the correct `raw/` prefix before downstream processing begins.
- [ ] Re-fetching identical content never creates a duplicate raw object; changed content creates a new immutable object/version.
- [ ] Normal application/scheduled-job roles cannot overwrite or delete `raw/` objects (enforced by credentials, not just code discipline).
- [ ] Every raw object has a `raw_objects` catalog row with identity/source/S3 location/hash/period/timestamps/state/retry/error metadata.
- [ ] Every derived Postgres/Qdrant/Neo4j record traces back to its originating raw object ID(s).
- [ ] New / duplicate / changed / retry / replay / historical-backfill cases are each distinguishable in the catalog and in audit logs.
- [ ] Raw objects can be replayed/reprocessed from S3 without contacting the original upstream source, filterable by object ID, entity/company, source/type, and period/date range.
- [ ] Failed/malformed objects are retained and marked `failed`/`quarantined`, never silently discarded.
- [ ] S3↔Postgres reconciliation identifies orphaned S3 objects and catalog rows pointing at missing objects.
- [ ] Scheduled jobs support fetch, retry, backfill, replay, reconciliation, weekly DR backup, and DR cleanup.
- [ ] Audit/Job Logs expose per-run counts (discovered/skipped/downloaded/processed/failed/retried/quarantined/replayed), S3 keys, statuses, retries, failures.
- [ ] Maintenance/backup/reconciliation/cleanup failures are visible and retryable via the same Audit Log/Run-now mechanism every other job uses.
- [ ] `infrastructure/` contains enough to recreate empty Postgres, Qdrant, and Neo4j stores from scratch.
- [ ] Weekly DR job exports current definitions/config to versioned `dr/` paths — no table data, no vectors/chunks.
- [ ] Retention correctly maintains 8 weekly / 12 month-end / indefinite year-end snapshots, plus the 30-day failed-snapshot rule.
- [ ] Cleanup cannot delete `raw/` objects and cannot remove the newest successful DR snapshot for any store.
- [ ] A documented, actually-run recovery test recreates empty stores, restores definitions, replays raw S3, rebuilds Postgres/Qdrant/Neo4j, and validates counts/checksums.
- [ ] Separate ingestion / application-runtime / maintenance-DR / admin identities exist with least privilege, and permission tests confirm the S3 prefix boundaries and Postgres/Qdrant/Neo4j privilege separation.
- [ ] Runtime roles cannot perform unrestricted Postgres DDL or privileged Qdrant/Neo4j administration.
- [ ] Secrets are absent from source code, logs, audit records, and S3 object metadata.
- [ ] Automated tests cover raw immutability, dedup, metadata/lineage, replay, reconciliation, DR backup/retention, cleanup safety, and permission boundaries.
- [ ] Existing behavior outside this persistence/DR scope is unchanged.
- [ ] Architecture/operational/DR docs accurately reflect the implemented system, not the aspirational one.

## Revisit when

Before starting implementation, decide:
1. The exact migration strategy for existing `processed/`-shaped objects (rename in place vs. dual-key compatibility).
2. Whether to resolve or explicitly defer the `financial_observations`/Postgres gap this ADR's lineage design otherwise has to route around.
3. Whether the overwrite-in-place bug (investigations/threads never actually versioning past `v1.json`) gets fixed as its own smaller, earlier change, or folded into this rollout.
4. Whether `raw_objects` is a new table or a generalization of the existing `documents` table.
5. Whether `schemas/` gets physically renamed to `infrastructure/postgres/` (a small settings-path change) or is simply declared the de facto infrastructure directory as-is.
6. The exact DR-snapshot key naming scheme (needed before the retention/cleanup job can identify "9th-oldest weekly" or "this year's year-end snapshot" reliably by key/prefix alone) and which IAM policy scope enforces "cleanup can only ever touch `dr/`."
