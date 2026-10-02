# L5 Causal Intelligence — MVP Task-Level Plan

**Status:** Planning only. No code, schema, migration, prompt or infrastructure change accompanies this document. Implementation starts only on an explicit "start M<n>" from you.
**Date:** 2026-10-02
**Parent document:** [L5_CAUSAL_INTELLIGENCE_PLAN.md](L5_CAUSAL_INTELLIGENCE_PLAN.md) (Section 38 defines the MVP boundary; Section 42 defines feedback).
**Assumed answers to the open questions:** (a) any signed-in user may give feedback in the first release, tagged `ordinary` / `expert` / `internal` (default `ordinary`; roles assigned later); (b) "sufficiently similar" context = sector + geography + regime + question type (regime is stored as NULL until a regime classifier exists).

---

## 1. What the MVP is, and is not

**Is:** make an L5 investigation produce a **persisted graph** of nodes and edges with evidence attached, compute **deterministic per-investigation metrics**, store **versioned immutable artifacts**, capture **structured feedback** (ledger only), and measure quality against **5 golden cases**.

**Is not:** the Causal Knowledge Service, durable `CausalAssertion` writes, ontology/sector packs, promotion, adaptation, adaptive ranking, validation automation, or admin analytics. Nothing the MVP stores is treated as causal truth; it is **investigation-level structure** (parent plan Layer L4).

Success is judged after 2–3 weeks of real investigations (Section 9): are the metrics informative, is feedback being given, and do golden runs separate good from bad explanations?

---

## 2. Repository findings that shape the MVP

| # | Finding (evidence) | Consequence |
|---|---|---|
| F1 | `research/investigation.py::_persist` is the single place all investigation data is written (tables via `FactStore` + one JSON artifact). | Graph/metrics/version stamping hook in here; no other call sites. |
| F2 | The S3 key is the literal `f"investigations/{id}/v1.json"` and `version=1` is hard-coded (`_persist`, last lines). Confirms ADR-022's finding: regeneration overwrites. | M2 is a real fix, not hygiene. |
| F3 | A hypothesis carries `chain_steps: list[str]` (cause → effect labels, `hypothesis_generator.Hypothesis`). Evidence is attached **per hypothesis** (`investigation_hypothesis_evidence`: stance, kind, label, value, citation), not per step. | MVP edges = consecutive `chain_steps` pairs. Edge-level evidence needs the evaluator to say which step each item bears on (M5, optional field, backward compatible). |
| F4 | Verdict vocabulary today: `SUPPORTED / PARTIALLY_SUPPORTED / REFUTED / INSUFFICIENT_EVIDENCE`; `confidence_score` is the model's own 0–100. | MVP keeps these as-is and stores them; computed confidence is P1.6, out of scope. |
| F5 | Persistence goes through the `FactStore` seam (`storage/fact_store.py`) backed by `storage/repositories.py` / `repositories_pg.py`. Schemas are dual: `schemas/sqlite_schema.sql` and `schemas/postgres_schema.sql`; SQLite also has `_migrate_*` functions in `storage/database.py`. | Each new table/column needs: both schema files, SQLite migration, both repository twins, FactStore methods, tests. |
| F6 | Cost/tokens per investigation are already queryable: `llm_call_log.investigation_id`. Latency: `execution_metrics` has `run_id` but no `investigation_id`. | Cost/tokens = join. Runtime = measured inside `run_investigation` (start/end), not joined. |
| F7 | `signals_eval` job + `scripts/run_signals_eval.py` + `batch_job_items` convention already exist; 3 verified L5 cases are in `research/signals_eval_cases.py`. | Golden runner is a sibling job reusing `BatchRun`; the three bank cases seed the benchmark. |
| F8 | The investigation page is `web/templates/investigation.html`, rendered by `web/app.py::_render_investigation`, and already shows hypotheses and chain steps. | Feedback controls attach here; no new page. |
| F9 | `research/temporal.py` / ADR-012: `as_of` is enforced in capabilities. | Golden cases fix an `as_of` so reruns are comparable. |
| F10 | `users` table exists; no role column. Cases have `owner_id` / `visibility`. | `user_class` is stored on the feedback row, defaulted; no RBAC work in MVP. |

---

## 3. Design decisions for the MVP

1. **Edge identity.** `edge_key = normalize(source_label) + "→" + normalize(target_label)` where `normalize` = lowercase, collapse whitespace, strip punctuation. `edge_id = "<investigation_id>:<hypothesis_id>:<position>"`. The key makes edges comparable across investigations later (Repeat Error Rate, replication) without needing an ontology yet; `ontology_ref` columns exist but stay NULL until P0.1.
2. **Path.** In the MVP, **path = a hypothesis's chain**; `path_id = hypothesis_id`. Separate path objects arrive with multi-hypothesis graph merging in P1.
3. **Nodes are per-hypothesis steps**, not merged across hypotheses (merging is an ontology problem, deferred). Duplicate labels in different hypotheses are different nodes in MVP; `normalized_label` allows a later merge.
4. **Evidence tagging is optional.** Evaluator output gains an optional `chain_step` (0-based index of the *edge* from step i to i+1, or the node index; see M5) on each evidence item. Untagged evidence stays hypothesis-level. Metrics report a `tagging_rate` so edge-level numbers are never presented as more reliable than they are.
5. **"Presented" and "material" are explicit, simple definitions** (Section 6), versioned as `metrics_definition_version`, so later refinement does not silently change history.
6. **Feedback never touches the graph or any weight in the MVP.** Ledger only.
7. **All new behaviour is behind one setting** (`CAUSAL_GRAPH_ENABLED`, default on locally, off until you decide on deploy), so the existing investigation path is unchanged if it is off, and any failure in the new code **must not fail the investigation** (wrapped, logged, investigation still persists as today).

---

## 4. Tasks

Effort in developer-days (low–high). Every task ends with: tests passing, both schemas updated where relevant, local verification on a real investigation, **no deploy** until you ask.

### M1 — Version registry (1–2 days)

- **Why:** every metric, run and feedback row must say which engine produced it.
- **What:** a small module exposing `engine_version`, `prompt_version`, `metrics_definition_version`, and a `config_hash` of the traversal-relevant settings (`MAX_EVIDENCE_ITERATIONS`, `INVESTIGATION_TIMEOUT_SECONDS`, model chains). `prompt_version` = short hash of the generator / planner / evaluator / synthesis system prompts. `model_version` is taken per investigation from `llm_call_log` (models used), not configured.
- **Where:** new `config/versions.py` (or a section of `config/settings.py`); stamped in `_persist`.
- **Depends on:** none.
- **Tests:** hash is stable across runs, changes when a prompt string changes.
- **Done when:** `investigations` rows (M3) carry the stamps for a locally generated investigation.

### M2 — Immutable, versioned investigation artifacts (1–2 days)

- **Why:** regeneration silently overwrites `v1.json` today (F2); reproducibility and feedback traceability need stable artifacts.
- **What:** key becomes `investigations/<id>/v<N>/artifact.json` plus companion files (M6/M7 add `graph.json`, `metrics.json`); `N` = `investigations.version + 1` on regeneration; `investigations.version` actually increments; the page keeps reading the **current** version. Existing `v1.json` objects stay valid (reader falls back to the legacy key when `s3_key` points at it).
- **Where:** `research/investigation.py::_persist`, `storage/repositories*.py::update_investigation_s3_metadata`, `web/app.py::_render_investigation` reader.
- **Depends on:** none; independent of every other task.
- **Risk:** other readers of `s3_key` (search the repo for `investigations/` keys before changing). Mitigation: grep gate in the task checklist.
- **Tests:** two persists of the same id produce two objects and `version = 2`; legacy-key reader still works.
- **Done when:** regenerating an investigation locally leaves both versions in the object store and the page shows the latest.

### M3 — Schema: graph, evidence tags, metrics, feedback, eval (3–5 days, with M4)

Drafts in Section 5. Both schema files, SQLite `_migrate_*`, additive only (no destructive change), `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` everywhere.

- **Where:** `schemas/sqlite_schema.sql`, `schemas/postgres_schema.sql`, `storage/database.py`.
- **Done when:** a fresh SQLite DB and the Neon schema apply cleanly; the existing 1,2xx tests still pass.

### M4 — Repository + FactStore methods (2–3 days, with M3)

- **What:** insert/select for nodes, edges, evidence tags, metrics, feedback, eval runs and case results — in `repositories.py`, `repositories_pg.py`, and `FactStore`.
- **Tests:** round-trip tests on SQLite (the repo's norm) plus a Postgres-parity test where the suite already has one.

### M5 — Evaluator emits per-step evidence tags (2–3 days)

- **Why:** without it, edge-level coverage cannot be computed honestly (F3).
- **What:** extend the evaluator's output schema with an optional `chain_step` on each supporting/contradicting item; parser accepts missing/invalid values as NULL; prompt gets a short instruction to tag when the step is clear. **No change to verdict logic.**
- **Where:** `research/hypothesis_evaluator.py` (`EvidenceItem`, prompt, `_parse_evidence_items`), `research/investigation.py::_persist_evidence_item`.
- **Tests:** parser handles tagged/untagged/invalid; existing evaluator tests unchanged.
- **Risk:** model ignores the instruction → `tagging_rate` low. That is itself a finding; the plan's response is a later structured-output tightening, not hiding the number.

### M6 — Graph builder and persistence (3–5 days)

- **What:** after evaluation, convert each hypothesis's `chain_steps` into nodes (position order) and consecutive edges; attach evidence counts per edge from tagged items; mark `presented`; store rows (M3/M4) and add `graph` to the artifact. Hypotheses with fewer than 2 steps produce no edges (the UI already falls back to prose for them).
- **Where:** new `research/investigation_graph.py` (pure functions: `build_graph(hypotheses, evaluations) -> Graph`), called from `_persist`.
- **Tests:** pure-function tests (no DB): 0/1/2/N steps, duplicate labels, tagged/untagged evidence, normalization.
- **Done when:** a real local investigation shows rows in both tables and a `graph` block in the artifact.

### M7 — Per-investigation metrics (2–3 days)

- **What:** compute the metrics in Section 6 from the persisted graph; join cost/tokens from `llm_call_log`; measure runtime and iteration count inside `run_investigation`; write one `l5_investigation_metrics` row; include in the artifact.
- **Where:** new `research/investigation_metrics.py`; hooks in `_run_investigation_impl` (timing) and `_persist`.
- **Tests:** pure-function metric tests on hand-built graphs including empty and all-unsupported cases.

### M8 — Structured feedback capture (4–6 days)

- **What:** endpoint `POST /investigate/<id>/feedback` accepting `{target: investigation|hypothesis|path|edge, target_id, feedback_type, comment?}`; validates `feedback_type` against the controlled taxonomy (parent plan 42.2) and that the target belongs to the investigation; one active vote per (user, target): a newer vote supersedes (`superseded_by`); `user_class` default `ordinary`; context fields (company, sector, geography, period, question type) copied from the investigation, `regime_tag` NULL. UI: compact controls on the investigation page — per hypothesis and per edge a small menu of the taxonomy values, optional comment, shown in the repo's collapsed-disclosure style (memory: real-estate-efficient UI). A read endpoint returns the user's own feedback to pre-fill state.
- **Where:** `web/app.py`, `web/templates/investigation.html`, new `config/causal_feedback.py` (taxonomy constants), repository methods from M4.
- **Rules enforced:** free text never replaces the type; no graph/weight mutation; rate limit per user.
- **Tests:** validation, supersede behaviour, ownership/visibility (a user cannot attach feedback to an investigation they cannot view), taxonomy rejection.
- **Done when:** feedback entered on a local investigation appears in `causal_feedback` with the right context columns.

### M9 — Golden mini-set and evaluator (6–9 days)

- **What:** 5 case definition files, a deterministic matcher, a runner and result tables.
  - **Cases (v1):** the 3 existing verified L5 bank cases (`l5_hdfc_post_merger_profitability`, `l5_idfcfirstb_growth_sustainability`, `l5_hdfc_vs_icici_divergence`) plus 1 auto (Maruti margin movement; input cost, volume, financing) and 1 cross-sector (a steel-input-cost question touching auto or capital goods). The two new cases use companies whose new Income Statement rows (materials, employee, tax, EBITDA) are already loaded.
  - **Case file:** `case_id`, `question`, `company_ids`, `as_of`, `expected_edges` (each with `edge_id`, `source_aliases[]`, `target_aliases[]`, `importance` essential/supporting), `known_weak_explanations[]`, `reviewed_by`, `reviewed_on`, `benchmark_version`.
  - **Matching (deterministic only):** an investigation edge matches an expected edge when its source label contains an alias of the expected source **and** its target label contains an alias of the expected target (case-insensitive, word-boundary). No LLM judge in the MVP; unmatched candidates are listed for human review in the report.
  - **Scores per run:** *golden recall* = matched essential expected edges ÷ essential expected edges; *golden precision (lower bound)* = presented edges matching any expected edge ÷ presented edges (labelled a lower bound because a golden set is never exhaustive); *unsupported-edge rate* and *evidence coverage* reuse M7.
  - **Runner:** new job `causal_eval` in `scheduling/jobs.py` (`enabled=False`, `manual_only=True`), `scripts/run_causal_eval.py` reusing `BatchRun`, writing `causal_eval_runs` / `causal_eval_case_results` and an S3 run artifact; a plain text/markdown report script for reading results.
  - **Governance:** case files live in the repo (`research/golden/v1/`) and are copied to S3 under `causal-evals/golden/v1/`; changing an existing case's expectations creates `v2`, never edits `v1`.
- **Where:** new `research/golden/`, `research/causal_eval.py`, `scripts/run_causal_eval.py`, `scheduling/jobs.py`.
- **Cost:** each case is a full L5 run (minutes, real LLM spend, per `run_signals_eval.py`'s own warning). Run on demand; first run is a **dry measurement of cost**, reported before any scheduling is considered.
- **Reviewer:** expected edges are authored by you or a named reviewer; I can draft candidates from the real investigations for review, but a draft is not "reviewed" until someone signs it.
- **Tests:** matcher unit tests (alias hits/misses, word boundaries), score arithmetic, runner with a stubbed investigation.

---

## 5. Schema drafts (for review; additive; not applied)

PostgreSQL shown; SQLite equivalents use `TEXT`/`INTEGER`/`REAL` and the repo's existing id conventions.

```sql
-- versions and timing on the existing investigations row
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS engine_version TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS config_hash TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS metrics_definition_version TEXT;

-- evidence tags on the existing per-hypothesis evidence table
ALTER TABLE investigation_hypothesis_evidence ADD COLUMN IF NOT EXISTS chain_step INTEGER;   -- NULL = hypothesis-level
ALTER TABLE investigation_hypothesis_evidence ADD COLUMN IF NOT EXISTS edge_id TEXT;         -- resolved by M6 when tagged
ALTER TABLE investigation_hypothesis_evidence ADD COLUMN IF NOT EXISTS source_tier TEXT;     -- ADR-005 tier, NULL until classified
ALTER TABLE investigation_hypothesis_evidence ADD COLUMN IF NOT EXISTS accepted INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS investigation_graph_nodes (
  node_id TEXT PRIMARY KEY,                      -- '<investigation_id>:<hypothesis_id>:n<position>'
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  hypothesis_id TEXT NOT NULL REFERENCES investigation_hypotheses(hypothesis_id),
  position INTEGER NOT NULL,
  label TEXT NOT NULL,
  normalized_label TEXT NOT NULL,
  node_type TEXT NOT NULL DEFAULT 'step',
  ontology_ref TEXT                               -- NULL until P0.1
);
CREATE INDEX IF NOT EXISTS idx_igraph_nodes_inv ON investigation_graph_nodes(investigation_id);

CREATE TABLE IF NOT EXISTS investigation_graph_edges (
  edge_id TEXT PRIMARY KEY,                       -- '<investigation_id>:<hypothesis_id>:e<position>'
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  hypothesis_id TEXT NOT NULL REFERENCES investigation_hypotheses(hypothesis_id),
  position INTEGER NOT NULL,
  source_node_id TEXT NOT NULL REFERENCES investigation_graph_nodes(node_id),
  target_node_id TEXT NOT NULL REFERENCES investigation_graph_nodes(node_id),
  edge_key TEXT NOT NULL,                         -- normalized 'source→target', for cross-investigation matching
  supporting_count INTEGER NOT NULL DEFAULT 0,    -- accepted, tagged to this edge
  contradicting_count INTEGER NOT NULL DEFAULT 0,
  presented INTEGER NOT NULL DEFAULT 0,
  hypothesis_verdict TEXT,                        -- copied for query convenience
  ontology_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_igraph_edges_inv ON investigation_graph_edges(investigation_id);
CREATE INDEX IF NOT EXISTS idx_igraph_edges_key ON investigation_graph_edges(edge_key);

CREATE TABLE IF NOT EXISTS l5_investigation_metrics (
  investigation_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
  investigation_version INTEGER NOT NULL DEFAULT 1,
  engine_version TEXT, prompt_version TEXT, config_hash TEXT, metrics_definition_version TEXT,
  models_used TEXT,                               -- JSON list from llm_call_log
  hypotheses_total INTEGER, hypotheses_evaluated INTEGER,
  nodes_explored INTEGER, edges_explored INTEGER, edges_presented INTEGER,
  supported_edges INTEGER, unsupported_edges INTEGER,
  contradicting_evidence_items INTEGER,
  evidence_coverage REAL, unsupported_edge_rate REAL, investigation_efficiency REAL,
  tagging_rate REAL,                              -- share of evidence items tagged to an edge
  cross_sector_edges INTEGER,                     -- NULL in MVP (no sector tags on nodes yet)
  model_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
  iterations INTEGER, runtime_ms REAL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS causal_feedback (
  feedback_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  investigation_version INTEGER NOT NULL DEFAULT 1,
  hypothesis_id TEXT, path_id TEXT, edge_id TEXT,  -- path_id = hypothesis_id in MVP
  feedback_type TEXT NOT NULL,                     -- CORRECT | NOT_RELEVANT | WRONG_RELATIONSHIP | MISSING_DRIVER |
                                                   -- MISSING_MEDIATOR | OVERSTATED | UNDERSTATED | WRONG_TIMING | INSUFFICIENT_EVIDENCE
  company_id TEXT, sector TEXT, geography TEXT, period TEXT, regime_tag TEXT, question_type TEXT,
  user_id INTEGER, user_class TEXT NOT NULL DEFAULT 'ordinary',
  comment TEXT,
  engine_version TEXT, graph_version TEXT,
  superseded_by INTEGER,                           -- newer vote by the same user on the same target
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_causal_feedback_inv ON causal_feedback(investigation_id);
CREATE INDEX IF NOT EXISTS idx_causal_feedback_edge ON causal_feedback(edge_id);

CREATE TABLE IF NOT EXISTS causal_eval_runs (
  eval_run_id TEXT PRIMARY KEY,
  benchmark_version TEXT NOT NULL,
  engine_version TEXT, prompt_version TEXT, config_hash TEXT, metrics_definition_version TEXT,
  cases_total INTEGER, cases_completed INTEGER,
  golden_recall REAL, golden_precision_lower_bound REAL,
  evidence_coverage REAL, unsupported_edge_rate REAL,
  estimated_cost_usd REAL, runtime_ms REAL,
  s3_key TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS causal_eval_case_results (
  eval_run_id TEXT NOT NULL REFERENCES causal_eval_runs(eval_run_id),
  case_id TEXT NOT NULL,
  investigation_id TEXT,
  status TEXT NOT NULL,                            -- ok | failed
  expected_essential INTEGER, matched_essential INTEGER,
  presented_edges INTEGER, matched_presented INTEGER,
  unmatched_presented_json TEXT,                   -- for human review
  error_detail TEXT,
  PRIMARY KEY (eval_run_id, case_id)
);
```

`graph_version` on feedback is the MVP's constant (`"mvp-0"`) until durable graph versions exist. Nothing here references `knowledge_relationships` (parent plan decision: unchanged).

---

## 6. Metric definitions (MVP, version `mvp-1`)

All deterministic; computed from persisted rows only.

| Metric | Definition |
|---|---|
| `nodes_explored` / `edges_explored` | Counts of graph nodes/edges across **all** generated hypotheses, including those whose evaluation failed (those have a chain but no verdict) |
| `edges_presented` | Edges of hypotheses with verdict `SUPPORTED` or `PARTIALLY_SUPPORTED` (what the synthesis can rely on) |
| supported edge | Presented edge with ≥ 1 **accepted, tagged** supporting item |
| `unsupported_edges` | Presented edges with 0 accepted tagged supporting items |
| `unsupported_edge_rate` | `unsupported_edges ÷ edges_presented` (NULL if none presented) |
| `evidence_coverage` | `supported_edges ÷ edges_presented`. **All presented edges count as material in the MVP** (no materiality signal exists yet); the metadata records `materiality_basis = "none"` |
| `investigation_efficiency` | `supported_edges ÷ edges_explored` |
| `tagging_rate` | Evidence items with a `chain_step` ÷ all evidence items. Low values mean edge-level metrics are weak; reported beside every edge metric |
| `contradicting_evidence_items` | Count of items with stance `contradicting` |
| `cross_sector_edges` | NULL (no sector scope on nodes yet; parent plan P0.1) |
| cost, tokens, calls | Sum over `llm_call_log` rows for the investigation |
| `runtime_ms`, `iterations` | Measured inside `run_investigation` (start-to-persist; evidence-loop attempts summed) |

**Honest limits:** with untagged evidence, supported-edge counts understate coverage; with all presented edges "material", the rate over-penalises minor edges. Both are labelled in the output, and both are expected to change under `metrics_definition_version` bumps (never edited in place).

---

## 7. Sequencing and timeline

One engineer with an AI assistant, about 4 productive days a week (parent plan Section 33A basis). **Total 23–36 developer-days ≈ 6–9 calendar weeks.**

| Order | Tasks | Dev-days | Why this order |
|---|---|---|---|
| 1 | **M2** (immutable artifacts), **M1** (versions) | 2–4 | Independent, small, M2 fixes a live issue; can ship alone |
| 2 | **M3 + M4** (schema, repositories) | 5–8 | Everything else writes to these |
| 3 | **M5** (evidence tags) | 2–3 | Needed before graph metrics are meaningful |
| 4 | **M6** (graph builder), **M7** (metrics) | 5–8 | Core of the MVP |
| 5 | **M8** (feedback) | 4–6 | Starts accumulating data early |
| 6 | **M9** (golden mini-set) | 6–9 | Needs M6/M7 to score |

**Checkpoints (each ends with a short report to you, nothing deployed):**
- **C1 after step 1:** artifacts versioned locally; legacy readers confirmed.
- **C2 after step 4:** one real investigation on a non-bank company and one bank show a graph, evidence tags, and a metrics row; I show you the numbers *and* the `tagging_rate`.
- **C3 after step 6:** first golden run, with cost and wall-clock reported, before any scheduling.
- **Decision gate (2–3 weeks later):** your call, using the criteria in Section 9, on investing in P1.

**Deploy policy:** local build and verification at every checkpoint. Lightsail deployment only when you say so for that release; M2 is the natural first candidate. Neon schema changes are additive and are applied by the deploy that needs them (or on request earlier); existing code ignores the new columns, so applying them ahead of a deploy is safe.

---

## 8. Risks specific to the MVP

| Risk | Likelihood | Mitigation |
|---|---|---|
| Models ignore the step-tagging instruction, so edge metrics are thin | Medium | `tagging_rate` is reported; fallback is a structured-output schema in a follow-up, not hiding the metric |
| `chain_steps` are too coarse or inconsistent to be meaningful edges | Medium | Graph is investigation-level only; Checkpoint C2 review of real graphs decides whether P1.1 needs a richer generator format |
| Golden aliases overfit the wording of one run | Medium | Aliases cover concepts, not phrases; unmatched presented edges are listed for review; `v2` rather than silent edits |
| Golden runs are expensive | Certain | On-demand only; first run measures cost; no schedule until you approve |
| New code breaks the working investigation path | Low-medium | Wrapped, never fails the investigation; setting to disable; existing test suite must stay green; no change to verdict logic |
| Feedback is sparse | Medium | Controls are low-friction and optional; the MVP does not depend on volume, only on capturing what exists |
| Dual-backend drift | Medium | Every schema/repo change lands in both twins with tests, per repo norm |

---

## 9. Success criteria at the decision gate

Invest in P1 only if most of these hold after 2–3 weeks of real use:

1. **Metrics discriminate:** across at least ~15 real investigations, `evidence_coverage` and `unsupported_edge_rate` vary meaningfully and the best and worst investigations by those metrics agree with your own judgement.
2. **Tagging works:** `tagging_rate` ≥ ~60%, or a credible path to it.
3. **Golden separates quality:** a deliberately degraded run (for example evidence retrieval disabled) scores clearly lower than a normal run on recall and coverage.
4. **Feedback is used:** feedback exists on a meaningful share of investigations you or others view, and the taxonomy fits (few "other"-style comments).
5. **No regression:** latency and cost per investigation are within ~10% of the pre-MVP baseline (measured in M7's first week).

If 1–3 fail, the finding is that the investigation structure (chain steps) is too coarse; the next step is then a richer hypothesis-graph generator, not more infrastructure.

---

## 10. Definition of done (MVP)

- M1–M9 complete, full test suite green (currently 1,235 passed, 30 skipped), new tests for every new pure function.
- Both schemas updated; SQLite fresh install and Neon apply verified.
- A real regenerated investigation yields two artifact versions, a graph, a metrics row, and accepts feedback.
- One golden run completed and reported with cost.
- This document and the parent plan updated with what was learned (metric definitions, any deviations).
- Nothing deployed without your explicit instruction.

## 11. Explicitly deferred (stays in the parent plan)

Causal Knowledge Service, durable assertions and lifecycle, ontology and sector packs, gated traversal, contradiction/confounder passes, deterministic confidence, validation automation, adaptation engine, adaptive ranking, admin analytics tab, reviewer workflow.

## 12. First action on your go-ahead

Start with **M2 + M1** (about 2–4 days): they are independent, low risk, and fix the artifact-overwrite issue. I'd report at checkpoint C1 before touching schemas.

---

## 13. Implementation status (2026-10-02)

Built and tested locally (1,257 tests passing, 30 skipped); **not deployed, Neon schema not yet applied, no real investigation run yet.**

| Task | Status | Notes / deviations from the plan |
|---|---|---|
| M1 version registry | Done | `config/versions.py` |
| M2 versioned artifacts | Done (reduced value) | New keys `investigations/<id>/v1/artifact.json` plus `graph.json`, `metrics.json`. **Finding:** the "overwrite on regeneration" risk does not occur in normal flows: each run uses a fresh uuid and `save_investigation` INSERTs, so a duplicate id would raise. The versioned layout is still in place for the companion files and for future versions |
| M3 schema | Done | Additive, both schemas + SQLite migration |
| M4 repositories | Done | New module pair `storage/causal_repository(.py/_pg.py)` swapped by `backend_bootstrap` (not via `FactStore`, which would have touched three files for no benefit). The Postgres twin is untested against a live database |
| M5 evidence tags | Done | Evaluator prompt/parser emit optional `link`; out-of-range or missing values stay hypothesis-level |
| M6 graph builder | Done | `research/investigation_graph.py`, wired into `_persist`, behind `CAUSAL_GRAPH_ENABLED`; failures are logged and never fail an investigation |
| M7 metrics | Done | `research/investigation_metrics.py`; cost/tokens joined from `llm_call_log` when available |
| M8 feedback | Done | `POST/GET /investigate/<id>/feedback`, controls in the deep-dive report (signed-in viewers, graph present). Light rate limit; admin feedback is tagged `internal` |
| M9 golden mini-set | Done, **drafts** | 5 cases in `research/golden/v1/` (3 bank cases, Maruti, Tata Steel), deterministic matcher, `scripts/run_causal_eval.py` (`--list`, `--score-existing`, full run). **Deviation:** not registered as a scheduled job (the scheduler has no enabled/manual-only flag), CLI only, on purpose. Cases are unreviewed drafts until a named reviewer fills `reviewed_by` / `reviewed_on` |

Next, each needing your go-ahead because it touches shared systems or spends money: (1) apply the additive schema to Neon, (2) run **one** real investigation locally to verify the graph, tags and metrics on real data (checkpoint C2), (3) a first golden run with its cost reported.

## 14. Measured results (2026-10-02, Maruti golden case, n = 1 per row)

| Run | Evaluation model | Cost | Hypothesis verdicts | Edges presented | Evidence coverage | **Tagging rate** | Golden recall (essential) |
|---|---|---|---|---|---|---|---|
| 1 | Sonnet (pre-change default) | $0.48 | 5 partial, 1 insufficient | 15 | 87% | **88%** | 2 / 4 |
| 2 | Haiku | $0.15 | 2 partial, 3 insufficient, 1 refuted | 6 | 50% | **25%** | 0 / 4 |
| 3 | Haiku + tightened tagging prompt | $0.16 | 2 partial, 2 insufficient, 2 refuted | 6 | 67% | **33%** | 0 / 4 |

Reading, with the caveat that each row is one stochastic run with different generated hypotheses (so differences are indicative, not statistically established):

- Haiku cuts the run cost by about 68%, as intended. `CAUSAL_EVALUATION_MODEL` stays `claude-haiku-4-5` by owner decision.
- Haiku tags evidence to causal links far less often (25–33% against 88%), so edge-level metrics are weaker under Haiku. A tighter prompt moved it only from 25% to 33%.
- Haiku is also harsher on verdicts (more INSUFFICIENT_EVIDENCE / REFUTED), so fewer edges are "presented" and recall against the golden edges is lower. Golden recall of 0 is partly the draft aliases and partly this.
- Golden aliases are unreviewed drafts and too narrow: both Maruti runs presented sensible edges (SUV mix, fixed-cost absorption, price realisation) that match no alias.

Options that keep Haiku (not yet built; each needs a go-ahead):
1. **A narrow tagging pass:** after evaluation, one short Haiku call that only assigns each evidence item to a link. Estimated $0.01–0.02 per investigation; keeps the evaluator unchanged.
2. **Deterministic tagging:** match an item's label/value to chain-step labels by keyword overlap. Free, but crude.
3. **Accept the low rate:** keep reporting `tagging_rate` and treat Haiku edge metrics as low-confidence.

Test rows from all three runs were deleted from Neon (the eval-run rows remain as a record). An earlier Haiku attempt hung for about 48 minutes between two model calls and was stopped; the cause was not diagnosed.

## 15. Link-tagger proof of concept (2026-10-02)

`research/link_tagger.py` assigns evidence to causal links in one small call per hypothesis on Jev's model chain (`JEV_CLASSIFIER_MODEL_CHAIN`, via `route_explicit_chain`; the free-tier Gemma first, Haiku as fallback; both were used). `scripts/tagger_poc.py` compares it with the evaluator's own tags. Not wired into the pipeline.

Maruti case, Haiku evaluation, 48 evidence items across 6 hypotheses:

| | Items tagged |
|---|---|
| Evaluator (Haiku) | 19 (40%) |
| Tagger | 13-14 (27-29%), 12 of its 13-14 agreeing with the evaluator |

Cost: 6 calls, about 4,500 input and 700 output tokens, under $0.01 even if all on Haiku (Gemma is free-tier).

Spot-check of the 13 tags from the first run (judged by me against the chain steps; **not** an independent expert review): about 9 clearly right, 3-4 defensible but ambiguous (e.g. "maximised production despite shortage" tagged to the procurement-cost link; "infrastructure investment" tagged to the cost-base link), and none clearly wrong. Where the tagger left an item untagged but the evaluator tagged it (6 items), most were generic financial evidence (ROA/ROE, net profit growth) that bears on the hypothesis as a whole, so null is arguably right; one clear miss (green-vehicle penetration to link 0).

Findings that change the plan:
1. **Tagging rate is the wrong target.** Most untagged evidence is generic (profit CAGR, ROA, ROE) and correctly belongs to no single link. A higher rate can mean over-tagging (Sonnet's 88% may include some). The metric should count evidence that supports a *specific link*, judged for correctness, and be validated against a labelled sample.
2. **The tagger is not the bottleneck; evidence retrieval is.** Across 6 hypotheses almost no retrieved evidence concerns the mechanisms themselves (steel prices, material cost, discounts, interest rates to demand). Edges stay "unsupported" because link-specific evidence is never fetched, not because it was mis-tagged.
3. The tagger roughly matches the evaluator where both tag and is cheaper than a Sonnet evaluation, but offers no gain in tagging rate here, so it is not worth wiring in yet.

Recommendation: do not wire the tagger in. Next, pursue link-targeted evidence retrieval (query per chain link using the macro and company series on file) and redefine the metric. Test rows from this run were deleted from Neon.
