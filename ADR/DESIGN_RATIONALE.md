# Signals — Design Rationale

> **Purpose**
>
> This document explains **how the major architectural decisions in Signals fit together**.
> `architecture.md` remains the source of truth for **what is implemented today**.
> `README.md` explains the product vision and development approach.
> Individual Architecture Decision Records in `ADR/` are the canonical source for the
> rationale, alternatives, consequences, invariants, and revisit conditions of durable
> architectural decisions.
>
> This document is intentionally architectural and integrative: it is meant to help a CTO,
> architect, contributor, or reviewer understand the engineering judgment behind the system
> without duplicating every ADR.

---

## Documentation ownership rule

To prevent architecture documentation from drifting:

- **`README.md`** owns the product story, purpose, development approach, and high-level capability overview.
- **`architecture.md`** owns the current-state implementation: what exists today, how components connect, and known implementation gaps.
- **`DESIGN_RATIONALE.md`** owns the architectural thesis and explains how accepted decisions fit together.
- **`ADR/*.md`** owns the canonical record of durable individual architecture decisions.
- **Operational documents** such as `SCHEDULED_JOBS.md` own current schedules, commands, runbooks, and operating configuration.

When an accepted ADR exists, this document should **summarize and link to it rather than duplicate its complete rationale**.

An ADR being `Accepted` means the architectural direction is accepted. It does **not** by itself mean every part of that decision is already implemented. Current implementation status belongs in `architecture.md` and relevant operational documentation.

---

## 1. Architectural thesis

Signals is designed as an **evidence-grounded AI research system**, not as a chatbot that happens to have access to financial data.

The central architectural rule is:

> **Authoritative sources establish reported evidence. Deterministic systems own facts and calculations. Retrieval systems find evidence. Graph systems represent relationships. LLMs reason over evidence. The investigation layer governs what to investigate, whether evidence is sufficient, what execution is allowed, and when to stop.**

That separation exists because financial research has a different risk profile from general-purpose conversational AI. A fluent answer is not enough: material claims should be reproducible, attributable, temporally correct, and challengeable.

The architecture therefore optimizes for:

- factual traceability;
- regulatory-source provenance;
- separation of deterministic and probabilistic behavior;
- replaceable infrastructure boundaries;
- graceful degradation;
- explicit evidence/inference classification;
- evidence-driven iteration;
- bounded investigation autonomy;
- cost and model observability;
- source-aware ingestion;
- evolutionary architecture rather than premature distribution.

---

## 2. Core design principles

### 2.1 Reported evidence begins with authoritative sources

For reported financial facts, Signals prefers official regulatory and exchange filings as the primary evidence layer.

Examples include:

- NSE/BSE official filings and XBRL for India;
- SEC EDGAR filings and XBRL/iXBRL for the United States.

Secondary datasets may still support backfill, discovery, cross-validation, or gaps, but should not silently redefine an authoritative filed observation.

Signals must distinguish:

```text
Reported fact
    ↓
Normalized canonical fact
    ↓
Deterministically calculated metric
    ↓
Research interpretation / inference
```

See: `ADR/005-official-regulatory-filings-primary-evidence.md`.

---

### 2.2 Facts and calculations are deterministic and auditable

Authoritative business facts live in the relational data foundation. Examples include:

- canonical and raw financial observations;
- reconciliation history;
- macro observations;
- document metadata and evidence;
- investigations, hypotheses, evidence, and verdicts;
- deterministic indicator evaluations;
- LLM observability records.

The LLM does not own financial truth and is not used as the accounting engine when a stable, testable rule exists.

Signals computes reproducible values such as:

- YoY growth;
- CAGR;
- ratios;
- deterministic indicators;
- temporal eligibility;
- reconciliation outcomes;

before the reasoning model interprets them.

See: `ADR/006-deterministic-computation-probabilistic-reasoning.md`.

---

### 2.3 Relationships are different from facts

Signals uses a Knowledge Graph to answer questions such as:

- what entities are connected;
- which claims relate to the same risk, metric, product, or company;
- how claims connect across companies;
- whether multi-hop relationships surface potentially relevant research evidence.

The graph is a **projection from authoritative persisted data**, not an independent factual source of truth.

A graph path may be useful for forming or challenging a hypothesis, but:

> **relationship discovery ≠ factual proof ≠ causation**

See:

- `ADR/003-neo4j-graph-projection.md`
- `ADR/009-graph-relationships-as-research-inference.md`

---

### 2.4 Retrieval indexes are derived access structures

Signals uses complementary retrieval mechanisms:

- **FTS5 / BM25** for lexical retrieval;
- **Qdrant** for semantic similarity;
- **Reciprocal Rank Fusion (RRF)** for deterministic hybrid ranking;
- **Knowledge Graph traversal** for relationship-oriented discovery.

Authoritative evidence remains outside these retrieval indexes.

A retrieval store may identify what is relevant; it does not become the authority for the underlying evidence.

Derived stores should be rebuildable and should degrade capability rather than corrupt factual ownership if unavailable.

See:

- `ADR/002-qdrant-semantic-retrieval.md`
- `ADR/004-hybrid-retrieval-rrf.md`
- `ADR/014-rebuildable-derived-stores.md`

---

### 2.5 Deterministic processing and probabilistic research reasoning are separated

Signals prefers deterministic software whenever a task has a stable, explicit, testable rule.

Typical deterministic responsibilities include:

- source parsing where deterministic parsing is available;
- normalization;
- reconciliation;
- calculations and ratios;
- indicators;
- eligibility and temporal filters;
- retrieval scoring/fusion;
- execution-budget checks;
- termination checks.

Probabilistic reasoning is reserved for tasks where semantic judgment adds value, such as:

- hypothesis generation;
- qualitative interpretation;
- evidence evaluation;
- contradiction analysis;
- knowledge extraction where deterministic extraction is insufficient;
- synthesis;
- explanation.

See: `ADR/006-deterministic-computation-probabilistic-reasoning.md`.

---

### 2.6 The system is a modular monolith by design

Signals currently favors one deployable application with explicit internal seams rather than premature service decomposition.

Important seams include:

- repository modules;
- `FactStore`;
- backend-neutral persistence contracts;
- model/provider interfaces;
- embedding-provider interfaces;
- vector-store interfaces;
- planner/tool contracts;
- event/worker contracts.

The intent is **logical modularity before physical distribution**.

Service extraction should be driven by measurable operational pressure such as different scaling/SLA needs, deployment ownership, workload isolation, or SaaS concurrency.

See: `ADR/011-modular-monolith-before-microservices.md`.

---

## 3. Major architecture decisions

| Decision | Architectural purpose | Canonical ADR |
|---|---|---|
| SQLite as current authoritative relational store | Simple, deterministic ownership for the current scale | `ADR/001-sqlite-source-of-truth.md` |
| Qdrant semantic retrieval | Semantic evidence discovery without moving factual ownership | `ADR/002-qdrant-semantic-retrieval.md` |
| Neo4j graph projection | Relationship traversal without making the graph canonical | `ADR/003-neo4j-graph-projection.md` |
| FTS + vector hybrid retrieval using RRF | Combine exact lexical and semantic retrieval deterministically | `ADR/004-hybrid-retrieval-rrf.md` |
| Official regulatory filings as primary financial evidence | Keep reported facts close to legally filed disclosures | `ADR/005-official-regulatory-filings-primary-evidence.md` |
| Deterministic computation / probabilistic reasoning boundary | Keep reproducible truth outside model judgment | `ADR/006-deterministic-computation-probabilistic-reasoning.md` |
| Hypothesis-driven research pipeline | Separate hypothesis, evidence, evaluation, and synthesis | `ADR/007-hypothesis-driven-research-pipeline.md` |
| Bounded evidence-sufficiency loop | Allow iterative research without unbounded autonomy | `ADR/008-bounded-evidence-sufficiency-loop.md` |
| Graph relationships treated as inference | Prevent graph connectivity from becoming accidental causal proof | `ADR/009-graph-relationships-as-research-inference.md` |
| Model-provider abstraction and routing | Depend on reasoning capabilities rather than model brands | `ADR/010-model-provider-abstraction-routing.md` |
| Modular monolith before microservices | Preserve simplicity while creating future extraction seams | `ADR/011-modular-monolith-before-microservices.md` |
| Point-in-time evidence filtering | Prevent look-ahead bias before evidence reaches the model | `ADR/012-point-in-time-evidence-filtering.md` |
| Internal dataset events before external broker | Decouple ingestion and downstream processing without premature distributed infrastructure | `ADR/013-internal-dataset-events.md` |
| Rebuildable derived stores | Keep graph/vector/search indexes replaceable and degradable | `ADR/014-rebuildable-derived-stores.md` |
| Scheduler/execution boundary | Separate when work runs from how work is performed | `ADR/015-scheduler-execution-boundary.md` |
| Idempotent, incremental, self-healing jobs | Make recurring jobs safe to retry and efficient to operate | `ADR/016-idempotent-incremental-self-healing-jobs.md` |
| Source-aware scheduling cadence | Schedule around authoritative publication lifecycles, not arbitrary clock frequency | `ADR/017-source-aware-scheduling-cadence.md` |
| Investigation budget governance | Bound research cost, time, calls, and execution deterministically | `ADR/018-investigation-budget-governance.md` |
| Layout-aware document extraction | Preserve document structure when structure carries evidence meaning | `ADR/019-layout-aware-document-extraction.md` |
| Controlled SQLite → server-DB migration path | Retain SQLite simplicity today without coupling domain logic to it forever | `ADR/020-sqlite-to-server-database-migration.md` |

---

## 4. Data truth, provenance, and storage ownership

Signals deliberately separates **source authority** from **storage technology**.

The source hierarchy answers:

> Where did this reported fact originate?

The persistence layer answers:

> Where does Signals store and reconcile that observation?

For structured reported financial facts, regulatory filings are preferred evidence. The relational foundation stores canonical observations and provenance. Derived indexes and graph projections provide alternate access paths without changing ownership.

This means the architecture should be read as:

```text
Official / authoritative source
        ↓
Raw observation + provenance
        ↓
Canonical relational fact
        ↓
Deterministic derivation
        ↓
Evidence package
        ↓
Research reasoning
```

rather than as a collection of peer databases competing to define truth.

See:

- `ADR/001-sqlite-source-of-truth.md`
- `ADR/005-official-regulatory-filings-primary-evidence.md`
- `ADR/014-rebuildable-derived-stores.md`

---

## 5. SQLite today, controlled migration tomorrow

SQLite remains the current authoritative relational implementation because the dominant architectural problem today is correctness, provenance, and research-system design rather than distributed database operations.

The important long-lived choice is the storage boundary:

> **Business and research logic depend on persistence interfaces; backend-specific behavior stays localized.**

This allows Signals to retain SQLite's simplicity while preserving a future migration path to PostgreSQL or another server RDBMS if operational evidence requires it.

A migration should be driven by pressures such as:

- sustained concurrent writes;
- multiple application replicas;
- distributed workers;
- HA/replication requirements;
- larger SaaS concurrency;
- measurable storage bottlenecks.

See:

- `ADR/001-sqlite-source-of-truth.md`
- `ADR/020-sqlite-to-server-database-migration.md`

---

## 6. Retrieval architecture

### 6.1 Semantic retrieval

Qdrant solves semantic retrieval, not factual storage.

Documents/chunks are projected into a vector index that can be rebuilt from authoritative persisted evidence.

See: `ADR/002-qdrant-semantic-retrieval.md`.

### 6.2 Graph retrieval

Neo4j provides relationship traversal and multi-hop discovery. Graph-derived paths are useful research inputs but remain inference until supported by evidence.

See:

- `ADR/003-neo4j-graph-projection.md`
- `ADR/009-graph-relationships-as-research-inference.md`

### 6.3 Hybrid lexical + semantic retrieval

Financial research requires both exact and conceptual retrieval.

Examples benefiting from lexical search include:

- accounting terminology;
- company/executive names;
- identifiers;
- exact management phrases.

Examples benefiting from vector search include paraphrases and conceptually similar language.

RRF provides a deterministic baseline for combining heterogeneous candidate rankings without assuming BM25 and vector scores are directly comparable.

See: `ADR/004-hybrid-retrieval-rrf.md`.

---

## 7. Research architecture

### 7.1 Hypothesis-driven stages

Signals separates research responsibilities:

```text
Research question
      ↓
Hypothesis generation
      ↓
Planning
      ↓
Evidence gathering
      ↓
Evaluation
      ↓
Evidence sufficiency
      ↓
Synthesis
```

This reduces the chance that the first plausible narrative becomes the final answer without challenge.

See: `ADR/007-hypothesis-driven-research-pipeline.md`.

### 7.2 Evidence-sufficiency loop

An evaluator may determine that evidence is insufficient and request additional research.

That loop is bounded rather than open-ended.

See: `ADR/008-bounded-evidence-sufficiency-loop.md`.

### 7.3 Investigation budget governance

Evidence need and execution permission are separate concepts.

The evaluator may want more evidence while deterministic orchestration denies further execution because a configured budget has been reached.

Potential governed dimensions include:

- iterations;
- wall-clock duration;
- LLM calls;
- token/cost envelope;
- retrieval operations;
- graph expansion;
- repeated/no-new-evidence thresholds.

The principle is:

> **The Planner/Evaluator determines what would be useful. The Orchestrator determines what is allowed.**

See: `ADR/018-investigation-budget-governance.md`.

---

## 8. Model-routing architecture

Model selection is treated as policy rather than being hard-coded inside product features.

The routing subsystem can consider:

- capability requirement;
- reasoning hardness;
- model availability;
- cost;
- latency;
- privacy/deployment policy;
- provider behavior;
- fallback configuration.

The application should depend on **reasoning capability**, not on one model brand.

Some workflows may intentionally pin a model to preserve a known quality bar. That remains a policy decision rather than an architecture limitation.

See: `ADR/010-model-provider-abstraction-routing.md`.

---

## 9. Point-in-time correctness

Historical research must prevent future information from entering the evidence set.

If an investigation asks:

> What could the system have concluded using information available at date X?

then evidence unavailable at X must be filtered before model reasoning.

This is a deterministic eligibility rule, not a prompt instruction.

Signals should distinguish timestamps such as:

- reporting period;
- publication date;
- filing date;
- observation date;
- revision/restatement date;
- ingestion date.

See: `ADR/012-point-in-time-evidence-filtering.md`.

---

## 10. Document understanding

Financial PDFs can encode meaning through layout, not only plain text order.

Important structure can include:

- sections and headings;
- tables and header hierarchies;
- footnotes;
- columns;
- chart labels/captions;
- speaker attribution;
- page boundaries.

Where structure materially affects meaning, ingestion should preserve layout-aware metadata before semantic chunking and retrieval.

This does **not** replace authoritative structured sources. If structured regulatory data exists for a financial fact, that source remains preferred over probabilistic PDF interpretation.

Implementation maturity may vary by document type; accepted architecture direction and current implementation status must remain separately documented.

See: `ADR/019-layout-aware-document-extraction.md`.

---

## 11. Ingestion events and projection processing

Successful ingestion can trigger independent downstream work such as:

- financial reconciliation/derivation;
- knowledge extraction;
- lexical/vector indexing;
- graph projection;
- other derived processing.

Signals creates an event/worker boundary so ingestion does not need to know every downstream implementation.

The accepted architecture uses internal event contracts before introducing Kafka/RabbitMQ-class infrastructure. A future external broker should be adopted only when durable distributed execution, horizontal worker scaling, or stronger isolation is justified.

See: `ADR/013-internal-dataset-events.md`.

---

## 12. Scheduled-job architecture

Scheduling is treated as an orchestration concern rather than domain logic.

### 12.1 Scheduler versus job responsibility

The scheduler owns:

- cadence;
- triggering;
- enable/disable state;
- next-run timing.

The job owns:

- scope resolution;
- fetching;
- parsing;
- normalization;
- validation;
- persistence;
- downstream processing semantics.

Scheduled, manual, and administrative execution should converge on the same underlying job implementation.

See: `ADR/015-scheduler-execution-boundary.md`.

### 12.2 Idempotent and incremental execution

Recurring jobs must be safe to retry.

They should preferentially process:

- new data;
- incomplete data;
- changed data;
- bounded repair windows;

rather than rebuilding complete history on every run.

Transient failures should remain retryable, while verified source absence should not create endless failure loops.

See: `ADR/016-idempotent-incremental-self-healing-jobs.md`.

### 12.3 Source-aware cadence

A dataset's nominal periodicity does not necessarily tell Signals when the source is available.

For example:

```text
Quarterly data period
      ≠
Quarter-end availability
      ≠
Correct ingestion date
```

Schedules should follow the publication lifecycle of the authoritative source, including regulatory filing windows and release calendars.

Exact job schedules and operational configuration belong in `SCHEDULED_JOBS.md`.

See: `ADR/017-source-aware-scheduling-cadence.md`.

---

## 13. Deterministic indicators

Indicators represent repeatable factual patterns worth surfacing.

They remain versioned/configurable deterministic rules rather than LLM-generated judgments.

Indicators may become evidence for research reasoning, but their factual computation stays outside the LLM.

This is an application of the broader deterministic-computation boundary documented in ADR-006.

---

## 14. Prompt caching and reuse-before-recompute

Prompt caching and investigation-result reuse solve different cost problems.

### Prompt caching

Reduces provider-side processing of stable prompt context repeatedly sent to a model.

### Reuse-before-recompute

Avoids an LLM call entirely when an eligible, sufficiently similar prior research result can safely be reused.

Reuse must remain conservative, especially around temporal scope and materially different research intent.

This area remains an optimization policy rather than a separate canonical ADR at present.

---

## 15. Price history as an operationally distinct dataset

Daily OHLCV differs from canonical financial-statement evidence:

- it is high-volume;
- it is comparatively regenerable;
- its source policy differs;
- it serves market-history and research context;
- it should not redefine reported financial-statement facts.

Physical storage separation may be revisited if future relational architecture makes consolidation operationally beneficial.

---

## 16. Graceful degradation and rebuildability

Optional infrastructure should improve Signals without making basic research brittle.

Examples of intended behavior:

- semantic retrieval unavailable → lexical retrieval can continue where supported;
- Neo4j unavailable → core evidence remains intact;
- preferred LLM unavailable → router may use an allowed fallback;
- one derived worker fails → authoritative persisted evidence remains valid;
- vector/graph/search projections can be rebuilt from authoritative data.

The general rule is:

> **Derived capability may fail; authoritative evidence must remain intact.**

See: `ADR/014-rebuildable-derived-stores.md`.

---

## 17. Current architectural limits — intentionally visible

Accepted architecture direction and current implementation maturity are intentionally separate.

The following remain important implementation/evolution areas and should be tracked in `architecture.md` or specialized documentation:

### 17.1 Investigation budget enforcement maturity

ADR-018 establishes budget governance as the accepted direction.

Current implementation must separately state which budget dimensions are actually enforced today versus only observed/configured.

### 17.2 Long-document knowledge extraction

Search/indexing may cover more document content than structured Knowledge Builder extraction. Long annual reports can therefore have asymmetric coverage between retrieval and structured graph knowledge.

### 17.3 Layout-aware extraction maturity

ADR-019 establishes layout-aware extraction as the accepted design direction.

Current implementation should explicitly state which formats currently preserve sections, tables, footnotes, charts, page structure, and speaker attribution.

### 17.4 Entity resolution

Knowledge extraction requires stronger canonical identity resolution when entities appear under multiple names.

### 17.5 Automated source acquisition

Scheduling and source-aware ingestion have accepted design rules in ADR-015 through ADR-017. Current source coverage, schedules, and automation maturity belong in `SCHEDULED_JOBS.md` and `architecture.md`.

### 17.6 SaaS-scale runtime

The modular-monolith / SQLite architecture remains appropriate for the current stage.

ADR-011 and ADR-020 define the accepted evolution boundaries, but do not imply that service decomposition or server-database migration is required today.

These are evolution points, not reasons to introduce complexity before measurable pressure exists.

---

## 18. Scaling philosophy

Signals should scale **by pressure, not by fashion**.

### Current

Modular monolith + explicit interfaces + SQLite authoritative relational implementation.

### Evolution paths

**Storage pressure**  
→ migrate relational persistence behind existing contracts when operational evidence justifies it.

**Worker pressure**  
→ move internal event workers toward durable asynchronous/distributed execution.

**Retrieval pressure**  
→ scale or replace Qdrant/ranking independently while authoritative evidence ownership remains stable.

**Graph pressure**  
→ scale Neo4j independently while keeping graph data a projection.

**Application pressure**  
→ separate web, research execution, ingestion, and analytics only when scaling, SLA, or deployment ownership materially diverges.

The objective is not to avoid change. It is to make future change **localized, evidence-driven, and explainable**.

See:

- `ADR/011-modular-monolith-before-microservices.md`
- `ADR/013-internal-dataset-events.md`
- `ADR/020-sqlite-to-server-database-migration.md`

---

## 19. Architectural invariants

These are stronger than technology preferences and should survive backend/provider changes.

1. **The LLM does not become the source of factual truth.**
2. **Reported financial evidence should remain attributable to authoritative source disclosures.**
3. **Financial calculations and other stable rule-based operations remain deterministic.**
4. **Every material research conclusion should be traceable to evidence or explicitly identified as inference.**
5. **Vector, graph, and search stores remain derived/rebuildable unless a future ADR explicitly changes ownership.**
6. **Multi-hop graph connectivity is not automatically factual proof or causation.**
7. **Point-in-time research prevents look-ahead evidence before model reasoning.**
8. **Provider-specific model behavior stays behind provider interfaces where practical.**
9. **Research autonomy remains bounded by deterministic orchestration policy.**
10. **Budget exhaustion must not be represented as evidence sufficiency.**
11. **Scheduled work is idempotent and safe to retry.**
12. **Scheduling follows source availability rather than nominal data periodicity alone.**
13. **Document structure is preserved when needed to interpret evidence correctly.**
14. **Failure in optional derived infrastructure must not corrupt authoritative data.**
15. **Persistence technology may evolve without redesigning domain/research logic.**
16. **Architecture complexity is introduced only when measurable requirements justify it.**
17. **Current-state documentation must distinguish implemented behavior from accepted architectural direction.**

---

## 20. Decision-record index

All currently accepted Architecture Decision Records:

### Data ownership, storage, and retrieval

- [`ADR/001-sqlite-source-of-truth.md`](ADR/001-sqlite-source-of-truth.md)
- [`ADR/002-qdrant-semantic-retrieval.md`](ADR/002-qdrant-semantic-retrieval.md)
- [`ADR/003-neo4j-graph-projection.md`](ADR/003-neo4j-graph-projection.md)
- [`ADR/004-hybrid-retrieval-rrf.md`](ADR/004-hybrid-retrieval-rrf.md)
- [`ADR/005-official-regulatory-filings-primary-evidence.md`](ADR/005-official-regulatory-filings-primary-evidence.md)
- [`ADR/014-rebuildable-derived-stores.md`](ADR/014-rebuildable-derived-stores.md)
- [`ADR/020-sqlite-to-server-database-migration.md`](ADR/020-sqlite-to-server-database-migration.md)

### Research and reasoning

- [`ADR/006-deterministic-computation-probabilistic-reasoning.md`](ADR/006-deterministic-computation-probabilistic-reasoning.md)
- [`ADR/007-hypothesis-driven-research-pipeline.md`](ADR/007-hypothesis-driven-research-pipeline.md)
- [`ADR/008-bounded-evidence-sufficiency-loop.md`](ADR/008-bounded-evidence-sufficiency-loop.md)
- [`ADR/009-graph-relationships-as-research-inference.md`](ADR/009-graph-relationships-as-research-inference.md)
- [`ADR/010-model-provider-abstraction-routing.md`](ADR/010-model-provider-abstraction-routing.md)
- [`ADR/012-point-in-time-evidence-filtering.md`](ADR/012-point-in-time-evidence-filtering.md)
- [`ADR/018-investigation-budget-governance.md`](ADR/018-investigation-budget-governance.md)

### Runtime, ingestion, and evolution

- [`ADR/011-modular-monolith-before-microservices.md`](ADR/011-modular-monolith-before-microservices.md)
- [`ADR/013-internal-dataset-events.md`](ADR/013-internal-dataset-events.md)
- [`ADR/019-layout-aware-document-extraction.md`](ADR/019-layout-aware-document-extraction.md)

### Scheduling and operations

- [`ADR/015-scheduler-execution-boundary.md`](ADR/015-scheduler-execution-boundary.md)
- [`ADR/016-idempotent-incremental-self-healing-jobs.md`](ADR/016-idempotent-incremental-self-healing-jobs.md)
- [`ADR/017-source-aware-scheduling-cadence.md`](ADR/017-source-aware-scheduling-cadence.md)

There is intentionally no separate "potential future ADR" list for decisions already accepted above.

New ADRs should be added only when a new durable architectural choice needs a canonical permanent record.

---

## 21. How to read the architecture documentation

For a new reviewer:

1. **`README.md`** — product intent, ownership, and development approach.
2. **`Architecture-Visual.png`** — one-page architectural overview.
3. **`architecture.md`** — current implementation, component relationships, and current gaps.
4. **`DESIGN_RATIONALE.md`** — architectural thesis and how accepted decisions fit together.
5. **`ADR/`** — canonical record of why individual durable decisions were made.
6. **`SCHEDULED_JOBS.md` and other operating documents** — current operational configuration and execution behavior.
7. **Validation documents** — evidence that specific capabilities were exercised and measured.

This separation keeps the repository understandable and reduces documentation drift:

> **README tells the product story. Architecture describes the current system. Design Rationale explains how the decisions fit together. ADRs are the canonical record of durable decisions. Operational documents describe how the system runs today.**
