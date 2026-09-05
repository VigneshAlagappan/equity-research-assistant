# ADR-013 — Internal Dataset Events for Post-Ingestion Processing Before an External Message Broker

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Successful data ingestion can trigger multiple downstream operations.

For example:

```text
Source
  ↓
Fetch
  ↓
Parse
  ↓
Normalize
  ↓
Validate
  ↓
Store
  ↓
DATASET_INGESTED
        ├──→ Knowledge Builder
        ├──→ Vector indexing
        ├──→ Graph projection
        └──→ Derived calculations
```

Directly coupling ingestion code to every downstream subsystem would make the pipeline increasingly difficult to evolve.

An external message broker could decouple these systems, but would also introduce significant infrastructure at the current project scale.

## Decision

Signal will use **explicit internal event and worker contracts** to separate ingestion from downstream processing while remaining inside the modular-monolith deployment.

The architecture should make event producers unaware of detailed downstream implementations.

An external queue or event broker is not required initially.

## Rationale

The goal is to create the architectural seam before introducing distributed infrastructure.

This provides:

- loose logical coupling;
- replayable processing design;
- easier future worker extraction;
- simple current deployment.

## Event expectations

Events should identify sufficient context for downstream processing, such as:

- dataset/document identity;
- company/entity where applicable;
- source;
- ingestion run;
- version or timestamp;
- event type.

Events should indicate that something happened, rather than contain all downstream business logic.

## Alternatives considered

### Direct function calls to every downstream processor

Simple initially but tightly couples ingestion and projection/indexing behavior.

### Kafka/RabbitMQ/cloud queues immediately

Powerful, but current throughput and deployment requirements do not justify the operational cost.

### Scheduled polling only

Loose coupling but introduces latency and unnecessary repeated database scanning.

## Consequences

### Positive

- explicit decoupling;
- simple deployment;
- future queue migration path;
- clearer worker responsibilities.

### Negative

- internal events do not provide the durability and isolation of a distributed broker;
- process-level failure can affect multiple capabilities;
- retry semantics require explicit implementation.

## Revisit when

Adopt an external queue/broker when:

- workloads become independently deployable;
- ingestion volume increases materially;
- durable cross-process delivery is required;
- horizontal workers are required;
- retry/isolation needs exceed the internal mechanism.
