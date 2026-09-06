# ADR-018 — Investigation Budget Governance

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal conducts hypothesis-driven investigations that may involve multiple research stages:

- hypothesis generation;
- planning;
- deterministic calculations;
- structured-data queries;
- keyword and semantic retrieval;
- knowledge-graph traversal;
- evidence evaluation;
- additional evidence gathering;
- synthesis.

An investigation can legitimately request more evidence when the evaluator determines that the available evidence is insufficient.

However, research quality does not improve indefinitely with additional tool calls, retrieval rounds, graph traversals, or model invocations.

Without explicit governance, an investigation can:

- consume unpredictable model cost;
- exceed acceptable response latency;
- repeatedly retrieve equivalent evidence;
- expand into low-value research paths;
- overuse external data sources;
- become difficult to reproduce operationally;
- create materially different resource consumption for similar questions.

This problem must not be delegated entirely to an LLM because deciding whether execution is still permitted is an operational and governance concern.

## Decision

Signal will treat **investigation budgets as deterministic execution policy owned by the orchestrator**.

A reasoning model may recommend:

- additional evidence;
- another hypothesis;
- another retrieval path;
- further validation.

The orchestrator decides whether the requested action is permitted within the active investigation budget.

Conceptually:

```text
Research request
      ↓
Investigation policy
      ↓
Budget allocation
      ↓
Planner / Evaluator requests work
      ↓
Orchestrator checks remaining budget
      ↓
┌───────────────────┐
│ permitted?        │
├─────────┬─────────┤
│ Yes     │ No      │
↓         ↓
Execute   Stop / synthesize with limits
```

## Governed dimensions

An investigation budget may include limits for:

- maximum research iterations;
- maximum wall-clock duration;
- maximum LLM calls;
- model/token budget;
- estimated monetary cost;
- retrieval calls;
- number of documents or chunks considered;
- graph traversal depth;
- number of graph expansion operations;
- external-source calls;
- per-source request limits;
- repeated/no-new-evidence thresholds.

Not every deployment must enforce every dimension initially.

The architecture must support explicit policy rather than implicit unlimited execution.

## Investigation depth

User-facing investigation depth may map to policy profiles such as:

```text
Quick
  → narrow evidence scope
  → low iteration budget
  → lower-cost reasoning where appropriate

Standard
  → balanced evidence and reasoning budget

Deep
  → larger evidence scope
  → additional hypothesis/evaluation cycles
  → higher permitted reasoning cost
```

These profiles are configuration, not separate research architectures.

All profiles use the same governed execution model.

## Budget exhaustion

Budget exhaustion is not itself a research conclusion.

If execution limits are reached before sufficient evidence is available, Signal must be able to return an outcome such as:

```text
Conclusion: uncertain

Reason:
Investigation reached its configured execution boundary before
sufficient evidence was obtained.
```

The system must not convert budget exhaustion into false evidentiary confidence.

## Relationship to evidence sufficiency

ADR-008 defines the bounded evidence-sufficiency loop.

This ADR defines **how the bounds are governed**.

The evaluator may determine:

```text
INSUFFICIENT_EVIDENCE
```

but the orchestrator may still decide:

```text
NO_ADDITIONAL_EXECUTION_ALLOWED
```

because the investigation budget has been exhausted.

Therefore:

```text
Research need
    ≠
Execution permission
```

## Model routing and cost

Model selection may be influenced by remaining budget.

For example:

- low-complexity tasks may use lower-cost models;
- harder evaluation tasks may use stronger models;
- fallback models may be selected according to policy.

However, the model router must operate inside the investigation's allowed policy envelope.

Model selection must not silently override overall budget governance.

## Auditability

Each investigation should preserve enough budget metadata to explain resource use, including where practical:

- policy/profile selected;
- initial limits;
- iterations consumed;
- model calls;
- tools invoked;
- elapsed time;
- termination reason.

This makes investigations easier to compare, debug, and evaluate.

## Alternatives considered

### Allow the LLM to decide when to stop

Rejected because the model would control both research judgment and resource consumption.

### Hard-code one fixed number of research rounds

Predictable but too rigid for questions of different difficulty.

### Unlimited investigation until evidence is sufficient

Rejected because evidence sufficiency may never be reached and operational cost would be unbounded.

### Cost controls only at the model-provider level

Insufficient because investigation cost includes retrieval, graph expansion, external sources, and orchestration time in addition to LLM tokens.

## Consequences

### Positive

- predictable operational cost;
- bounded latency;
- explicit autonomy limits;
- better SaaS scalability;
- more reproducible investigations;
- easier evaluation across research-depth profiles;
- clear separation between research judgment and execution permission.

### Negative

- budgets require tuning;
- an investigation can terminate before all useful evidence is explored;
- resource accounting introduces additional orchestration state;
- different tools require different budget semantics.

## Architectural invariant

> **The reasoning system may request additional investigation, but deterministic orchestration governs whether additional work is permitted.**

And:

> **Budget exhaustion must reduce confidence or completeness; it must never be represented as evidence sufficiency.**

## Revisit when

Budget dimensions, thresholds, and user-facing investigation profiles should evolve as telemetry and evaluation data become available.

The requirement for explicit bounded execution remains.
