# India Economic Graph + Economic Data Ingestion Foundation — Plan

**Status:** Phase 1 implemented (branch `economic-graph-phase1`, not yet merged). Phases 2+ planned, not started.
**Scope constraint (locked):** no format adapters, no live data fetching, no real ingestion until a specific Deep Dive need justifies one dataset at a time. This document is the architecture/registry foundation only.

This is the consolidated, current plan — it supersedes the incremental revision turns that produced it. Where a decision changed across revisions, only the final, locked version is recorded here.

---

## 1. Current-State Assessment (as of plan authoring)

Signals already had most of the architectural pillars this feature needs:

- **Postgres**: `macro_observations` (raw, append-only, per-source time series) already served RBI/IMD/IITM/MoSPI/IRDA/FRED data.
- **Neo4j**: a rebuildable *projection*, never a source of truth (ADR-014) — `context/graph_neo4j.py`'s `sync_*()` functions do full idempotent `MERGE`-based rebuilds from Postgres + a hand-curated seed file.
- **Qdrant**: one collection (`retrieval/vector_store_qdrant.py`), payload-indexed by `company_id`/`document_id`.
- **S3**: `raw_objects` + `raw_object_lineage` (ADR-022) already implement "immutable raw artifact before normalization," backed by `storage/document_store.py`.
- **Ontology** (`config/knowledge_ontology.py`): `ENTITY_TYPES` (incl. `MacroFactor`, `Industry`), `RELATIONSHIP_TYPES` (incl. `DRIVES`, `MAY_AFFECT`, `EXPOSED_TO`), `CLAIM_TYPES` (incl. `CORRELATION`, `CAUSATION`, reserved unused).
- **Seed causal edges** (`config/knowledge_graph_seed.py`): hand-curated, version-controlled `(source, relationship, target, strength, reason)` tuples.
- **Source-family adapters**: `sources/base.py` + `sources/macro.py`'s `MacroDataAdapter` already prove "one class, many sources" works.
- **Registry-of-pluggable-things** is the established idiom (`ingestion/detector.py`'s `ADAPTER_CLASSES`, `indicators/framework.py`'s `RULE_REGISTRY`, `scheduling/jobs.py`'s `SCHEDULED_JOBS`).
- **`indicators/` package already exists** and means something unrelated — company-level rule-triggered indicators (`IndicatorRule`, `TriggeredIndicator`). The new work must not collide with this name.
- **`research/macro_knowledge_builder.py`** already uses an LLM to classify `MacroFactor → Industry` relationships (`DRIVES`/`MAY_AFFECT`/`EXPOSED_TO`) into `knowledge_relationships` — a real precedent that needed reconciling with "LLM never invents causal relationships" (resolved, see Section 5).

---

## 2. Target Data Model (locked)

```
EconomicIndicator (1) ──< EconomicSeries (1) ──< EconomicObservation
                              │
                              └──< (via SourceDataset) SourceEndpoint
```

**`EconomicIndicator`** — a concept (e.g. "Consumer Price Inflation"). No `series_key`. Holds semantic/reporting metadata:
```
indicator_id, name, category, economic_meaning, higher_is, leading_lagging,
report_section, headline_weight, preferred_chart_window,
material_change_mom, material_change_yoy, material_change_ytd,
status (registered_only | ingesting | live),   -- mandatory, never faked
created_at, updated_at
```

**`EconomicSeries`** — a measurable stream (e.g. "CPI Combined YoY — India"). Owns `series_key`:
```
series_id, indicator_id FK, dataset_id FK, series_key, geography,
unit, frequency, seasonal_adjustment, notes, created_at, updated_at
```

**`EconomicObservation`** — the **single canonical** fact/vintage table (no competing second table):
```
observation_id, series_id FK, period, period_type, release_date, vintage,
revision_status (provisional | revised | final), value, unit,
raw_object_id FK -> raw_objects.object_id, ingested_at
```
`UNIQUE(series_id, period, vintage)`.

Required query semantics:
```python
latest(series_id)                          # highest vintage, most recent period
as_of(series_id, date)                      # highest vintage with release_date <= date
history(series_id, start_date, end_date)    # latest vintage per period in range
vintages(series_id, period)                 # every vintage of one period, release order
```

**Source registry:**
```
SourceOrganization → SourceDataset → SourceEndpoint
```
- `SourceOrganization`: `source_org_id, name, authority_level, description`
- `SourceDataset`: `dataset_id, source_org_id FK, authority_level, priority, access_method, cadence, historical_start, backfill_supported, license_notes` — **no URL fields**.
- `SourceEndpoint` (0..N per dataset — a dataset may be API-only, XLS-only, PDF-only, or expose several formats): `endpoint_id, dataset_id FK, url, access_method, priority, enabled, authentication_type, parser_config, availability_status, last_verified_at`.

**`macro_observations` transition** (audited, not yet acted on): exactly two writer functions (`insert_macro_observations` in `storage/repositories.py` / `storage/repositories_pg.py`), called from exactly two sites in `ingestion/pipeline.py` (`ingest_macro_file()`, `ingest_fred_series()`). Small, well-understood write surface — safe to convert to a compatibility view over `economic_observations` in a later phase; not done in Phase 1.

---

## 3. Storage Responsibilities (locked)

| Store | Owns | Never holds |
|---|---|---|
| **Postgres** | Every registry table above; `economic_observations` (the only fact/vintage table); `causal_evidence` (canonical evidence identity/metadata); transformation lineage fields | Causal reasoning, mechanism structure |
| **Neo4j** | Indicator/series/source lineage structure; `Mechanism`/`EconomicFactor`/`Sector`/`Industry`/`Company`/`KPI`/`Commodity`/`GovernmentPolicy`/`Geography` nodes; `CausalAssertion` edges (classification, polarity, provenance_type, scope, lag) — one edge per assertion; `SUPPORTS` edges to `Evidence` nodes carrying only `evidence_id` (a Postgres FK) | Any observation value, vintage, or revision — zero exceptions, stricter than the existing financials-graph precedent which does store point-in-time values as node properties |
| **Qdrant** | Embedded evidence text for semantic retrieval, payload keyed by `evidence_id` | Canonical evidence identity |
| **S3** | Every raw fetched artifact, immutable, content-hashed — reuses `raw_objects`/`raw_object_lineage` directly | Parsed/normalized values |
| **LLM** | Explanation/narrative only, over already-assembled vetted context | Inventing an assertion, a classification, or a provenance upgrade — enforced structurally, not by convention |

---

## 4. Causal Model (locked)

**One `CausalAssertion` = one directed edge, always.** A chain (e.g. Repo Rate → Funding Cost → NIM) is multiple assertions linked through `Mechanism` nodes — never one assertion spanning multiple hops. This is what prevents shortcut edges like `Monsoon → Hero MotoCorp`.

```
(:EconomicIndicator {Repo Rate})-[:INPUT_TO]->(:CausalAssertion A1)-[:INCREASES]->(:Mechanism {Funding Cost})
(:Mechanism {Funding Cost})-[:INPUT_TO]->(:CausalAssertion A2)-[:DECREASES]->(:KPI {NIM})
```

**`CausalAssertion` properties:**
```
assertion_id, source_entity_ref, target_entity_ref, relationship_type,
evidence_status, polarity, confidence, provenance_type, scope (JSON),
lag_min, lag_max, lag_unit, rationale, effective_from, effective_to
```

- **`evidence_status`** (the vetting strength, formerly called `classification`): `CAUSAL | SUPPORTED_CAUSAL | HYPOTHESIZED | LEADING_INDICATOR | PROXY | CORRELATED | DERIVED`
- **`polarity`**: `POSITIVE | NEGATIVE | NON_MONOTONIC | CONDITIONAL | UNKNOWN`
- **`scope`**: structured JSON (`geography`, `sector`, `industry`, `economic_regime`, `conditions`) — an assertion with no scope populated is implicitly global, but must be an explicit empty object, never an absent field.
- **`provenance_type`** (mandatory, enforced at the write layer): `CURATED_RESEARCH | SOURCE_DOCUMENT | STATISTICAL_TEST | ANALYST_HYPOTHESIS | LLM_HEURISTIC | SYSTEM_DERIVED`. `LLM_HEURISTIC`/`ANALYST_HYPOTHESIS`/`SYSTEM_DERIVED` can never carry `evidence_status=CAUSAL` or `SUPPORTED_CAUSAL` — structurally forbidden, not just filtered at read time.

**Evidence ownership**: canonical evidence identity lives in Postgres (`causal_evidence`: `evidence_id, assertion_id, raw_object_id, document_id, source_url, page, section, published_at, evidence_type, created_at`), never in Qdrant. Qdrant payload carries `evidence_id` as a pure retrieval index.

**Transformation lineage** (lightweight, rides on existing `raw_objects`/`raw_object_lineage` — no new workflow engine): every `EconomicObservation` records `adapter_version`, `parser_version`, `parser_config_hash`, `raw_object_id`, so "how was this exact observation derived from this raw artifact" is always answerable.

**Canonical vs. derived series**: default is *persist source facts, compute analytical views (MoM/YoY/YTD/rolling/trend/materiality) on demand*. A derived series is only persisted when it becomes a first-class reusable research series, is expensive to recompute, or point-in-time reproducibility requires it — not automatically for every computable view.

---

## 5. `knowledge_relationships` vs. `CausalAssertion` (locked — final resolution)

Both are kept **permanently**, as two complementary layers over shared entity identity — not unified, not replaced:

```
Economic Knowledge Graph
│
├── Semantic Knowledge Layer (existing, unchanged)
│   └── knowledge_relationships: OFFERS, OPERATES_IN, COMPETES_WITH, SUPPLIES,
│       DEPENDS_ON, MAY_AFFECT, DRIVES, EXPOSED_TO, ...
│       — discovery, graph navigation, candidate generation. NOT evidence-gated.
│
└── Causal Knowledge Layer (new)
    └── CausalAssertion — evidence-aware, provenance-tagged, one edge per relationship
```

**Runtime rule**: default research/report causal traversal reads `CausalAssertion` only. `knowledge_relationships` may surface candidate entities/areas to investigate, but a semantic edge (e.g. `Airlines -[:EXPOSED_TO]-> Crude Oil`) must never be read as if it were a causal conclusion (`Crude Oil ↑ → Airline Margin ↓`) — that requires an actual vetted `CausalAssertion`.

**Existing LLM-generated `knowledge_relationships` rows** (`research/macro_knowledge_builder.py`) stay exactly as they are — no migration, no schema change to `knowledge_relationships` itself. This is simpler than an earlier considered approach (retrofitting a `provenance_type` column onto `knowledge_relationships`) and touches zero existing production code.

**Shared entity identity**: an `(:Industry {Airlines})` node is the same node for both layers — created once via the existing `MERGE`-on-natural-key pattern, never duplicated per layer.

**Optional future bridge** (design only, not implemented): `relationship_bridge (bridge_id, legacy_relationship_id, causal_assertion_id, mapping_status [LEGACY_ONLY|CANDIDATE|MAPPED|REPLACED|REJECTED], reviewed_at, reviewed_by)` — lineage/audit only, never auto-syncs, never deletes the original semantic relationship.

**Final architectural statement:**
> `knowledge_relationships` is the generic semantic association layer used for discovery and contextual graph traversal. `CausalAssertion` is the canonical evidence-aware causal reasoning layer used by research and reporting. The two share entity identity but have distinct semantics. Existing semantic relationships are not automatically causal and are never automatically promoted into the causal layer.

---

## 6. Ingestion/Source Model (locked scope: registry only, no adapters yet)

Format families recognized by the architecture (`API`, `CSV`, `XLS/XLSX`, `JSON`, `HTML_TABLE`, `HTML_PAGE`, `PDF`, `DASHBOARD_EXPORT`, `STRUCTURED_QUERY`) — but **adapters are built only when a real pilot dataset needs that format**, never speculatively ahead of real data. Config-over-code is the default (mirrors `sources/macro.py`'s one-class-many-sources precedent); a genuinely quirky official source gets a specialized parser rather than a forced config-only fit.

**Explicit standing constraint (per user instruction):** no adapter code and no live data fetching happen until a specific Deep Dive need justifies ingesting one real dataset. This plan's implemented phases stop short of that line deliberately.

---

## 7. Scheduler Approach (locked, simplified)

Minimal `ScheduledJob` extension — 3 fields, not 5 (two were derivable):
```python
enabled: bool = True                 # existing jobs default True, unaffected
schedule_expression: str | None = None
manual_only: bool = True
```
Every economic-data job registers `enabled=False, manual_only=True, schedule_expression=None`. Global switch: `ECONOMIC_DATA_SCHEDULER_ENABLED=false` (checked at the async/cron dispatch point). Manual invocation (CLI, Settings "Run now") bypasses the switch entirely, unaffected — same as every existing job today.

---

## 8. Domain Framing

"Economic Data" is the broader domain; "Macro" is one sub-domain among several (Monetary, Fiscal, Trade, Commodity, Energy, Agriculture, Infrastructure, Credit, Payments, Mobility, Labor). No renaming of existing production components (`macro_observations`, `research/macro_evidence.py`, etc.) in this phase.

---

## 9. Phased Implementation Plan

| Phase | Scope | Status |
|---|---|---|
| **0** | Architecture decisions / model lock | ✅ Done (this document) |
| **1** | Pilot registry: `EconomicIndicator`/`EconomicSeries`/`SourceOrganization`/`SourceDataset`/`SourceEndpoint`/`EconomicObservation` schema (dual SQLite/Postgres); all 94 indicators registered (12 with real, verified sourcing — Repo Rate, CPI, GDP, IIP, Bank Credit Growth, 10Y G-Sec Yield, Brent Crude, Rainfall, GST Collections, Vehicle Registrations, Power Generation, UPI Transactions; 82 honest stubs, no fabricated URLs); `latest()`/`as_of()`/`history()`/`vintages()` implemented and tested; `macro_observations` write-path audited (untouched) | ✅ **Implemented** — branch `economic-graph-phase1`, commit `a49fd7a`, not yet merged. 47/47 new tests pass; full suite 1025 passed / 2 pre-existing unrelated failures / 3 skipped, independently reverified. |
| **2** | `CausalAssertion`/`Mechanism`/`causal_evidence` schema; Neo4j sync scaffolding (`sync_economic_graph()`, structural only); ontology extension; scheduler safety fields; the 4 example causal chains seeded as hand-curated repo-file data (no adapters, no live fetch) | Planned, scoped, not started |
| **3** | `macro_observations` → compatibility view; manual ingestion of first real dataset(s), strictly demand-driven by an actual Deep Dive need | Deferred — no adapter/ingestion work until explicitly requested |
| **4+** | Derived analytics, BCG-style report prototype, scale-out toward full data coverage, selective scheduling enablement | Deferred |

---

## 10. Acceptance Criteria (summary — full detail in prior planning transcript)

- One authoritative numeric fact path (`economic_observations`); `latest()`/`as_of()`/`history()`/`vintages()` verified against a synthetic multi-vintage series.
- One `EconomicIndicator` maps to ≥2 `EconomicSeries` without duplicating series-level fields onto the indicator.
- Disabling a `SourceEndpoint` never requires changing `SourceDataset` identity.
- Every vetted `CausalAssertion` has non-null source/target/relationship_type/evidence_status/polarity/provenance_type/scope.
- `provenance_type=LLM_HEURISTIC` assertions never appear in default vetted-causal traversal, and can never be written with `evidence_status=CAUSAL`/`SUPPORTED_CAUSAL`.
- Every `CAUSAL`/`SUPPORTED_CAUSAL` assertion has ≥1 linked `causal_evidence` row.
- Zero observation/vintage/value properties on any economic-graph Neo4j node (automated Cypher check).
- Neo4j economic graph is fully reconstructable from repo config + Postgres metadata + curated causal seed files.
- No economic job executes automatically while `ECONOMIC_DATA_SCHEDULER_ENABLED=false`; manual execution works regardless.
- A `knowledge_relationships` edge alone never satisfies a causal traversal used by reports; creating/updating one never auto-creates a `CausalAssertion`.
- The same entity (e.g. `Airlines`) is one shared Neo4j node across both relationship layers, never duplicated.

---

## Recommended Architecture Decisions — Status

1. `macro_observations` becomes a compatibility view over `economic_observations`, one-time backfill, no dual-write — **audited, not yet executed** (Phase 3).
2. One `CausalAssertion` = one directed edge, always — **locked**.
3. `provenance_type` mandatory, `LLM_HEURISTIC`/`ANALYST_HYPOTHESIS`/`SYSTEM_DERIVED` structurally forbidden from `CAUSAL`/`SUPPORTED_CAUSAL` — **locked**, enforcement lands in Phase 2.
4. Evidence identity lives in Postgres, never Qdrant — **locked**, lands in Phase 2.
5. Neo4j economic-graph nodes carry zero value/vintage properties, no exception — **locked**, stricter than the existing financials-graph precedent.
6. Pilot set is 10-15 indicators before scale-out — **superseded**: all 94 were registered in Phase 1 (metadata only, 12 real + 82 stub), per the later decision to front-load the registry while keeping ingestion strictly demand-driven.
7. `knowledge_relationships` and `CausalAssertion` are permanently separate layers sharing entity identity — **locked** (Section 5).
8. Whether to build the optional `relationship_bridge` table now or defer until a real promotion candidate exists — **deferred**, not scheduled into any phase above.

---

## Open Items Carried Forward

- `macro_observations` → view conversion: audit complete, execution deferred to Phase 3.
- `Sector` as a first-class Neo4j node vs. today's `Company.sector` property / `SAME_SECTOR_AS` edge — still unresolved, independent of the `knowledge_relationships`/`CausalAssertion` decision.
- No adapter or ingestion work proceeds until a specific Deep Dive need identifies the first real dataset to wire end-to-end.
