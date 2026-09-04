# Signals — Design Rationale

> **Purpose**
>
> This document explains **why** the major architectural choices in Signals exist.
> `architecture.md` remains the source of truth for **what is implemented today**.
> `README.md` explains the product vision and development approach.
>
> This file is intentionally decision-oriented: it is meant to help a CTO, architect,
> contributor, or reviewer understand the engineering judgment behind the system without
> having to infer that reasoning from the implementation.

---

## 1. Architectural thesis

Signals is designed as an **evidence-grounded AI research system**, not as a chatbot that
happens to have access to financial data.

The central architectural rule is:

> **Deterministic systems own facts and calculations. Retrieval systems find evidence.
> Graph systems represent relationships. LLMs reason over evidence. The investigation
> layer decides what to investigate, when evidence is insufficient, and when to stop.**

That separation exists because financial research has a different risk profile from
general-purpose conversational AI. A fluent answer is not enough: material claims should
be reproducible, attributable, and challengeable.

The architecture therefore optimizes for:

- factual traceability;
- separation of deterministic and probabilistic behavior;
- replaceable infrastructure boundaries;
- graceful degradation;
- explicit provenance;
- evidence-driven iteration;
- cost and model observability;
- evolutionary architecture rather than premature distribution.

---

## 2. Core design principles

### 2.1 Facts are deterministic and auditable

Authoritative business facts live in the relational data foundation. Examples include:

- canonical financial observations;
- raw financial observations and reconciliation history;
- macro observations;
- document metadata and document chunks;
- extracted knowledge claims and their evidence;
- investigations, hypotheses, evidence, and verdicts;
- indicator configuration and evaluations;
- LLM observability records.

The LLM does **not** own these values and is not expected to calculate financial metrics
from raw prose.

**Why:**  
Financial analysis must remain reproducible. If revenue growth, ROE, a macro series, or a
reported ratio changes, the system should be able to show exactly which source record and
calculation produced it.

**Consequence:**  
The LLM is used where it is strongest — interpretation, hypothesis generation,
evaluation, and synthesis — without becoming the accounting engine or database.

---

### 2.2 Relationships are a different concern from facts

Signals uses a Knowledge Graph to answer questions such as:

- what entities are connected;
- which claims relate to the same risk, metric, product, or company;
- how claims connect across companies;
- whether multi-hop relationships surface potentially relevant research evidence.

The graph is **projected from authoritative relational data**. It is not a second factual
source of truth.

**Why:**  
A graph database is valuable because of traversal and relationship semantics, not because
it should own financial values that already have a deterministic home.

**Consequence:**  
Neo4j can be rebuilt from SQLite-backed knowledge and financial data. Losing the graph
reduces relationship-search capability; it does not destroy the underlying evidence.

See: `ADR/003-neo4j-graph-projection.md`.

---

### 2.3 Retrieval indexes are disposable projections

Signals uses multiple retrieval mechanisms over the same evidence:

- **FTS5 / BM25** for lexical retrieval;
- **Qdrant** for vector similarity;
- **hybrid retrieval** to combine the two;
- **Knowledge Graph traversal** for relationship-oriented retrieval.

The authoritative document text/chunks remain outside Qdrant.

**Why:**  
Keyword search and vector search solve different retrieval problems. Exact company names,
accounting terms, identifiers, and phrases benefit from lexical retrieval; paraphrases and
conceptual similarity benefit from semantic retrieval.

**Consequence:**  
The retrieval index can be rebuilt, replaced, tuned, or temporarily degraded without
changing factual ownership.

See:
- `ADR/002-qdrant-semantic-retrieval.md`
- `ADR/004-hybrid-retrieval-rrf.md`

---

### 2.4 Deterministic processing and probabilistic reasoning are separated

The following are intentionally deterministic:

- source parsing;
- normalization;
- source reconciliation;
- financial calculations;
- ratios;
- configurable indicators;
- chunk creation;
- lexical retrieval;
- vector similarity retrieval;
- hybrid rank fusion;
- evidence filtering and temporal scoping;
- orchestration termination checks.

LLMs are used for tasks where semantic judgment adds value:

- knowledge extraction;
- hypothesis generation;
- hypothesis evaluation;
- research synthesis;
- selected macro-series interpretation;
- user-facing explanation and narrative.

**Why:**  
An LLM should not be used simply because it can perform a task. Deterministic code is
preferred when the task has a stable, testable rule.

---

### 2.5 The system is a modular monolith by design

Signals currently keeps application capabilities in one Python application with explicit
module boundaries rather than decomposing prematurely into microservices.

The codebase still creates seams through:

- repository modules;
- `FactStore`;
- backend-agnostic database types;
- model/provider interfaces;
- embedding-provider interfaces;
- vector-store interfaces;
- planner capabilities;
- event/worker contracts.

**Why:**  
Microservices would introduce distributed failure modes, deployment complexity, service
discovery, remote contracts, tracing, retries, and consistency concerns before those costs
are justified.

**Trade-off:**  
A single process limits independent horizontal scaling and workload isolation.

**Revisit when:**  
Different workloads have materially different scaling/SLA requirements, multiple teams
need independent deployment ownership, or multi-user SaaS concurrency makes process-level
separation operationally valuable.

---

## 3. Major architecture decisions

| Decision | Why chosen | Alternatives considered | Trade-offs | Revisit when |
|---|---|---|---|---|
| **SQLite as the authoritative source of truth** | Simple, deterministic, auditable ownership; excellent fit for current scope; supports FTS5 and transactional provenance | PostgreSQL, graph-first persistence, document DB | Lower concurrency and HA capability than a server database | Multi-user SaaS, sustained concurrent writes, HA/replication needs |
| **Storage abstractions around SQLite** | Prevent business logic from depending directly on SQLite implementation details | Direct `sqlite3` access throughout app | Additional interfaces/repository code | Keep; replace implementation if backend changes |
| **Neo4j as a graph projection** | Relationship traversal and multi-hop exploration without duplicating factual ownership | SQL-only graph traversal, Neo4j as primary truth store, RDF/triple store | Projection/sync lifecycle; another runtime service | Graph volume/query complexity justifies stronger graph operational role |
| **Qdrant as semantic index** | Dedicated vector retrieval with a replaceable `VectorStore` seam | FAISS, pgvector, Pinecone/managed vector stores, vector-in-SQLite | Extra service/index lifecycle | Infra consolidation, hosted scale, operational requirements |
| **FTS5 retained alongside vector retrieval** | Exact terms, names, accounting vocabulary, identifiers remain important | Vector-only retrieval | Maintains two retrieval indexes | Only if evidence shows lexical retrieval adds no value |
| **RRF hybrid retrieval** | Deterministic fusion without requiring score calibration between BM25 and vector similarity | Weighted normalized score fusion, vector-only, cross-encoder-first | Ranking is simple rather than learned | Sufficient retrieval evaluation data justifies learned/reranked fusion |
| **Planner → evidence gathering → evaluator → synthesizer** | Separates hypothesis creation, evidence collection, challenge, and narrative | One large prompt; free-running multi-agent framework | More orchestration code and persistence | Expand only if independent planning/delegation materially improves quality |
| **Evidence-sufficiency loop** | A research system should recognize incomplete evidence rather than synthesize prematurely | Single-pass RAG | More latency and LLM cost | Keep; add stronger budget governance |
| **Bounded multi-hop KG evidence** | Finds indirect relationships while preventing graph explosion and overclaiming | Unlimited traversal; single-hop only | May miss deeper useful paths | Evaluation demonstrates need for larger/learned traversal |
| **Multi-hop results classified as inference** | Connectivity is not proof | Treat graph paths as facts | More conservative outputs | Should remain an invariant |
| **Event bus inside modular monolith** | Decouples ingest from post-ingest processing, supports replay/versioned workers without Kafka-class infrastructure | Direct function chaining, Kafka/RabbitMQ | Synchronous/in-process throughput | Workloads need independent scaling or durable async execution |
| **Model router + provider abstraction** | Capability-based model choice, fallback, cost control, provider replaceability | Hard-coded one-model calls | Routing policy requires maintenance | Continue evolving as provider economics/capabilities change |
| **Prompt caching for stable evidence** | Reduce repeated token cost for recurring research over the same evidence | Resend full prompt every time | Provider-specific optimization | Provider behavior changes or cache economics no longer help |
| **Reuse-before-recompute with lexical + semantic matching** | Avoid unnecessary LLM work while supporting paraphrases | Exact match only, semantic-only reuse | False reuse is a research risk, so thresholds stay conservative | Better evaluation set supports safer recall |
| **Point-in-time (`as_of`) filtering** | Prevent look-ahead bias in historical research | Prompt-only instruction | Some capabilities cannot operate historically | Keep as a research correctness invariant |
| **Deterministic indicator framework** | Surface repeatable factual patterns without asking an LLM to rediscover them | LLM-generated insights only | Rules need explicit implementation/config | Expand as new indicator families prove useful |
| **Separate price-history store** | Price history is regenerable, high-volume, and operationally distinct from canonical research facts | Main database table | Cross-store coordination | Consolidate if backend migration makes separation unnecessary |

---

## 4. Why SQLite remains the authoritative fact store

SQLite is not being used because the architecture assumes the system will never scale.
It is used because **today's dominant problem is correctness and research-system design,
not distributed database operations**.

The important architectural choice is not merely "SQLite". It is:

> **Business logic depends on storage interfaces, while the storage package owns backend
> details.**

This gives Signals a controlled migration path. If PostgreSQL becomes necessary, the
desired architectural change is to replace/adapt storage implementations rather than
rewrite research, financial, indicator, graph, and web logic.

SQLite also contributes FTS5 lexical search today, but that does not make the application
contractually dependent on FTS5 as the only retrieval strategy.

See `ADR/001-sqlite-source-of-truth.md`.

---

## 5. Why Qdrant exists beside SQLite

Qdrant solves **semantic retrieval**, not factual storage.

Documents are chunked from authoritative source material. Their semantic representations
are then projected into the vector store. The vector index can be recreated from the
document chunks.

This architecture intentionally avoids:

> "The vector database says this is the source."

Instead:

> "The vector database says this chunk is semantically relevant; the authoritative
> evidence is still the source document/chunk and provenance record."

An `EmbeddingProvider` and a `VectorStore` abstraction keep both the embedding model and
vector backend replaceable.

See `ADR/002-qdrant-semantic-retrieval.md`.

---

## 6. Why Neo4j is a projection rather than canonical storage

A financial research system has two different information models:

### Factual model
Examples:

- company X reported revenue Y;
- RBI series Z had value V on date D;
- a document published on date D contains statement S.

### Relationship model
Examples:

- company is exposed to a risk;
- macro factor may affect a metric;
- claim concerns an entity;
- one company is connected to another through shared concepts;
- a claim is supported by a piece of evidence.

Relational storage is an appropriate home for the former. Graph traversal is a strong
tool for the latter.

Signals therefore projects relationally persisted knowledge into Neo4j and constrains
multi-hop traversal so graph connectivity does not become accidental factual authority.

See `ADR/003-neo4j-graph-projection.md`.

---

## 7. Why hybrid retrieval uses FTS5 + Qdrant + RRF

Vector-only retrieval is attractive conceptually but insufficient for financial research.

A user may ask for:

- a precise accounting term;
- a company or executive name;
- an identifier;
- an exact management phrase;
- a concept expressed with different wording.

Lexical and semantic retrieval therefore run as complementary candidate generators.

Signals combines them using **Reciprocal Rank Fusion (RRF)** rather than trying to compare
raw BM25 scores directly against vector similarity scores.

**Why RRF:**

- deterministic;
- easy to explain;
- no score normalization dependency;
- robust when candidate sets come from different retrieval systems;
- simple to evaluate and replace later.

This is a deliberate baseline, not a claim that RRF is the final ranking method.

Potential later evolution:

1. hybrid candidate generation;
2. metadata/temporal filtering;
3. optional reranker;
4. retrieval-quality evaluation;
5. learned ranking only when enough representative queries exist.

See `ADR/004-hybrid-retrieval-rrf.md`.

---

## 8. Why the LLM never owns financial calculations

LLMs are capable of arithmetic, but that does not make them the appropriate calculation
engine.

Signals computes values such as YoY growth, CAGR, ratios, and deterministic indicators in
Python/SQL before the model sees them.

The model receives a compact Evidence block and is asked to interpret it.

Benefits:

- reproducibility;
- testing;
- easier debugging;
- lower hallucination risk;
- clear attribution;
- calculation logic can be audited independently of model behavior.

This is a core system invariant, not merely a prompt instruction.

---

## 9. Why Signals uses specialized research stages rather than one prompt

A single large prompt could theoretically:

1. invent hypotheses;
2. gather context;
3. judge those hypotheses;
4. write the answer.

Signals deliberately separates these responsibilities.

### Hypothesis Generator
Produces competing explanations rather than immediately committing to one narrative.

### Planner / capability layer
Collects evidence from structured facts, macro data, documents, indicators, and graph
relationships.

### Evaluator
Challenges each hypothesis against evidence and can return `INSUFFICIENT_EVIDENCE`.

### Orchestrator
Controls bounded iteration and termination.

### Synthesizer
Ranks and narrates only after evidence/evaluation has occurred.

**Why:**  
This makes research behavior more inspectable and reduces the chance that the model's
first narrative becomes the system's conclusion without challenge.

---

## 10. Why the evidence-sufficiency loop is bounded

Signals allows an insufficient-evidence verdict to cause additional retrieval and
re-evaluation.

The loop is bounded using controls such as:

- evidence sufficiency;
- maximum evidence iterations;
- wall-clock deadline;
- no-new-evidence detection.

This is intentionally more constrained than an open-ended autonomous agent.

**Why:**  
Financial research benefits from iteration, but an unconstrained loop creates cost,
latency, nontermination, and unpredictable behavior.

### Known next step: budget governance

The current architecture can observe and attribute LLM cost, but the Orchestrator should
eventually treat budget as an execution constraint.

A future `InvestigationBudget` should be able to express controls such as:

- maximum LLM calls;
- maximum input/output tokens;
- maximum estimated cost;
- maximum retrieval operations.

The principle should be:

> **The Planner decides what would be useful. The Orchestrator decides what is allowed.**

---

## 11. Why multi-hop graph evidence stays an inference

A path such as:

`Company → Risk → MacroFactor → Metric`

may be useful for forming or challenging a research hypothesis.

It does **not** prove that the macro factor caused the company's metric to change.

Signals therefore:

- bounds multi-hop traversal;
- limits the amount of graph evidence added to context;
- deduplicates results;
- treats multi-hop results as inference.

This preserves an important distinction:

> **relationship discovery ≠ factual proof ≠ causation**

---

## 12. Why ingestion uses an event bus without a message broker

Successful ingestion can cause several independent follow-on actions:

- reconciliation / financial derivation;
- knowledge extraction;
- chunk indexing;
- vector indexing;
- other future projections.

Hard-wiring these actions into the ingestion pipeline would couple ingestion to every
downstream feature.

Signals instead publishes dataset-ingested events to versioned workers.

The event mechanism is:

- persisted;
- replayable;
- idempotent at worker-processing level;
- failure-isolated;
- in-process.

**Why no Kafka/RabbitMQ yet:**  
The current problem requires decoupling, replay, and worker isolation — not distributed
message throughput.

The current design preserves a natural migration point if asynchronous/distributed
execution becomes necessary.

---

## 13. Why model routing is a separate subsystem

Model selection is treated as policy rather than being hard-coded inside product features.

The routing layer separates:

- task complexity;
- model capability metadata;
- policy-disabled models;
- provider-specific API behavior;
- fallback behavior;
- observability.

This makes it possible to change model economics and capabilities without rewriting every
research call site.

Some paths may still pin a model intentionally to preserve a known quality bar. That is a
product policy choice rather than an architecture limitation.

---

## 14. Why prompt caching and semantic reuse are separate

These mechanisms solve different cost problems.

### Prompt caching
Reduces provider-side processing of stable prompt context that is sent repeatedly.

### Reuse-before-recompute
Avoids the LLM call entirely when a sufficiently similar recent research result already
exists.

Signals combines lexical and embedding-based reuse checks conservatively and guards
against temporal-period conflicts.

**Why conservative:**  
In research, a false cache/reuse hit can silently answer the wrong question. Missing a
reuse opportunity costs money; incorrect reuse costs trust.

---

## 15. Why `as_of` is enforced in retrieval rather than prompts

Historical research can ask:

> What could the system have concluded using only information available at date X?

If post-X evidence is retrieved and then the prompt says "pretend you don't know it," the
system already has look-ahead leakage.

Signals therefore applies the cutoff in evidence retrieval.

**Design rule:**

> Temporal correctness belongs in the data/retrieval boundary, not in natural-language
> model obedience.

This principle should remain even if models, databases, or retrieval backends change.

---

## 16. Why indicators are deterministic

Indicators represent factual patterns worth noticing, such as changes in holdings or
financial trajectories.

They are deliberately implemented as versioned deterministic rules with configurable
scope rather than LLM-generated judgments.

They can later become evidence used by research agents, but their factual computation
remains outside the LLM.

This gives:

- testability;
- configurable thresholds;
- repeatability;
- auditability;
- separation between observation and interpretation.

---

## 17. Why price history is operationally separate

Daily OHLCV data is different from canonical financial-statement evidence:

- it is high-volume;
- it is cheaply regenerable;
- its source policy is different;
- it is primarily display/market-history data;
- it is not treated as authoritative financial-statement input.

Keeping it in a separate store avoids turning regenerable market data into a dependency of
the core research database.

This separation can be revisited if a future server-database architecture makes physical
database separation unnecessary.

---

## 18. Graceful-degradation strategy

Optional infrastructure should improve Signals without making basic research brittle.

Examples of intended behavior:

- semantic retrieval unavailable → lexical retrieval can continue;
- Neo4j unavailable → graph capability can fall back where supported;
- preferred LLM unavailable → router can attempt an allowed fallback;
- one ingestion worker fails → other workers can still complete;
- graph/vector indexes can be rebuilt from authoritative data.

This reflects a general principle:

> **Derived capability may fail; authoritative evidence must remain intact.**

---

## 19. Current architectural limits — intentionally visible

A design rationale should state not only why choices were made, but also where they stop
being sufficient.

The most important current limits include:

### 19.1 Investigation budget enforcement
Per-investigation cost can be observed, but cost/token/call limits are not yet a
termination condition.

### 19.2 Long-document knowledge extraction
Search can index the whole document, but structured Knowledge Builder extraction is still
bounded by the extraction-size limit. Long annual reports therefore have asymmetric
coverage between search and graph knowledge extraction.

### 19.3 Layout-aware document understanding
Sentence-aware chunking is stronger than arbitrary fixed cuts, but financial PDFs contain
sections, tables, footnotes, charts, and layout that plain text extraction does not fully
preserve.

### 19.4 Entity resolution
Knowledge extraction still needs stronger canonical identity resolution for entities that
can appear under multiple names.

### 19.5 Automated source acquisition
The ingestion/event architecture is capable of processing new material, but complete
automated/recurring official-source acquisition is still evolving.

### 19.6 SaaS-scale runtime
The current modular-monolith / SQLite / in-process-worker architecture is appropriate for
the present product stage. A high-concurrency multi-tenant SaaS deployment would require
additional runtime and operational architecture.

These are explicit evolution points rather than reasons to introduce complexity before
the need exists.

---

## 20. Scaling philosophy

Signals should scale **by pressure, not by fashion**.

### Current
Modular monolith + clear interfaces.

### Likely evolution path

**Storage pressure**
→ SQLite implementation can migrate behind repository/store interfaces to PostgreSQL or
another transactional backend.

**Worker pressure**
→ event workers can move to durable asynchronous execution.

**Retrieval pressure**
→ Qdrant can scale independently; ranking/reranking can evolve without changing document
ownership.

**Graph pressure**
→ Neo4j can become a separately scaled relationship service while authoritative fact
ownership remains unchanged.

**Application pressure**
→ Flask/web concerns, research execution, ingestion, and analytics can separate only when
their scaling or deployment requirements diverge.

The architectural objective is not to avoid change. It is to make future change **localized
and explainable**.

---

## 21. Architectural invariants

These are stronger than technology preferences and should survive backend/provider changes.

1. **The LLM does not become the source of factual truth.**
2. **Financial calculations remain deterministic.**
3. **Every material research conclusion should be traceable to evidence or explicitly
   identified as inference.**
4. **Vector and graph stores remain derived/rebuildable unless an explicit future ADR
   changes that ownership model.**
5. **Multi-hop graph connectivity is not automatically factual evidence or causation.**
6. **Point-in-time research must prevent look-ahead evidence at retrieval time.**
7. **Provider-specific capabilities stay behind provider interfaces where practical.**
8. **A failure in optional derived infrastructure must not corrupt authoritative data.**
9. **Architecture complexity should be introduced only when a measurable requirement
   justifies it.**
10. **Current-state documentation must distinguish implemented behavior from roadmap intent.**

---

## 22. Decision-record index

Detailed ADRs currently maintained:

- [`ADR/001-sqlite-source-of-truth.md`](ADR/001-sqlite-source-of-truth.md)
- [`ADR/002-qdrant-semantic-retrieval.md`](ADR/002-qdrant-semantic-retrieval.md)
- [`ADR/003-neo4j-graph-projection.md`](ADR/003-neo4j-graph-projection.md)
- [`ADR/004-hybrid-retrieval-rrf.md`](ADR/004-hybrid-retrieval-rrf.md)

Potential future ADRs worth adding only when the decisions need a permanent record:

- modular monolith vs. service decomposition;
- deterministic calculation boundary;
- event bus vs. external message broker;
- model-router policy;
- investigation budget governance;
- layout-aware document extraction;
- migration from SQLite to a server database.

---

## 23. How to read the architecture documentation

For a new reviewer:

1. **`README.md`** — product intent, ownership, development approach.
2. **`Architecture-Visual.png`** — one-page visual overview.
3. **`architecture.md`** — current implementation and known gaps.
4. **`DESIGN_RATIONALE.md`** — why the important design choices exist.
5. **`ADR/`** — durable records for the highest-impact individual decisions.
6. **Validation documents** — evidence that specific research/retrieval capabilities were
   exercised and measured.

This separation keeps the repository understandable:

> **README tells the story. Architecture describes the system. Design Rationale explains
> the judgment. ADRs preserve the decisions.**
