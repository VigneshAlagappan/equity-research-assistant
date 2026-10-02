# Signal L5 — Causal Intelligence Engine: Architecture Analysis & Implementation Plan

**Status:** Planning only. Nothing in this document is implemented. No code, schema, prompt, migration or infrastructure change accompanies it. Implementation waits for explicit approval of a phase (Section 36 recommends the smallest one).
**Date:** 2026-10-01
**Scope:** how the existing Level-5 ("Hypothesize") capability evolves into a scalable, evidence-backed, self-measuring causal investigation engine.
**Basis:** a read of the repository on branch `feature-v3`. Files read are cited inline; where I inferred rather than read, it is stated.

Related existing documents this plan builds on and must not contradict: ADR-006 (deterministic computation, probabilistic reasoning), ADR-007 (hypothesis-driven pipeline), ADR-008 (bounded evidence-sufficiency loop), **ADR-009 (graph relationships are research inference, not proof of causation)**, ADR-012 (point-in-time evidence), ADR-014 (rebuildable derived stores), ADR-018 (investigation budget governance), ADR-021/022 (persistence split, S3 object store), ADR-023 (Jev complexity routing), and `docs/economic-graph/PLAN.md` (the already-designed `CausalAssertion` layer).

---

## 1. Executive Summary

Signal already has most of an L5 *pipeline* and almost none of an L5 *knowledge base or measurement system*.

**What exists.** Jev classifies every question into levels 1–5 (`llm/complexity.py`, `research/routing_policy.py`). Level 5 runs `research/investigation.py`: generate competing hypotheses → plan and gather evidence through capability seams → evaluate each hypothesis independently → rank and synthesize, with an evidence-sufficiency loop (`MAX_EVIDENCE_ITERATIONS = 2`) and a wall-clock budget (`INVESTIGATION_TIMEOUT_SECONDS = 180`). Hypotheses, their verdicts and their evidence are persisted per investigation. Neo4j is a rebuildable projection of Postgres. A separate, carefully reasoned design for evidence-aware causal edges (`CausalAssertion`) and an economic-indicator registry exists in `docs/economic-graph/PLAN.md`, and the registry tables are already in the schema.

**What does not exist.**
1. Durable, structured causal knowledge. The only causal edges in Neo4j are 12 hand-written `AFFECTS` tuples with a single `strength` number (`config/knowledge_graph_seed.py`) — exactly the "A → B = 0.82" form this brief rejects. `CausalAssertion`, `Mechanism` and `causal_evidence` are designed but not built.
2. A graph-shaped investigation. A hypothesis today carries `chain_steps` (a JSON list of labels) and prose; there is no graph of nodes and edges per investigation, so edges cannot be counted, evidenced, scored or validated.
3. Contradiction search, confounder analysis and alternative-comparison as explicit steps. The evaluator separates supporting / contradicting / missing evidence, which is a good base, but nothing hunts for contradictions or confounders deliberately.
4. Any loop that compares what was expected with what happened. Guidance, forecasts and hypotheses are never revisited when later data arrives.
5. Causal KPIs. The only eval (`scripts/run_signals_eval.py`, 11 cases) measures whether Jev picked the right *level*, not whether L5's explanations are any good.

**Recommendation in one paragraph.** Do not build a parallel system. Extend the economic-graph plan's `CausalAssertion` as the single causal-knowledge model; make the per-investigation graph a first-class persisted object produced by the existing investigation loop; add a thin Causal Knowledge Service over Neo4j/Postgres so the model never writes durable knowledge directly; add a generic expectation→observation→validation primitive in Postgres; and measure everything with deterministic per-investigation metrics first, then a Golden Investigation benchmark. The smallest sensible MVP (Section 36) is: the investigation graph persisted with evidence stances, the per-investigation deterministic metrics, and 5 golden cases — **no** knowledge promotion, no automation, no new service yet.

The governing rule throughout: **the model proposes; the system validates and decides.** This is already ADR-006 and ADR-009; this plan makes it enforceable for causal claims.

---

## 2. Current Signal Architecture Findings

Classification key used throughout: **REUSE / EXTEND / NEW / REPLACE / NOT NEEDED**.

| Area | What the repository has (evidence) | Classification |
|---|---|---|
| **Complexity levels L1–L5** | Jev: `llm/complexity.py::classify_complexity`, ADR-023. L1 lookup, L2 calc (both no LLM), L3 interpret, L4 compare, L5 hypothesize. Falls back to L3, never L1. `research/routing_policy.py::route_question`. | REUSE |
| **Model routing** | `llm/router.py` (`route`, `route_explicit_chain`), config-driven chains `JEV_CLASSIFIER_MODEL_CHAIN`, `LEVEL_MODEL_CHAIN` (ADR-010, ADR-023). | REUSE |
| **Investigation workflow** | `research/investigation.py` (orchestrator), `hypothesis_generator.py` (2E), `investigation_planner.py` (2F, deterministic routing), `hypothesis_evaluator.py` (2G, one call per hypothesis), `research_synthesis.py` (2H). | EXTEND |
| **Investigation state** | Tables `investigations`, `investigation_companies`, `investigation_hypotheses` (verdict, `confidence_score`, `chain_steps`, `synthesis_rank`), `investigation_hypothesis_evidence` (stance supporting/contradicting/missing, kind incl. CORRELATION/CAUSATION). Case lifecycle in `research/case_runner.py` and `research_cases`. | EXTEND |
| **Agent/runtime loops** | Bounded loop per hypothesis (`MAX_EVIDENCE_ITERATIONS=2`), shared deadline (180 s), ADR-018 governance. No agent graph loop. | EXTEND |
| **Deterministic tools** | `financials/calculations.py`, `retrieval/structured_search.py`, `research/macro_evidence.py`, `indicators/` rule engine, `research/aggregate_query.py`. | REUSE |
| **Company models** | `companies`, `company_index_membership`, `sectors`, `industries`, `company_identifier_history`; `fiscal_year_end_month`. | REUSE |
| **Sector/industry classification** | `sectors`, `industries` tables; `companies.basic_industry` / `macro_economic_sector`; `web/company_kind.py::is_financial_company`. | REUSE |
| **Financial facts** | `financial_observations` → `canonical_financials` (trust-ranked reconciliation), `metrics_dictionary`, `derived_financial_feeds`. | REUSE (source of numeric truth) |
| **Macro data** | `macro_observations` (legacy), plus `economic_indicator_registry`, `economic_series`, `economic_observations` (vintage-aware) and `source_*` registry tables — present in the schema. 94 indicators registered, 12 with real sourcing, per `docs/economic-graph/PLAN.md`. | REUSE / EXTEND |
| **Neo4j** | `context/graph_neo4j.py`: rebuildable `MERGE`-based projection. Nodes: `Company`, `Concept`, `Investigation`, knowledge entities/claims/evidence, financial `Metric`/observation nodes. Edges: `SAME_SECTOR_AS`, `AFFECTS` (12 seed edges), `DISCUSSED_IN`, `ABOUT_CONCEPT`, knowledge relationships. Traversal: `find_multi_hop_claims(max_hops=2)`. Selected by `GRAPH_BACKEND`; SQLite fallback `context/graph.py`. | EXTEND |
| **Neon/Postgres** | The system of record for everything structured (ADR-021). Dual SQLite/Postgres schemas. | REUSE |
| **Qdrant** | `retrieval/vector_store_qdrant.py`, chunk index payload-keyed by `company_id`/`document_id`; hybrid RRF (ADR-004). | REUSE |
| **S3** | `storage/document_store.py`; `raw_objects` + `raw_object_lineage` (ADR-022); investigation JSON at `investigations/<id>/v1.json`. **ADR-022 records that the `v1` key is never incremented, so a re-run overwrites** — verify before relying on it for reproducibility. | EXTEND |
| **Evidence/provenance** | `knowledge_claims` (claim_type FACT…CAUSATION), `knowledge_evidence` (quote + document), per-observation provenance in financials, ADR-005 source hierarchy. | REUSE / EXTEND |
| **Point-in-time** | `research/temporal.py` + ADR-012: `as_of` enforced inside capabilities. Essential for validation without look-ahead bias. | REUSE |
| **Source citations** | `[FACT]/[CALCULATION]/[MANAGEMENT_STATEMENT]/[INFERENCE]` tagging in answers; evidence rows carry `citation`. | REUSE |
| **Background workers / scheduler** | `scheduling/jobs.py` `SCHEDULED_JOBS`, EventBridge cron, `ingestion/batch_log.py` (`batch_job_runs/items`), ADR-015/016/017. `dataset_events` table (ADR-013). | REUSE |
| **Audit / event logging** | `llm_call_log` (has `investigation_id`), `execution_metrics` (+ daily rollup), `signals_routing_log`, `retrieval_diagnostics`, `dataset_events`. | EXTEND |
| **Analytics** | Settings → Admin → *Eval Analytics* panel (`web/static/js/eval_analytics_charts.js`). | EXTEND |
| **Evaluation framework** | `research/signals_eval_cases.py` (11 cases, classification only), `scripts/run_signals_eval.py`, job `signals_eval` (weekly). `docs/SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md` (manual). | EXTEND |
| **Configuration** | `config/settings.py`; ontologies in `config/knowledge_ontology.py` (11 entity types, 13 relationship types, 7 claim types). | EXTEND |
| **Versioning** | `PARSER_VERSION` per adapter; derived-feed calc-code hash (`web/derived_feed_store.py`); `investigations.version` (hard-coded 1). No engine/ontology/prompt/graph version anywhere. | NEW (small) |
| **Entitlements / capabilities** | `users`, `owner_id`, `visibility` on cases/investigations. `research/capabilities.py` is *planner* capability seams (Protocols), not entitlements. No tenant model found. | REUSE (visibility); entitlements NOT NEEDED now |

**Three findings that shape the whole plan**

1. **ADR-009 already says the right thing.** "Connectivity is evidence for investigation, not proof of causation." The knowledge graph is a *discovery* layer. This plan does not relax that; it adds the layer that *can* carry causal claims, gated by evidence.
2. **Causal modelling is half-designed.** `docs/economic-graph/PLAN.md` locks a `CausalAssertion` model (one edge = one assertion, `evidence_status`, `polarity`, `provenance_type`, `scope`, lags, structural ban on LLM-provenance edges being marked CAUSAL) and a storage split. Phase 2 of that plan (the assertion/mechanism/evidence schema) is unbuilt. L5 should be its first real consumer, not a competitor.
3. **One standing conflict to resolve.** `context/graph_neo4j.py::sync_financials` projects observation values into Neo4j as nodes; the economic-graph plan forbids value/vintage properties in the economic graph and this brief says not to duplicate numeric series into Neo4j. Resolution (Section 11): keep the financials projection as is for its existing consumers, but the causal graph references observations by key only.

---

## 3. Existing Components That Can Be Reused (summary)

- **Pipeline skeleton:** the 2E→2H investigation loop, `PlannerCapabilities` seams, `case_runner` lifecycle, ADR-018 budgets.
- **Evidence tiers:** ADR-005 source hierarchy and ADR-009 evidence classification (Reported Fact … Conclusion).
- **Numeric truth:** `canonical_financials`, `economic_observations` with `as_of()`/`vintages()` — validation reads these, never copies them.
- **Time travel:** `research/temporal.py` for every historical validation.
- **Audit plumbing:** `llm_call_log.investigation_id`, `execution_metrics`, `signals_routing_log`, `batch_job_runs`.
- **Scheduler:** new jobs register in `SCHEDULED_JOBS`; the economic-graph plan's `enabled / manual_only / schedule_expression` safety fields apply unchanged.
- **Eval runner shape:** `signals_eval` job + `batch_job_items` pass/fail convention.
- **Rebuildability (ADR-014):** every Neo4j structure stays reconstructable from Postgres + versioned seed files + S3 snapshots.

---

## 4. Gap Analysis

| # | Required capability (from the brief) | Today | Gap |
|---|---|---|---|
| G1 | Structured causal edge (direction, mechanism, scope, lag, confidence, magnitude, status, version, provenance) | 12 seed `AFFECTS` tuples with one `strength`; `knowledge_relationships` semantic edges (not evidence-gated) | Entire model. Designed in economic-graph plan, unbuilt |
| G2 | Controlled causal vocabulary | `RELATIONSHIP_TYPES` has `DRIVES / MAY_AFFECT / EXPOSED_TO`; no polarity, no effect/lag/scope vocabulary; extraction LLM can add arbitrary text only via claims | Ontology extension + normalization of extracted relationships |
| G3 | L1 primitives, L2 sector mechanisms, L3 company overlay | Sector is a property + `SAME_SECTOR_AS`; `Sector` not a first-class node (open item in economic-graph plan); no primitives | New ontology + sector node |
| G4 | Dynamic investigation graph | Hypothesis `chain_steps` JSON only | Persisted per-investigation graph |
| G5 | Bounded traversal with expansion gates | `max_hops=2` on knowledge claims; evidence-loop cap 2 | Gated expansion with materiality / evidence / temporal checks |
| G6 | Evidence ledger (supporting/contradicting, quantitative/qualitative, replication, confounders) | `investigation_hypothesis_evidence` (stance, kind, label, value, citation) per hypothesis; `knowledge_evidence` per claim | Ledger keyed to *edges*, not hypotheses; quality/independence fields; rejected evidence retention |
| G7 | Contradiction search & confounder analysis | Evaluator reports contradicting evidence it is *given*; no targeted search | New planner pass + confounder list |
| G8 | Alternative-explanation comparison | Competing hypotheses are generated up front and ranked in 2H | Compare on shared evidence; surface what rival explanations the evidence cannot separate |
| G9 | Deterministic confidence | `confidence_score` is the **model's own 0–100 estimate** (`hypothesis_evaluator`) | Application-computed, versioned confidence (model classifies evidence; code scores) |
| G10 | Knowledge promotion lifecycle | None; LLM relationships written straight into `knowledge_relationships` | Lifecycle states + gates + human review path |
| G11 | Expectation–observation–validation | None generic. Raw material exists: `knowledge_claims` with `claim_type=PREDICTION`/category `guidance`; hypotheses | New primitive (3 tables) |
| G12 | Temporal / event-driven validation | `dataset_events` exists (ADR-013) but has no consumers for validation | Matching + idempotent validation job |
| G13 | Causal KPIs and guardrail KPIs | `execution_metrics` (latency/cost), `signals_routing_log`; Jev accuracy eval | Per-investigation causal metrics, golden benchmark, eval-run history |
| G14 | Versioning of engine/model/prompt/ontology/graph/confidence/benchmark | `investigations.version` constant 1; no others | Version registry |
| G15 | Reproducibility artifacts | One JSON per investigation, overwritten on re-run | Immutable versioned artifacts |
| G16 | Governance: LLMs cannot write durable knowledge | Not enforced: `macro_knowledge_builder` writes LLM relationships directly | Service boundary + write permissions |
| G17 | Graph-friendly API DTOs | HTML report with `reports/components/causal_chain.html` (linear chain) | Library-neutral graph DTO |

---

## 5. Target L5 Causal Architecture

```
                         QUESTION  (Jev -> Level 5)
                              |
                   1. HYPOTHESIS GENERATION        (existing 2E, extended)
                              |
          2. CAUSAL KNOWLEDGE SERVICE  <---------- L1 primitives / L2 sector /
             (read-only for the model)             L3 company overlay (Neo4j)
                              |
          3. INVESTIGATION GRAPH (L4)  built under bounded expansion gates
                              |
          4. EVIDENCE COLLECTION           (existing 2F via capabilities + new
                              |             contradiction & confounder passes)
          5. DETERMINISTIC TESTS           timing, magnitude, direction (SQL)
                              |
          6. EVIDENCE CLASSIFICATION       (LLM classifies; code scores)
                              |
          7. ALTERNATIVES + SYNTHESIS      (existing 2H, extended)
                              |
          8. PERSIST: graph + ledger + metrics + EXPECTATIONS
                              |
          9. LATER: new data -> validation events -> evidence for edges
                              |
         10. PROMOTION GATE (deterministic, human-reviewed at the top tiers)
```

Responsibilities, in one line each:
- **LLM:** propose hypotheses; interpret documents; classify an evidence item (supports / contradicts / neutral / confounds, with quote); write the narrative.
- **Code:** choose what to traverse, enforce bounds, run numeric/temporal tests, compute confidence, change lifecycle state, check permissions, write audit.

---

## 6. Four-Layer Causal Model

| Layer | Content | Lifetime | Where it lives |
|---|---|---|---|
| **L1 Universal primitives** | Demand, Supply, Price, Volume, Capacity, Utilization, Revenue, Margin, Input/Labor/Energy/Financing Cost, Working Capital, Capex, Leverage, Rates, Inflation, FX, Commodity, Competition, Regulation, Technology, Customer Behavior; plus the generic mechanisms between them (price × volume → revenue; revenue − cost → margin) | Years; changes only by versioned ontology releases | Neo4j nodes/edges, seed files in repo, snapshot to S3 |
| **L2 Sector knowledge** | Sector-specific concepts (ASK, RPK, load factor, yield; NIM, CASA; ASP, utilization) each *mapped onto* an L1 primitive, plus sector mechanisms and cross-sector dependencies | Months; promoted from evidence | Neo4j, `scope.sector` |
| **L3 Company overlay** | `BELONGS_TO`, `EXPOSED_TO`, `DEPENDS_ON`, `SUPPLIES`, `COMPETES_WITH`; weighted by exposure (e.g. % revenue by geography) | Per filing cycle | Relationships in Neo4j; **numbers stay in Postgres** |
| **L4 Investigation graph** | The nodes/edges a single question activated, with the evidence ledger entries and test outcomes | One investigation (immutable once finished) | Postgres rows + S3 artifact; optional Neo4j projection for similarity search |

**No duplication rule.** A company or sector never copies a mechanism; it *selects* it. L3 states exposure ("Maruti is exposed to Steel price"); the mechanism "Steel price → Auto input cost → Auto margin" lives once in L2. Every L4 edge carries a `knowledge_edge_id` reference to the L1–L3 edge it instantiates (or `null` if novel — a promotion candidate).

---

## 7. Universal Ontology Design

- **Node kinds (extend `config/knowledge_ontology.py`, do not fork it):** `Primitive`, `Mechanism`, `Concept` (sector-specific, maps to a primitive), `Sector`, `Industry`, `Company`, `EconomicIndicator`, `Commodity`, `Geography`, `Policy`. The existing entity types (`MacroFactor`, `Industry`, `Metric`, `Risk`…) map onto these; `Metric` nodes keep pointing at `metrics_dictionary` keys.
- **Primitive vocabulary.** Start with the list in the brief (~25). The ontology rule: a new sector concept must declare `maps_to_primitive` and `unit_family`; a concept that cannot map is rejected or parked, which is what stops vocabulary sprawl.
- **Extensibility.** Ontology is a versioned file (`ontology_version`), loaded into Neo4j by an idempotent sync like `sync_graph`. Additions are code-reviewed changes; models can only *propose* an addition (a candidate row), never create one.
- **Identity.** One node per real-world thing (ADR-009 shared-identity rule from the economic-graph plan): `Airlines` is one node across the semantic and causal layers.

---

## 8. Sector Scaling Strategy

Scaling unit = a **sector pack**, not a graph. A pack is a small versioned file containing:
1. sector concepts, each with `maps_to_primitive`;
2. the sector's *attachments* to existing mechanisms (e.g. Airlines `fuel_cost` → primitive `Energy Cost`);
3. only the mechanisms genuinely new to the sector (e.g. `Load Factor = RPK / ASK`; `Fleet → Capacity`);
4. the metric/indicator keys that evidence each concept (links into `metrics_dictionary` / `economic_series`).

Worked example, **Airlines**: reuses Demand, Price, Volume, Capacity, Utilization, Energy Cost, Labor Cost, Financing Cost, FX, Margin; adds ASK, RPK, Load Factor, Yield, Fleet, Fuel Hedging, each mapped (ASK→Capacity, RPK→Volume, Load Factor→Utilization, Yield→Price). Result: the airline margin question traverses *existing* cost and rate mechanisms with no airline-specific causal chain authored.

Coverage order matches data readiness (the app already has deep financials for banks, NBFCs, autos, IT, and US large caps): **Banking → NBFC → Auto → Energy → Pharma → Airlines → Insurance → Semis → Telecom → Utilities → Retail → Real Estate → Construction → Commodities.** The existing `infrastructure/economic_graph/indicators/*.yaml` (banking, auto, energy, …) already organize indicators by sector; packs should sit beside them.

Guardrail: a pack passes review only if it adds **mechanisms and attachments, never company chains**. Automated check: no L2 edge may reference a `Company` node.

---

## 9. Cross-Sector Dependency Design

Cross-sector links are **not stored as sector↔sector edges.** They emerge from traversal across shared primitives:

```
Policy Rate -> Bank Lending Rate -> Auto Financing Cost -> Vehicle Demand -> Auto Volume
Chip Supply  -> Production Capacity -> Vehicle Inventory -> Vehicle Price -> Auto Margin
```

Each arrow is a typed L1/L2 edge with `scope` (geography, sector). A "cross-sector hop" is simply a step where `scope.sector` changes; the traversal counts these (`max_cross_sector_hops`). Discovery is graph traversal from the effect node toward causes (`expand_causes`), filtered by scope and gates (Section 16); nobody authors "Semiconductor → Auto".

Where a real cross-sector dependence has no mechanism yet (the knowledge gap), the investigation records a **candidate edge** (lifecycle `CANDIDATE`) rather than a shortcut.

---

## 10. Company Overlay Design

Relationships only, weighted by *references* to numbers:

```
(Company)-[:BELONGS_TO]->(Sector/Industry)
(Company)-[:EXPOSED_TO {kind, weight_ref}]->(Concept|Primitive|Commodity|Geography|Policy)
(Company)-[:DEPENDS_ON|SUPPLIES|COMPETES_WITH]->(Company|Concept)
```

`weight_ref` points at a Postgres fact (e.g. a segment revenue share row), never a stored value. Sources: `knowledge_builder.py` extraction from filings (existing), sector tables, shareholding/segment data where present. Existing `knowledge_relationships` rows **seed** the overlay as `CANDIDATE` exposures; they are not auto-trusted (consistent with the economic-graph plan §5 and ADR-009).

Company numbers (revenue, margin, cost lines — including the Income Statement breakdown just added to the Financials tab: materials, employee, other, tax split, EBITDA/EBIT) remain in `canonical_financials`. Those lines are the natural quantitative tests for L4 edges such as "material cost ↑ → margin ↓".

---

## 11. Neo4j Graph Design

Reuse `context/graph_neo4j.py`'s pattern: Postgres + versioned seed files are the truth; Neo4j is a rebuildable projection (ADR-014).

- **New labels:** `Primitive`, `Mechanism`, `Sector`, plus `CausalAssertion` as a node linking `(source)-[:INPUT_TO]->(assertion)-[:INCREASES|DECREASES|…]->(target)` exactly as the economic-graph plan specifies (one assertion = one directed edge; chains are several assertions through `Mechanism` nodes).
- **Existing labels untouched:** `Company`, `Concept` (+ the 12 seed `AFFECTS` edges, migrated into assertions with `provenance_type=CURATED_RESEARCH` and `evidence_status=HYPOTHESIZED` until evidenced), `Investigation`, claim/evidence nodes.
- **No numeric series in the causal graph.** Evidence is referenced by `(table, key)` pointers. The existing financials projection (`sync_financials`) stays for its current consumers but is **not** read by causal traversal.
- **Edge properties (kept separate, per brief):** `direction/polarity`, `confidence`, `effect_strength` (magnitude), `typical_lag_min/max/unit`, `mechanism_id`, `scope` (JSON), `conditions`, `status`, `version`, `provenance_type`, timestamps. `confidence` never encodes magnitude.
- **Projection of L4:** optional `(:Investigation)-[:ACTIVATED]->(:CausalAssertion)` so "which prior investigations used this edge" is one query — useful for replication evidence and for reuse (`context/reuse.py` already reuses prior investigations).
- **Rebuild:** `sync_causal_graph()` from Postgres tables + ontology/sector-pack files; deterministic, idempotent, snapshotted to S3 with `graph_version`.
- **Backend parity.** Neo4j is optional today (`GRAPH_BACKEND=sqlite` fallback). Causal traversal over `causal_assertions` rows must work in SQL (recursive CTE, bounded depth) so tests and non-Neo4j environments keep working, mirroring `_find_multi_hop_claims_sqlite`.

---

## 12. Evidence Ledger Design

**Principle: ingestion creates evidence, not truth.** Mention frequency never raises confidence.

One ledger entry = one *piece of evidence about one edge or hypothesis*, independent of how many documents repeat it:

```
evidence_ledger_entry
  entry_id, edge_ref (assertion_id | candidate id) , investigation_id
  stance            SUPPORT | CONTRADICT | NEUTRAL | CONFOUND
  evidence_kind     QUANTITATIVE | QUALITATIVE
  source_tier       PRIMARY_FILING | MANAGEMENT_STATEMENT | MACRO_OFFICIAL | THIRD_PARTY | MODEL_INFERENCE   (ADR-005)
  source_ref        (raw_object_id | document_id | economic_series_id | canonical key), quote/page
  period            fiscal period the evidence describes (for temporal tests)
  independence_key  groups duplicates (same underlying disclosure) so they count once
  accepted          bool + rejection_reason   (rejected evidence is kept, not dropped)
  classified_by     model/version or 'deterministic'
```

Design notes:
- **Reuse:** extends `investigation_hypothesis_evidence` (already has stance/kind/label/value/citation) and `knowledge_evidence`; the economic-graph plan's `causal_evidence` table is the canonical home. Do not create a fourth evidence table — migrate toward one.
- **Independence.** Ten press articles quoting one filing = one entry. This is the specific defence against frequency-based truth.
- **Replication** is computed, not stored: distinct companies / distinct periods with `SUPPORT` entries on the same edge.
- **Quantitative evidence** points at SQL facts and the test that was run (`test_id`, parameters, result) so it is re-runnable.
- **Confounders** are first-class entries (`stance=CONFOUND`) naming the competing driver.

---

## 13. Storage Responsibility Matrix

| Data | S3 | Neon/Postgres | Qdrant | Neo4j |
|---|---|---|---|---|
| Raw filings, transcripts, macro snapshots | **truth** (`raw_objects`) | catalog/lineage | – | – |
| Financial facts, macro observations, time series | – | **truth** | – | never |
| Narrative chunks | source docs | chunk metadata | **index** | – |
| Ontology, sector packs | versioned snapshot | version registry | – | projection |
| Causal assertions (durable) | graph snapshot per `graph_version` | **truth** (assertions, versions, lifecycle) | – | projection for traversal |
| Evidence ledger | export in investigation artifact | **truth** | evidence *text* embeddings keyed by `entry_id` | pointer only |
| Investigation graph (L4) | **immutable artifact** | rows (nodes, edges, tests, metrics) | – | optional projection |
| Expectations / observations / validations | – | **truth** | – | edge confidence history referenced |
| KPI & eval history | eval run JSON | **truth** | – | – |
| Golden definitions | **truth** (versioned) | index of versions | – | – |

This matches ADR-021/022 and the economic-graph plan; the only departure is making Postgres, not S3, the truth for assertions (a queryable, transactional store), with S3 holding immutable snapshots.

---

## 14. Causal Knowledge Service Design

**Recommendation: EXTEND, do not add a network service.** ADR-011 (modular monolith before microservices) applies. Create an in-process module (e.g. `causal/knowledge_service.py`) behind a Protocol, following the `research/capabilities.py` pattern, so it can be remoted later.

Operations (read side is unrestricted; write side is the controlled surface):

```
Read:   get_drivers, get_dependents, expand_causes, expand_effects,
        get_sector_mechanisms, get_cross_sector_dependencies,
        get_company_exposures, build_investigation_graph
Write:  create_candidate_edge, attach_evidence, attach_contradiction,
        score_confidence, estimate_effect, estimate_lag,
        promote_edge, deprecate_edge
```

Permissions:
- Investigation code (and therefore any LLM output passing through it) may call **read** operations and `create_candidate_edge` / `attach_*`.
- `score_confidence`, `promote_edge`, `deprecate_edge`: **service-internal or operator only.** The model has no path to them. Every write records actor, reason, version, and goes to an append-only history.
- The service enforces the economic-graph plan's structural rule: `LLM_HEURISTIC` / `ANALYST_HYPOTHESIS` / `SYSTEM_DERIVED` provenance can never carry `CAUSAL`/`SUPPORTED_CAUSAL`.

---

## 15. Dynamic L5 Investigation Flow

"Why did Company X's margins decline?" (e.g. a non-financial company with the new EBITDA/COGS breakdown):

1. **Frame.** Resolve company, period, sector (`companies`, `company_kind`). Start node: `Margin`.
2. **Hypotheses** (existing 2E) — now *grounded in the knowledge service*: seed with `expand_causes(Margin, scope=sector)` so hypotheses cover Price / Volume / Cost branches, not just whatever the model recalls.
3. **Quantitative decomposition first (deterministic).** Use `canonical_financials`: change in margin split into price/mix, input (materials), employee, other, depreciation, interest. This *prunes* the tree: if employee cost moved 0.1 pt of a 4 pt decline it fails the materiality gate and is not expanded.
4. **Expand surviving branches** under the gates in Section 16, down to L2/L3: Material Cost → Steel Price (cross-sector) → Iron Ore; Volume → Demand → Financing Availability → Bank Lending → Policy Rate. Each hop checks evidence availability (a series or filing passage exists for `as_of`).
5. **Test.** Timing (lag window vs. observed change dates), magnitude (does the effect size fit?), direction (sign agreement) — all SQL over `economic_observations` / `canonical_financials` using `as_of`.
6. **Evidence & contradiction search.** For each surviving edge, run a *separate* retrieval whose query is the negation ("margin improved while steel rose", "company hedged"), through the existing document/knowledge capabilities.
7. **Confounders & alternatives.** Enumerate co-moving drivers over the same window; compare rival explanations on the shared evidence.
8. **Synthesize** (existing 2H) from the persisted graph + ledger; emit expectations (Section 20).
9. **Persist** graph, ledger, metrics (Section 25), artifact (Section 26).

---

## 16. Bounded Traversal Strategy

Never unrestricted (ADR-009 "Decision on traversal", ADR-018). Candidate expansion passes gates in order; the first failure stops it and the reason is logged.

```
Candidate edge -> in scope? -> evidence obtainable (as_of)? -> material? ->
temporally plausible? -> adds explanatory value (not already explained)? -> EXPAND
```

Recommended initial defaults (all config in `config/settings.py`, versioned with the engine; tune against golden results, not by feel):

| Parameter | Default | Rationale |
|---|---|---|
| `max_depth` | 4 | Margin→Cost→Material→Commodity is 3; one spare |
| `max_branches_per_node` | 4 | Top-4 by materiality |
| `max_total_nodes` | 30 | Readable and affordable |
| `max_total_edges` | 40 | |
| `max_iterations` | 3 (per hypothesis; today 2) | Keep the existing evidence loop bound; raise only if goldens show recall loss |
| `max_cross_sector_hops` | 2 | Rates→Banking→Auto is already 2 |
| `evidence_threshold` | ≥1 accepted primary or quantitative entry to expand beyond depth 2 | Prevents story-telling at depth |
| `materiality_threshold` | branch explains ≥10% of the observed change (quantified where possible) | Deterministic where a decomposition exists; otherwise "unquantified, depth ≤ 2" |
| wall clock | existing 180 s, raised only with measured need | ADR-018 |

---

## 17. Hypothesis / Contradiction / Confounder Design

Verdict vocabulary per edge: **SUPPORTED, PLAUSIBLE, WEAK, CONTRADICTED, UNRESOLVED.** This refines today's hypothesis-level verdicts (`SUPPORTED / PARTIALLY_SUPPORTED / REFUTED / INSUFFICIENT_EVIDENCE`); keep a mapping table so existing rows and UI remain valid.

Sequence per edge: timing test → quantitative test → supporting evidence → **targeted contradiction search** → confounders → alternatives. Rules enforced in code, not prompts:

- A verdict above WEAK requires ≥1 accepted entry that is `QUANTITATIVE` or `PRIMARY_FILING` (ADR-005).
- Management statements alone can reach PLAUSIBLE at best (consistent with the existing evaluator rule "never promote a management opinion to fact").
- A passing timing test is necessary, not sufficient.
- Any accepted `CONTRADICT` entry caps the verdict at PLAUSIBLE unless a confounder explains it (recorded).
- Evidence the evaluator cannot place is `UNRESOLVED`, never rounded up.

The model's role: classify one evidence item against one edge, return stance + quote + rationale. Code decides what that does to the verdict.

---

## 18. Confidence Model

Deterministic, versioned (`confidence_algorithm_version`), additive and inspectable, per the brief:

```
confidence = clamp( prior(status, provenance)
                  + w1 * primary_source_support
                  + w2 * quantitative_support
                  + w3 * temporal_consistency
                  + w4 * period_replication      # distinct periods
                  + w5 * company_replication     # distinct companies
                  - w6 * contradiction_weight
                  - w7 * confounder_risk )
```

- Inputs are counts/flags from the ledger after de-duplication by `independence_key`.
- Weights, caps and priors live in a versioned config file; changing them bumps the version and historical scores keep the version they were computed under. Recomputation under a new version is a batch job, never an in-place edit.
- Every score stores its component breakdown so "why 0.62?" is answerable.
- **Replaces** the model-self-reported `investigation_hypotheses.confidence_score` as the number shown for ranking. Keep the model's number as an audit column (it is useful as a disagreement signal), do not delete it.
- No learned weights in P0–P2. Calibration against validation outcomes is P3.
- Magnitude (`effect_strength`) and lag are estimated separately and never folded into confidence.

---

## 19. Knowledge Promotion Lifecycle

```
CANDIDATE -> OBSERVED -> EVIDENCE_BACKED -> VALIDATED -> PROMOTED
                    \-> DEPRECATED (from any state)
```

| Transition | Entry criteria | Who decides |
|---|---|---|
| (new) → CANDIDATE | Proposed by an investigation or an extraction; must map to ontology nodes and relationship type | Code (rejects out-of-vocabulary) |
| CANDIDATE → OBSERVED | Seen in ≥2 independent investigations or ≥2 independent sources | Code |
| OBSERVED → EVIDENCE_BACKED | ≥1 accepted primary/quantitative entry, no uncontested contradiction, confidence ≥ θ1 | Code |
| EVIDENCE_BACKED → VALIDATED | ≥1 SUPPORT validation event from an *out-of-sample future* observation (Section 21), across ≥2 periods or ≥2 companies | Code |
| VALIDATED → PROMOTED | Generalizes across ≥N companies and ≥M periods inside its scope; **human review** | Code proposes, human approves |
| any → DEPRECATED | Sustained CONTRADICT validations or source retraction; or manual | Code or human; keeps history |

- Scope widens only with evidence: company-specific → sector → universal, each step a separate promotion; "universal" requires human approval always.
- Every transition writes an immutable history row (`from`, `to`, `reason`, `evidence_refs`, `actor`, `confidence_algorithm_version`).
- Automation opportunity: transitions through EVIDENCE_BACKED can be fully automatic; the top two stay gated.
- Rollback = deprecate + new version; never delete.
- **Important:** in-sample evidence (the data that prompted the hypothesis) can reach EVIDENCE_BACKED at most; only later data can validate. That is what keeps ingestion volume from creating truth.

---

## 20. Expectation–Observation–Validation Framework

A generic platform primitive, not an L5 feature.

```
Expectation  --(future data arrives)-->  Observation  -->  Validation(result, delta)
```

Four distinct concepts, never merged: **Observation** (a fact), **Evidence** (a fact bearing on a claim), **Expectation** (a preserved prediction/claim with a window), **Validation** (a comparison event). A causal edge is a fifth, separate thing that *accumulates* validations.

Reuse: an Observation is rarely a new row — it is a **pointer** to `canonical_financials` / `economic_observations` / a `knowledge_claims` fact. Only the Expectation and Validation need new storage.

---

## 21. Expectation Model, Observation Model, Validation Model, Methods

```
expectations
  expectation_id, kind (HYPOTHESIS | GUIDANCE | FORECAST | RELATIONSHIP | PEER_RELATIVE)
  subject_type/subject_id (company, sector, series, edge)
  metric_key | edge_id
  expected_direction, expected_value | expected_low/high, unit
  window_start, window_end, conditions (JSON)
  source (investigation_id | claim_id | forecast run), confidence_at_creation
  as_of_created, version, status (OPEN | RESOLVED | EXPIRED | WITHDRAWN)

observations_ref            -- pointer, not a copy
  subject, metric_key, period, source_table, source_key, value_snapshot, observed_at, vintage

validations
  validation_id, expectation_id, observation_ref
  result  SUPPORT | CONTRADICT | NEUTRAL | PARTIAL
  expected, observed, delta, method, method_params
  confidence_before, confidence_after, algorithm_version
  validated_at, evidence_ref
  UNIQUE(expectation_id, observation_ref, method)      -- idempotency
```

Methods (a registry of small deterministic functions keyed by `method`; adding one is a code change, the engine is generic): `absolute_delta`, `percentage_delta`, `direction_match`, `range_match`, `threshold_match`, `temporal_match`, `relative_peer_delta`, `historical_deviation`, `causal_mechanism_validation`.

`causal_mechanism_validation` is a composite: cause moved in the window (observed) → effect moved in the expected direction → effect timing within the lag window → no stronger confounder moved. It returns SUPPORT/CONTRADICT/NEUTRAL/PARTIAL with each sub-test recorded. NEUTRAL (cause did not move, nothing to test) must be distinct from CONTRADICT; conflating them is the usual way these systems fool themselves.

---

## 22. Reusable Applications (one engine, five uses)

| Use | Expectation source in this repo | Observation |
|---|---|---|
| Management guidance accuracy | `knowledge_claims` with `claim_type=PREDICTION`, category `guidance`, `speaker`, fiscal period | Later `canonical_financials` |
| Causal validation | Edge + lag + direction from an investigation | Cause/effect series |
| Forecast validation | Valuation Model projections (`valuation_feed.py` forecast columns) | Actual results next year |
| Company vs peer | Peer-derived expected relationship (`research/peer_resolver.py`) | Company data |
| Historical relationship | Stored historical elasticities | Current data |

Guidance and forecast validation can ship with zero LLM involvement and double as proof the primitive is generic.

---

## 23. Event-Driven Temporal Validation

```
New data ingested -> dataset_events row (exists, ADR-013) -> find OPEN expectations
whose (subject, metric, window) match -> testable? -> run method -> write validation
-> update edge confidence/history -> update KPIs
```

Design decisions to settle at implementation time:
- **Trigger.** Start as a scheduled job (`SCHEDULED_JOBS`, daily, `enabled=False` until reviewed) scanning `dataset_events` since a watermark; move to event-triggered only if latency matters. This follows ADR-015/016 (scheduler owns timing, job owns idempotent logic).
- **Matching.** Indexed on `(subject_id, metric_key, window)`; period equivalence via the existing fiscal-year conventions (India Apr–Mar, US Jan–Dec — the app already encodes this per company).
- **Testable?** Window closed or enough of it observed; data not provisional if the method needs final figures.
- **Idempotency.** `UNIQUE(expectation_id, observation_ref, method)`; re-runs are no-ops.
- **Restatements / vintages.** Validation stores the vintage used. A later restatement creates a *new* validation with the new vintage and marks the old one superseded; confidence history keeps both.
- **Conflicting sources.** Use the reconciled canonical value (existing trust ranking); record if sources disagreed beyond a tolerance, and mark the validation `PARTIAL` rather than picking silently.
- **Expiry.** Window passed with no observation → `EXPIRED`, counted in resolution rate, never silently dropped.
- **Look-ahead safety.** Validation uses data published *after* `as_of_created`; golden/backtests use `research/temporal.py`.
- **Audit.** Every validation links evidence_ref; nothing is overwritten.

---

## 24. Causal KPI Definitions

Headline (kept separate; **no composite score**):

| KPI | Definition | Notes |
|---|---|---|
| **Causal Precision** | relevant edges retained ÷ edges presented | "Relevant" = matches a golden edge (exact or judged equivalent) or is later validated; for non-golden production runs, a proxy: presented edges not later CONTRADICTED |
| **Causal Recall** | golden important pathways found ÷ golden pathways | Golden-only |
| **Evidence Coverage** | material presented edges with ≥ threshold accepted evidence ÷ material presented edges | Deterministic |
| **Causal Validation Rate** | hypotheses/edges validated ÷ evaluated | Only counts out-of-sample validations; report with sample size |

Guardrails: Unsupported Edge Rate ↓ (presented edges with no accepted evidence), Contradiction Detection Rate ↑ (golden-known contradictions found), Cross-Sector Recall ↑, Investigation Efficiency (useful edges ÷ explored), Cost / investigation (from `llm_call_log`), Latency (from `execution_metrics`).

Expectation KPIs: **Resolution Rate** (resolved ÷ eligible) and **Support Rate** (supported ÷ resolved). Support Rate alone is gameable by trivial expectations; always report with **specificity** (share of expectations that name a magnitude or window), **materiality** (share on material metrics) and evidence quality. Never present Support Rate as a headline.

---

## 25. Evaluation Architecture

Three loops, increasing cost and rigour.

**A. Every investigation (deterministic, near-free).** Computed at persist time from the graph + ledger: nodes/edges explored and presented, supported/unsupported, contradictions found, cross-sector edges, evidence coverage, efficiency, model/tool calls, tokens, cost, runtime. Cost/latency already exist (`llm_call_log.investigation_id`, `execution_metrics`) and are joined, not re-measured.

**B. Golden Investigations (recurring, mostly deterministic).** Extend the existing `signals_eval` infrastructure; do not build a second runner. Each golden case: question, company, `as_of`, expected material drivers, expected pathways (as ontology-node paths), expected cross-sector dependencies, key evidence, known contradictions, known weak explanations, reviewed conclusion. Matching order: **exact** node/edge/path match → **ontology-alias** match → **LLM-as-judge only for the remainder**, with every judgement logged (model, prompt version, inputs, verdict). Recommended initial size: **12 cases for P2** (a first slice of 5 for the MVP), spread: 2 banking, 2 NBFC, 1 insurance, 2 auto, 1 semis, 1 energy, 1 pharma, 1 airlines, 1 macro→sector — with at least 4 requiring a cross-sector hop. Reuse the three L5 cases already verified in `SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md` (HDFC Bank, ICICI Bank, IDFC First Bank) as the seed. Governance: golden definitions are versioned files in S3 (`benchmark_version`), changed only by reviewed PR; each case records reviewer and date; **a case may be added freely, but a change to an existing case's expectations creates a new benchmark version** so trends stay comparable. Refresh policy: review each quarter, and after any ontology major version; retire cases whose data coverage regressed rather than silently editing them. Cost note: L5 runs are minutes and real LLM spend (as `run_signals_eval.py` already warns) — golden runs are on-demand and weekly at most; use cached evidence/retrieval where reproducibility allows.

**C. Temporal validation (continuous, deterministic first).** Section 23.

---

## 26. KPI Storage Design

Check existing tables first (done): `execution_metrics`, `execution_metrics_daily`, `llm_call_log`, `signals_routing_log`, `batch_job_runs/items` cover cost, latency and run bookkeeping. **EXTEND** by joining them on `investigation_id`/`run_id`; add only what's genuinely new:

- `l5_investigation_metrics` — one row per investigation (fields as in the brief), with `engine_version`, `model_version`, `prompt_version`, `ontology_version`, `graph_version`, `confidence_algorithm_version`. Cost/latency columns are denormalised snapshots from the existing tables for query convenience.
- `causal_eval_runs` — one row per golden run (precision, recall, evidence coverage, validation rate, unsupported rate, contradiction detection, cross-sector recall, cost, latency, `benchmark_version`) plus `causal_eval_case_results` (per case, so regressions are traceable to a case).
- `causal_validation_events` — the brief's table is subsumed by `validations` (Section 21) plus an edge-level `edge_confidence_history`; do not keep two.
- `eval_judge_log` — every LLM-as-judge decision.

Both SQLite and Postgres schemas must be updated (the repo's established dual-schema discipline).

---

## 27. S3 Artifact Design

Adopt the brief's layout but **fix immutability first**: ADR-022 documents that `investigations/<id>/v1.json` is overwritten on regeneration. Plan: keys carry a real version (`.../v<N>/`) and are write-once; the DB row points at the current version.

```
signals/
  investigations/<investigation_id>/v<N>/
      causal-graph.json   evidence.json   expectations.json
      validation.json     evaluation.json   run-meta.json   (versions, config hash)
  causal-evals/golden/v<K>/<case_id>.json
  causal-evals/runs/YYYY/MM/<eval_run_id>.json
  causal-graph-snapshots/graph_v<G>/...        (assertions + ontology + packs)
```

`run-meta.json` records every version identifier plus a hash of the traversal/confidence configuration — the minimum needed to re-run an investigation and explain a difference.

---

## 28. Versioning Strategy

Introduce one small registry (config module + a column set) carrying: `engine_version`, `model_version` (resolved model id from `llm_call_log`), `prompt_version` (hash of the system prompts in `hypothesis_*`/`research_synthesis`), `ontology_version`, `graph_version` (monotonic on any assertion lifecycle change snapshot), `confidence_algorithm_version`, `benchmark_version`. Stamp every investigation, metric row, eval run and validation. This enables the version-comparison table in the brief, and is the precondition for trusting any "improvement" claim.

---

## 29. Observability and Audit

Reconstructability requirement: for any investigation, answer *why this conclusion*. The persisted graph + ledger + tests give: question, start nodes, nodes/edges explored **including those rejected at a gate and why**, cross-sector hops, evidence retrieved/accepted/rejected, hypotheses generated/rejected, contradictions, confounders, confidence components over time, expectations generated, model/tool calls (existing `llm_call_log`), iterations, tokens, cost, runtime. Gate decisions are logged as rows (cheap, high value). No chain-of-thought is stored — only observable actions and outputs, consistent with the existing routing-audit convention.

---

## 30. Admin Analytics

Extend the existing Eval Analytics panel (`eval_analytics_charts.js`) with a "Causal Intelligence" tab: the four headline KPIs, the guardrail KPIs, cost and latency, each filterable by time, engine/model/prompt/ontology version, sector, company, geography and investigation type; version-over-version comparison table. No composite score. Data source: `l5_investigation_metrics` + `causal_eval_runs` (+ validation aggregates).

---

## 31. API / UI Contract Recommendations

Return a library-neutral DTO; the page (and `reports/components/causal_chain.html`) renders it however it likes.

```json
{ "investigation_id": "...", "versions": {...},
  "nodes": [{"id","label","type","layer","sector","metric_ref"}],
  "edges": [{"id","source","target","relationship","direction",
             "mechanism","confidence","effect_strength","lag":{"min","max","unit"},
             "status","verdict","evidence_count","supporting":[...],
             "contradictions":[...],"confounders":[...],"scope":{...}}],
  "pathways": [{"edge_ids":[...],"rank","summary"}],
  "alternatives": [...], "unresolved": [...], "expectations": [...] }
```

Existing investigation pages keep working off the current tables during migration.

---

## 32. Security and Governance

- **LLM write permissions:** none on durable causal knowledge; candidate creation and evidence attachment only through the service (Section 14).
- **Promotion:** deterministic gates; top tiers human-approved; every transition immutable-logged.
- **Provenance & integrity:** evidence entries carry `raw_object_id` content hashes (ADR-022); the ledger rejects entries whose source cannot be resolved.
- **Audit/versioning/rollback:** append-only history, deprecate-never-delete, manual override recorded with actor and reason.
- **Tenancy:** the repo has owner/visibility on cases, not a tenant model. Investigation graphs inherit the investigation's `visibility`/`owner_id`; **promoted knowledge is global** and must be derived only from evidence that is not private (user-uploaded private documents must not leak into shared causal knowledge — add an explicit `shareable` flag on evidence; default false for uploaded documents).
- **Retention:** keep rejected evidence and superseded validations; prune only raw LLM prompts/outputs under existing retention rules.

---

## 33. Implementation Roadmap

For every item: WHY / WHAT / WHERE / classification / dependencies / risk / effort (S ≤ 3 days, M ≈ 1–2 weeks, L > 2 weeks) / priority.

### P0 — Foundation (nothing user-visible changes)

| Item | WHY | WHAT | WHERE | Class | Deps | Risk | Effort |
|---|---|---|---|---|---|---|---|
| P0.1 Ontology extension | Controlled vocabulary is the scaling lever | Primitive/Mechanism/Sector kinds, causal relationship types, `maps_to_primitive` rule, sector-pack file format | `config/knowledge_ontology.py`, new `config/causal_ontology.py`, packs beside `infrastructure/economic_graph/` | EXTEND | none | Over-design; keep ≤ 25 primitives | M |
| P0.2 Causal schema | G1 | Build `causal_assertions`, `mechanisms`, evidence ledger (extend `investigation_hypothesis_evidence` → entry model), lifecycle + history tables — implementing economic-graph PLAN Phase 2, not a new design | `schemas/sqlite_schema.sql`, `schemas/postgres_schema.sql`, `storage/*_repository(.py/_pg.py)` | EXTEND | P0.1 | Dual-schema drift | M |
| P0.3 Version registry | G14 | The seven version ids, stamped into new rows | `config/`, `research/investigation.py` | NEW (small) | none | Low | S |
| P0.4 S3 immutability fix | G15 | Versioned write-once investigation artifacts | `research/investigation.py`, `storage/document_store.py`, `storage/repositories*.py` | EXTEND | none | Existing readers expect `v1.json` | S |
| P0.5 Causal Knowledge Service (read side + candidate/attach writes) | G16 | In-process module + Protocol; SQL recursive-CTE traversal + Neo4j implementation | new `causal/`; mirror `context/knowledge_graph.py` dispatch | NEW (thin) | P0.2 | Dual-backend parity | M |
| P0.6 Migrate 12 seed `AFFECTS` edges to assertions | Retire the `strength`-only form | One-off, `CURATED_RESEARCH`, status HYPOTHESIZED | `config/knowledge_graph_seed.py` → seed loader | REPLACE (data) | P0.2 | Existing `context/graph.py` consumers | S |

### P1 — L5 usable

| Item | WHAT | WHERE | Class | Effort |
|---|---|---|---|---|
| P1.1 Persisted investigation graph | Hypothesis `chain_steps` → nodes/edges; each edge gets ledger entries and a verdict | `research/hypothesis_generator.py`, `investigation.py` | EXTEND | M |
| P1.2 Knowledge-grounded hypothesis generation | Seed hypotheses from `expand_causes` | `hypothesis_generator.py` | EXTEND | S–M |
| P1.3 Quantitative decomposition + materiality | Deterministic margin/revenue/cost decomposition from `canonical_financials` (Income Statement rows now exist for India and US) | new `research/decomposition.py` using `financials/calculations.py` | NEW | M |
| P1.4 Gated bounded expansion + cross-sector hops | Section 16 gates, config, gate-decision logging | new traversal module; `investigation_planner.py` | EXTEND | M |
| P1.5 Contradiction & confounder passes | Negated-query retrieval; co-mover enumeration | `investigation_planner.py`, `hypothesis_evaluator.py` | EXTEND | M |
| P1.6 Deterministic confidence v1 | Section 18, replace displayed `confidence_score` | `causal/confidence.py` | NEW | S–M |
| P1.7 Per-investigation metrics (loop A) | `l5_investigation_metrics` populated at persist | `research/investigation.py` | NEW | S |
| P1.8 Graph DTO + report component | Section 31 | `web/`, `reports/components/causal_chain.html` | EXTEND | M |

### P2 — Evaluation and learning

| Item | WHAT | Class | Effort |
|---|---|---|---|
| P2.1 Golden Investigations | 12 cases, evaluator, `causal_eval_runs`, S3 benchmark versions; reuse `signals_eval` job/runner conventions | EXTEND | L |
| P2.2 Expectation/Observation/Validation primitive | Section 20–22 tables + method registry; guidance and forecast validation first (no LLM) | NEW | L |
| P2.3 Temporal validation job | Section 23, scheduled, disabled until reviewed | NEW | M |
| P2.4 Promotion lifecycle | Section 19 gates + review queue UI | NEW | L |
| P2.5 Admin analytics tab | Section 30 | EXTEND | M |
| P2.6 Sector packs: Banking, NBFC, Auto first | Section 8 | NEW (data) | L |

### P3 — Advanced

Empirical effect/lag estimation (needs enough validated history; start with simple lagged-correlation checks flagged as `STATISTICAL_TEST` provenance), confidence calibration against validation outcomes, assisted ontology expansion (model proposes, human approves), large-scale sector coverage, graph optimization. All explicitly gated on P2 data existing.

Sequencing note: **P0.4 and P0.3 are independent and cheap; do them first regardless of whether the rest is approved**, because the overwritten-artifact issue is a live reproducibility risk today.

---

## 33A. Effort and Timeline Estimate

**Basis and assumptions** (change these and the numbers move):
- Unit is **developer-days of focused work**, including tests, dual SQLite/Postgres schema work, local verification and a deploy, assuming one engineer working with an AI coding assistant, as this repo has been built. Rough comparison from this repo's own history: a feature of the "Income Statement rows for India + US, collapsible UI, backfill" kind was about 3–5 days.
- Calendar conversion: **about 4 productive days per week** (reviews, deploys, interruptions, other backlog). Ranges are low–high; **plan on the high end**. The high end already carries roughly 25% contingency; it does not cover unknown unknowns.
- Excluded: waiting time for external reviewers (golden-case experts, promotion reviewers), LLM spend for golden runs, and any new data ingestion (the plan deliberately needs none).
- Confidence: **P0 medium, P1 medium-low, P2 low, P3 not estimable** until P2 produces data. Prototype risk is concentrated in P1.4 (gated traversal), P1.5 (contradiction/confounder search) and P2.4 (promotion).

### Per-item effort (developer-days)

| Phase | Item | Low | High | Notes |
|---|---|---|---|---|
| P0 | P0.1 Ontology extension | 3 | 5 | Mostly design discipline; small code |
| P0 | P0.2 Causal schema (both backends, repos) | 5 | 8 | Dual-schema + `_pg` repository twins |
| P0 | P0.3 Version registry | 1 | 2 | |
| P0 | P0.4 S3 immutability fix | 1 | 2 | Verify current behaviour first (open question 8) |
| P0 | P0.5 Causal Knowledge Service | 6 | 9 | SQL traversal + Neo4j parity |
| P0 | P0.6 Seed-edge migration | 1 | 2 | |
| | **P0 total** | **17** | **28** | |
| P1 | P1.1 Persisted investigation graph | 6 | 9 | |
| P1 | P1.2 Knowledge-grounded hypotheses | 3 | 5 | |
| P1 | P1.3 Quantitative decomposition | 5 | 8 | Reuses the new Income Statement rows |
| P1 | P1.4 Gated bounded traversal | 6 | 9 | Highest design risk |
| P1 | P1.5 Contradiction + confounder passes | 6 | 9 | Quality depends on prompt iteration |
| P1 | P1.6 Deterministic confidence v1 | 3 | 5 | |
| P1 | P1.7 Per-investigation metrics | 2 | 3 | |
| P1 | P1.8 Graph DTO + report component | 5 | 8 | |
| | **P1 total** | **36** | **56** | |
| P2 | P2.1 Golden Investigations (framework 8–12 + 12 cases 6–10) | 14 | 22 | Case authoring needs expert review time on top |
| P2 | P2.2 Expectation/Observation/Validation primitive | 8 | 12 | Guidance + forecast validation first |
| P2 | P2.3 Temporal validation job | 5 | 8 | |
| P2 | P2.4 Promotion lifecycle + review queue | 8 | 12 | |
| P2 | P2.5 Admin analytics tab | 4 | 6 | |
| P2 | P2.6 Sector packs: Banking, NBFC, Auto | 12 | 18 | 4–6 each; needs domain review |
| | **P2 total** | **51** | **78** | |
| P3 | Empirical effects/lags, calibration, assisted ontology growth, wider sector coverage | — | — | Order of magnitude 30–60+ days; scope only after P2 data exists |
| | **P0 + P1 + P2** | **104** | **162** | |

### Calendar timeline, one engineer

| Milestone | Dev-days | Calendar | Cumulative |
|---|---|---|---|
| **MVP** (Section 38) | 19–30 | 5–8 weeks | 5–8 weeks |
| P0 complete | 17–28 | 4–7 weeks | 4–7 weeks |
| P1 complete (L5 usable) | 36–56 | 9–14 weeks | 13–21 weeks |
| P2 complete (evaluation + learning) | 51–78 | 13–20 weeks | 26–41 weeks |

MVP breakdown (19–30 days): P0.3 (1–2) + P0.4 (1–2) + P0.2 subset (3–5) + P1.1 (6–9) + P1.7 (2–3) + five golden cases with deterministic matching (6–9). The MVP overlaps P0/P1 rather than adding to them, so MVP effort is **not** additional to the phase totals above.

After the MVP ships, allow **2–3 calendar weeks of real investigations** before deciding what to build next; that data is the decision input and cannot be compressed.

### With two engineers

Two natural, low-conflict tracks:

- **Track A, knowledge and reasoning:** P0.1, P0.2, P0.5, P1.2–P1.6, P2.4, P2.6.
- **Track B, measurement and validation:** P0.3, P0.4, P1.1, P1.7, P1.8, P2.1, P2.2, P2.3, P2.5.

Track B's first two items need only the schema subset, so B can start in week 1. The critical path runs through Track A: ontology → schema → service → gated traversal → contradiction/confounder → promotion. Expected: **P0+P1 in about 8–13 weeks, P0–P2 in about 16–26 weeks** (not half of the single-engineer figure, because of integration work, shared schema changes and review).

### What would change these numbers

- **Longer:** Neo4j turns out to be required in production rather than optional (open question 3); the expert-review turnaround for golden cases exceeds about a week; confidence weights need several tuning rounds against goldens; a per-investigation cost ceiling forces redesign of the contradiction search (open question 6).
- **Shorter:** drop P1.8 (reuse the existing report component for the first release); defer P2.4 promotion until validation data exists; start sector packs with Banking only.
- **Hard external dependencies:** a named reviewer for goldens and promotion; a decision on guidance validation as a product feature (open question 5); an LLM budget for weekly golden runs (each Level-5 run is minutes of real spend, per `scripts/run_signals_eval.py`).

### Recommended commitment

Commit now to **the MVP only: 5–8 weeks, one engineer**, with a review gate afterwards. Treat P1 as a conditional second commitment (9–14 further weeks) decided on the MVP's metrics, and P2 as a third, decided on P1's. Do not commit a date for P3.

---

## 34. Files / Modules Likely to Change

- Extend: `config/knowledge_ontology.py`, `config/settings.py`, `research/investigation.py`, `hypothesis_generator.py`, `hypothesis_evaluator.py`, `investigation_planner.py`, `research_synthesis.py`, `research/capabilities.py` (new causal-knowledge capability), `context/graph_neo4j.py` (+ `sync_causal_graph`), `context/knowledge_graph.py` (dispatch pattern), `storage/repositories.py` / `repositories_pg.py`, `storage/document_store.py` callers, `scripts/run_signals_eval.py`, `research/signals_eval_cases.py`, `scheduling/jobs.py`, `reports/components/causal_chain.html`, Eval Analytics JS/templates.
- New: `causal/` package (service, confidence, traversal, validation methods), `research/decomposition.py`, sector-pack files, golden-case files, new scripts for sync/eval/validation jobs.
- Tests: follow the existing per-module pattern (`tests/test_*.py`); the traversal bounds, confidence arithmetic, lifecycle transitions and validation methods are all pure-function testable and should land with their modules.

## 35. Database / Schema Changes Likely Required

New (both schemas): `causal_assertions`, `mechanisms`, `causal_assertion_history`, `evidence_ledger` (superseding/extending `investigation_hypothesis_evidence` and absorbing the plan's `causal_evidence`), `investigation_graph_nodes`, `investigation_graph_edges`, `edge_confidence_history`, `expectations`, `validations`, `l5_investigation_metrics`, `causal_eval_runs`, `causal_eval_case_results`, `eval_judge_log`, `engine_version_registry` (or config-only). Altered: `investigations` (version columns, real `version`), `investigation_hypotheses` (link to graph edge, store model-reported vs computed confidence), `knowledge_relationships` (**unchanged**, per economic-graph plan §5). A `sectors` first-class node requires only a Neo4j projection change; the table exists.

---

## 36. Risks

| Risk | Mitigation |
|---|---|
| **Vocabulary sprawl / ontology bikeshedding** | Hard cap on primitives; `maps_to_primitive` mandatory; additions via reviewed files only |
| **Two evidence models diverge** (`knowledge_evidence`, `investigation_hypothesis_evidence`, planned `causal_evidence`) | One ledger; migration plan in P0.2; no fourth table |
| **Deterministic confidence looks rigorous but weights are arbitrary** | Version them, show components, validate against golden/validation outcomes before trusting; defer learned weights |
| **Golden set overfitting / benchmark gaming** | Version benchmarks, hold-out cases, review cadence, report per-case |
| **Golden set cost** (each L5 run is minutes of LLM spend) | On-demand + weekly cap; cache retrieval; small initial N |
| **Validation on trivial expectations inflates Support Rate** | Specificity/materiality reporting; no headline Support Rate |
| **Look-ahead bias** in validation/backtests | `as_of` everywhere; validations only from post-creation data |
| **Restatements silently change validation outcomes** | Vintage-stamped validations, superseding not editing |
| **Data coverage limits recall** (macro only 12 of 94 indicators sourced) | Recall measured on coverage-adjusted goldens; coverage reported next to KPIs; the plan does not require new ingestion |
| **Confounders are hard to enumerate** | Start with co-moving drivers from the same decomposition; mark as best-effort, record "confounder search coverage" |
| **Neo4j optionality** | SQL traversal parity from day one |
| **Private documents leaking into shared knowledge** | `shareable` flag, default false for uploads |
| **Scope creep into a "complete economy model"** | Mechanisms not companies; sector packs; MVP boundary below |
| **Existing UIs/readers break** | Additive schema, mapping table for verdicts, keep current pages until P1.8 |

## 37. Open Questions

1. Promotion at the top tier: who is the human reviewer, and is review in-app (a queue) or by PR to a seed file? (Plan assumes in-app queue for EVIDENCE_BACKED→VALIDATED→PROMOTED; confirm.)
2. Is `CausalAssertion` Phase 2 of the economic-graph plan still the intended model, or has your thinking moved? This plan assumes **yes**, and only extends it (lifecycle, ledger independence, validation history).
3. Neo4j in production: is it running on Lightsail/Neon-adjacent infra today, or still `GRAPH_BACKEND=sqlite`? Affects whether P0.5 can rely on Neo4j for anything beyond tests.
4. Scope of "expert-reviewed" goldens: who reviews, and what is the acceptable turnaround? The benchmark's value depends on reviewer quality more than on quantity.
5. Guidance validation: is management-guidance accuracy a product feature you want surfaced to users, or an internal quality signal only? Changes Section 22's priority.
6. Cost budget for L5: ADR-018 governs depth but this plan adds contradiction search and evidence classification calls. Is there a per-investigation dollar ceiling to design against?
7. Should US and India share one ontology and graph (recommended — the primitives are universal) with `scope.geography` separating regimes?
8. Verify during P0.4: whether the investigation artifact overwrite described in ADR-022 still holds in current code (I read the ADR, not every call site).

## 38. Recommended MVP Boundary

The smallest slice that proves the idea and is worth shipping alone:

1. **P0.3 + P0.4** — version registry and immutable versioned artifacts (small, fixes a live issue).
2. **P0.2 (subset)** — `investigation_graph_nodes/edges` and the evidence-ledger fields on existing evidence rows, plus `l5_investigation_metrics`.
3. **P1.1 + P1.7** — persist the investigation graph from the existing loop; compute the deterministic per-investigation metrics (evidence coverage, unsupported-edge rate, efficiency, cost, latency).
4. **P2.1 (mini)** — **5 golden cases** (reuse the 3 existing L5 cases + 1 auto, 1 cross-sector) with deterministic exact/alias matching only, no LLM-judge yet.

**Explicitly out of the MVP:** promotion lifecycle, durable causal knowledge writes, the Causal Knowledge Service, Neo4j changes, sector packs, validation automation, admin analytics, learned anything.

Why this boundary: it makes edges *countable and evidenced*, which is the precondition for every KPI in the brief, and it changes no user-facing behaviour or durable knowledge. If after 2–3 weeks of data the per-investigation metrics are not informative, the larger investment is not justified; if they are, they tell us which of P1.2–P1.6 matters most.

## 39. Recommended Next Step

Approve (or amend) the MVP boundary above, answer open questions 1–3 and 8, and I will turn the MVP into a task-level implementation plan with schema DDL drafts for review (still no code until you approve). Independently of that decision, I recommend authorising P0.4 (immutable artifacts) as a stand-alone fix.

## 40. Target Architecture Diagram

```
                                   +-------------------------------+
   Question --> Jev (L1..L5) ----> |  L5 INVESTIGATION ORCHESTRATOR |  (existing 2E-2H, extended)
                                   +---------------+---------------+
                                                   |
        read-only for the model                    | proposals (hypotheses, evidence classification)
   +-----------------------------+                 v
   |  CAUSAL KNOWLEDGE SERVICE   |<---- gated expansion / tests / scoring (deterministic code)
   |  get_* / expand_* (read)    |
   |  create_candidate / attach  |       +-------------------+     +--------------------+
   |  score / promote (internal) |       | EVIDENCE CAPS     |     | DETERMINISTIC TESTS|
   +------+---------------+------+       | docs/Qdrant, macro|     | timing/magnitude/  |
          |               |              | knowledge, SQL    |     | direction (as_of)  |
          v               v              +---------+---------+     +----------+---------+
   +-----------+   +--------------+                |                          |
   |  NEO4J    |   |  NEON/PG     |<---------------+--------------------------+
   | L1 prims  |   | assertions   |   financial + macro facts (never copied to Neo4j)
   | L2 sector |   | ledger       |
   | L3 overlay|   | investigation|--> expectations --> [data arrives] --> validations
   | (rebuild) |   |  graphs      |                              |
   +-----------+   | metrics/eval |<---- edge confidence history-+
                   +------+-------+
                          |
                          v
                   +--------------+        +-----------------------------+
                   |  S3          |        |  EVAL: per-investigation    |
                   | immutable    |        |  metrics, golden runs,      |
                   | artifacts +  |        |  temporal validation -> KPIs|
                   | graph/golden |        |  (Admin analytics)          |
                   | snapshots    |        +-----------------------------+
                   +--------------+
```

---

*End of plan. Nothing here has been implemented; the approval gate is Section 39.*
