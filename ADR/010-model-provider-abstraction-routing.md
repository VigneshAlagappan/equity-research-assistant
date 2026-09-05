# ADR-010 — Model-Provider Abstraction and Capability-Based Reasoning Routing

**Status:** Accepted  
**Date:** 2026-09-05

## Context

LLM capabilities, pricing, latency, privacy characteristics, context limits, and deployment models change rapidly.

Hard-coding core research workflows directly to one provider or model would couple Signal's architecture to a rapidly changing external dependency.

Different research tasks also have different reasoning requirements.

For example:

- lightweight classification;
- knowledge extraction;
- hypothesis generation;
- complex evidence evaluation;
- synthesis.

The most capable and expensive model does not need to execute every task.

Local models may also be desirable for:

- privacy;
- offline operation;
- cost control;
- experimentation;
- resilience.

## Decision

Signal will depend on **reasoning capabilities through provider-neutral interfaces**, rather than making application logic depend directly on individual model vendors.

Conceptually:

```text
Research Task
      ↓
Capability Requirement
      ↓
Reasoning Router
      ↓
Model / Provider Selection
      ↓
Execution
```

Selection may consider:

- required capability;
- reasoning hardness;
- model availability;
- cost;
- latency;
- privacy policy;
- context requirements;
- local versus hosted execution;
- fallback configuration.

## Provider abstraction

Application components should request capabilities such as:

```text
generate_hypotheses
evaluate_evidence
extract_knowledge
synthesize_research
```

rather than embedding assumptions such as:

```text
call_specific_vendor_model()
```

Provider adapters own provider-specific request/response handling.

## Rationale

The architecture should depend on **what reasoning capability is required**, not **which company currently provides the strongest model**.

This allows Signal to evolve as model capabilities change.

## Alternatives considered

### One fixed model for every task

Simple, but creates vendor coupling and unnecessary cost.

### User directly chooses model for every operation

Flexible but exposes implementation complexity and prevents system-level optimization.

### Self-host every model

Maximum infrastructure control but high operational and hardware cost and potentially lower capability for difficult research tasks.

## Consequences

### Positive

- provider portability;
- local/cloud flexibility;
- model fallback;
- cost optimization;
- experimentation;
- easier adoption of future models.

### Negative

- provider interface maintenance;
- capability evaluation becomes necessary;
- outputs may differ between models;
- routing policy adds complexity.

## Architectural invariant

> **Signal's research architecture depends on reasoning capabilities, not model brands.**

## Revisit when

Individual provider adapters and routing policies should change frequently.

The abstraction boundary should remain unless model execution becomes completely commoditized.
