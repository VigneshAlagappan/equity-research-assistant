# Signals — Equity AI Research Assistant

**Signals** is an evidence-grounded AI research platform for listed companies, with a
primary focus on the US and India markets. It combines structured financial data,
company filings, management commentary, macro data, deterministic calculations,
knowledge-graph relationships, hybrid retrieval, and LLM reasoning to answer
investment-research questions with traceable evidence.

**This is not a trading system.** It does not produce buy/sell recommendations.

---

## Product & Architecture at a Glance

Signals is designed as an **AI-native research system**, not as a general-purpose
financial chatbot.

The platform deliberately separates:

- **Authoritative facts & deterministic calculations** — relational evidence and reproducible financial logic
- **Relationships** — Knowledge Graph / Neo4j projection
- **Lexical retrieval** — FTS5 / BM25
- **Semantic retrieval** — embeddings + Qdrant
- **Hybrid evidence retrieval** — deterministic rank fusion across lexical and semantic search
- **Reasoning** — LLMs operating on retrieved evidence rather than inventing facts
- **Research control** — Hypothesis Generator, Planner, Orchestrator, Evaluator, and Synthesizer
- **Context & efficiency** — semantic reuse, prompt caching, model routing, fallback, and cost observability

### Architecture Visuals

- [Open Product / Investor Architecture Visual](Architecture-Visual-Investor.png) — product capability and executive view
- [Open Technical Architecture Visual](Architecture-Visual-Technical.png) — detailed system architecture view

---

## Architecture Principles

The key architectural invariants are:

- **LLMs do not own financial facts or deterministic calculations.**
- **SQLite-backed relational data remains the authoritative evidence layer.**
- **FTS5, Qdrant, and Neo4j are derived, rebuildable indexes/projections — not competing truth stores.**
- **Material conclusions should be traceable to evidence or explicitly identified as inference.**
- **Multi-hop graph relationships are research signals, not automatic proof of causation.**
- **Point-in-time research prevents look-ahead evidence at the retrieval boundary.**
- **Optional infrastructure should degrade gracefully without corrupting authoritative evidence.**
- **Architecture complexity is introduced only when product, scale, or reliability requirements justify it.**

---

## Architecture Documentation

Different documents serve different purposes:

| Document | Purpose |
|---|---|
| [`Architecture-Visual-Investor.png`](Architecture-Visual-Investor.png) | Product capability / executive architecture view |
| [`Architecture-Visual-Technical.png`](Architecture-Visual-Technical.png) | Detailed technical architecture view |
| [`architecture.md`](architecture.md) | Current implementation and technical architecture |
| [`DESIGN_RATIONALE.md`](DESIGN_RATIONALE.md) | Why the major architecture choices were made |
| [`ADR/`](ADR/) | Durable Architecture Decision Records for individual major decisions |
| [`FeatureList.md`](FeatureList.md) | Shipped vs. open capabilities |
| [`SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md`](SIGNAL_GOLDEN_RESEARCH_LOOP_VALIDATION.md) | Research-loop validation |
| [`SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md`](SIGNAL_HYBRID_RETRIEVAL_VALIDATION.md) | Hybrid-retrieval validation |
| [`USER_GUIDE.md`](USER_GUIDE.md) | How to run and use Signals |

> **How to read this repository**
>
> `README.md` tells the product story and preserves the original design/scoping rationale.  
> `architecture.md` describes what is implemented today.  
> `DESIGN_RATIONALE.md` explains the engineering judgment behind the major choices.  
> `ADR/` preserves individual architecture decisions, alternatives, consequences, and revisit conditions.

---

## Project Ownership & Development Approach

The **product concept, product strategy, system architecture, user experience, research
approach, data/source strategy, design decisions, and overall technical direction of
Signals are my original work**.

AI coding tools, primarily **Claude Code**, are used as a **vibe-coding and implementation
assistant** to help translate these designs into working software, accelerate prototyping,
generate and refactor code, and explore implementation alternatives.

The project is grounded in **software and solution architecture** and core
software-engineering principles such as **modularity, abstraction, interfaces, separation
of concerns, maintainability, testability, and clear system boundaries**.

This project also serves as a hands-on learning environment for developing deeper
understanding of:

- AI agents and agentic systems
- AI agent design patterns
- Planning, orchestration, tool use, memory, retrieval, and evaluation
- Knowledge graphs, semantic search, and multi-store architectures in AI-agent systems
- Product management for AI-native products
- Practical application of software architecture and engineering principles in AI systems

A core belief behind this project is that **deep technical expertise does not become less
important in an AI-driven software world; it becomes more important**.

AI can significantly accelerate implementation, but effective use of AI still depends on
strong technical judgment: understanding system boundaries, architecture trade-offs, data
models, interfaces, failure modes, scalability, security, reliability, and
software-engineering fundamentals.

The differentiating skill is therefore not simply the ability to write code manually, but
the ability to **design the right system, ask the right questions, evaluate AI-generated
implementations, recognize architectural weaknesses, make sound trade-offs, and guide AI
toward robust engineering outcomes**.

AI-assisted coding accelerates implementation; **it does not define the product vision,
architecture, research methodology, or overall technical intent of Signals**.

---

## License

[Business Source License 1.1](LICENSE) — free for personal, educational, non-commercial,
and internal evaluation/development use. Commercial use (by or for a for-profit entity,
or offering this or a derivative as a paid/hosted/embedded service) requires a separate
commercial license.

See [`LICENSE`](LICENSE) for the governing terms.

---

> **Documentation note**
>
> The sections below preserve the **original design proposal and scoping rationale**.
> They explain how Signals was initially conceived and why the early system was shaped
> the way it was. They should **not** be treated as the source of truth for the current
> implementation.
>
> For the current technical architecture, see [`architecture.md`](architecture.md).
> For the reasoning behind major design decisions, see
> [`DESIGN_RATIONALE.md`](DESIGN_RATIONALE.md).
> For durable individual architecture decisions and trade-offs, see [`ADR/`](ADR/).
> For shipped vs. open capabilities, see [`FeatureList.md`](FeatureList.md).
> For operating instructions, see [`USER_GUIDE.md`](USER_GUIDE.md).
>
> The numbered implementation sequences below are retained as historical roadmap and
> scoping context. Their current implementation status lives in `FeatureList.md`.

---

## Table of Contents

- [Product & Architecture at a Glance](#product--architecture-at-a-glance)
  - [Architecture Visuals](#architecture-visuals)
- [Architecture Principles](#architecture-principles)
- [Architecture Documentation](#architecture-documentation)
- [Project Ownership & Development Approach](#project-ownership--development-approach)
- [License](#license)
- [Objective](#objective)
- [High-Level Architecture](#high-level-architecture)
- [Folder Structure](#folder-structure)
- [Source Adapters](#source-adapters)
- [Data Layers: Raw → Normalized → Derived](#data-layers-raw--normalized--derived)
- [Schema](#schema)
  - [Company Master](#company-master)
  - [Company Lifecycle / Archiving](#company-lifecycle--archiving)
  - [Financial Observations](#financial-observations)
  - [Source / Provenance & Reconciliation](#source--provenance--reconciliation)
  - [Documents & Chunks](#documents--chunks)
- [Ingestion Approach by Source](#ingestion-approach-by-source)
- [Retrieval Architecture](#retrieval-architecture)
- [Deterministic Calculation Layer](#deterministic-calculation-layer)
- [Evidence & Citations](#evidence--citations)
- [LLM Model Auto-Routing](#llm-model-auto-routing)
- [Implementation Sequence](#implementation-sequence)
- [Web UI Implementation Sequence](#web-ui-implementation-sequence)
- [MVP Delivery Plan](#mvp-delivery-plan)
- [Open Decisions](#open-decisions)
- [Engineering Principles](#engineering-principles)

---

## Objective

The system should eventually answer questions like:

\- Analyze HDFC Bank for the last 10 years.

\- Compare HDFC Bank and ICICI Bank.

\- Explain why a company's margins changed.

\- Identify important changes in management commentary.

\- Explain how a weak monsoon could affect Mahindra & Mahindra versus HDFC Bank.

\- Identify which financial metrics to monitor if a macro condition changes.

It should behave like a personal financial research analyst — not a financial-data

chatbot that only looks numbers up.

**## High-Level Architecture**

\**\*This is the \*original\** proposed pipeline, kept as-is for historical context — not

current state.\*\* The system has since grown well past this single linear flow (a

Flask web app alongside the CLI, a knowledge graph, a real Planner/Orchestrator with

an iterative evidence-sufficiency loop, a Model Router/Fallback layer, an event bus,

price history, and more) — so read "No multi-agent framework, no orchestrator/planner

agents" below as *\*this proposal's original scope\**, not a claim about what's built

today. See [architecture.md's High-level architecture]\(architecture.md#high-level-architecture)

for the current, accurate diagram.

\`\`\`

Sources (manually obtained files or periodic ingestion)

      │

      ▼

Ingestion  (detect → parse → validate → normalize → store)

      │

      ▼

Storage:  RAW  →  NORMALIZED  →  DERIVED         (SQLite)

      │

      ▼

Retrieval  (structured query + keyword/semantic search + metadata filter)

      │

      ▼

Research Assistant  (deterministic tools  +  LLM reasoning)

      │

      ▼

Answer  (FACT / CALCULATION / MANAGEMENT STATEMENT / INFERENCE, all cited)

\`\`\`

No multi-agent framework, no orchestrator/planner agents, no LangChain/LangGraph —

one research assistant backed by deterministic tools and retrieval is

sufficient. Components are shaped so they *\*can\** later be registered as callable tools

(\`get\_company\_financials\`, \`calculate\_ratio\`, \`compare\_companies\`, \`generate\_chart\`, …)

without building that registry now.

**## Folder Structure**

The originally proposed layout below shaped the initial scaffold; the actual

package layout has since evolved well past it (several proposed modules were

never built as named — e.g. \`research/synthesis.py\`, \`documents/\` — while

others exist that this proposal didn't anticipate — \`context/\`, \`llm/\`,

\`web/\`). For the real, current module map, see

[architecture.md's Module map]\(architecture.md#module-map).

\`\`\`

equity-research-assistant/

  main.py                        # CLI entrypoint

  config/settings.py             # paths, source trust order, LLM config

  sources/

    base.py                      # SourceAdapter interface, NormalizedObservation dataclass

    screener.py  nse.py  bse.py  investor\_relations.py  macro.py

  ingestion/

    detector.py  pipeline.py  validation.py

  normalization/

    financials.py  periods.py  units.py  companies.py

  storage/

    database.py  repositories.py  documents.py

  financials/

    calculations.py  ratios.py  comparisons.py

  documents/

    parser.py  chunker.py  metadata.py

  retrieval/

    structured\_search.py  document\_search.py  hybrid\_search.py

  research/

    assistant.py  synthesis.py  evidence.py

  charts/financial\_charts.py

  companies/registry.py  lifecycle.py

  schemas/sqlite\_schema.sql

  tests/

  data/raw/  data/normalized/  data/documents/

  logs/

  agent.json

\`\`\`

\`macro.py\` and \`investor\_relations.py\` are stubbed early (interface only, no real

ingestion yet) so the schema never needs to change once they're filled in.

**## Source Adapters**

\`\`\`

SourceAdapter (interface)

     │

     ├── ScreenerAdapter            manual Excel/CSV export

     ├── NSEAdapter                 manually obtained CSV/XLS/XBRL/PDF

     ├── BSEAdapter                 manually obtained CSV/XLS/XBRL/PDF

     ├── InvestorRelationsAdapter   annual reports, presentations, transcripts

     └── MacroDataAdapter           RBI / MOSPI / IMD / commodity data (stub for now)

\`\`\`

The research/retrieval/calculation layers never depend on a vendor-specific format —

every adapter outputs the same \`NormalizedObservation\` / document shape. \`NSEAdapter\`

(manual) can later be swapped for a licensed-feed adapter, and Screener files for a

licensed vendor adapter, without touching anything downstream.

No automated scraping, CAPTCHA bypass, or circumvention of access controls — files are

manually obtained and dropped into \`data/raw/\`.

**## Data Layers: Raw → Normalized → Derived**

\`\`\`

RAW            data/raw/\<COMPANY>/\<source>/\<original files, untouched, kept forever>

      ↓

NORMALIZED     financial\_observations  — one row per (company, metric, period, source),

               full provenance preserved

      ↓

DERIVED        canonical\_financials    — reconciled "best" value per (company, metric,

               period), computed ratios/CAGR/growth (not stored, computed on demand)

      ↓

AI INDEX       document\_chunks (+ FTS5 / future embeddings) for retrieval

\`\`\`

**\*\*Non-company sources (RBI, IMD, MOSPI, IRDA)\*\*** — \`macro.py\` (step 8) has no

\`company\_id\` to key by, since none of these have one. Convention:

\`data/raw/\_macro/\<source>/\<file>\` — \`\_macro\` is a sentinel folder name

(leading underscore, never a valid ticker) sitting alongside the per-company

folders, so it's unambiguous at a glance and in code

(\`ingestion/detector.py::is\_macro\_path()\`).

\`\`\`

data/raw/

  HDFCBANK/screener/...          (existing, unchanged)

  \_macro/

    rbi/     — repo rate + dozens of other indicators (CSV/XLSX); circulars (PDF) not wired yet

    iitm/    — long-period regional/subdivisional rainfall series (fixed-width text, own parser)

    imd/     — rainfall data (registered source, no files ingested yet)

    mospi/   — GDP/inflation, if added later

    irda/    — only relevant once an insurer is actually in scope

\`\`\`

**\*\*Structured CSV ingestion is real, end-to-end\*\*** (\`sources/macro.py\`,

\`ingestion/pipeline.py::ingest\_macro\_file()\`) — \`python main.py ingest

data/raw/\_macro/imd/rainfall\_index.csv\` works today, the same as ingesting a

company file. \`financial\_observations.company\_id\` is \`NOT NULL\`, so this

data couldn't live there anyway; it has its own \`macro\_observations\` table

(\`series\_key\`/\`region\`/\`period\` instead of \`company\_id\`/\`fiscal\_year\`).

CSV columns: \`period\` ("YYYY" or "YYYY-MM" — calendar year, not fiscal year:

RBI/IMD publish by calendar year, and forcing India's Apr-Mar fiscal year

onto rainfall data would just be wrong), \`value\`, \`unit\`, optional \`region\`

(blank = all-India). \`series\_key\` defaults to the filename stem. RBI, IMD,

MOSPI, and IRDA are each their own row in \`sources\` (trust-rankable

individually, same as NSE/BSE/Screener) rather than one generic "macro"

placeholder. No reconciliation layer yet — with one source per series today

there's nothing to reconcile, same starting point \`financial\_observations\`

had before NSE/BSE (step 6).

**\*\*Narrative files (PDF circulars, policy reports) aren't wired up yet\*\*** —

they'd reuse the existing \`documents\`/\`document\_chunks\` pipeline as-is

(\`documents.company\_id\` is already nullable, so \`company\_id = NULL\` needs no

schema change). That pipeline itself now exists (page-scoped chunking + FTS5

keyword search, see [architecture.md's Document Retrieval]\(architecture.md#document-retrieval-retrievaldocument\_searchpy)),

but nothing in \`sources/\` fetches a macro narrative file in the first place —

this is a missing source, not a missing pipeline.

**\*\*Worked example:\*\***

\`\`\`

data/raw/HDFCBANK/screener/HDFCBANK.xlsx        (untouched)

        │  ScreenerAdapter.parse()

        ▼

financial\_observations:

  { company\_id: HDFCBANK, metric\_key: net\_profit, fiscal\_year: FY2025,

    value: 67347, unit: INR\_CRORE, source: screener, ...provenance }

        │  reconciliation (only source present → pass-through)

        ▼

canonical\_financials:

  { canonical\_value: 67347, reason: "only source available" }

        │  financials/ratios.py → roe(net\_profit, avg\_equity)

        ▼

research/assistant.py:

  "Net Profit FY2025: ₹67,347 Cr [FACT, source: screener]. ROE: 17.2% [CALCULATION]."

\`\`\`

If NSE later supplies the same metric, it's added as a **\*\*second\*\*** \`financial\_observations\`

row (the first is never overwritten) and reconciliation re-runs for that

(company, metric, period).

**## Schema**

The rationale below is why the schema is shaped this way — for the actual current

DDL, see [\`schemas/sqlite\_schema.sql\`]\(schemas/sqlite\_schema.sql) (the single

source of truth; it has grown well past what's sketched here, e.g.

\`macro\_observations\`, \`generated\_reports\`, \`llm\_call\_log\`, \`users\`,

\`watchlist\_items\`), or [architecture.md's Data model]\(architecture.md#data-model-sqlite-schemassqlite\_schemasql)

for a current, grouped summary.

**### Company Master**

Ticker symbols change; the internal \`company\_id\` does not. Identifier history is

tracked separately so renames/relistings never corrupt historical joins.

**### Company Lifecycle / Archiving**

Two statuses today: \`active\` → \`archived\` (future: \`inactive\`, \`delisted\`,

\`merged\`, \`acquired\`). Archiving only flips metadata (\`status\`, \`archived\_at\`,

\`archive\_reason\`) on the \`companies\` row — it never touches observations, documents, or

canonical data, so nothing is ever reconstructed on restore. Ingestion checks

\`status == active\` before running (a no-op today since ingestion is manual, but the

gate already exists). Queries default to active companies; historical/comparison

queries can explicitly include archived ones.

**### Financial Observations**

Raw, per-source, pre-reconciliation. The metric vocabulary is a lookup table, not

hardcoded columns, so a new metric (e.g. a bank-specific or manufacturing-specific one)

is a data row, not a migration.

**### Source / Provenance & Reconciliation**

Conflicting sources are never silently overwritten — both are kept, and the

reconciliation decision is itself recorded.

Default source priority for structured financial facts: \*\*NSE XBRL (once

validated for a period) → official company filing / hand-curated proprietary

data → licensed data provider → secondary financial source.\*\* NSE XBRL is the

target source of truth here (2026-08 directive) — once a reporting period has

a validated NSE observation on file, every other source is ineligible for

*\*that period\**, metric by metric: a metric the filing didn't report goes

blank rather than being backfilled from legacy data (\`storage/repositories.py\`'s

\`reconcile()\`/\`\_period\_is\_xbrl\_migrated()\`). Until a period is migrated, the

older default order still applies: official company filing → NSE/BSE filing →

licensed data provider → secondary financial source. This is what lets the

assistant say:

\> Net Profit FY2025: ₹X crore — Primary source: NSE filing. Cross-check: Screener.

**### Documents & Chunks**

Narrative documents (annual reports, presentations, transcripts, announcements) are

kept separate from structured financial data. File metadata lives in SQLite; the file

itself lives on the filesystem (future: object storage) — large binaries never go into

the relational database.

**## Ingestion Approach by Source**

\`\`\`

User downloads file → data/raw/\<company>/\<source>/ → ingestion command

   → detect source/type → parse → validate → normalize → store → index

\`\`\`

Only \`ScreenerAdapter\` (and \`sources/macro.py\`/\`sources/rbi\_\*.py\`/\`sources/iitm\_rainfall.py\`

for non-company data) is actually built today — NSE, BSE, and Investor Relations

below are the original design for [Implementation Sequence]\(#implementation-sequence)

steps 6-7, not yet started; see [FeatureList.md]\(FeatureList.md) for current status.

**\*\*Screener\*\*** (\`ScreenerAdapter\`) — \`.xlsx\` exports with fixed-ish sheet names (Profit &

Loss, Balance Sheet, Cash Flow, Quarters, Ratios, Shareholding), wide-format (years as

columns). Row labels aren't standardized across sectors (bank sheets say "Interest

Earned" instead of "Sales"), so mapping goes through a \`metric\_aliases\` table rather

than hardcoded row positions — a new alias is a data edit, not a code change. Transform

wide → long into \`financial\_observations\`; validate numeric parsing (strip commas,

handle blanks), fiscal-year headers ("Mar-24" → FY2024), consolidated vs standalone.

**\*\*NSE\*\*** (\`NSEAdapter\`) — dispatches by detected \`document\_type\`: financial results

(XBRL/PDF) → structured parser → observations; announcements/corporate actions

(PDF/text) → stored as \`documents\` (narrative, feeds retrieval, not the observation

table); shareholding pattern (XLS) → observations (\`shareholding\_promoter\_pct\`, etc.);

annual reports → routed into the document pipeline.

**\*\*BSE\*\*** (\`BSEAdapter\`) — mirrors NSE's shape. Since NSE and BSE filings are often the

identical filing submitted to two exchanges, they sit at the same \`trust\_rank\` tier. If

values match, BSE is linked as a confirming cross-check rather than a competing

canonical candidate; if they genuinely differ, both are kept and the conflict is

surfaced rather than auto-resolved.

**\*\*Documents\*\*** (\`InvestorRelationsAdapter\` + \`documents/\` pipeline):

\`\`\`

PDF/file → Text extraction → Section/heading detection

    → Chunking (with overlap) → Metadata attach → Index

\`\`\`

Each chunk retains company, document\_type, fiscal\_year, published\_at, source, filename,

page\_number, section\_heading, chunk\_index.

Every parser validates required columns, types, dates, currency, units, and company

identity; malformed data is rejected with a warning, never silently accepted.

**## Retrieval Architecture**

**\*\*Historical proposal, since implemented\*\*** — the diagram and "not yet built"

language below reflect this document's original drafting-time plan, kept

for its design rationale. Semantic retrieval, Qdrant, and a real

\`hybrid\_search.py\` module all exist today — see [architecture.md's Hybrid

Document Retrieval]\(architecture.md#hybrid-document-retrieval-retrievalhybrid\_searchpy)

and [Open Decisions]\(#open-decisions) below for current state.

\`\`\`

structured\_search.py   SQL over canonical\_financials/financial\_observations,

                        filtered by company/metric/period

document\_search.py     FTS5 keyword search + metadata filtering today;

                        pluggable semantic backend later (see Open Decisions)

hybrid\_search.py        combines both → typed Evidence objects with full provenance

\`\`\`

Built as named for structured data (\`retrieval/structured\_search.py\`).

Document evidence *\*for Q&A/Signals reports\** took a leaner shape than proposed

here — \`research/documents.py\` does direct PDF extraction per question, no

caching (see [architecture.md's Known gaps]\(architecture.md#documents--docs-tab)).

Page-scoped chunking + FTS5 keyword search were since built as their own

standalone capability (\`research/document\_chunker.py\`, \`retrieval/document\_search.py\`

— see [architecture.md's Document Retrieval]\(architecture.md#document-retrieval-retrievaldocument\_searchpy)),

and — unlike when this section was first drafted — \`retrieval/hybrid\_search.py\`

now exists too, combining FTS5 with Qdrant-backed semantic search and wired

into both Q&A and the Investigation Planner (see Open Decisions below).

Retrieval never calls the LLM. Full company archives are never sent to the model —

only retrieved, filtered evidence. The LLM client is a thin, swappable interface, so

provider and retrieval technique can each change independently of research logic.

**## Deterministic Calculation Layer**

\`\`\`

User → LLM interprets request → Retrieve financial observations

     → Python calculation → Validated result → LLM explains result

\`\`\`

\`financials/calculations.py\` — pure functions: \`cagr()\`, \`yoy\_growth()\`,

\`qoq\_growth()\`, \`rolling\_avg()\`.

\`financials/ratios.py\` — \`roa()\`, \`roe()\`, \`nim()\`, \`gnpa\_ratio()\`, margins — sector-aware,

raises a clear error if required inputs are missing rather than guessing.

\`financials/comparisons.py\` — \`peer\_compare(company\_ids, metric\_key, period\_range)\` for

tables/charts.

All operate on \`canonical\_financials\` via repositories and return typed results that

carry their own input citations, e.g. \*"CAGR = 4.2%, calculated from FY2023–FY2026

reported revenue."\* The LLM never performs a calculation Python can do deterministically.

Unit-tested against fixture values.

**## Evidence & Citations**

Every answer distinguishes FACT (a reported number or statement, with source),

CALCULATION (a deterministic computation, with inputs cited), MANAGEMENT STATEMENT

(quoted/paraphrased commentary, with source), and INFERENCE (reasoning that connects

facts — never presented as confirmed). This is still the current tagging scheme,

unchanged since this proposal — see [architecture.md's Key design

principles]\(architecture.md#key-design-principles) #1-#2 for how it's actually

enforced in code today (every LLM claim traces back to a specific retrieved

\`Evidence\` line; the LLM never computes a number itself).

Example, from this proposal's original scoping question (a weak monsoon's effect on

Mahindra & Mahindra):

\`\`\`

Fact:        Rural segment revenue declined 8%.  [Source: FY2026 investor presentation]

Calculation: Three-year revenue CAGR = 4.2%.      [FY2023–FY2026 reported revenue]

Inference:   Weak rural income may have contributed to slower tractor demand.

             [Evidence: management commentary + rainfall data + rural demand indicators]

\`\`\`

Causal chains (e.g. weak monsoon → agricultural output → rural income → tractor demand

→ M&M volume/margins) must have retrieved evidence behind each link — the LLM is not

allowed to construct unsupported causal narratives.

**## LLM Model Auto-Routing**

Original design intent: a deterministic keyword/heuristic router picks a model

tier per question (peer comparison / deep-analysis wording / evidence volume /

plain lookup) with no extra LLM call, rather than always calling the strongest

(most expensive) model. That's the concept that shipped — but the specific

tier→model assignments, resolution order, and cost-control policy (which

models are even allowed to run) have moved since this was written and now

live in one place: [\`config/settings.py\`]\(config/settings.py)'s "Model

tiering policy" section. See [architecture.md's Research / AI layer]\(architecture.md#research--ai-layer-context-optimization--model-routing--fallback)

for the full current pipeline (Context Optimizer → Hardness Evaluator → Model

Router → Fallback → Observability).

**## Implementation Sequence**

The roadmap as originally sequenced, kept stable so other docs (and code

comments) can cross-reference a step by number. \*\*Status against each step

lives in [FeatureList.md]\(FeatureList.md), not here\*\* — this section is the

plan and its rationale, not a running checklist.

1\. **\*\*Scaffold\*\*** — folder structure, SQLite schema init, config, logging. No adapters yet.

2\. **\*\*First companies via Screener only\*\*** — \`CompanyRegistry\`, \`ScreenerAdapter\`,

   ingestion pipeline (P&L/BS/CF/ratios/quarters) → observations → trivial canonical

   pass-through.

3\. **\*\*Calculation layer + CLI\*\*** — \`ratios.py\`/\`calculations.py\` +

   \`main.py analyze HDFCBANK\` producing a text report. Proves raw→normalized→derived

   end-to-end, no LLM yet.

4\. **\*\*Charts\*\*** — \`financial\_charts.py\` wired into the same CLI command.

5\. **\*\*Research assistant + LLM\*\*** — \`assistant.py\`, \`evidence.py\` (FACT/CALCULATION

   labeling). Answers **\*\*Question 1\*\*** (single-company deep dive) and **\*\*Question 2\*\***

   (peer comparison) — the original questions this system was designed to answer.

6\. **\*\*NSE + BSE for one company\*\*** — shareholding pattern, announcements as documents;

   exercise \`reconciliation\_log\` on a real two-source case.

7\. **\*\*Investor Relations + document pipeline\*\*** — annual report/presentation → chunking →

   FTS5 → \`hybrid\_search\`. Enables management-commentary questions; also the

   prerequisite for ingesting narrative macro files (RBI circulars, policy reports)

   mentioned in [Data Layers]\(#data-layers-raw--normalized--derived).

8\. **\*\*Add a non-financial-sector company\*\*** (e.g. M&M) — proves sector-specific metrics

   (segment revenue, volume, realization) fit the schema without redesign; add a

   real macro dataset (rainfall/repo-rate/etc.) via \`macro.py\`.

9\. **\*\*Question 3\*\*** — a company vs. a non-financial peer under a macro scenario (e.g.

   weak monsoon), exercising the FACT/INFERENCE distinction explicitly.

10\. **\*\*Round out to 5 companies\*\*** (stretch) — one more bank + one more non-financial

    peer, to confirm generality before considering Postgres/web UI.

**## Web UI Implementation Sequence**

Originally wireframed at \`claude.ai/design\` ("Vantage Research" — Research /

Companies / Investigations / Watchlist), superseding the read-only viewer

\`web/app.py\` started as. Continues numbering from

[Implementation Sequence]\(#implementation-sequence) step 10. Same rule as

that section: \*\*this is the plan and its rationale — current status and the

actual (sometimes differently-shaped) implementation live in

[FeatureList.md]\(FeatureList.md) and [architecture.md]\(architecture.md)\*\*, not

here. In particular, several tables proposed below by name

(\`research\_threads\`, \`research\_thread\_companies\`) were superseded by a

differently-shaped real implementation (\`generated\_reports\`,

\`research\_thread\_evidence\`, \`research\_thread\_followups\`) — see

architecture.md's Data model for what actually exists.

**\*\*11. Structured research-thread response\*\*** — the Research-result screen needs

each part of an answer addressable on its own: key finding, confidence label +

note, a collapsible methodology paragraph, an evidence *\*table\** (source /

document / period / type — columns, not prose), and follow-up question

suggestions — rather than one opaque tagged-text blob. Confidence should be

bounded by deterministic signal (evidence count, FACT/INFERENCE ratio) rather

than the LLM's unchecked self-report.

**\*\*12. Persist investigations\*\*** — every submitted question should be saved

(question, answer, evidence, timestamp) and reopenable as a read, not

recomputed from scratch — the basis for a real Investigations list, a

per-company Threads tab, and reuse-before-recompute.

**\*\*13. Hypothesis chains & cross-metric charts\*\*** — new capability, not a

reshaping of an existing one: a causal chain of named steps (optionally

forking into branches, e.g. rainfall → farm income → {tractor demand, loan

repayment}) backing indexed (rebased-to-100) comparison series across

*\*different\**, cross-domain metrics (a macro series against a financial one) —

\`financials/comparisons.py\` only peer-compares the *\*same\** metric across

companies today. This is [Implementation Sequence]\(#implementation-sequence)

step 9's macro-reasoning work given a concrete UI shape.

**\*\*14. Company page: tabbed profile\*\*** — extend a company's page from one flat

report into Overview / Financials / Commentary / Threads. Commentary

specifically needs real attributed pull-quotes ("Q4 FY24 earnings call"),

which requires the Investor Relations document pipeline (step 7) plus a

highlight/pull-quote extraction step document chunks don't provide today.

**\*\*15. Example investigations\*\*** — the Research-home screen shows curated

example questions before any real investigation exists, so the product isn't

empty on first run.

**\*\*16. Watchlist\*\*** — pin companies/investigations for quick return access.

**## MVP Delivery Plan**

Two questions this section originally answered, while the

[Web UI Implementation Sequence]\(#web-ui-implementation-sequence) wireframe

still ran on mock data with no backend: whether to ship it as a UI-only demo

("Track A" — mock fixtures, nothing computed from real data) or build the

minimum real backend needed to make it a working product ("Track B" —

structured/persisted investigations, real data everywhere the backend already

supported it, mocked only where it depended on ingestion that didn't exist

yet). **\*\*Decided and largely built: Track B\*\*** — see

[FeatureList.md]\(FeatureList.md) for exactly what's real vs. still mocked

today. The reasoning that led there: a wireframe's own information

architecture (reopenable Investigations, a per-company Threads tab, a

Watchlist) has no meaning against a stateless request/response viewer, so

"ship the UI, backend unchanged" was never actually on the table once the

goal was answering real questions about real companies — only the *\*scope\** of

"how much backend, how soon" was the open call.

**### What makes hypothesis-based research questioning** *\*strong\**

This is orthogonal to the UI and is the part most likely to be underestimated:

a chart with a smooth line and a confidence badge is easy to fake and easy to

mistake for rigor. What the current codebase's own discipline — deterministic

calculation, FACT/CALCULATION/INFERENCE tagging ([Evidence & Citations]\(#evidence--citations)) —

implies for a *\*hypothesis chain\** specifically, not just a single answer:

1\. **\*\*The chain is structured data, not LLM prose.\*\*** Steps and branches must be

   stored as typed rows (as in step 13), not parsed out of generated text after

   the fact — otherwise there's nothing to validate, no way to re-render it

   consistently, and no way to tell a well-supported chain from a fluent-sounding

   one.

2\. **\*\*Evidence is per-edge, not per-thread.\*\*** \`research/evidence.py\`'s \`Evidence\`

   list today backs one whole answer. A chain of "monsoon → farm income → tractor

   demand" needs each *\*link\** individually cited — otherwise a thread with 5 solid

   facts and one fabricated causal leap looks identical to one where every link

   is grounded.

3\. **\*\*No edge exists without a warrant.\*\*** Every step-to-step link must resolve to

   one of: (a) a cited management/analyst statement asserting the mechanism, (b)

   a deterministic/accounting relationship (e.g. lower repo rate → lower cost of

   funds), or (c) a computed statistical correlation *\*labeled as correlation\**.

   An LLM asserting plausibility on its own is none of these and shouldn't be

   allowed to originate a link.

4\. **\*\*Mechanisms come from a constrained playbook, not free generation.\*\*** Letting

   the model invent chain topology from scratch risks confident-sounding, made-up

   mechanisms. Safer: a small, growing library of named mechanism templates keyed

   by sector/metric pairs (rate cycle → NIM, monsoon → rural demand → asset

   quality, input cost → margin) that the assistant selects and instantiates with

   real ingested series, rather than free-generates.

5\. **\*\*Confidence is computed, not self-reported.\*\*** "Moderate confidence" should

   derive from measurable signal — correlation strength, consistency across

   sub-periods, number and independence of sources — combined with the

   FACT/CALCULATION/INFERENCE mix, not the LLM's own unchecked claim (this

   sharpens step 11's confidence field from "asked for" to "derived").

6\. **\*\*Hypotheses must be falsifiable, not just confirmable.\*\*** The wireframe's own

   follow-ups ("Test a one-year lag", "Compare drought years only") are robustness

   checks in disguise. The backend needs chain *\*templates\** that are parameterized

   (lag, sub-period, control company) and re-runnable — a real research tool

   surfaces disconfirming cases, not only the one narrative that happened to fit.

7\. **\*\*Cross-domain data needs the same provenance discipline as financials.\*\***

   Macro series (rainfall, repo rate, commodity prices) can't be a shortcut input

   — they need the same source/citation/versioning treatment as

   \`financial\_observations\`, or every cross-domain chain is only as trustworthy

   as an unsourced number.

8\. **\*\*Retrieval happens per hop, not once for the whole question.\*\*** A

   macro-to-company-to-peer chain needs independent retrieval (and independent

   evidence) at each hop — \`retrieval/structured\_search.py\`'s single

   comparison-evidence call doesn't generalize to a multi-hop chain without this.

9\. **\*\*Results are reproducible and know their own age.\*\*** A persisted investigation

   (step 12) should record what data snapshot it was computed against. If new

   data lands later, reopening an old thread should show the same conclusion it

   showed originally — with an explicit "refresh with latest data" action to

   recompute — never a silently different answer under an unchanged thread\_id.

None of this is required to make the *\*UI\** work — a chain of three plausible

strings and a smooth SVG line will render fine either way. It's required for

the chain to mean what the confidence badge claims it means.

The step-by-step build checklist, what's shipped since, and the open backlog

now live in [FeatureList.md]\(FeatureList.md) — split out to keep this

document focused on architecture and scoping decisions rather than a running

status list. Check there (plus \`git log\`/\`git status\`) for current state.

**## Open Decisions**

Two calls made while drafting this architecture, both since settled:

\- **\*\*Embeddings sequencing\*\*** — decided: FTS5 keyword search first, vector

  embeddings later once the keyword-only path proved out. This shipped as

  \`retrieval/document\_search.py\` (real, page-scoped chunking + FTS5), and the

  semantic layer has since followed: \`retrieval/hybrid\_search.py\` combines

  FTS5 with Qdrant-backed vector search (RRF-fused), wired into both Q&A and

  the Investigation Planner. See [architecture.md's Hybrid Document

  Retrieval]\(architecture.md#hybrid-document-retrieval-retrievalhybrid\_searchpy)

  for the current implementation.

\- **\*\*NSE/BSE trust-rank tie\*\*** — decided: both sit at the same priority tier (both are

  official exchange filings). Matching values → BSE recorded as a confirming

  cross-check. Differing values → both kept, conflict surfaced, no auto-resolution.

  NSE XBRL ingestion is real today ([Source / Provenance]\(#source--provenance--reconciliation));

  BSE still has no adapter — see [FeatureList.md]\(FeatureList.md).

**## Engineering Principles**

1\. Keep code human-readable; avoid unnecessary frameworks.

2\. Separate ingestion from research, structured data from documents, deterministic

   calculation from LLM reasoning.

3\. Preserve provenance and raw source material — never modify raw files.

4\. Never silently overwrite conflicting observations.

5\. Make every source and every storage layer replaceable.

6\. Prefer explicit logic over hidden framework behavior.

7\. Write unit tests for parsers and financial calculations.

8\. Build incrementally; optimize for correctness and traceability before autonomy.

9\. No multi-agent orchestration until a concrete requirement proves it necessary.

**---**

**## Related documentation**

This file is the original design proposal — why the system is shaped this way.

For everything else:

\- **\*\*[USER\_GUIDE.md]\(**USER\_GUIDE.md**)\*\*** — how to actually run it: setup, every CLI

  command, and troubleshooting.

\- **\*\*[architecture.md]\(**architecture.md**)\*\*** — the current, accurate technical

  picture (what's actually built, as opposed to what was originally proposed

  here).

\- **\*\*[FeatureList.md]\(**FeatureList.md**)\*\*** — what's shipped vs. still open, and the

  running status against this document's numbered roadmap steps.