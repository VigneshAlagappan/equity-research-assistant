# ADR-006 — Separate Deterministic Computation from Probabilistic Research Reasoning

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Equity research combines two fundamentally different classes of work.

Some operations have objectively reproducible outputs:

- arithmetic;
- financial ratios;
- growth rates;
- CAGR;
- period comparisons;
- screening rules;
- indicator thresholds;
- reconciliation rules;
- filtering;
- ranking;
- temporal constraints.

Other tasks require interpretation or judgment:

- generating hypotheses;
- interpreting management commentary;
- identifying plausible relationships;
- explaining why a trend may exist;
- evaluating contradictory evidence;
- deciding what additional evidence may be useful;
- synthesizing a research conclusion.

Large language models can perform both categories.

That does not mean they should.

Using probabilistic reasoning for tasks that have stable, testable algorithms reduces reproducibility and unnecessarily expands the trusted surface area of the model.

## Decision

Signal will explicitly separate:

1. **deterministic computation**, and
2. **probabilistic research reasoning**.

### Deterministic computation owns

- parsing where deterministic parsers exist;
- normalization;
- financial calculations;
- ratios;
- CAGR and growth calculations;
- reconciliation rules;
- indicators;
- temporal filtering;
- point-in-time eligibility;
- lexical retrieval;
- vector similarity computation;
- deterministic retrieval fusion;
- ranking rules;
- evidence filtering;
- budget and termination checks.

Given the same inputs, configuration, software version, and state, these components should produce the same result.

### Probabilistic reasoning owns

- hypothesis generation;
- qualitative interpretation;
- semantic extraction where deterministic extraction is insufficient;
- evidence interpretation;
- contradiction analysis;
- hypothesis evaluation;
- research synthesis;
- user-facing explanation.

## Architectural model

```text
Authoritative Evidence
        ↓
Canonical Facts
        ↓
┌────────────────────────────┐
│ Deterministic Computation  │
│                            │
│ calculations               │
│ ratios                     │
│ comparisons                │
│ indicators                 │
│ filters                    │
└─────────────┬──────────────┘
              ↓
       Evidence Package
              ↓
┌────────────────────────────┐
│ Probabilistic Reasoning    │
│                            │
│ hypotheses                 │
│ interpretation             │
│ contradiction              │
│ evaluation                 │
│ synthesis                  │
└─────────────┬──────────────┘
              ↓
      Research Conclusion
```

## Example

Instead of asking an LLM:

```text
Revenue FY24 = 10,000
Revenue FY25 = 11,500

Calculate the growth and explain what happened.
```

Signal should calculate:

```text
Revenue growth = 15.0%
```

deterministically.

The reasoning layer then receives:

```text
Observed evidence:
Revenue growth FY25 = 15.0%
```

and may investigate:

```text
What factors plausibly explain the acceleration?
```

## Rationale

The distinction limits probabilistic behavior to places where semantic judgment provides value.

It improves:

- reproducibility;
- testability;
- debugging;
- trust;
- auditability;
- model portability.

Financial calculations can be unit-tested.

A hypothesis about why margins declined cannot be validated in exactly the same way and therefore belongs in a different architectural layer.

## Alternatives considered

### Let the LLM perform all calculations and reasoning

Rejected because output consistency and reproducibility would depend unnecessarily on the model.

### Use deterministic software for all research

Rejected because many research questions have no fixed algorithmic solution.

### Encode interpretation as increasingly complex rules

Rejected because qualitative research and hypothesis generation are open-ended semantic problems.

## Consequences

### Positive

- smaller trusted AI surface;
- deterministic financial results;
- simpler testing;
- easier model replacement;
- clearer provenance;
- easier explanation of system behavior.

### Negative

- additional boundaries between calculation and reasoning;
- structured evidence packages must be constructed;
- some tasks require careful classification between deterministic and probabilistic execution.

## Architectural invariant

> **If a task has a stable, explicit, testable rule, Signal should prefer deterministic code over an LLM.**

And:

> **LLMs reason over evidence; they do not own financial truth.**

## Revisit when

The boundary may evolve as capabilities change, but reproducible financial computation should remain deterministic unless there is a compelling architectural reason otherwise.
