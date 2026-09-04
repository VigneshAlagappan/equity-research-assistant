# ADR-001: SQLite as the Authoritative Source of Truth

- **Status:** Accepted
- **Decision scope:** Core transactional / research fact ownership
- **Related:** `architecture.md`, `storage/`, `schemas/sqlite_schema.sql`

## Context

Signals needs a canonical home for financial observations, reconciled facts, macro data,
documents, extracted claims, investigation records, indicator configuration, provenance,
and observability.

The system also uses specialized derived stores/indexes:

- FTS5 for lexical document retrieval;
- Qdrant for vector retrieval;
- Neo4j for graph traversal.

Without an explicit ownership rule, a multi-store AI system can drift into a state where
different databases appear to hold competing versions of truth.

## Decision

SQLite-backed relational storage is the **authoritative source of truth** for the current
Signals architecture.

Qdrant, FTS5 indexes, and Neo4j are treated as derived/rebuildable retrieval or relationship
projections.

Business logic should depend on storage abstractions such as repositories, `FactStore`,
and backend-agnostic connection/row types instead of importing SQLite-specific behavior
throughout the codebase.

## Why this decision

### 1. Deterministic ownership
Financial research needs an unambiguous answer to:

> Where does the authoritative value live?

The relational database provides that answer.

### 2. Auditability
Raw observations can be retained, reconciliation decisions logged, and derived/canonical
values traced back to source records.

### 3. Transactional simplicity
The current product stage benefits more from correctness and low operational overhead than
from distributed database infrastructure.

### 4. Portability is handled through interfaces
The strategic choice is **relational canonical ownership behind a storage seam**, not a
permanent architectural dependency on a specific database engine.

## Alternatives considered

### PostgreSQL now

**Advantages**
- greater concurrency;
- server-side operational model;
- stronger path to multi-user SaaS scale;
- mature replication/HA ecosystem.

**Why not now**
- adds deployment and operational complexity before current workloads require it;
- does not materially improve the core research architecture at today's scale.

### Neo4j as the primary data store

**Advantages**
- native relationship traversal;
- one store for graph-shaped queries.

**Why rejected**
- financial facts, reconciliation, observations, and transactional records are naturally
  relational;
- it would blur the boundary between factual ownership and relationship projection.

### Vector database as primary document store

**Why rejected**
Vector similarity is a retrieval mechanism. It should not decide what source text is
authoritative.

## Consequences

### Positive
- clear factual ownership;
- easy local operation;
- strong provenance;
- simple backups;
- FTS5 available for lexical search;
- graph/vector projections can be rebuilt;
- business logic remains insulated through repository/store boundaries.

### Negative
- limited concurrent write throughput;
- no native multi-node HA;
- future SaaS scaling may require migration;
- some SQLite-specific schema/migration behavior remains inside storage.

## Guardrails

1. Application modules outside storage should not introduce new direct SQLite dependencies
   without a deliberate reason.
2. Derived stores must carry identifiers/provenance that allow results to resolve back to
   authoritative records.
3. A Qdrant or Neo4j outage must not redefine or corrupt factual ownership.
4. Migration to another transactional backend should occur behind the storage layer.

## Revisit when

Reconsider the physical database backend when one or more are true:

- sustained multi-user concurrent writes become material;
- horizontal application scaling requires a shared transactional server;
- HA/replication/RPO/RTO requirements exceed SQLite's operating model;
- deployment architecture becomes multi-tenant SaaS;
- database size/locking becomes an observed bottleneck.

The likely future decision is not "abandon relational truth"; it is "replace the SQLite
implementation behind the existing storage boundary."
