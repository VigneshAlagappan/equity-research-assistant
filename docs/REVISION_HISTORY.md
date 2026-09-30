# Revision History

Newest first. Derived from git history on `feature-v3`; only the deployment marked below is confirmed against Lightsail. Earlier entries are grouped by commit date.

## 2026-09-29 — Post-deploy changes (not yet deployed)
- Simplified to three user-facing concepts: Cases, Conversations, Investigations (L1–L5 = investigation depth); Report/Thread/Research/Deep Dive terminology removed
- `research_cases` is the single container: new `origin`, `hidden_at`, `deleted_at` columns and a `case_companies` company-tag table (schema applied to Neon); every question now creates a case
- Cases page reads only cases; company pages show the same cases filtered by tag; tags editable per case; Delete is soft for every case
- All historical cases, reports, investigations and their S3 artifacts cleared from production before the change
- One URL per case: `/cases/<id>` renders the conversation, investigation, saved answer or progress in place; `/research/thread/<id>` and `/investigate/<id>` 301-redirect to it
- Routing/assistant tests updated to the current model-tier config (no product change)
- Company page: Investigations and Conversations combined under one "Cases" section
- Cases list moved to `/cases` (`/investigations` 301-redirects, filters preserved)
- Multi-turn Conversations: `/conversations/<case_id>` with background follow-up turns (`case_turns` table, `research/conversation.py`); Ask AI/chat now hand off to the conversation page
- Level 1/2 now escalate to Level 3 on missing data, not only on unparseable questions (reconciled with the parallel Level 1–2 fast-path/ROE-ROA work)
- Cases list: bulk select with Hide/Delete

## 2026-09-29 — Deployed to Lightsail (deployment v62, image `:signals-app.signals-app.59`, commit `6a6103a`)
- Execution Analytics panel (Admin > Settings) for Signal Complexity Levels 1–5, reconciled with Jev/routing_policy
- Bug fixes for Execution Analytics
- Docs reconciled with shipped Execution Analytics and Eval Analytics panels

## 2026-09-28 — Jev complexity routing (ADR-023)
- Jev: LLM-based complexity classification and routing policy; research items tagged with 5 levels, Quick/Deep toggle removed
- Periodic Jev accuracy eval wired into the scheduler; Eval Analytics admin panel (and fix for it breaking other admin panels)
- All ADRs reconciled with Jev; Investor/Architect views added to architecture.md
- L1/L2 work

## 2026-09-27 — Macro data and guardrails
- FRED historical S3 archival, Alpha Vantage commodities, economic graph builder
- RBI reference-rate parser closes the USD/INR macro gap; dedup of macro re-ingestion; Postgres connection fix in ingest
- Investment-advice questions rejected; India/US tag scoping fixed
- OpenRouter model sequence; investigations delete bug fixed; test isolation fix

## 2026-09-26 — US universe and Watchlist
- Russell 3000 company registration
- Watchlist redesigned as Grid/Feed activity workspace
- Landing-page company counts rounded to nearest 50
- Added docs/playbook.md (adding geographies, companies, macro data, filings)

## 2026-09-24 to 2026-09-25 — US data backfill
- US price backfill, SEC EDGAR backfill fixes (threading, annual-only and year-window filters)
- US company uniqueness fix; roadmap update
