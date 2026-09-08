# ADR-004: Hybrid Retrieval with FTS5, Qdrant, and Reciprocal Rank Fusion

- **Status:** Accepted
- **Decision scope:** Document evidence retrieval/ranking
- **Related:** `retrieval/document_search.py`, semantic search, hybrid search,
  retrieval diagnostics

## Context

Financial research queries contain two different retrieval problems.

### Lexical problem

Exact words matter for:

- company names;
- accounting terms;
- regulatory vocabulary;
- identifiers;
- management phrases;
- ticker/symbol references.

FTS5/BM25 is strong here.

### Semantic problem

Meaning may be expressed differently:

- paraphrases;
- synonyms;
- conceptually related descriptions;
- management language vs. analyst language.

Vector search is strong here.

Neither lexical-only nor vector-only retrieval is sufficient as the general retrieval
strategy.

A second problem is score incompatibility: BM25 relevance values and vector similarity
scores do not share a meaningful common scale.

## Decision

Run lexical and semantic retrieval independently, then fuse ranked candidates using
**Reciprocal Rank Fusion (RRF)**.

The current logical pipeline is:

`query`
→ FTS5/BM25 candidates  
→ Qdrant semantic candidates  
→ RRF rank fusion  
→ deduped/ranked document passages  
→ evidence path

Hybrid retrieval should gracefully degrade to lexical retrieval when vector infrastructure
is unavailable.

## Why RRF

RRF scores candidates by rank position rather than attempting to compare raw scoring scales.

Conceptually:

`RRF(document) = Σ 1 / (k + rank_in_retriever)`

for each retriever in which the document appears.

### Advantages

- deterministic;
- simple to test;
- no BM25/vector score normalization;
- rewards passages surfaced by both systems;
- robust baseline with limited tuning;
- easy to replace later if evaluation supports a better ranker.

## Alternatives considered

### Keyword-only retrieval

**Rejected as the end-state**
because semantic paraphrases may be missed.

### Vector-only retrieval

**Rejected**
because exact terminology, proper nouns, identifiers, and accounting phrases remain
important in equity research.

### Weighted raw-score fusion

**Problem**
BM25 and vector scores are not naturally comparable; normalization adds tuning assumptions
that may be dataset-dependent.

### Cross-encoder reranking of all candidates

Potentially stronger ranking quality, but adds:

- model latency;
- operational/model dependency;
- cost/compute;
- another probabilistic stage.

This remains a possible later stage once representative retrieval evaluation sets justify
the complexity.

### LLM-based ranking

Not preferred as the baseline because retrieval ranking should remain deterministic,
cost-efficient, and independently testable where possible.

## Consequences

### Positive
- exact-match precision plus semantic recall;
- deterministic fusion;
- straightforward diagnostics;
- graceful semantic degradation;
- independent tuning/replacement of lexical and vector candidate generators.

### Negative
- two indexes must be maintained;
- RRF uses rank, not nuanced calibrated relevance;
- retrieval evaluation becomes more important;
- candidate-set sizes and fusion constants require sensible defaults.

## Observability requirements

Hybrid retrieval should record diagnostics sufficient to answer:

- Did lexical search return candidates?
- Did vector search return candidates?
- Did vector search degrade/fail?
- How long did each retrieval method take?
- How many final passages came from lexical, vector, or both?
- What ranking/fusion configuration was used?

Raw sensitive/source evidence text does not need to be copied into diagnostics if stable
identifiers can provide traceability.

## Temporal and metadata filtering

Hybrid retrieval must continue respecting research constraints such as company scope and
`as_of` date.

Semantic similarity must never bypass temporal correctness.

## Future evolution

Possible later stages:

1. lexical + semantic candidate generation;
2. RRF;
3. metadata/diversity controls;
4. optional lightweight reranker;
5. evaluation-driven tuning.

Any future ranker should prove improvement on representative Signals research queries rather
than being adopted solely because it is more sophisticated.

## Revisit when

Revisit RRF when:

- a meaningful retrieval benchmark set exists;
- measurable errors are primarily ranking errors rather than candidate-recall errors;
- a reranker demonstrates material quality improvement at acceptable latency/cost;
- ranking needs user/domain personalization.

The invariant to preserve is:

> **Hybrid retrieval expands evidence discovery; it never changes the authoritative source
> of the evidence returned.**
