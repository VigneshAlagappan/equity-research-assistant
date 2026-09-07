# ADR-011 — Modular Monolith Before Microservices

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal contains several logical capabilities:

- financial ingestion;
- document ingestion;
- knowledge extraction;
- deterministic calculations;
- retrieval;
- vector indexing;
- graph projection;
- research orchestration;
- model routing;
- investigations;
- indicators;
- scheduled jobs.

These capabilities could be separated into independently deployed microservices.

However, Signal is still in a stage where:

- product behavior is evolving rapidly;
- usage patterns are not yet proven;
- workload boundaries are still changing;
- one team can reason about the whole system;
- operational simplicity has high value.

Premature distribution would introduce:

- network contracts;
- service discovery;
- tracing;
- retries;
- distributed failure modes;
- deployment coordination;
- eventual consistency;
- more complex local development.

## Decision

Signal will remain a **modular monolith** while maintaining explicit internal architectural seams.

Examples include:

- repository interfaces;
- `FactStore`;
- backend-neutral data access;
- vector-store interfaces;
- embedding interfaces;
- model-provider interfaces;
- planner/tool contracts;
- event contracts;
- worker boundaries.

Logical modularity should precede physical distribution.

## Rationale

A well-structured monolith preserves the option to extract services later without paying distributed-system costs before they are justified.

The architecture optimizes for:

> **evolutionary separation rather than speculative separation.**

## Alternatives considered

### Microservices from the beginning

Rejected because current scale and team structure do not justify the operational cost.

### Unstructured monolith

Rejected because it would create tightly coupled implementation and make later extraction expensive.

### Serverless decomposition

Potentially useful for selected future workloads, but introduces similar distributed boundaries without current evidence that they are needed.

## Consequences

### Positive

- simpler deployment;
- easier local development;
- lower operational cost;
- simpler transactions;
- faster product iteration;
- still preserves extraction seams.

### Negative

- limited independent scaling;
- less workload isolation;
- one process can become a larger failure domain;
- discipline is required to preserve module boundaries.

## Revisit when

Consider service extraction when:

- workloads have materially different scaling patterns;
- ingestion and interactive research compete for resources;
- SLA requirements differ;
- independent deployment ownership becomes necessary;
- SaaS concurrency exceeds practical single-process boundaries.

## Architectural invariant

> **Distribute because operational evidence requires it, not because individual modules can theoretically become services.**
