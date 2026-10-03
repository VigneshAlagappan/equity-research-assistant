# L5 causal engine: deployment, wiring and empirical validation

Run date 2026-10-02/03. Scope: deploy the persistent causal graph (Neon + Aura), wire dynamic causal-chain traversal into the L5 investigation pipeline, run a small controlled real-data set, measure, classify failures, stop. No graph was expanded to make a case pass; no LLM was added.

## A. Deployment

| Item | Result |
|---|---|
| Baseline | 1384 tests passed before deployment; 1395 passed after wiring. Persistent edges unchanged by investigations is asserted by tests (in-memory) and by a before/after snapshot of every edge on real Aura. |
| Neon | Only the already-designed DDL applied (4 tables: `causal_graph_events`, `_evidence_refs`, `_validation_refs`, `_feedback_refs`; 5 indexes + 4 primary keys). Row counts of `companies`, `canonical_financials`, `macro_observations`, `investigations`, `causal_feedback`, `l5_investigation_metrics` identical before and after. Re-applying is a no-op. |
| Aura | 7 uniqueness constraints (one per node family) and 10 relationship `edge_id` indexes created via `ensure_schema()`. Seed `cg-seed-v1`: **28 nodes, 32 edges** (spec asked 23+/25+). Re-running the seed added nothing. Legacy graph untouched: every legacy label count is identical except `Company` 133 -> 134 (TATASTEEL did not exist); legacy relationship types that share names with causal ones (DRIVES, EXPOSED_TO, DEPENDS_ON, SUPPLIES) grew only by the seeded causal edges, which are separated by `layer='causal'` and the `:CausalNode` label. |
| Relationship types | 8 of the 10 canonical types appear in the seed (AFFECTS, BELONGS_TO, DECREASES, DEPENDS_ON, DRIVES, EXPOSED_TO, INCREASES, SUPPLIES). CONSUMES and FINANCES are supported and indexed but not seeded. |
| Integration test | `scripts/causal_graph_integration_check.py` against **real Neon + real Aura**: 25/25 checks (node/relationship retrieval, drivers, dependents, upstream/downstream traversal incl. auto-financing, competition and commodity chains, sector mechanisms, company exposures, cross-sector, bounded traversal, duplicate node/edge rejection, Aura-enforced uniqueness, evidence/validation/feedback references round-trip through Neon). The reference test rows were tagged and deleted; no edge changed. One check initially failed because my expectation was wrong (Banking is not upstream of the *Auto sector node*; it reaches auto margin through Auto Financing Cost); the check was corrected, the graph was not. |

## B. Pipeline

- **Where:** `research/investigation.py::_run_investigation_impl`, after the evaluation loop and the link-evidence / gap-fill passes, before synthesis. L1-L4 do not import it (a test asserts this).
- **New:** `research/causal_chain_stage.py` (failure-soft wrapper: statuses `ok | skipped | unavailable | no_target | error`), `reports/components/dynamic_chain.html` (report section), `scripts/run_causal_chain_validation.py`, `scripts/causal_graph_integration_check.py`.
- **Reused:** `CausalKnowledgeService`, `build_dynamic_chain`, `SqlHistory`, existing artifact persistence (the chain is stored as `dynamic_chain` in the investigation artifact and as `dynamic_chain.json`), `hybrid_search_documents` for narrative search, `make_loader`/concepts from `link_evidence`, `investigation_id` (so ids are shared), existing `/investigate/<id>` page.
- **Config:** `CAUSAL_CHAIN_ENABLED` (default on, independent of `GRAPH_BACKEND`), `CAUSAL_CHAIN_ATTACH_EVIDENCE` (default **off**: an investigation leaves nothing on the persistent graph's evidence record), plus the `CHAIN_*` limits.
- **Tests added:** `tests/test_causal_chain_stage.py` (stage on/off, persistent graph and references untouched, attach switch, empty graph, missing target, missing company, Neo4j unreachable, Neon failure, report rendering, L1-L4 isolation), two pipeline tests in `tests/test_investigation.py`; `tests/conftest.py` now disables the stage by default so tests never reach the real Aura named in `.env`.

## C. Real investigations (standalone chain stage, real Neon + Aura, $0 LLM)

All eight ran through `run_causal_chain_stage`, the exact function the pipeline calls, with evidence attachment off.

| # | Case | Target found | Candidates / tested / retained | Result |
|---|---|---|---|---|
| 1 | Maruti operating margin "decline" FY23-FY26 | Operating Margin (correct) | 7 / 7 / 1 | **Premise false**: stored data shows margin *up*. Every cost/demand path was "contradicted" under the false premise; the one retained path (Competitive Intensity -> Pricing Pressure -> Margin) is UNRESOLVED (no data). Real mechanism visible in data: material cost share 73.4% -> 72.3%. |
| 2 | Maruti vehicle volume change | Vehicle Volume (correct) | 3 / 3 / 2 | Two cross-sector paths via Banking -> Auto Financing Cost retained as SUPPORTED, but the support is **circular** (see below). |
| 3 | Tata Steel operating margin "decline" | Operating Margin | 4 / 4 / 1 | Premise false again (margin up). Paths retained/rejected are auto-oriented (Iron Ore -> Steel -> **Auto** -> ...). No steel-specific mechanism exists. |
| 4 | HDFC Bank net interest margin change | **Wrong**: Operating Margin ("margin" synonym); no NIM node | 4 / 4 / 1 | All candidates are auto/steel/commodity chains for a bank. 18 false narrative contradictions. |
| 5 | HDFC Bank revenue growth | Revenue | 1 / 1 / 0 | Only path (repo -> lending rate -> credit demand -> loan growth -> revenue) "contradicted": repo rose 4.00 -> 5.25 while revenue grew. |
| 6 | IDFC First Bank revenue growth | Revenue | 1 / 1 / 0 | Same as 5. |
| 7 | RBI repo rate -> banking sector revenue (no company) | Revenue | 3 / 3 / 2 | Retained paths include the **US** Fed-funds chain for an Indian question; nothing testable (no direction in the question). |
| 8 | JPMorgan revenue growth FY22-FY25 | Revenue | 2 / 2 / 0 | JPM is not in the graph (no sector context); both paths "contradicted" because US rates rose while revenue grew. |

### Two full pipeline runs (existing L5 pipeline with the stage wired in)

| Run | Pipeline (existing LLM stages) | Chain stage | Result |
|---|---|---|---|
| Maruti "Why did operating margin *move* FY23-FY26" | 6 hypotheses, 14 model calls, $0.179, 226 s | status ok, 0 model calls, $0, 8.0 s | With a neutral question the engine took the direction from the data (margin up), so paths were tested on a true premise: 3 retained SUPPORTED: Iron Ore -> Steel -> Auto -> Material Cost -> Margin and Iron Ore -> Steel Price -> Auto -> Material Cost -> Margin (material cost share 73.4% -> 72.3% explains the rise), and the Banking -> Auto Financing -> Demand -> Volume path (circular revenue-proxy support, see WEAK_EVIDENCE). The Lending-Rate sibling is WEAK because repo rose. |
| IDFC First Bank revenue growth FY23-FY26 | 6 hypotheses, 9 model calls, $0.253, 442 s | status ok, $0, 3.5 s | One path, contradicted, 0 retained (same yield-channel gap as cases 5/6). |

Wiring verified end to end: the stage ran inside `run_investigation`, shared the investigation id, was persisted in the artifact as `dynamic_chain`, and left the existing hypotheses, verdicts and cost untouched. The two test investigations were deleted from Neon afterwards (including the `investigation_companies` child rows); the three pre-existing investigations remain.

Full per-case JSON (every hypothesis, finding, rejected edge, narrative passage) is reproducible with `python -m scripts.run_causal_chain_validation --out <file>`.

### Per-investigation evaluation (the chain, not the prose)

| Question | 1 Maruti margin | 2 Maruti volume | 3 Tata Steel | 4 HDFC NIM | 5/6 Bank revenue | 7 Macro->banking | 8 JPM |
|---|---|---|---|---|---|---|---|
| Target correct? | yes | yes | yes | **no** | yes | yes | yes |
| Context correct? | yes | yes | yes | sector yes, **target wrong** | yes | **no** (geography, sector) | partly (no sector) |
| Important paths found? | partly (cost chain yes; price/mix yes via competition) | yes | **no** | **no** | partly (demand channel only) | partly | partly |
| Irrelevant paths included? | no | no | **yes** (Auto) | **yes** (all) | no | **yes** (US chain) | no |
| Important driver missing? | pricing/mix, FX/royalty | supply, inventory | coking coal, steel price spread, demand | NII, funding cost | funding cost, deposits | n/a | funding cost |
| Important mediator missing? | - | - | steel price -> steel margin | lending yield | lending rate -> interest income | same | same |
| Ranking sensible? | mostly | mixed (see WRONG_PATH_RANKING) | no better option existed | no | n/a (1 path) | n/a | n/a |
| Lag respected? | not exercised | not exercised | not exercised | not exercised | not exercised | not exercised | not exercised |
| Evidence sufficient? | no (3 of 5 / 2 of 5 nodes unmeasured) | no (revenue proxy) | no | no | no (bank metrics unmeasured) | no | no |
| Contradictions identified? | numeric yes; narrative 0 true, 1+ missed | numeric yes | numeric yes | 18 narrative, all false | numeric yes (valid under the encoded edges); 12 narrative, all false | none | numeric yes |
| Weak hypotheses rejected? | yes | yes | yes | no (rejected on false narrative evidence) | n/a | n/a | n/a |
| Cross-sector found when needed? | yes (Steel, Banking) | yes (Banking) | wrong direction (Auto) | n/a | n/a | not needed | n/a |
| Economically sensible final explanation? | no (premise) | partly (circular) | no | no | no (incomplete) | no | no |

## D. Failure summary

Counts are failure instances across the 8 runs (a case can have several). Some failures are properties of the seed graph and the data, not of the engine.

| Failure type | Count | Where |
|---|---|---|
| MISSING_DATA | 8 (every case) | No Steel/Iron Ore/Auto Financing/Lending Rate/Vehicle Volume/Pricing Pressure series linked or stored; bank and US-financial company metrics are not measurable (`link_evidence` skips financials); Loan Growth/Credit Demand/Lending Rate nodes carry no reference to the RBI series that exist (`bank_credit`, `base_rate`, `weighted average lending rates`). |
| IRRELEVANT_PATH | 7 paths in 3 cases | Tata Steel: 2 paths through Auto; HDFC "NIM": 4 auto/steel/commodity/competition paths for a bank; macro question: US Fed-funds chain for an Indian question. Edges are sector-agnostic, so sector-specific chains reach unrelated companies. |
| MISSING_EDGE | 4 | The graph holds only the *demand* channel of rates (rate up -> credit demand down). It lacks the *yield* channel (rate up -> lending yield / NIM up -> interest income up), so rising-rate/rising-revenue banks (HDFC, IDFC, JPM) are "contradicted"; and no steel-sector margin mechanism. |
| WEAK_EVIDENCE | 2 | Maruti: Vehicle Demand and Vehicle Volume are both measured through the *same* revenue series (price and mix included), and counted twice (node finding + edge-consistency finding), enough to reach SUPPORTED. |
| FALSE_CONTRADICTION | 3 cases, 30 findings from 10 distinct passages | Narrative cue words: all 10 distinct matched passages were false (derivative-hedging accounting policy, mortgage "pass through certificates", "flat" inside inflation commentary). |
| MISSING_NODE | 2 | No Net Interest Margin metric node; JPM not in the graph. |
| WRONG_CONTEXT | 2 | HDFC NIM question resolved to Operating Margin (the synonym "margin" is not metric-specific); the macro question's geography ("RBI") and sector ("banking sector") were not inferred from the text. |
| MISSING_DRIVER | 2 | Banks: deposit/funding cost, merger effect. |
| MISSED_CONTRADICTION | 1 (possible) | Maruti call-transcript passages on realisations moving higher, mix, transfer pricing and precious-metal costs were retrieved but contain no cue word. Needs a human read to confirm they contradict a cost-driven story. |
| MISSING_PATH | 1 | Tata Steel: nothing from the commodity chain reaches steel margin. |
| WRONG_PATH_RANKING | 1 | Maruti volume: an edge with direction UNKNOWN (repo -> Banking) stops expected-direction propagation, so the rate contradiction vanishes; that path is SUPPORTED while its sibling through Lending Rate with the same repo premise is WEAK. |
| WRONG_LAG, OVERSTATED_EFFECT, UNDERSTATED_EFFECT, WRONG_EDGE | 0 observed | The lag filter was never triggered (all seed lags fit the 4-year windows) and no ground truth exists to judge effect size. These are untested, not passing. |
| **FALSE_PREMISE (proposed new class)** | 2 | Maruti and Tata Steel margin "declined" per the question, but stored data shows an increase. The graph, data and paths were not wrong; the *question's stated direction* was. This does not fit any listed class (WRONG_CONTEXT is about scope, not the user's claim), and it changes what the engine should do (answer the true movement, not test a false one). |

## E. Baseline metrics

| Case | Nodes expanded | Edges considered | Edges retained | Evidence coverage | Unsupported edge rate | Efficiency | Cross-sector edges retained | Contradictions | Runtime (ms) |
|---|---|---|---|---|---|---|---|---|---|
| auto_margin | 17 | 22 | 2 | 0.0 | 1.0 | 0.0 | 0 | 10 | 14,991 |
| auto_volume | 7 | 9 | 5 | 0.2 | 0.8 | 0.556 | 5 | 1 | 4,970 |
| steel_margin | 12 | 13 | 2 | 0.0 | 1.0 | 0.0 | 0 | 4 | 5,342 |
| bank_nim | 12 | 13 | 4 | 0.0 | 1.0 | 0.0 | 0 | 18 | 5,450 |
| bank_revenue_hdfc | 5 | 6 | 0 | n/a | n/a | 0.0 | 0 | 7 | 2,911 |
| bank_revenue_idfc | 5 | 6 | 0 | n/a | n/a | 0.0 | 0 | 7 | 3,303 |
| macro_banking | 7 | 6 | 5 | 0.0 | 1.0 | 0.0 | 0 | 0 | 1,935 |
| us_bank | 6 | 6 | 0 | n/a | n/a | 0.0 | 0 | 2 | 3,298 |

- LLM cost of the chain stage: **$0**, 0 model calls, in every run. Median runtime 4.1 s, max 15.0 s (dominated by narrative search).
- Hypothesis statuses over 25 tested paths: CONTRADICTED 13, UNRESOLVED 6, WEAK 4, SUPPORTED 2. Retained: 5 UNRESOLVED, 2 SUPPORTED (and the 2 SUPPORTED are the circular ones).
- Edge rejections: 11, all `scope_mismatch` (correct). No `lag_exceeds_window`, budget, branch-limit or materiality rejection fired on this seed.
- Efficiency definition: edges in retained SUPPORTED/PLAUSIBLE paths / edges considered. Evidence coverage: retained edges with at least one finding. Both are noisy at 0-5 edges per case; treat as a baseline, not a score.
- Cue-word narrative contradiction: 51 passages examined, 26 cue hits, 10 distinct passages, 0 true contradictions, 10 false; plus at least 1 probable miss. Retrieval was keyword-only (local Qdrant returns 403).

## F. Architecture findings

1. **Is the persistent graph sufficient for real L5 traversal?** Not yet, and the cause is breadth and shape, not mechanics. Traversal, bounding, scoping and references work on real Neon/Aura. But the graph has one channel per mechanism, only 3 sectors, and no way to say a chain applies only to some sectors.
2. **Is the seed sufficient for initial testing?** Sufficient to test the engine (it exercised every code path except lag and budgets); insufficient to answer real questions. Auto margin/volume was the only family with a meaningful chain. Banking has the demand channel only, steel has none.
3. **Is path ranking working?** Mechanically yes (best-first, deterministic, components visible, weak links penalised). The ranking has no sector-specificity: a chain valid for autos outranks nothing but also is not penalised for a bank. One real ranking defect (UNKNOWN-direction edges hide contradictions).
4. **Are cross-sector paths working?** Yes where the graph encodes them (Banking -> Auto Financing -> Vehicle Demand -> Volume was found, ranked and tested). The hop limit works; hops are also counted for irrelevant sectors (Auto for a steel company), which is correct accounting but wrong relevance.
5. **Is current macro data sufficient?** For rates yes (repo, fed funds, 10-year, crude all observed). For the chains tested, no: no steel/iron-ore/coking-coal price, no vehicle sales, no financing-cost series wired to the nodes even where RBI series exist.
6. **Largest data gaps:** (a) company metrics for banks/financials, which `link_evidence` deliberately skips (blocks every banking case); (b) commodity prices for steel/iron ore; (c) volume/price/mix for autos; (d) graph nodes not linked to existing macro series.
7. **Largest graph gaps:** the yield/NIM channel for rates; a steel-sector margin mechanism; a NIM metric node; sector scoping on edges; JPM and other companies; a few common drivers (deposits/funding cost).
8. **Where deterministic reasoning fails:** (a) cue-word narrative contradiction (0 of 10 true); (b) a false question premise makes every path "contradicted"; (c) target detection by synonym ("margin" matches the wrong metric); (d) proxies (revenue for volume) create circular evidence; (e) direction-UNKNOWN edges stop propagation.
9. **Does semantic/model reasoning appear necessary?** For narrative contradiction, yes: the baseline is 0/10 true with at least one clear miss. For target identification and premise handling, a model *or* better metadata could do it; both are cheap to try. The numeric path testing needs no model; it needs better data links.
10. **Next smallest improvement:** make the evidence honest before adding intelligence. Concretely: (i) stop counting one series twice / drop proxy-only support from SUPPORTED, (ii) add an explicit premise check that reports "the data shows the opposite" and answers the observed movement, (iii) require sector fit for sector-scoped chains. Each is deterministic, $0 and affects every case. Graph/data expansion (yield channel, steel mechanism, NIM node, bank metrics) should follow as a separate, deliberate step, not driven by individual cases.

## Notes and limits

- Eight standalone chain runs plus two full pipeline runs; small and hand-chosen. Two of the eight (Maruti and Tata Steel margin "decline") were my own mis-framed questions, which turned out to be a useful finding. Rewording the Maruti question neutrally ("move") produced a sensible, supported material-cost explanation, which shows how much the premise handling matters.
- Qdrant returned 403 locally, so narrative search was FTS5-only; semantic retrieval might change the narrative results.
- Failure counts are my classification and are open to review; MISSED_CONTRADICTION is the least certain.
- Nothing was written to the graph. Evidence attachment was off for the validation runs.

## Follow-up 2026-10-03: evidence-honesty fixes (step 1 of the recommendation)

Four deterministic, $0 changes, each aimed at a failure above. The same 8 real cases were re-run against live Neon + Aura; the graph, data and seed were not changed.

| Failure | Change | Effect on the real cases |
|---|---|---|
| WEAK_EVIDENCE (circular support) | One underlying series counts once however many nodes it stands for (a direct reading wins over a stand-in); an edge test between two nodes reading the same series is skipped; evidence from a stand-in series or a keyword hit in text counts half and can never reach SUPPORTED alone or condemn a path alone. | Maruti volume: the two "SUPPORTED" circular paths are now PLAUSIBLE (support weight 0.5). |
| FALSE_PREMISE | Premise status (CONSISTENT / CONTRADICTED_BY_DATA / FLAT / UNVERIFIED / NOT_STATED). When the data contradicts the question, paths are tested against the *observed* movement and the warning says so; a flat target is reported as "nothing to explain". | Maruti margin: now 2 retained SUPPORTED paths (Iron Ore -> Steel (Price) -> Auto -> Material Cost -> Margin; material cost share 73.4% -> 72.3%) instead of one untestable lead. |
| IRRELEVANT_PATH | Sector fit: a path through another sector must be anchored to the company (its own sector, or an edge scoped to it) and must not leave the company's sector for one downstream of it. | Tata Steel: both Auto-routed paths rejected (`leaves_company_sector_downstream`, `foreign_sector_without_company_link`). HDFC "NIM": 2 of 4 paths rejected; the other 2 are WEAK and none retained. Maruti keeps its Steel supplier and Banking financing chains. |
| WRONG_PATH_RANKING | A path containing an edge whose direction the graph does not state tops out at PLAUSIBLE and lists `untestable_edges`. | The Banking path with `repo -> Banking` (UNKNOWN) is no longer SUPPORTED while its Lending-Rate sibling is WEAK. |

Over the 8 cases the 25 tested paths went from CONTRADICTED 13 / UNRESOLVED 6 / WEAK 4 / SUPPORTED 2 to CONTRADICTED 8 / UNRESOLVED 5 / PLAUSIBLE 6 / WEAK 2 / SUPPORTED 2 (candidate paths: 25 -> 22 after sector fit; the 2 SUPPORTED are now the direct material-cost explanations, not circular ones). Not addressed here, by design: the bank/US yield-channel gap (MISSING_EDGE), missing data links and bank metrics (MISSING_DATA), wrong target for "net interest margin" (MISSING_NODE), geography/sector inference from question text (WRONG_CONTEXT), and cue-word narrative contradiction (still the fixed baseline; it can now only weaken a path, never condemn it). Tests: 1407 passed, 31 skipped.
