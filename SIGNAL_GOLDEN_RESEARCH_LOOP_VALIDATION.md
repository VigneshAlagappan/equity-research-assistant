# Signals — Golden Research Loop Validation

**This is a permanent, re-runnable benchmark.** It records whether Signals' existing
hypothesis-driven investigation architecture can execute five real research
questions end-to-end through the product's own investigation mechanism. Future runs should
update or version this file rather than replacing it, so improvements can be compared over
time.

---

## 1. Run metadata

| Field | Value |
|---|---|
| Validation date | 2026-09-02 (UTC) |
| Repository | `equity-research-assistant` ("Signals") |
| Branch | `feature-v1` |
| Baseline commit at start of validation | `0c8b0e0ef15e3390d6472c7a2c6469954416640d` |
| Commit during validation | `b0a0157274bf3c598b2eea9eeb9d80ae6f8b708a` ("todays all requests" — a user-initiated commit that swept in some in-progress files from this validation; no commit was created by the validation itself) |
| Code state tested | commit `b0a0157` **plus the uncommitted working tree** listed in §7 |
| Database | `data/equity_research.db` (the real application database, 2,585 companies) |
| LLM | live Anthropic API, `claude-sonnet-5` at the `deep` tier for generation/evaluation/synthesis, `claude-haiku-4-5` for macro series selection. Real API calls; nothing simulated. |
| Graph backend | `GRAPH_BACKEND=sqlite` (the documented default). See §6.5 for why the optional Neo4j backend was not used. |
| Execution path | `research.investigation.run_investigation(...)` — the identical function `POST /investigate/generate` calls, writing to the identical tables the UI reads. The route is a thin wrapper (auth/validation + `jsonify`); no orchestration logic lives in it. |
| Test suite after changes | **729 passed, 9 failed** as of this validation run — the same 9 pre-existing, unrelated failures as the baseline (3 × `test_assistant.py` model-tier routing, 1 × `test_llm_router.py`, 5 × `test_web.py` HTML-content assertions). Baseline was 695 passed / 9 failed; the +34 are this validation's new tests. *(Since fixed — as of 2026-09-02's later architecture.md accuracy pass, the full suite is 738 passed / 0 failed. This row is a frozen snapshot of this run, not a claim about the suite's current state — see architecture.md's Testing/CI section for the current number.)* |

### Prerequisite checks performed before any run

| Check | Result |
|---|---|
| `ANTHROPIC_API_KEY` usable | Yes — present in `.env` (108 chars, `sk-ant-…`), loaded via `python-dotenv` exactly as `main.py` does. Confirmed by real 200s from `api.anthropic.com`. |
| `HDFCBANK` registered | Yes — HDFC Bank, Financial Services |
| `ICICIBANK` registered | Yes — ICICI Bank |
| `IDFCFIRSTB` registered | Yes — IDFC First Bank |
| `INDUSINDBK` registered | Yes — IndusInd Bank |
| Peers used for #5 | `AXISBANK`, `KOTAKBANK` (both registered, both with full canonical financials) |

### Data actually on file (measured, not assumed)

| Company | Canonical rows | Distinct metrics | FY range | Quarterly rows | Documents | Chunks | Shareholding obs. | KG claims |
|---|---|---|---|---|---|---|---|---|
| HDFCBANK | 1,013 | 34 | FY2004–FY2027 | 456 | 3 | 3,679 | 20 | 55 |
| ICICIBANK | 1,114 | 36 | FY2000–FY2027 | 484 | 0 | 0 | 20 | 0 |
| IDFCFIRSTB | 972 | 41 | FY2004–FY2027 | 473 | 9 | 3,429 | 12 | 136 |
| **INDUSINDBK** | **182** | **21** | **FY2000–FY2013** | **0** | **0** | **0** | **0** | **0** |
| AXISBANK | 713 | 32 | FY2000–FY2027 | 432 | 0 | 0 | 20 | 0 |
| KOTAKBANK | 1,014 | 36 | FY2000–FY2027 | 498 | 3 | 4,350 | 20 | 41 |

Macro: 158,759 `macro_observations` rows across ~490 series — including `policy_repo_rate`,
`bank_credit`, `aggregate_desposits`, `credit_deposit_ratio`, `cash_reserve_ratio`,
`consumer_price_index_*`, `m3`, `10_year_g_sec_yield_fbil`. **Coverage starts 2017-10-13** for
every RBI series.

**The single most consequential data finding:** IndusInd Bank has only partial *annual* data
ending at **FY2013**, with **no** `net_profit`, `advances`, `gross_npa_percent`,
`net_npa_percent` or `interest_earned` at all, **no** quarterly data, **no** documents, **no**
shareholding history and **no** knowledge-graph claims. The IndusInd deterioration the test
question is about occurred in 2024–25. Investigation #3 therefore cannot be answered from
Signals' data, and this document reports that rather than working around it.

---

## 2. The five test questions (verbatim)

1. **HDFC Bank — Post-Merger Profitability.** *"Why has HDFC Bank's profitability changed
   following the merger? Evaluate competing explanations such as funding/deposit costs, NIM,
   loan growth, asset quality, operating costs, and merger-related balance-sheet effects."*
   → `company_ids=["HDFCBANK"]`
2. **IDFC FIRST Bank — Growth vs. Sustainability.** *"Is IDFC FIRST Bank's growth translating
   into sustainable profitability, or are credit costs, funding characteristics, asset quality,
   or other factors creating risks beneath the growth?"* → `company_ids=["IDFCFIRSTB"]`
3. **IndusInd Bank — Historical Early Warning.** *"Using only information available at each
   historical point in time, could Signals have detected meaningful deterioration at IndusInd
   Bank before it became obvious? Identify which indicators/facts changed and evaluate competing
   explanations. No look-ahead bias."* → `company_ids=["INDUSINDBK"]`, `as_of="2013-03-31"`
4. **HDFC Bank vs. ICICI Bank — Performance Divergence.** *"Why have HDFC Bank and ICICI Bank
   performed differently? Evaluate factors such as credit growth, deposits/funding, NIM, asset
   quality, operating efficiency, fee mix, profitability, and capital allocation, and distinguish
   company-specific factors from sector-wide effects."* → `company_ids=["HDFCBANK","ICICIBANK"]`
5. **Macro → Banking Outcomes.** *"How do changes in RBI policy rates, system credit/deposit
   growth, inflation, liquidity, and other relevant available macro factors relate to subsequent
   changes in growth, margins, profitability, or asset quality for HDFC Bank and appropriate
   peers?"* → `company_ids=["HDFCBANK","ICICIBANK","AXISBANK","KOTAKBANK"]`

---

## 3. Execution results

All five ran through `run_investigation()` against the real database with live LLM calls.
Every result below is queried back out of `investigations` / `investigation_hypotheses` /
`investigation_hypothesis_evidence`, and every `/investigate/<id>` URL was rendered through
Flask's real route (HTTP 200) before being recorded here.

| # | Investigation | ID | Companies (join table) | `as_of` | Hypotheses | Verdicts | Wall clock |
|---|---|---|---|---|---|---|---|
| 1 | HDFC post-merger | `8afa000b30c0` | HDFCBANK | — | 6 | 4 PARTIALLY_SUPPORTED, 2 INSUFFICIENT_EVIDENCE | 292 s |
| 2 | IDFC FIRST growth | `b7ebadb572bb` | IDFCFIRSTB | — | 6 | 4 PARTIALLY_SUPPORTED, **1 REFUTED**, 1 INSUFFICIENT_EVIDENCE | 441 s |
| 3 | IndusInd early warning | `fe88a2289ada` | INDUSINDBK | **2013-03-31** | 6 | 6 INSUFFICIENT_EVIDENCE | 97 s |
| 4 | HDFC vs ICICI | `a78909363298` | HDFCBANK, ICICIBANK | — | 6 | 3 PARTIALLY_SUPPORTED, **2 REFUTED**, 1 INSUFFICIENT_EVIDENCE | 179 s |
| 5 | Macro → banks | `720016d2c143` | HDFCBANK, ICICIBANK, AXISBANK, KOTAKBANK | — | 6 | 2 PARTIALLY_SUPPORTED, 4 INSUFFICIENT_EVIDENCE | 259 s |

URLs: `/investigate/8afa000b30c0`, `/investigate/b7ebadb572bb`, `/investigate/fe88a2289ada`,
`/investigate/a78909363298`, `/investigate/720016d2c143`.

### 3.1 Evidence volume and provenance (persisted rows)

| Investigation | Evidence lines retrieved per hypothesis | KG claims | Passages | Persisted supporting | Persisted contradicting | Persisted "missing" | Cited |
|---|---|---|---|---|---|---|---|
| `8afa000b30c0` | 142 | 27–33 | 8 | 27 | 9 | 27 | **36 / 36** |
| `b7ebadb572bb` | 120–135 | 107–124 | 8 | 27 | 31 | 24 | **58 / 58** |
| `fe88a2289ada` | 12 | 0 | 0 | 0 | 2 | 35 | **2 / 2** |
| `a78909363298` | 278 | 27–31 | 8–9 | 18 | 15 | 31 | **33 / 33** |
| `720016d2c143` | 391–421 | 57–69 | 8–12 | 24 | 11 | 28 | **35 / 35** |

"Cited" = non-`missing` evidence rows carrying a non-empty `citation`. **100 % across every run.**

### 3.2 Planning is per-hypothesis, not a fixed template

`investigation_planner.plan_and_gather` records `sources_queried` per hypothesis, and the
entity-driven knowledge-graph routing genuinely differs between hypotheses of the same
investigation. From `a78909363298` (HDFC vs ICICI):

* h1 (merger integration drag) → `financial_engine:HDFCBANK`, `financial_engine:ICICIBANK`,
  `knowledge_graph:Company:HDFC Bank`, `document_search`
* h3 (loan-mix / NIM) → additionally `knowledge_graph:Product:Credit Cards`
* h4 (asset quality) → additionally `knowledge_graph:Risk:Asset Quality Deterioration`

From `720016d2c143` (macro → banks), the routing widens further per hypothesis and only the
`macro`/`regulatory`-category hypotheses pulled the macro engine:

* h1 → `knowledge_graph:MacroFactor:Inflation`, `Metric:Net Interest Margin`,
  `Product:Deposits`, `Product:Loans`
* h4 → `Product:Kisan Gold Card`, `Segment:Financial Inclusion`, **`macro_engine`**
* h5 → **`macro_engine`**
* h6 → `Product:Digital Banking`, `Product:Mobile Banking`

The macro engine contributed 30 extra evidence lines to h4/h5 (421 vs 391) — a measurable,
category-driven routing decision, not a uniform fetch.

### 3.3 Real disconfirming evidence (a required acceptance criterion)

Two hypotheses in `a78909363298` were **REFUTED outright** by retrieved data, both proposing
that ICICI was structurally better and both killed by Signals' own numbers:

> **h2 — "ICICI Bank has structurally superior deposit franchise momentum."** Verdict: **REFUTED**.
> Contradicting evidence: HDFC deposits YoY FY2026 = **14.34 %** vs ICICI **11.48 %**; HDFC
> deposits CAGR FY2004–FY2026 = **20.04 %** vs ICICI **16.14 %**.
> *"The only directly relevant metric available — total deposit growth — shows HDFC Bank
> outpacing ICICI Bank both in the most recent year and over the long run, contradicting the
> claim."*

> **h4 — "ICICI's post-2018 underwriting overhaul produced structurally better asset quality."**
> Verdict: **REFUTED**. Contradicting evidence: HDFC gross NPA FY2026 **1.15 %** vs ICICI
> **1.41 %**; net NPA **0.38 %** vs **0.37 %**.

Contradicting evidence also appeared *against the winning hypotheses*, which is the harder test.
In `8afa000b30c0` h3 ("slower loan growth post-merger", ranked #2) the evaluator recorded:

> *contradicting/FACT* — "Very high loan growth in FY2024 post-merger: Domestic Loan Portfolio at
> ₹24,46,212 crore grew by 56.9 % over March 31, 2023"
> *contradicting/FACT* — "Strong NII growth immediately post-merger: Net Interest Income for
> FY2024 was ₹1,08,532 crore, up 25.0 % over FY2023"

The strongest single example is `b7ebadb572bb` h1 (IDFC FIRST), **REFUTED** by **11** separate
disconfirming facts drawn from the bank's own filings against only 2 supporting ones:

> **h1 — "IDFC First's retail-heavy loan growth is outpacing its ability to build matching
> low-cost deposits, forcing reliance on costlier borrowings that compress NIM."**
> Contradicting: CASA ratio improved from **8.7 %** (Dec-18, at merger) to **46.9 %** (Mar-25);
> cost of funds down **132 bps** since merger; Certificate-of-Deposit reliance cut 37 %
> (₹12,420 cr → ₹7,826 cr); **₹52,851 cr** of legacy wholesale borrowings repaid *entirely from
> retail deposits*; retail deposits up from 9 % to 67 % of total funding; deposits CAGR
> FY2004–FY2026 **17.66 %** vs advances **8.60 %**. Management attributes the NIM decline that
> did occur to microfinance, not funding mismatch.

Note that the *same* investigation's top-ranked hypothesis (h2, microfinance credit costs) carries
5 supporting **and 5 contradicting** rows — the pipeline does not stop looking for disconfirmation
once a hypothesis is winning.

And the pipeline caught a genuine **data-quality** problem on its own, in `a78909363298` h5:

> *contradicting/FACT* — "Deposits data anomaly: ICICIBANK and HDFCBANK deposits/total assets show
> inconsistent or erroneous figures for early years (e.g. HDFCBANK Deposits FY2021 reported as
> 13,333,720 INR_CRORE)".

### 3.4 Calculations are real numbers, not vague text

Sampling persisted `CALCULATION` rows: ROE 17.24 % (FY2024) → 14.57 % (FY2025) → 13.83 %
(FY2026); ROA 1.95 % → 1.68 % → 1.63 %; net profit CAGR FY2004–FY2026 HDFC 22.5 % vs ICICI
17.76 %; ICICI ROE 18.18 % (FY2025) vs HDFC 14.57 %. These are produced deterministically by
`financials/calculations.py` and `financials/ratios.py` via
`retrieval/structured_search.get_company_evidence` — the model restates them, it does not compute
them. Spot-check against the database: HDFC `net_profit` FY2023–FY2026 = 46,148 / 64,062.04 /
70,792.25 / 76,025.97 INR crore, and FY2026 YoY = 7.39 %, exactly as cited in the synthesis.

### 3.5 Uncertainty is stated, not glossed

Every investigation persisted `unanswered_questions` and `additional_evidence_needed`, and the
per-hypothesis `missing` evidence rows (27–35 per investigation) name specific absent datasets.
`8afa000b30c0`'s synthesis states the limitation explicitly:

> *"The biggest reason for caution is that no hypothesis has direct NIM, cost-of-funds,
> cost-to-income, or segment-level credit-cost data — all rely on proxy metrics (ROA/ROE decline)
> that are consistent with multiple competing mechanisms simultaneously."*

### 3.6 Investigation #3 (IndusInd) — an honest failure, not a papered-over one

The point-in-time machinery worked exactly as designed and is provable:

* `investigations.as_of = '2013-03-31'` is persisted on the row and rendered on both the
  investigation page ("evidence as of 2013-03-31") and the company card.
* The macro engine's LLM planner ran twice, but `macro_engine` never appears in any hypothesis's
  `sources_queried` — because **every** RBI series begins 2017-10-13, so under a 2013 cutoff
  `period_visible()` filtered all 1,383 `policy_repo_rate` observations to **zero**. (Verified
  directly: 0 visible as of 2013-03-31, 387 visible as of 2020-03-31.) No 2017+ data could reach
  a 2013 investigation.
* Financial evidence was cut from the full history to 12 lines — the only IndusInd metrics that
  exist at all before FY2013.

**But the investigation could not be answered**, because IndusInd has no data covering the period
the question is about. All six hypotheses returned INSUFFICIENT_EVIDENCE, and the synthesis said
so plainly rather than inventing an answer:

> *"None of the six hypotheses can be favored over another: every evaluation drew on the same
> narrow dataset (IndusInd Bank total deposit figures and growth/CAGR calculations for
> FY2004–FY2013)… The deposit-growth data available actually contradicts (rather than supports)
> the funding-pressure mechanism in h4, but this is too thin and too dated (a decade before the
> deterioration period in question) to establish any leading-indicator signal. As a result, the
> investigation cannot yet say whether Signals could have detected deterioration early."*

That is the correct behaviour under the spec's "do not manufacture evidence" rule. The failure is
a **dataset** failure (§6.1), not an architectural one — and it is worth noting that the *same*
`as_of` machinery would produce a real answer on any company that does have data spanning the
period in question.

---

## 4. Persistence and UI verification

Every investigation was verified through the product's own surfaces, not just by row existence.

**Association (the join table, §7.1):**

```
HDFCBANK     ['8afa000b30c0', '720016d2c143', 'a78909363298']
ICICIBANK    ['720016d2c143', 'a78909363298']
AXISBANK     ['720016d2c143']
KOTAKBANK    ['720016d2c143']
INDUSINDBK   ['fe88a2289ada']
IDFCFIRSTB   ['b7ebadb572bb']

investigations rows: 6      investigation_companies rows: 10
```

Six investigation records, ten associations — the cross-company ones are associated with several
companies each and **not duplicated**: `720016d2c143` is one record listed under four companies.

**Rendered UI (Flask test client, real routes).** `GET /companies/HDFCBANK` → 200, and its
Investigations section contains all three of its investigations, with cross-company ones labelled:

```html
<section class="merged-section" id="sec-investigations">
    <h2>Investigations</h2>
    <div class="investigations-list" id="company-investigations-list">
      <div class="card elev-sm investigation-card" data-investigation-id="8afa000b30c0">
        <a class="investigation-row" href="/investigate/8afa000b30c0">
            <div class="card-kicker">Deep Dive</div>
            <span class="tag tag-outline">6 hypotheses</span>
          <div class="card-title">Why has HDFC Bank&#39;s profitability changed following the merger? …</div>
      …
      <div class="card elev-sm investigation-card" data-investigation-id="720016d2c143">
        <a class="investigation-row" href="/investigate/720016d2c143">
            <div class="card-kicker">Deep Dive · also ICICIBANK, AXISBANK, KOTAKBANK</div>
      …
      <div class="card elev-sm investigation-card" data-investigation-id="a78909363298">
            <div class="card-kicker">Deep Dive · also ICICIBANK</div>
```

`GET /companies/INDUSINDBK` → 200 with `fe88a2289ada` present and tagged `as of 2013-03-31`.
`GET /investigate/<id>` → 200 for all five (62–80 KB rendered). The global
`/investigations` feed already listed structured investigations before this validation and
continues to.

---

## 5. Final validation matrix

| Investigation | Hypotheses | Planning | Evidence | Contradiction | Calculations / Charts | Evaluation | Synthesis | Provenance | Persistence | **Overall** |
|---|---|---|---|---|---|---|---|---|---|---|
| **1. HDFC Post-Merger** | Pass | Pass | Pass | Pass | Partial | Pass | Pass | Pass | Pass | **Pass** |
| **2. IDFC FIRST** | Pass | Pass | Pass | 31TRA | Partial | Pass | Pass | Pass | Pass | **Pass** |
| **3. IndusInd Early Warning** | Pass | Pass | **Fail** | Partial | Partial | Pass | Partial | Pass | Pass | **Partial** |
| **4. HDFC vs ICICI** | Pass | Pass | Pass | **Pass** | Partial | Pass | Pass | Pass | Pass | **Pass** |
| **5. Macro → Banks** | Pass | Pass | Pass | Pass | Partial | Pass | Pass | Pass | Pass | **Pass** |

### Evidence for each rating

**Hypotheses — Pass (all five).** Every run produced 6 genuinely distinct, testable hypotheses
spanning multiple categories, each with a stated causal mechanism and named unknowns. `a78909363298`
spanned `financial`, `operational`, `strategic`, `management`; `720016d2c143` spanned `financial`,
`industry`, `macro`, `competitive`. Category validation is enforced against a frozen vocabulary —
a hallucinated category is dropped, not stored (`hypothesis_generator.py:_parse_response`).

**Planning — Pass (all five).** `sources_queried` differs per hypothesis and is driven by the
hypothesis's own text and category (§3.2). The loop is Orchestrator-controlled, not LLM-controlled:
`research/investigation.py` decides whether an `INSUFFICIENT_EVIDENCE` verdict means "retry",
bounded by four documented termination controls. Confirmed in run #3, where evaluation calls
exceeded hypothesis count (retries fired) and stopped on the no-new-evidence check.

**Evidence — Pass ×4, Fail ×1.** 142–421 structured evidence lines, 27–69 knowledge-graph claims,
8–12 document passages per hypothesis for #1/#2/#4/#5. **#3 Fail**: 12 evidence lines, 0 claims,
0 passages — the dataset genuinely does not exist (§1, §3.6).

**Contradiction — Pass ×4 (2 outright REFUTED verdicts in #4), Partial ×1.** §3.3 shows real
disconfirming evidence killing hypotheses and challenging winning ones. #3 Partial: only 2
contradicting rows total, though notably the pipeline still surfaced deposit-growth data
*against* its own top-ranked hypothesis rather than reporting nothing.

**Calculations / Charts — Partial (all five).** Calculations are a clear Pass: deterministic
YoY / CAGR / ROA / ROE / vendor-reported ratios with real values, computed by
`financials/calculations.py` and `financials/ratios.py`, not by the model (§3.4). **Charts are a
flat Fail**: `charts/financial_charts.py` exists (`plot_metric_trend`, `plot_indexed_comparison`,
`plot_ratio_comparison`, `figure_to_base64_png`) but **nothing in `research/` imports it and
`investigation.html` contains no chart element at all** — an investigation cannot attach a
visualization today. This column is Partial on the strength of calculations alone.

**Evaluation — Pass (all five).** Each hypothesis is evaluated in its own LLM call against only
its own evidence — never batched — so one hypothesis's evidence cannot anchor another's verdict.
Verdicts span the full vocabulary in practice (SUPPORTED / PARTIALLY_SUPPORTED / REFUTED /
INSUFFICIENT_EVIDENCE), including 2 REFUTED in #4. Synthesis then ranks them
(`synthesis_rank`); refuted hypotheses are correctly left unranked (`rank=None`).

**Synthesis — Pass ×4, Partial ×1.** Syntheses explicitly weigh competing explanations against
each other and name what would change the conclusion (§3.5). #3 Partial: the synthesis is
well-formed and honest but reaches no conclusion, because it correctly refused to.

**Provenance — Pass (all five).** 100 % of non-`missing` persisted evidence rows carry a
citation (§3.1), tracing to a reconciled `canonical_financials` decision, a source file, a
document/page, or a knowledge-graph claim. Evidence kinds distinguish FACT / CALCULATION /
MANAGEMENT_OPINION / CORRELATION / CAUSATION / INFERENCE, and CORRELATION was never silently
promoted to CAUSATION in any run.

**Persistence — Pass (all five).** §4. Every investigation is queryable from each associated
company, renders in that company's Investigations section, and cross-company ones exist as a
single shared record.

**Reproducibility / auditability — Pass.** Persisted per investigation: `investigation_id`,
verbatim question, `company_ids`, `statement_type`, `as_of`, `generated_at`; per hypothesis:
`generation_order`, `category`, `verdict`, `confidence_basis`, `synthesis_rank`; per evidence
row: `stance`, `kind`, `label`, `value`, `citation`. Separately, `llm_call_log` records
`task_name`, `company_ids`, `question`, `model_used`, `provider_used`, `fallback_used`,
`attempts_json`, token counts, cost and latency for every LLM call in every investigation.

---

## 6. Missing capabilities and data found during this validation

Each is classified by the spec's own Gap-Handling vocabulary, and by whether it was **fixed**
generically during validation or **reported** as a remaining gap.

### 6.1 Missing data — IndusInd Bank coverage *(reported, not fixed)*

**Category: missing data.** IndusInd has 182 canonical rows ending FY2013, no `net_profit`,
`advances` or NPA metrics, no quarterly data, no documents, no shareholding history, no KG claims.
The question asks about a 2024–25 deterioration. **This is not fixable by architecture** — it needs
an ingestion run, and manufacturing the data to make the test pass is exactly what the spec
forbids. Investigation #3 is rated Partial on that basis. **Re-run instruction:** once IndusInd is
ingested to parity with HDFCBANK, re-run #3 with `as_of` set to a date shortly before the
deterioration (e.g. `2024-03-31`) — the point-in-time machinery is already in place and proven.

### 6.2 Point-in-time / temporal reasoning *(FIXED — new generic capability)*

**Category: temporal/point-in-time reasoning.** Before this validation, **no** retrieval path in
Signals had any notion of an "as of" date. Every capability returned everything on file, so any
historically-framed question was answered with post-hoc data and the look-ahead bias was invisible
— it lived in the evidence block itself, where no prompt wording could remove it.

Fixed generically: `research/temporal.py` plus an `as_of` keyword threaded into all five
evidence-gathering implementations and **bound once** in
`research.capabilities.default_capabilities(as_of=...)`. The Planner's `Protocol` signatures are
unchanged — the Planner does not know a cutoff exists — so a future capability gains point-in-time
support by honouring one keyword argument, not by every caller learning a new contract. The cutoff
fails closed (an undated document is excluded, not assumed old enough), and is persisted on
`investigations.as_of` so a historical conclusion states its information set. See §3.6 for proof
it actually blocks data.

### 6.3 Hypothesis-generation robustness *(FIXED — two real production failures)*

**Category: hypothesis-generation weakness.** Two separate defects each failed an entire
investigation outright during this validation. Generation is the one step
`run_investigation()` cannot degrade past — if it fails there is nothing to investigate.

1. **Token truncation.** `MAX_TOKENS` was 3072. Real runs produced 1836–3495 output tokens for a
   6-hypothesis object, so the cap was inside the normal operating range: investigation #3 failed
   with *"model response was truncated at the 3072-token limit before finishing."* Raised to 8192,
   for the same measured reason `research/hypothesis_evaluator.py` already documents for its own
   call. Regression-guarded by a test asserting `MAX_TOKENS >= 4096`.
2. **JSON control characters.** After the token fix, #3 failed again on *"Invalid control character
   at line 20 column 361"* — an unescaped newline inside a `mechanism` field. `json.loads(...,
   strict=False)` (the exact fix `research/knowledge_builder.py` already carried) applied to all
   four remaining LLM-response parsers. Structural validation is unchanged: a non-array response,
   a hallucinated category and an empty statement are still rejected.

### 6.4 Deterministic indicators were not an investigation input *(FIXED — new generic capability)*

**Category: evidence retrieval / trusted-facts input.** The spec's flow starts at *"Trusted Facts /
**Indicators** / Question"*, but the Configurable Indicator Framework (`indicators/`) was a
company-page presentation layer only: neither the hypothesis generator nor the planner could see a
triggered rule, so an investigation re-derived from raw series what a frozen, versioned rule had
already established — and could not cite the rule.

Fixed generically via a new `IndicatorEvidenceCapability` on `PlannerCapabilities`
(`research/indicator_evidence.py`), consumed in **both** hypothesis generation (as context)
and evidence gathering (as per-hypothesis `CALCULATION` evidence carrying `rule_id`, `rule_version`,
classification, severity, threshold summary and the rule's own provenance). It runs with
`persist=False` and `user_id=None`: an investigation must not append to the user-facing
`indicator_evaluations` audit trail as a side effect of reading it, and a conclusion must not
silently depend on whose thresholds happened to be configured.

**Verified working, but it contributed nothing to these five investigations — and that is a
finding, not a bug.** Zero rules fire for HDFCBANK / ICICIBANK / IDFCFIRSTB / INDUSINDBK /
KOTAKBANK. Signals' V1 rule library has **five rules in two families** (promoter-holding
up/down; net-profit growth; net-profit and total-assets YoY moves ≥ 25 %). These banks are
professionally managed (no promoter stake to move) and their profit moves are single-digit
(HDFC FY2026 +7.4 %, IDFC +8.1 %), so nothing triggers. The capability was proven end-to-end
against companies where rules *do* fire (e.g. ADANIPORTS: "Net Profit grew 420.2 % year over year
(FY2025 to FY2026, consolidated)"; ADANIENT: "Promoter holding declined from 74.84 % to 71.97 %,
a decline of 2.87 percentage points"). **The real gap is rule coverage** (§8, Now): there is no
NIM, credit-cost, CASA, cost-to-income, asset-quality or funding indicator — precisely the
indicators these five banking questions are about.

### 6.5 Neo4j knowledge-graph backend re-syncs on every read *(FIXED, then avoided)*

**Category: evidence retrieval (performance/concurrency).** `context/graph_neo4j.sync_knowledge_graph`
performed a full write-transaction rebuild of the graph on *every* `find_claims_about_entity` call,
by design ("resync before every query"). The Planner calls that capability up to 6× per hypothesis,
so a single 6-hypothesis investigation pushed the entire graph to Neo4j **~36 times**; the first
attempt at investigation #1 spent 25 minutes without finishing one hypothesis, and five concurrent
investigations deadlocked against each other on Neo4j write locks.

Fixed generically: the sync now skips when SQLite hasn't changed since the last sync, fingerprinted
on row counts plus the highest `claim_id`. The `knowledge_*` tables are append-only (nothing in
`storage/repositories.py` ever UPDATEs or DELETEs them), the fingerprint is per-process and
in-memory (a fresh process always syncs once), so correctness is unchanged and 36 syncs collapse
to 1. **The remaining, unfixed issue is architectural:** a *read* capability still performs graph
*writes*, which serializes concurrent investigations. Reported as a remaining gap (§8, Later).
This validation therefore ran on `GRAPH_BACKEND=sqlite` — the documented default, whose
`KnowledgeClaimView` results are identical (the same `_apply_as_of` filter now runs on both paths,
so backend choice cannot change what a point-in-time investigation may see).

### 6.6 Charts are never attached to an investigation *(reported, not fixed)*

**Category: calculations/charts.** `charts/financial_charts.py` provides `plot_metric_trend`,
`plot_ratio_trend`, `plot_indexed_comparison`, `plot_ratio_comparison`,
`plot_advances_vs_deposits` and `figure_to_base64_png`. **Nothing under `research/` imports it**,
`investigation.html` has no chart element, and there is no schema column to persist one. Column 5
of the matrix is therefore Partial everywhere on calculations alone. Not fixed here: it needs a
schema addition, a persistence path and a render path — more than a "minimum generic missing
capability", and the spec asks for charts only "where they materially help".

### 6.7 Cross-company investigations were not queryable per company *(FIXED — new generic capability)*

**Category: investigation persistence/UI.** `investigations.company_ids` was a JSON blob, and the
company page had a "Threads" section listing single-narrative Signals reports but **no
Investigations section at all** — a structured hypothesis-driven investigation was reachable only from the
global feed or its direct URL. Answering "which investigations touch this company?" would have
needed a full-table `LIKE` scan that also matches substrings (`HDFCBANK` inside `HDFCBANKX`).
Fixed with a real many-to-many join table (§7.1). This is what makes golden investigations #4 and
#5 satisfy the spec's "associate with every relevant company… do not duplicate the underlying
record" rule.

### 6.8 Observations worth noting (not rated as gaps)

* **Cross-company knowledge-graph bleed.** `find_claims_about_entity` deliberately returns claims
  from *any* company connected to an entity, so investigation #5 (HDFC + peers) cited SBFC Finance
  management commentary on inflation. That is documented, intended behaviour and the citation
  names the company, so it stays auditable — but a reader should know that a KG-sourced claim in a
  company investigation may originate from a different company.
* **Graceful degradation confirmed live.** The macro planner returned unparseable output during
  investigation #2 and fell back to the keyword/regex heuristic, exactly as designed, without
  failing the investigation.
* **Data-quality signal.** Investigation #4 independently flagged an implausible
  `HDFCBANK deposits FY2021 = 13,333,720 INR_CRORE`. Worth a reconciliation review.
* **The research path ignores per-user indicator overrides, by design — and it mattered.** The
  database carries a pre-existing `indicator_rule_config` row lowering
  `financial_trajectory.net_profit_growth` to 5 % for `IDFCFIRSTB` under `user_id=1`. IDFC's
  FY2026 growth of 8.07 % clears that, so a signed-in admin viewing the company page sees the
  indicator fire — but investigation #2 correctly did **not**, because
  `research/indicator_evidence.py` evaluates with `user_id=None` (system defaults, 25 %). A
  conclusion must not silently depend on whose thresholds happened to be configured. (This
  config row predates the validation and was left untouched; it is worth reviewing as possible
  test residue from an earlier session.)

---

## 7. Generic changes made during this validation

Every change is generically reusable by any future investigation. No company-specific logic,
hypothesis, answer or workflow was added anywhere.

### 7.1 New: one-investigation-to-many-companies association

| File | Change |
|---|---|
| `schemas/sqlite_schema.sql` | New `investigation_companies(investigation_id, company_id, position)` table + `idx_investigation_companies_company`; new `investigations.as_of` column. |
| `storage/investigation_repository.py` | **New file.** `insert_investigation_companies`, `select_company_ids_for_investigation`, `select_investigations_for_company`, `count_investigation_hypotheses`, `select_investigations_missing_company_rows`, `backfill_investigation_companies`. |
| `storage/repositories.py` | `save_investigation` now writes the associations in the same transaction and persists `as_of`. |
| `storage/database.py` | `_migrate_investigations_as_of_column`, `_migrate_investigation_companies` (idempotent backfill from the legacy JSON column) wired into `init_db()`. |
| `web/app.py` | Company route builds `company_investigations` through the join table. |
| `web/templates/company.html` | **New "Investigations" section + tab entry**, with an "also &lt;other companies&gt;" kicker and an "as of" tag. |
| `web/templates/investigation.html` | Renders "evidence as of &lt;date&gt;" when the investigation was point-in-time. |

### 7.2 New: point-in-time (`as_of`) evidence scoping

| File | Change |
|---|---|
| `research/temporal.py` | **New file.** `normalize_as_of`, `fiscal_year_visible` (fiscal-period-end aware, quarter aware, non-March year-ends), `period_visible` (macro `YYYY` / `YYYY-MM` / `YYYY-MM-DD`), `date_visible`. Fails closed on undated items. |
| `research/capabilities.py` | `default_capabilities(as_of=...)` binds the cutoff into every capability; Protocol signatures unchanged. |
| `retrieval/structured_search.py` | `get_company_evidence(as_of=…)` — truncates each series, then computes YoY/CAGR/ROA/ROE **on the truncated series**. |
| `research/macro_evidence.py` | `get_macro_evidence(as_of=…)`. |
| `research/documents.py` | `get_document_evidence(as_of=…)` — filters on `published_at`. |
| `retrieval/document_search.py` | `search_documents(as_of=…)` — post-ranking filter, so relevance order is preserved. |
| `context/knowledge_graph.py` | `find_claims_about_entity(as_of=…)`, applied identically to the SQLite and Neo4j paths. |
| `research/investigation.py` | `run_investigation(as_of=…)`; `Investigation.as_of`; persisted on the row. |
| `web/app.py` | `POST /investigate/generate` accepts an optional `as_of`. |

### 7.3 New: deterministic indicators as investigation input/evidence

| File | Change |
|---|---|
| `research/indicator_evidence.py` | **New file.** `get_indicator_evidence` — `TriggeredIndicator` → `CALCULATION` `Evidence` with rule id/version/classification/severity/threshold/provenance. Read-only (`persist=False`), system defaults (`user_id=None`), degrades to `[]` on failure. |
| `research/capabilities.py` | New `IndicatorEvidenceCapability` Protocol + `indicator_evidence` field (defaults to a neutral no-op so partial bundles stay valid); disabled entirely under an `as_of` cutoff, since indicator rules have no historical mode. |
| `research/investigation_planner.py` | Gathers indicator evidence per company; records `indicators:<company_id>` in `sources_queried`. |
| `research/hypothesis_generator.py` | Optional `capabilities=` parameter; `_company_context` surfaces triggered indicators alongside sector and known entities. |

### 7.4 Robustness fixes

| File | Change |
|---|---|
| `research/hypothesis_generator.py` | `MAX_TOKENS` 3072 → 8192; `json.loads(..., strict=False)`. |
| `research/hypothesis_evaluator.py`, `research/research_synthesis.py`, `research/system_insights.py` | `json.loads(..., strict=False)` — same control-character tolerance. |
| `context/graph_neo4j.py` | `sync_knowledge_graph` skips an unchanged graph (append-only fingerprint). |

### 7.5 New tests (+34; suite 695 → 729 passing)

| File | Covers |
|---|---|
| `tests/test_temporal.py` (11) | Cutoff arithmetic incl. non-March year-ends and quarters; fail-closed on undated items; **bound capabilities cannot return post-cutoff data**; derived calculations recomputed on the truncated series; indicator capability disabled under a cutoff. |
| `tests/test_investigation_companies.py` (9) | Cross-company investigation listed under each company from **one** record; ordering; duplicate company ids; newest-first; `as_of` round-trip; idempotent backfill; backfill skipping a deleted company; hypothesis count scoping. |
| `tests/test_indicator_evidence.py` (5) | Triggered rule → citable `CALCULATION` evidence; **never writes to the audit trail**; empty for a company with no data; routes through the capability seam; no source line when empty. |
| `tests/test_hypothesis_generator.py` (+5) | Raw newline inside a string still parses; structural validation survives the lenient parse; token-budget regression guard; triggered indicators reach the prompt; unchanged behaviour without a capability bundle. |
| `tests/test_web.py` (+4) | Company page lists its investigations; a cross-company investigation appears under every company it covers with one DB record; empty state; `as_of` labelled on both surfaces. |

### 7.6 Architecture guardrail compliance

| Guardrail | Status |
|---|---|
| 1. Modular monolith, single process | Held — no new services, agents or queues. |
| 2. Interface-first DI | Held — indicators reach research via a new `PlannerCapabilities` Protocol, never a direct import from business logic; `as_of` is bound at the same seam. |
| 3. Storage behind a repository | Held — all new SQL is in `storage/investigation_repository.py`; no business module calls `conn.execute`. |
| 4. Source-of-truth boundaries | Held — indicators stay a separate deterministic layer; `research/indicator_evidence.py` is an adapter that computes nothing and writes nothing. |
| 5. Investigation memory is never a fact | Held — untouched. |
| 6. Conclusions trace to retrieved evidence | Held — 100 % citation coverage (§3.1). |
| 7. Planner/Orchestrator controls the loop | Held — no LLM call decides whether to iterate; the four termination controls are unchanged. |
| 8. Single-agent architecture | Held. |
| DB portability (`DBConnection`/`Row`) | Held — `grep -rl "sqlite3" --include="*.py" . \| grep -v -E "\.venv\|__pycache__\|/tests/\|^\./storage/"` returns only the pre-existing `scripts/db_shard.py`. |

---

## 8. Remaining gaps, score and recommendations

### Signals Golden Research Loop Score: **8 / 10**

**What earns the 8.** The loop the spec asks for — *Trusted Facts / Indicators / Question →
multiple testable hypotheses → investigation plan → evidence gathering → evaluation → synthesis →
auditable conclusion* — executed end-to-end, for real, on all five questions, through the
product's own mechanism, and produced work that would survive review. Four of five reached a
defensible, evidence-grounded conclusion. Hypotheses are genuinely competing rather than
rationalisations; planning visibly varies per hypothesis; evidence is retrieved deterministically
and cited at 100 %; **three hypotheses across two investigations were REFUTED by Signals' own
data**, including one killed by 11 disconfirming facts; calculations are real deterministic
numbers, not model arithmetic; syntheses weigh explanations against each other and state their
limits; and everything is persisted, queryable, auditable and rendered under each associated
company.

**What costs the 2 points.**

* **−1, dataset coverage.** Investigation #3 could not be answered at all, and every investigation
  hit the same wall from the other side: the single most common `missing_evidence` item across all
  five runs is **NIM, cost of funds, CASA and segment-level credit cost** — the metrics banking
  analysis actually turns on. Signals' canonical schema has `interest_earned`/`interest_expended`
  but no NIM, no CASA, no segment breakdown. Every "PARTIALLY_SUPPORTED rather than SUPPORTED"
  verdict in runs #1, #4 and #5 traces to this. Note the contrast with #2, where IDFC's nine
  ingested documents produced 107–124 knowledge-graph claims and the richest result of the five —
  **document coverage, not model capability, is what separates a good investigation from a thin
  one.**
* **−0.5, no charts.** §6.6. An investigation cannot attach a visualization at all.
* **−0.5, indicator rule coverage.** §6.4. The indicators→hypotheses path is now wired and
  proven, but five rules across two families fire on none of these banks, so the spec's flow
  effectively started at "Trusted Facts / Question" rather than "Trusted Facts / **Indicators** /
  Question" for every one of the five.

### Highest-priority remaining gaps

| # | Gap | Category | Impact |
|---|---|---|---|
| 1 | No NIM / CASA / cost-of-funds / segment credit-cost metrics in `canonical_financials` | Missing data | Caps nearly every banking hypothesis at PARTIALLY_SUPPORTED |
| 2 | Document coverage is uneven (IDFC 9 docs → richest result; ICICI/AXIS 0 docs) | Missing data | Determines investigation quality more than anything else |
| 3 | IndusInd data ends FY2013 | Missing data | Investigation #3 unanswerable |
| 4 | Indicator rule library has no banking-relevant rules | Hypothesis-generation input | Indicators contribute nothing to bank investigations |
| 5 | Charts never attached to an investigation | Calculations/charts | Matrix column 5 capped at Partial |
| 6 | Neo4j KG read path performs graph writes | Evidence retrieval | Serializes concurrent investigations; unusable under load |
| 7 | Wall-clock deadline covers only the retry loop, not the first pass | Orchestration | An investigation can run 7+ minutes unbounded (#2: 441 s) |

### Recommendations

**Now**

1. **Ingest NIM, CASA, cost of funds and segment-level asset quality** into the canonical schema
   for banks. This is the single highest-leverage change in this document: it is what four of five
   investigations explicitly asked for and did not get. Everything else is a smaller effect.
2. **Ingest documents for the covered banks** (ICICI, Axis have zero). Investigation #2's quality
   advantage came entirely from IDFC's nine documents. Document coverage is the cheapest available
   quality multiplier.
3. **Backfill IndusInd Bank** to parity, then re-run investigation #3 with `as_of="2024-03-31"`.
   The point-in-time machinery is built, tested and proven (§3.6) — only the data is missing.
4. **Add banking indicator rules** (NIM compression, credit-cost spike, CASA decline,
   cost-to-income deterioration, GNPA/NNPA move). Each is one `register_rule(...)` call in
   `indicators/rules.py` — the framework's own stated extensibility contract — and they would flow
   into hypothesis generation and per-hypothesis evidence automatically via §7.3.
5. **Expose `as_of` in the UI.** The capability is server-side complete and the route accepts it;
   the Research page has no field for it, so a user cannot currently run a point-in-time
   investigation without calling the API directly.

**Later**

6. **Attach charts to investigations** — a `investigation_charts` table plus a render path in
   `investigation.html`, sourced from the existing `charts/financial_charts.py`.
7. **Make the Neo4j knowledge-graph read path read-only** (sync on ingestion events rather than on
   query), removing write-lock contention between concurrent investigations.
8. **Extend the wall-clock deadline to cover the first retrieval pass**, not just retries.
9. **Cache link-only document fetches.** `research/documents.py` re-downloads every link-only PDF
   on every retrieval pass; this dominated investigation #2's 441 s.
10. **Reconcile the flagged data-quality anomaly** (`HDFCBANK deposits FY2021 = 13,333,720
    INR_CRORE`, §6.8) — the pipeline found it; the fix belongs in ingestion.

---

## 9. How to re-run this benchmark

```bash
# Prerequisites: ANTHROPIC_API_KEY in .env; data/equity_research.db present.
# Each investigation is one call to the same function POST /investigate/generate uses:
python - <<'PY'
from dotenv import load_dotenv; from pathlib import Path
load_dotenv(Path(".env").resolve())
from storage.database import init_db
from research.investigation import run_investigation

conn = init_db()
inv = run_investigation(
    conn,
    "<one of the five questions verbatim from §2>",
    ["HDFCBANK"],                 # the §2 company scope for that question
    statement_type="consolidated",
    as_of=None,                   # "2024-03-31" for the point-in-time question
)
print(inv.investigation_id, len(inv.hypotheses))
PY
```

Then verify each dimension against §5 by querying `investigations`,
`investigation_hypotheses` and `investigation_hypothesis_evidence` for that
`investigation_id`, and by rendering `/investigate/<id>` and `/companies/<company_id>`.

**When re-running, record:** the commit, the data-coverage table (§1) as of that date, the
per-investigation counts (§3.1), the matrix (§5), and the score — then compare against this run.
A higher score should be justified by *more evidence and fewer INSUFFICIENT_EVIDENCE verdicts*,
not by a more confident narrative. A polished narrative by itself does not constitute a pass.

### Version history

| Date | Commit | Score | Note |
|---|---|---|---|
| 2026-09-02 | `b0a0157` + working tree | **8 / 10** | First run. 4 of 5 investigations Pass, 1 Partial (IndusInd — dataset). Added: cross-company investigation association + Company→Investigations UI; point-in-time `as_of` scoping; deterministic indicators as investigation input/evidence; hypothesis-generation robustness fixes; Neo4j sync de-duplication. |
