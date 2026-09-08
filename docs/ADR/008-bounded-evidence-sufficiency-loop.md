# ADR-008 — Bounded Evidence-Sufficiency Loop for Research Investigations

**Status:** Accepted  
**Date:** 2026-09-05

## Context

A research system should be able to recognize when available evidence is insufficient.

A single-pass workflow cannot naturally distinguish between:

```text
Enough evidence to reach a conclusion
```

and:

```text
Not enough evidence, but a plausible answer can still be generated.
```

Allowing unrestricted autonomous research, however, creates the opposite problem.

The system could:

- continue searching indefinitely;
- repeatedly retrieve equivalent evidence;
- accumulate unnecessary model cost;
- create unpredictable latency;
- expand into irrelevant research paths.

## Decision

Signal will support an **evidence-sufficiency loop**, but the loop must always be bounded.

Conceptually:

```text
Gather Evidence
      ↓
Evaluate
      ↓
Evidence sufficient?
   ↙            ↘
 Yes             No
 ↓                ↓
Synthesize    Identify Gap
                  ↓
             Gather More
                  ↓
               Evaluate
```

The orchestrator owns the loop rather than the reasoning model.

## Termination conditions

An investigation must terminate when one or more configured conditions are reached, including:

- sufficient evidence;
- maximum research iterations;
- wall-clock budget;
- model/token/cost budget where configured;
- repeated retrieval of already-seen evidence;
- no meaningful new evidence;
- no eligible tools or sources remaining;
- explicit user constraint.

The final result may legitimately be:

```text
Insufficient evidence to support a reliable conclusion.
```

## Rationale

Research quality requires the ability to continue when evidence is incomplete.

Operational safety requires bounded execution.

Therefore:

> **Research autonomy is permitted inside explicit execution boundaries.**

## Alternatives considered

### Single-pass RAG

Predictable and inexpensive, but may synthesize prematurely.

### Unlimited autonomous agent

Flexible but operationally unpredictable and difficult to evaluate.

### Fixed number of retrieval rounds

Simple but does not respond to evidence quality.

## Consequences

### Positive

- better handling of incomplete evidence;
- explicit uncertainty;
- bounded latency;
- bounded cost;
- easier observability;
- prevents uncontrolled agent loops.

### Negative

- more orchestration state;
- choosing appropriate budgets requires tuning;
- stopping early may miss useful evidence;
- continuing too long may increase cost without improving quality.

## Architectural invariant

> **The reasoning model may recommend additional investigation, but deterministic orchestration controls whether that investigation is allowed to execute.**

## Revisit when

Budget policies and sufficiency criteria should evolve as investigation evaluation datasets become available.
