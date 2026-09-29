# ADR-023 — Jev: LLM-Based Complexity Classification and Execution Routing Policy

**Status:** Accepted
**Date:** 2026-09-28

## Context

Signal answers research questions that range enormously in what they actually require:

- a single reported number ("What was HDFC Bank ROE in FY2025?");
- a reported number plus one deterministic calculation ("HDFC Bank's 5-year profit CAGR?");
- interpretation of a single company's own data ("Analyze HDFC Bank's profit growth over the last five years.");
- a real comparison against another dataset ("Compare HDFC Bank credit growth with the Indian banking industry.");
- hypothesis generation and causal reasoning ("Why has HDFC Bank's credit growth diverged from the banking system, and what factors are driving it?").

Before this ADR, every question reaching `research/assistant.py::answer_question` went through the same evidence-gathering + single LLM call shape, with `llm/hardness.py` picking one of three MODEL tiers (quick/standard/deep) for that one call — a genuine, useful mechanism, but one that answers "how strong a model does this need," not "does this question need a calculation at all, a comparison dataset, or a full hypothesis investigation." Those latter questions already had real, separate execution paths built for other reasons — `financials/calculations.py` for deterministic math, `research/investigation.py`'s hypothesis pipeline (ADR-007) for causal research — but nothing decided, up front, which path a given question should actually take. `llm/hardness.py`'s own comments already named the five-level vocabulary this ADR formalizes (`TIER_LEVEL`'s comment: "a stand-in for the prompt's LEVEL 0-5 vocabulary — this app only ever needs three practical buckets") as a compression that a real routing policy would eventually replace.

Two consequences of not having that policy:

1. A simple lookup ("What was net profit in FY2024?") always cost a real LLM call, even though the answer is one row in `canonical_financials` — no reasoning is involved at all.
2. A comparison question ("compare X to its peers/industry") had no dedicated grounding step — `research/assistant.py::gather_evidence` only ever pulls evidence for the company_ids it's given; nothing resolved "the industry" into concrete peer companies, so the LLM either had to invent a comparison or decline to make one.

## Decision

Signal adopts the Signals Complexity Classification and Execution Routing Policy: every research question is first assigned a Complexity Level (1–5), which determines its execution path, its data sources, and whether an LLM is used at all. (Not to be confused with ADR-005's "Level 1–4" evidence-provenance hierarchy — reported fact → canonical value → derived metric → interpretation. That's a classification of *evidence*; this is a classification of *questions*. Same word, deliberately unrelated numbering scheme, easy to conflate at a glance.)

```text
Question
   ↓
Jev (llm/complexity.py) — classify complexity, level 1-5
   ↓
research/routing_policy.py::route_question() — dispatch by level
   ↓
Level 1   Neon (canonical_financials) -> Answer                          (no LLM)
Level 2   Neon -> financials/calculations.py -> Answer                   (no LLM)
Level 3   Neon/Docs/Macro/KG evidence -> configured LLM -> Answer         (single dataset only)
Level 4   peer/macro grounding -> evidence -> configured LLM -> Answer    (comparison, capped)
Level 5   research/investigation.py's existing hypothesis pipeline       (ADR-007, unchanged)
   ↓
llm/routing_audit.py — one signals_routing_log row per question
```

### Jev

Jev (`llm/complexity.py::classify_complexity`) is an LLM call, not deterministic code — see the ADR-006 addendum for why classification specifically is exempted from that ADR's "prefer deterministic code" invariant. Jev does only classification: it returns `(level, confidence, reason)` and touches nothing else — no dataset selection, no retrieval, no answering. Its model chain is configured, never hard-coded (`config.settings.JEV_CLASSIFIER_MODEL_CHAIN`: the configured OpenRouter free-tier model first, a configured Anthropic model as fallback), run through `llm.router.route_explicit_chain` — a new sibling to `llm.router.route()` that tries an explicit, operator-ordered model chain instead of deriving one from a hardness tier. If every configured model is unavailable, classification falls back to a fixed Level 3 default (never Level 1 — see that function's own docstring for why erring toward *more* rigor is the safe default) rather than failing the question outright.

### Levels 1–2 — deterministic, no LLM in the answer path

`research/routing_policy.py::_level1_retrieve` / `_level2_calculate` match the question against the closed `metrics_dictionary` vocabulary (`storage.repositories.list_all_metrics`, widened by a small alias table for common abbreviations like "ROE"/"NIM") plus explicit fiscal-year/operation wording (`FY2025`, `CAGR`, `YoY`, `N-year`). When extraction can't confidently resolve a single metric, period, and (for Level 2) operation, the level escalates to the next one rather than guessing — never inventing missing data, per the policy's own Level 1 rule, generalized as the escalation path's safety net. Escalation also fires when extraction succeeds but the *data* doesn't exist (no `canonical_financials` row for the period, or a CAGR/YoY calculation missing one of its input years) — same Level 3 ceiling, see the 2026-09-29 addendum below. Levels 1 and 2 never call an LLM to produce the answer itself.

### Level 3 — interpret, single dataset

Reuses `research/assistant.py`'s evidence-gathering (`gather_evidence`) and evidence-citation system prompt (`SYSTEM_PROMPT`, `[FACT]`/`[CALCULATION]`/`[MANAGEMENT_STATEMENT]`/`[INFERENCE]` tagging), with an added instruction to stay within the retrieved evidence and never introduce an external comparison. Routed through `config.settings.LEVEL_MODEL_CHAIN[3]`, not `research/assistant.py::answer_question`'s own tier auto-routing — Level 3/4 need the "configured model, explicit Anthropic fallback" chain shape this policy specifies, not the reasoning-strength-gated chain `llm/hardness.py`/`llm/router.py::route()` already provide for other call sites.

### Level 4 — compare, grounded

`research/peer_resolver.py::resolve_comparison_group` interprets ambiguous comparison language ("industry", "peers", "the market", "benchmark") by grounding it against the anchor company's own sector classification (`companies.basic_industry`/`macro_economic_sector` — the same columns `context/graph.py`'s existing sector-peer traversal uses, Neo4j-backed when `GRAPH_BACKEND=neo4j`, SQLite otherwise), capped at `config.settings.MAX_COMPARISON_DATASETS` (2) unless the question explicitly asks for broader scope. This grounding step is deliberately deterministic, not a second LLM call — the anchor company's sector is already a known, closed value once `company_ids` names it, so ADR-006's "prefer deterministic code" still governs the *grounding* step even though Jev already used an LLM to decide the question needed Level 4 at all. A macro benchmark (e.g. comparing growth to a repo rate) is grounded through `research/macro_evidence.py`'s existing LLM-based series planner, already invoked generically by `gather_evidence` for every level — not duplicated here. When no peer/benchmark can be grounded, Level 4 states the limitation in its answer and audit record rather than inventing one, per policy.

### Level 5 — hypothesis / causal research

Unchanged: `research/investigation.py::run_investigation` (ADR-007's hypothesis-generate → plan-and-gather → evaluate → synthesize loop, itself already grounded through the knowledge graph and bounded by `MAX_EVIDENCE_ITERATIONS`/`INVESTIGATION_TIMEOUT_SECONDS` — ADR-018's investigation-budget governance). `research/routing_policy.py` only renders its result into a narrative answer and folds its evidence/verdict trail into the audit record.

### Audit logging

Every `route_question()` call writes one row to `signals_routing_log` (`llm/routing_audit.py`, schemas in both `schemas/sqlite_schema.sql` and `schemas/postgres_schema.sql`): the question, Jev's level/confidence/reason, the model(s) used, data sources accessed, whether Neo4j/a planner ran, calculations performed, evidence identifiers, missing-data issues, final confidence, execution status, latency, and token/cost figures — everything section 5 of the policy asks for, with no chain-of-thought stored, only observable actions and outputs. This is a separate table from `llm_call_log` (`llm/observability.py`): that table logs one row per individual model call; this one logs one row per routed question end-to-end, so a future eval runner can ask "what did Signals do for this question, and did it match its complexity level's expected behavior."

## Model configuration

No model name is hard-coded into `llm/complexity.py` or `research/routing_policy.py` — every model choice reads from `config.settings` (`JEV_CLASSIFIER_MODEL_CHAIN`, `LEVEL_MODEL_CHAIN`), exactly like the existing `TIER_PREFERRED_MODEL`/`TIER_FALLBACK_CHAIN_OVERRIDE` (ADR-010). Level 5 is deliberately NOT given its own model-configuration surface here: it already routes through the existing tier-based system via `research/investigation.py`'s own calls, and giving the same underlying model calls two independent configuration knobs would only create drift between them.

## Alternatives considered

### Extend `llm/hardness.py`'s 3-tier regex classifier to 5 levels

Keeps everything deterministic (no ADR-006 exception needed), but the underlying problem — free-text phrasing with no closed vocabulary — doesn't go away just by adding more regexes; it only grows the list of hand-picked trigger phrases that fail on anything phrased differently, with silent misclassification as the failure mode rather than a visible one.

### Route every question through a single planner/agent loop

Rejected for the same reason ADR-011 (modular monolith before microservices) and the existing "no orchestrator/planner agents" position reject it: an unbounded agent loop for a question that's really a one-row lookup is strictly worse on cost, latency, and reproducibility, and reintroduces exactly the unbounded-loop risk ADR-018's budget governance was written to avoid.

## Consequences

### Positive

- a plain factual lookup costs zero LLM calls instead of one;
- comparison questions get a real, grounded peer/benchmark set instead of an ungrounded or declined comparison;
- every routed question is now audit-logged end-to-end, independent of the per-call `llm_call_log`;
- model routing for the new levels is config-driven, consistent with ADR-010.

### Negative

- a second classification pass (Jev) runs before every question, adding one small LLM call plus latency to every request that isn't already deterministic;
- two audit tables (`llm_call_log`, `signals_routing_log`) exist with no shared `run_id` linking a routed question to its individual model calls yet;
- Level 3/4's evidence-gathering and prompt-construction duplicates a slice of `research/assistant.py::answer_question` rather than sharing it outright, since that function's own tier auto-routing and model-pinning semantics don't fit this policy's explicit-chain-with-fallback requirement.

## Addendum (2026-09-28) — live web app: dispatch replaces the manual Quick Answer/Deep Dive toggle

`research.html`'s composer previously let the user manually pick "Quick answer" vs "Deep dive" (`/research/understand` only *suggested* one via `llm/hardness.py`'s 3-tier classifier, which the user could override before submitting). That toggle is removed. `/research/understand` now calls `research/routing_policy.py::classify_and_log` (Jev) and returns `complexity_level`/`complexity_label`/`case_type` instead of `suggested_case_type`/`suggestion_reason`; the composer shows the level as a live preview pill while typing, and re-classifies fresh, authoritatively, at submit time (`case_type` — `"ask"` for Levels 1-4, `"investigation"` for Level 5, via `case_type_for_level()`) to decide whether to POST to `/research/ask-async` or `/investigate/generate-async`. Both endpoints, and the synchronous `_answer_question_response` path they share with `/chat`/`/companies/<id>/ask`, now classify (or reuse an already-classified level) and store it as `complexity_level` on the resulting `research_cases`/`generated_reports`/`investigations` row.

This is UI-level dispatch only — it decides which of the two EXISTING pipelines (`research/assistant.py::answer_question` vs `research/investigation.py::run_investigation`) runs, not a swap-in of `route_question()`'s own Level 1-4 deterministic/grounded execution paths for live traffic. `route_question()` remains available (CLI: `python main.py route-ask`) as the fuller reference implementation of the policy; wiring it in as the live execution engine for Levels 1-4 (replacing `answer_question()`'s always-LLM path with the deterministic Retrieve/Calculate short-circuits and Level 4's peer grounding) was a natural follow-up, not done at the time of this addendum — the Level 1/2 half has since shipped; see the "Levels 1-2 fast path" addendum below.

Every research item across the Cases (`/investigations`) feed — in-progress cases, completed Quick Answers, completed Deep Dive investigations — is now tagged with its Jev level (`"Level 3 · Interpret"` etc., falling back to the old generic "Quick Answer"/"Deep Dive" label for a pre-Jev row with `complexity_level=NULL`), and the feed gained a Level filter (`iv_level`) alongside the existing Type/Status filters.

## Addendum (2026-09-28) — the eval runner section 5 anticipated now exists

`scripts/run_signals_eval.py` is the "future eval runner" the Audit Logging section above and this document's original text pointed at: a small, versioned golden question set (`research/signals_eval_cases.py`, spanning all 5 levels, reusing the exact companies/questions already verified against real ingested data in `docs/SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`) run periodically through the real `route_question()`, comparing Jev's classified level against each case's expected level. A mismatch is recorded as a normal `batch_job_items` failure via `ingestion/batch_log.py`'s established per-item audit convention — the same mechanism every other recurring job in this app already uses — so Jev's accuracy over time is answerable from Audit Log → Job Runs, not just a one-off manual check.

Registered in `scheduling/jobs.py`'s `SCHEDULED_JOBS` (`job_id="signals_eval"`, category "Evals", cadence "Weekly"), which makes it reachable identically via `python -m scripts.run_job signals_eval`, the Settings → Schedule panel's "Run now" button, and the cron-triggered endpoint — no new scheduling mechanism, reusing ADR-015's existing "scheduler owns timing, job owns logic" seam. Real recurring LLM spend (3 of the 11 cases run the full Level 5 investigation pipeline) — the weekly cadence is a starting judgment call, not a fixed requirement.

## Addendum (2026-09-28) — Eval Analytics admin panel

Settings → Administration → System now has an "Eval Analytics" panel (`_eval_analytics_panel_context()`, `web/app.py`; markup in `web/templates/settings.html`), the trends layer the two addenda above made possible but didn't yet surface anywhere. It aggregates the same two sources described above rather than adding a third:

- `signals_routing_log` (every real routed question, not just eval traffic) — grouped by Jev level into a volume-by-level chart plus a table of avg latency/cost/confidence and answered/insufficient-data/error counts per level, over a selectable 7/30/90-day/all-time window. This is the "how is Signals actually being used, and what does it cost" half — the input a model-routing tuning decision (e.g. moving a level to a cheaper/faster model chain in `config.settings.LEVEL_MODEL_CHAIN`) needs.
- `batch_job_runs`/`batch_job_items` for `job_name="signals_eval"` — parsed via the same stable `"expected=L{n} actual=L{n} ..."` detail-string format `scripts/run_signals_eval.py` writes, into a per-level matched/total bar chart for the latest run and a pass-rate trend line across recent runs. This is the "is Jev's classifier actually accurate, and is it getting better or worse" half.

Audit Log → Job Runs already lists `signals_eval`'s raw run/item history (any `BatchRun`-wrapped job does, by construction) — this panel deliberately doesn't repeat that list, only the aggregation across it, reached via a link from the panel's own intro text rather than duplicated inline.

Charts are hand-rolled SVG (`web/static/js/eval_analytics_charts.js`), following this app's existing no-charting-library convention (`web/static/js/charts_overlay.js`) and the `dataviz` skill's procedure: complexity level is an *ordinal* encoding (one hue, monotone light→dark, `--eval-level-1..5`), matched/mismatched is a *status* encoding (the skill's fixed good/critical pair, `--eval-status-good/critical`, always paired with a legend + label, never color alone) — both validated via the skill's `validate_palette.js` (`--ordinal`, light surface and this app's own `dark`/`signals` theme dark surfaces). Every chart ships a "View as table" fallback rendered server-side from the same data, not just a JS-only presentation.

## Addendum (2026-09-29) — Levels 1-2 fast path is live; confidence labelling for non-LLM answers

The Level 1/2 half of the follow-up above is now live. When Jev classifies a single-company question as Level 1 or 2, `web/app.py::_answer_question_response` calls `research/routing_policy.py::attempt_deterministic_level()` before `answer_question()`. That function never re-classifies (it reuses the level already decided), runs `_level1_retrieve`/`_level2_calculate` (via the shared `_dispatch_levels_1_2()` helper — see the escalation addendum just below), and returns the deterministic answer with no LLM call. If both levels escalate (or the question isn't single-company) it returns `None` and the existing `answer_question()` path runs unchanged. A real Level 1/2 outcome writes a `signals_routing_log` row and an Execution Analytics run (`signals_fast_path`); an escalated attempt writes only the Execution Analytics row (status `escalated`), so `signals_routing_log` never records an answer that a different path actually produced.

Still NOT live: Level 3/4 via `route_question()` (including Level 4 peer grounding) — live "ask" traffic at Levels 3-4 still runs `answer_question()`. `route_question()` remains the CLI reference implementation (`python main.py route-ask`).

**Confidence labelling.** A Level 1/2 answer is a fixed template, so it has no `**Confidence:**` line and `_level1_retrieve` sets no `final_confidence`. The UI previously parsed that absence as "Unknown confidence", which read as doubt about a value read straight from `canonical_financials`. `web/app.py::_confidence_tag` now labels these by how they were produced — "Direct lookup · reported data" (Level 1) and "Calculated from reported data" (Level 2), with matching Research-list filter keys — and keeps "Unknown confidence" only for an LLM answer that omitted its confidence line. `signals_routing_log.final_confidence` is still NULL for Level 1/2 rows, so Execution Analytics' avg-confidence column doesn't count them.

## Addendum (2026-09-29) — escalate on missing data, not just unparseable questions

The escalation path above (`_level1_retrieve`/`_level2_calculate` returning `None`) only covered "couldn't confidently parse the question" — an ambiguous metric, no fiscal year, no explicit CAGR window. It did *not* cover the case where parsing succeeds but the data doesn't: a metric the vocabulary recognizes but `canonical_financials` has no row for in the requested period, a CAGR/YoY calculation missing a prior fiscal year (`financials/calculations.py` raising `CalculationError`/`MissingDataError`), or a derived ratio (ROE/ROA, `financials/ratios.py`) that couldn't be computed from what's on file (`MissingDataError`/`SectorMismatchError`). Previously all three returned a terminal `LevelOutcome(execution_status="insufficient_data")` — "No reported X on file", "Could not calculate X CAGR: ...", or "Could not calculate X: ..." — as the final answer.

`_dispatch_levels_1_2()` — the shared helper both `route_question()` and `attempt_deterministic_level()` (the live fast path, addendum above) call — now treats that outcome the same way it treats an unparseable question: escalate to the next level (same ceiling — Level 1/2 escalation stops at Level 3, never jumping to Level 4/5, which solve a different problem shape) rather than returning it as final. Because the check is on `execution_status`, not which branch produced it, this covers CAGR/YoY and the ROE/ROA derivation uniformly — no per-calculation-type escalation logic needed. The reasoning: Level 3's evidence-gathering (`gather_evidence`) pulls from documents and macro series too, not just a single `canonical_financials` lookup, and a partial series (e.g. two of the five years a CAGR needs) is still useful context for an LLM even when it isn't enough for a deterministic formula — so a higher level may still produce something worth reading, or at minimum explain the gap more usefully than a bare "not on file." The discarded Level 1/2 outcome's own message isn't wasted either: its `missing_data_issues` reason is folded into the escalation note in the audit trail (`signals_routing_log.missing_data_issues`), so "why did this escalate" stays answerable even though the terminal answer came from a higher level. On the live fast path specifically, this escalation is one more reason `attempt_deterministic_level()` returns `None` and falls through to `answer_question()` — indistinguishable, from that caller's side, from any other escalation.

## Revisit when

- `run_id` could be threaded into `llm/observability.record()` so `llm_call_log` rows join back to their `signals_routing_log` row directly;
- Level 3/4's evidence-gathering could be unified with `research/assistant.py::answer_question` if that function grows explicit-chain routing of its own;
- Level 4's peer grounding could gain its own bounded LLM refinement step (today it's fully deterministic) if sector-field matching turns out to be too coarse for some question shapes;
- the eval set (`research/signals_eval_cases.py`) should grow alongside real observed misclassifications, not stay frozen at its initial 11 cases — every production mismatch is a candidate new eval case;
- the Eval Analytics panel's level-breakdown query reads `signals_routing_log` with `limit=5000` and filters in Python — fine at today's volume, but a real `WHERE created_at >= ?` pushed into SQL (mirroring `list_batch_job_runs`'s own `since_iso` parameter) is the natural fix once routed-question volume grows past that.
