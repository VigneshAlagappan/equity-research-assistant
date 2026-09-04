# ADR-002: Qdrant for Semantic Document Retrieval

- **Status:** Accepted
- **Decision scope:** Semantic/vector retrieval
- **Related:** `retrieval/`, embedding provider abstraction, vector-store abstraction

## Context

FTS5/BM25 provides strong lexical retrieval but cannot reliably connect semantically similar
phrasing when the query and source passage use different words.

Financial research frequently contains such language differences:

- "interest-rate cuts" vs. "monetary easing";
- "asset-quality deterioration" vs. "rising stressed loans";
- "pricing pressure" vs. "margin compression".

Signals therefore needs semantic retrieval in addition to lexical search.

## Decision

Use **Qdrant** as the current vector index behind a `VectorStore` abstraction.

Embeddings are produced through an `EmbeddingProvider` abstraction.

Authoritative document chunks remain in the relational data store. Qdrant stores a
rebuildable semantic representation/index used to retrieve relevant chunk identifiers and
metadata.

## Why this decision

### 1. Dedicated vector-search capability
Qdrant is designed for similarity search and metadata-filtered vector retrieval rather than
forcing vector behavior into unrelated application logic.

### 2. Clear separation from authoritative storage
The vector database answers:

> Which chunks are semantically close to this query?

It does not answer:

> What is the canonical source text?

### 3. Replaceability
The application-facing retrieval design depends on `VectorStore`, not on Qdrant-specific
calls everywhere.

### 4. Independent operational evolution
Semantic retrieval can scale or change independently from canonical financial storage.

## Alternatives considered

### FAISS

**Advantages**
- lightweight local vector search;
- no external service.

**Trade-off**
- more application responsibility for persistence, metadata filtering, index lifecycle,
  and server-style concurrent access.

### pgvector

**Advantages**
- combines relational and vector infrastructure;
- attractive if PostgreSQL becomes the canonical backend.

**Why not current choice**
- current canonical backend is SQLite;
- adopting PostgreSQL only to host vectors would conflate two independent architecture
  decisions.

### Managed vector services

**Advantages**
- operational simplicity at scale.

**Why not current choice**
- unnecessary external dependency/cost for the current product stage;
- Signals intentionally preserves provider/backend replaceability.

### SQLite vector extensions

Potentially attractive for infrastructure consolidation, but current architecture already
has a dedicated `VectorStore` seam, so this can be evaluated later without changing
higher-level retrieval contracts.

## Consequences

### Positive
- semantic recall over paraphrased document content;
- dedicated vector-search semantics;
- independent scaling;
- clean degradation to lexical retrieval;
- vector backend can be changed behind the interface.

### Negative
- additional runtime infrastructure;
- embeddings and index must remain synchronized with document chunks;
- embedding-model/version changes may require backfill;
- semantic retrieval introduces new observability and evaluation requirements.

## Failure strategy

Qdrant is a derived index. If semantic retrieval is unavailable:

1. authoritative document chunks remain intact;
2. lexical retrieval can continue;
3. the research path should degrade rather than fail solely because the vector index is
   unavailable.

## Index lifecycle

The vector index should be populated from ingestion events and support idempotent backfill
for existing documents.

Changing embedding model/dimension should be treated as an index-versioning/backfill event,
not as a mutation of authoritative source evidence.

## Revisit when

Reconsider the backend when:

- vector volume or latency materially changes;
- hosted/multi-region requirements emerge;
- PostgreSQL becomes canonical and pgvector materially simplifies operations;
- another vector engine demonstrates better filtering/ranking economics;
- infrastructure consolidation becomes more valuable than dedicated vector functionality.

The invariant to preserve is:

> **Semantic indexes are derived retrieval infrastructure, not the source of research truth.**
