# ADR-009 — Knowledge-Graph Relationships Are Research Inference, Not Proof of Causation

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal uses a knowledge graph to represent relationships among:

- companies;
- industries;
- management;
- financial concepts;
- risks;
- products;
- macroeconomic variables;
- claims;
- evidence.

Multi-hop traversal can expose valuable relationships that are difficult to retrieve through ordinary lexical or semantic search.

For example:

```text
Rainfall
   ↓
Agricultural activity
   ↓
Tractor demand
   ↓
Vehicle financing
   ↓
NBFC performance
```

This path may be highly relevant to a research question.

But graph connectivity alone does not establish:

- statistical relationship;
- economic materiality;
- direction of influence;
- temporal consistency;
- causality.

## Decision

Signal will classify graph-discovered multi-hop relationships as **candidate research inference or evidence-discovery paths**, not as established facts.

Graph paths may:

- suggest hypotheses;
- expand evidence retrieval;
- identify entities worth investigating;
- connect claims across documents;
- surface potentially relevant historical relationships.

Graph paths must not by themselves be represented as proof of causation.

## Evidence classification

Signal should preserve distinctions such as:

```text
Reported Fact
Calculated Fact
Direct Claim
Retrieved Evidence
Graph Relationship
Research Inference
Hypothesis
Conclusion
```

These categories may support different confidence and presentation rules.

## Rationale

A knowledge graph answers:

> What is connected?

It does not automatically answer:

> Did A cause B?

Treating traversal as proof would create an epistemic error at the architecture layer rather than merely a prompting problem.

## Alternatives considered

### Treat graph paths as facts

Rejected because structural connectivity is not equivalent to evidentiary proof.

### Avoid graph inference entirely

Rejected because multi-hop discovery is a valuable research capability.

### Allow unrestricted graph traversal

Rejected because deep traversal can create relationship explosion and increasingly weak semantic connections.

## Decision on traversal

Graph traversal should be bounded by configurable constraints such as:

- maximum hop count;
- relationship type;
- entity scope;
- temporal eligibility;
- confidence/provenance requirements where available.

## Consequences

### Positive

- conservative research output;
- graph remains useful without overstating evidence;
- supports hypothesis discovery;
- clearer provenance.

### Negative

- additional inference classifications;
- users may see fewer definitive claims;
- some useful relationships require additional validation.

## Architectural invariant

> **Connectivity is evidence for investigation, not proof of causation.**

## Revisit when

Traversal algorithms may evolve.

The distinction between relationship discovery and causal proof should remain an invariant.
