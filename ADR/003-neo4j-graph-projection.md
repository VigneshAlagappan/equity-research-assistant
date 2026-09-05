# ADR-003: Neo4j as a Projected Knowledge Graph

- **Status:** Accepted
- **Decision scope:** Relationship modeling and traversal
- **Related:** `context/knowledge_graph.py`, `context/graph_neo4j.py`,
  `knowledge_entities`, `knowledge_claims`, `knowledge_relationships`,
  `knowledge_evidence`

## Context

Relational queries are effective for retrieving known factual records, but research also
needs relationship-oriented questions such as:

- which companies discuss the same risk;
- what macro factors may affect a metric;
- which claims connect to a particular entity;
- which claims become relevant through a bounded multi-hop path;
- how company, claim, evidence, time, metric, risk, and macro concepts relate.

A graph database is naturally suited to traversal, but making it a second authoritative
store would create ownership ambiguity.

## Decision

Use Neo4j as an **optional projected Knowledge Graph** built from authoritative relational
knowledge.

SQLite-backed knowledge entities/claims/relationships/evidence remain canonical.

Neo4j is used for graph-native traversal and visualization. Where supported, the application
can fall back to relational traversal when Neo4j is unavailable.

## Why this decision

### 1. Relationship traversal is the graph's real value
The graph exists to efficiently explore connectivity, not to replace structured fact
ownership.

### 2. Provenance remains first-class
Claims and graph edges trace back to evidence and originating documents rather than
becoming unexplained graph assertions.

### 3. Shared company identity
Company nodes used in different graph features should represent the same canonical company,
not duplicate unrelated graph identities.

### 4. Rebuildability
A projected graph can be recreated when graph schema/traversal logic evolves.

## Graph semantics

The graph may represent structures such as:

- `Company --STATES--> Claim`
- `Claim --SUPPORTED_BY--> Evidence`
- `Claim --VALID_DURING--> TimePeriod`
- `Claim --ABOUT--> Entity`
- `MacroFactor --MAY_AFFECT--> Metric`
- `Company --EXPOSED_TO--> Risk`

These edges represent extracted/provenanced knowledge or structural links.

## Multi-hop rule

A bounded path can surface potentially relevant evidence.

It must **not** automatically upgrade graph connectivity into fact or causation.

Therefore:

> **Multi-hop graph results are treated as inference unless independently supported by
> direct evidence.**

Traversal should also be capped/deduplicated to avoid graph explosion and prompt flooding.

## Alternatives considered

### SQL-only traversal

**Advantages**
- no extra service;
- canonical data already relational.

**Why not sufficient alone**
- complex graph-shaped exploration becomes harder to express, inspect, and evolve;
- graph-native traversal is useful as relationship density grows.

### Neo4j as authoritative fact store

**Rejected because**
- financial observations and reconciliation are relational/transactional concerns;
- facts and relationships have different ownership semantics;
- duplicating canonical values across stores introduces drift risk.

### RDF / triple store

Potentially appropriate for standards-heavy semantic-web requirements, but current use cases
benefit more from property-graph traversal and Python/Neo4j ecosystem simplicity.

## Consequences

### Positive
- natural relationship traversal;
- bounded multi-hop research;
- graph visualization/debugging;
- relationship layer can evolve independently;
- canonical facts remain deterministic.

### Negative
- synchronization/projection complexity;
- another service to operate;
- entity resolution becomes increasingly important;
- graph traversal quality needs its own evaluation and ranking strategy.

## Synchronization strategy

Graph synchronization should be:

- idempotent;
- change-aware where practical;
- reconstructable from authoritative data;
- safe to repeat.

Fingerprints/change detection may avoid unnecessary full synchronization while preserving
the rule that a fresh process can rebuild graph state.

## Failure strategy

Neo4j failure should reduce graph capability, not invalidate the underlying knowledge
claims or financial facts.

## Revisit when

Revisit if:

- graph traversal becomes a dominant workload requiring independent scaling;
- entity resolution matures enough for broader cross-company graph reasoning;
- the graph becomes an external product/API surface;
- incremental/event-driven graph synchronization is required for freshness;
- another graph engine better matches operational needs.

The invariant to preserve is:

> **The graph knows relationships; authoritative stores own the underlying evidence.**
