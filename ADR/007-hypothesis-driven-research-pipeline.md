# ADR-007 — Hypothesis-Driven Research Pipeline Instead of Single-Pass LLM Answering

**Status:** Accepted  
**Date:** 2026-09-05

## Context

A common retrieval-augmented generation architecture follows:

```text
Question
   ↓
Retrieve documents
   ↓
LLM
   ↓
Answer
```

This pattern is suitable for many knowledge-assistance applications.

Equity research has a different requirement.

A research question may have:

- multiple plausible explanations;
- incomplete evidence;
- contradictory evidence;
- evidence across structured and narrative datasets;
- indirect macroeconomic relationships;
- information that should invalidate an initially attractive explanation.

If retrieval and synthesis are combined into one prompt, the model can anchor on the first plausible narrative produced from the first retrieved evidence.

Signal is intended to investigate questions rather than merely formulate fluent answers.

## Decision

Signal will use an explicit staged research pipeline:

```text
Research Question
       ↓
Hypothesis Generation
       ↓
Research Planning
       ↓
Evidence Gathering
       ↓
Hypothesis Evaluation
       ↓
Evidence Sufficiency Decision
       ↓
Research Synthesis
```

Responsibilities should remain conceptually distinct even if several stages initially execute within the same application process.

### Hypothesis Generator

Produces competing explanations or candidate hypotheses.

### Planner

Determines what evidence would help evaluate those hypotheses.

### Evidence Gathering

Uses appropriate deterministic and retrieval tools to obtain evidence.

### Evaluator

Assesses support, contradiction, uncertainty, and missing evidence.

### Orchestrator

Controls execution, iteration, budgets, and stopping conditions.

### Synthesizer

Produces the final research narrative from the evaluated evidence and hypotheses.

## Rationale

The separation makes reasoning inspectable.

Instead of only storing:

```text
Question → Answer
```

Signal can represent:

```text
Question
  ↓
Hypotheses
  ↓
Evidence requirements
  ↓
Evidence
  ↓
Evaluation
  ↓
Conclusion
```

This is important because a user should be able to understand not only what the system concluded, but how the investigation developed.

## Alternatives considered

### One large LLM prompt

Simpler to implement but mixes planning, retrieval assumptions, reasoning, and synthesis into one opaque operation.

### Generic free-running multi-agent architecture

Provides high autonomy but introduces additional coordination complexity and unpredictable execution without demonstrating that independent agents improve research quality.

### Fixed research templates only

Highly reproducible but insufficiently flexible for open-ended user questions.

## Consequences

### Positive

- explicit research structure;
- competing hypotheses;
- easier evaluation;
- better observability;
- research memory can persist individual stages;
- easier identification of failure modes;
- reduces premature narrative commitment.

### Negative

- more orchestration code;
- additional LLM calls;
- higher latency;
- more state to persist;
- requires explicit stopping policies.

## Architectural principle

> **Signal does not ask an LLM simply to answer a research question. It asks the system to conduct a bounded investigation.**

## Revisit when

Stages may be consolidated or expanded when evaluation data demonstrates that doing so improves quality, cost, or latency.

The conceptual separation between evidence gathering, evaluation, and synthesis should remain.
