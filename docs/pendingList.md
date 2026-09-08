# Pending List

Snapshot as of 2026-09-02, after the database-portability refactor, the
Configurable Indicator Framework, and the Golden Research Loop validation
(see `architecture.md` and `SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`).
Nothing below is uncommitted or blocking — this is the backlog, not open work.

## Configurable Indicator Framework — deliberately deferred (not bugs)

- **Feedback loop** (Agree / Disagree / Not Sure, spec section 11) —
  design-only by the original spec; an `indicator_feedback` table keyed off
  `evaluation_id` is the intended shape, noted in `indicators/evaluation.py`'s
  docstring as an extension point, not built.
- **Critical (red) classification tier** — vocabulary slot reserved, unused.
  Only `positive` / `observation` / `warning` are live.
- **9 of the 11 anticipated indicator families** — only `shareholding` and
  `financial_trajectory` are seeded. Not yet built: promoter pledging, debt/
  refinancing, revenue/profitability, operating performance, valuation,
  governance, corporate actions, cash flow, capital allocation.
- **Audit-trail browsing UI** — `indicator_evaluations` is real, queryable,
  append-only data, but no Admin/Tools panel renders it yet.

## Golden Research Loop validation — real gaps, ranked by priority

1. **Missing financial metrics in `canonical_financials`** — no NIM, CASA,
   cost-of-funds, or segment-level credit-cost data. The most common
   `missing_evidence` item across all five golden-loop runs, and the main
   reason verdicts land at PARTIALLY_SUPPORTED rather than SUPPORTED.
2. **Uneven document ingestion coverage** — IDFC First Bank has 9 documents
   → 124 knowledge-graph claims (richest result of the five); ICICI and Axis
   have zero documents ingested. Investigation quality varies by company for
   data-coverage reasons, not pipeline capability.
3. **IndusInd Bank's ingested annual data ends at FY2013** — the "early
   warning" golden question concerns 2024-25, which is currently
   unanswerable. Not an architecture gap: the point-in-time (`as_of`)
   evidence-scoping capability built to support exactly this kind of
   question is implemented and proven (`research/temporal.py`); a re-run
   will work as soon as more recent IndusInd data is ingested.
4. **Charts never attach to an investigation record** — `charts/
   financial_charts.py` exists but nothing under `research/` calls it, and
   there's no schema column on `investigations`/`investigation_hypotheses`
   to hold a chart reference.
5. **Indicator rule coverage was thin against the golden-loop banks** — the
   5 existing rules (2 families) didn't fire on most of the five test
   companies (professionally managed banks, small profit moves), so
   `IndicatorEvidenceCapability` contributed little evidence in this
   particular validation despite being wired into the investigation
   pipeline. Expected to matter more once more indicator families exist
   (see above) and/or on companies whose indicators actually trigger.

## Status

Git is clean — all work described above (and everything that produced this
list) is committed. This file tracks backlog only; update or re-generate it
after picking any item up.
