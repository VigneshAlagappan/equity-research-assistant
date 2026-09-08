# ADR-014 — Derived Stores and Indexes Must Be Rebuildable and Gracefully Degradable

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal uses specialized storage technologies for specialized access patterns.

Examples include:

```text
Relational Store
    → canonical facts and authoritative evidence

Qdrant
    → semantic retrieval projection

FTS
    → lexical retrieval index

Neo4j
    → relationship projection
```

These systems provide valuable capabilities, but making each one an independent source of truth would create:

- duplicated ownership;
- synchronization ambiguity;
- difficult disaster recovery;
- unclear conflict resolution.

Signal should remain useful even when an optional projection is temporarily unavailable.

## Decision

Specialized retrieval and relationship stores will be treated as **rebuildable projections derived from authoritative persisted evidence** wherever practical.

The relational/evidence foundation owns canonical data.

Qdrant, lexical indexes, and Neo4j provide specialized access paths.

Conceptually:

```text
                   ┌──→ FTS
Authoritative Data ├──→ Qdrant
                   └──→ Neo4j
```

not:

```text
SQLite ↔ Qdrant ↔ Neo4j
      competing truth stores
```

## Graceful degradation

Failure of a projection should reduce capability rather than corrupt factual ownership.

Examples:

### Qdrant unavailable

Semantic retrieval may be unavailable.

Lexical/document retrieval can continue where possible.

### Neo4j unavailable

Multi-hop relationship exploration may be unavailable.

Canonical evidence and ordinary research remain intact.

### FTS unavailable

Exact lexical retrieval is degraded.

Other evidence-access mechanisms may remain operational.

## Rebuildability

Projection records should preserve enough stable identifiers and provenance to allow indexes to be recreated from authoritative data.

A projection should not contain irreplaceable business evidence that exists nowhere else.

## Rationale

Different databases should be used because they solve different query problems, not because each becomes another authority over the same fact.

This reduces operational coupling and makes technology replacement easier.

## Alternatives considered

### Each specialized store owns its native data

Can improve local autonomy but creates distributed ownership and synchronization complexity.

### Put everything in one database

Simplifies infrastructure but compromises specialized retrieval and graph capabilities.

### Synchronize all stores bidirectionally

Rejected because bidirectional ownership makes reconciliation substantially harder.

## Consequences

### Positive

- clear ownership;
- easier disaster recovery;
- technology replaceability;
- graceful degradation;
- simpler reconciliation;
- indexes can be re-created.

### Negative

- projection/rebuild pipelines are required;
- indexes may temporarily lag authoritative data;
- additional observability is necessary.

## Architectural invariant

> **Loss of a derived store may reduce retrieval capability, but must not destroy or redefine Signal's authoritative evidence.**

## Revisit when

Projection technology may change freely.

The distinction between authoritative evidence and derived access structures should remain.
