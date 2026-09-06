# ADR-020 — Preserve a Controlled Migration Path from SQLite to a Server Database

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal currently uses SQLite as the authoritative relational store for the initial deployment model.

SQLite is well suited to the current stage because it provides:

- minimal operational overhead;
- simple local deployment;
- transactional consistency;
- straightforward backup and inspection;
- low infrastructure cost;
- a productive development environment.

However, future Signal deployments may require capabilities better served by a client/server relational database such as PostgreSQL.

Potential triggers include:

- substantially higher concurrent writes;
- multiple application instances;
- distributed workers;
- stronger operational HA requirements;
- server-side connection management;
- remote database access;
- larger SaaS deployment scale;
- independent database operations.

The architectural risk is not using SQLite today.

The risk would be allowing application logic to become so dependent on SQLite-specific behavior that future migration requires rewriting large parts of Signal.

## Decision

Signal will continue using SQLite while preserving a **database abstraction boundary that allows migration to a server relational database when operational evidence justifies it**.

The migration target is not mandated today.

PostgreSQL is a likely future candidate, but application architecture should depend on relational capabilities and repository contracts rather than on a specific future vendor.

Conceptually:

```text
Application / Domain Logic
          ↓
Repository / FactStore interfaces
          ↓
Relational persistence implementation
          ↓
      SQLite today

Possible future replacement:

Application / Domain Logic
          ↓
same conceptual interfaces
          ↓
Server database adapter
          ↓
PostgreSQL / other suitable RDBMS
```

## What the abstraction must protect

Application and research logic should avoid direct reliance on SQLite-specific behavior where that dependency would materially hinder migration.

The boundary should cover, where practical:

- canonical fact reads/writes;
- investigations;
- source metadata;
- ingestion state;
- job/audit state;
- derived deterministic results;
- transactional operations;
- query patterns required by application services.

SQLite-specific implementation details may exist inside the adapter/repository layer.

They should not leak unnecessarily into research and domain services.

## SQL portability

This ADR does not require pretending all SQL dialects are identical.

Some database-specific SQL may be necessary.

The principle is:

> database-specific behavior should be localized.

Potential migration differences include:

- UPSERT syntax and semantics;
- JSON features;
- date/time handling;
- FTS behavior;
- locking;
- transaction isolation;
- auto-increment behavior;
- schema migrations;
- connection pooling.

These should be addressed through controlled persistence boundaries rather than scattered throughout the application.

## SQLite remains authoritative today

This ADR does not declare SQLite temporary or inadequate.

SQLite remains the accepted source-of-truth implementation for the current architecture.

Migration should occur only when measurable operational needs justify the additional infrastructure.

## Migration triggers

A server-database migration should be considered when one or more of the following become material:

- concurrent write contention becomes an operational problem;
- multiple application replicas require shared transactional state;
- distributed workers need robust shared access;
- database availability requirements exceed local SQLite deployment;
- database size or maintenance characteristics exceed practical SQLite operation;
- SaaS concurrency materially changes the workload;
- operational telemetry demonstrates that the persistence tier is a bottleneck.

Migration should not occur merely because a server database is considered more enterprise-like.

## Migration approach

When migration becomes necessary, the preferred approach is:

```text
1. Validate repository/interface coverage
2. Identify remaining SQLite-specific dependencies
3. Introduce server-database adapter
4. Run compatibility and migration tests
5. Migrate authoritative data
6. Validate counts, constraints, and canonical facts
7. Switch persistence implementation
8. Preserve rollback/recovery plan
```

Application/domain behavior should require minimal change.

## FTS and specialized capabilities

SQLite FTS functionality is conceptually separate from the canonical relational ownership decision.

If migration to another relational database occurs, lexical retrieval may:

- use the target database's native full-text capabilities;
- remain a separate projection;
- use another compatible search technology.

The migration of canonical storage must not force unrelated retrieval decisions to be made at the same time.

Similarly, Qdrant and Neo4j remain specialized projections as defined in other ADRs.

## Data migration correctness

Because the relational store owns canonical data, migration requires deterministic validation.

Validation should include where appropriate:

- row counts;
- uniqueness constraints;
- source/provenance integrity;
- canonical financial observations;
- investigation state;
- audit/job state;
- referential integrity;
- representative application queries.

LLM-based validation is not sufficient for database migration correctness.

## Alternatives considered

### Start with PostgreSQL immediately

Provides stronger server deployment capabilities but introduces infrastructure and operational cost before current requirements justify it.

Rejected for the current stage.

### Allow direct SQLite access throughout the application

Simple initially but creates high future migration cost.

Rejected as an architectural pattern.

### Commit now to PostgreSQL as the mandatory future target

Unnecessary because future deployment requirements may change.

The architecture should preserve relational portability rather than prematurely locking the next database vendor.

### Use an ORM and assume portability is solved

Rejected as a complete strategy.

An ORM may help, but portability also depends on schema design, transaction assumptions, query semantics, full-text features, locking, and database-specific extensions.

## Consequences

### Positive

- retains SQLite simplicity today;
- avoids premature infrastructure;
- preserves future scaling options;
- limits database-specific coupling;
- supports controlled migration;
- makes persistence assumptions explicit.

### Negative

- repository boundaries require discipline;
- some database-specific optimizations may need abstraction;
- portability tests and adapters add engineering work;
- a future migration will still require data and operational planning.

## Architectural invariant

> **Signal may change its relational database implementation without requiring a redesign of research, reasoning, or domain logic.**

And:

> **Database migration should be triggered by demonstrated operational requirements, not by architecture fashion.**

## Relationship to ADR-001

ADR-001 establishes SQLite as the current source-of-truth implementation.

This ADR establishes the migration boundary and conditions under which that implementation may later change.

The two decisions are complementary:

```text
ADR-001
SQLite is the authoritative relational store today.

ADR-020
The application must preserve a controlled path to a server database
if future operational requirements justify migration.
```

## Revisit when

Revisit this decision when workload telemetry or deployment architecture demonstrates that SQLite no longer meets operational requirements.

At that point, a separate migration ADR may record the specific selected server database and migration plan.
