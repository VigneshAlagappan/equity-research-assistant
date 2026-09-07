# ADR-012 — Point-in-Time Evidence Filtering to Prevent Look-Ahead Bias

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Historical research can accidentally use information that was not available at the historical point being analyzed.

For example, when investigating:

```text
What could an investor have known on 31 March 2024?
```

the system must not use:

- later annual reports;
- future earnings results;
- subsequent management commentary;
- later macroeconomic revisions;
- later restatements unless explicitly requested.

Without point-in-time controls, research can suffer from look-ahead bias.

This is particularly important when Signal is used to study historical hypotheses, investment decisions, or relationships.

## Decision

Evidence retrieval should support a **point-in-time eligibility boundary**.

When an investigation specifies an "as-of" date:

```text
evidence.available_at <= investigation.as_of
```

must be enforced by deterministic filtering wherever source metadata makes this possible.

The reasoning model must not decide whether future evidence is eligible.

## Relevant timestamps

Signal should distinguish where possible:

- reporting period;
- document publication date;
- filing date;
- ingestion date;
- effective date;
- observation date;
- revised/restated date.

These timestamps represent different concepts and must not be treated interchangeably.

## Rationale

Temporal correctness is part of factual correctness.

A perfectly accurate fact can still be invalid evidence if it was unavailable at the time being investigated.

## Alternatives considered

### Allow the LLM to infer temporal relevance

Rejected because temporal eligibility can be expressed deterministically.

### Ignore point-in-time availability

Simpler, but makes historical research vulnerable to hindsight bias.

### Maintain complete point-in-time database snapshots

Potentially stronger but operationally heavier than required at current scale.

## Consequences

### Positive

- supports historical research;
- reduces look-ahead bias;
- more reproducible investigations;
- clearer research provenance.

### Negative

- requires accurate availability timestamps;
- historical source metadata may be incomplete;
- revisions/restatements require careful modeling.

## Architectural invariant

> **Evidence that was unavailable at the investigation's as-of date must not silently influence a point-in-time conclusion.**

## Revisit when

Storage implementation may evolve toward stronger bitemporal or versioned data models as requirements justify it.
