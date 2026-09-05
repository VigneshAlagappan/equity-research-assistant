# ADR-005 — Official Regulatory Filings as Primary Financial Evidence

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal is an evidence-grounded equity research system. Financial observations used by the system must therefore be attributable, reproducible, and distinguishable from derived calculations and model-generated interpretations.

Traditional financial data providers and aggregators offer significant convenience. They normalize financial statements, provide consistent APIs, and often offer long historical coverage.

However, using an aggregator as the ultimate source of financial truth introduces an additional transformation layer:

```text
Company
  ↓
Regulatory Filing
  ↓
Data Provider
  ↓
Provider Normalization
  ↓
Signal
```

The provider may make decisions about:

- taxonomy mapping;
- fiscal-period alignment;
- restatements;
- consolidated versus standalone statements;
- missing observations;
- units and scaling;
- accounting classifications;
- historical normalization.

Those decisions can be useful, but they are not decisions Signal controls.

For an evidence-driven research system, a financial observation should ideally be traceable to the disclosure from which it originated.

## Decision

Signal will treat **official regulatory filings and company disclosures filed with the relevant market regulator or exchange as the preferred primary evidence for reported financial facts**.

For supported jurisdictions this includes, for example:

### India

- NSE/BSE regulatory filings;
- official XBRL submissions where available;
- company filings published through recognized exchange mechanisms.

### United States

- SEC EDGAR filings;
- SEC XBRL/iXBRL financial facts and filing documents.

The architectural principle is jurisdiction-neutral:

> **Reported financial truth should originate as close as practical to the legally filed company disclosure.**

Secondary financial datasets may continue to be used for:

- historical backfill;
- discovery;
- cross-validation;
- market data;
- coverage gaps;
- prototyping;
- data that is not available from regulatory filings.

However, secondary sources must not silently overwrite or replace authoritative regulatory observations when an authoritative observation exists.

## Source hierarchy

The intended conceptual hierarchy is:

```text
Level 1
Official regulatory / exchange filing
        ↓
Level 2
Canonical normalized financial observation
        ↓
Level 3
Deterministically derived metric
        ↓
Level 4
Research interpretation / inference
```

Each level must remain distinguishable.

For example:

```text
Reported revenue
    ↓
Canonical revenue
    ↓
YoY growth = 14.7%
    ↓
"Growth appears to be accelerating"
```

The first value is reported.

The second is normalized.

The third is calculated.

The fourth is interpreted.

They must not be represented as equivalent forms of truth.

## Why XBRL is preferred where available

XBRL and iXBRL provide machine-readable semantic context around reported financial information.

They can expose information including:

- financial concept;
- reporting entity;
- reporting period;
- unit;
- dimensions;
- statement context;
- filing provenance.

This reduces the need to infer structured financial facts from visual PDF layouts or unconstrained natural-language extraction.

Narrative PDFs remain important for:

- management commentary;
- risks;
- strategy;
- footnotes;
- qualitative disclosures;
- earnings discussions.

Structured filings and narrative documents therefore serve complementary purposes.

## Alternatives considered

### Commercial or public financial-data APIs as canonical truth

**Advantages**

- fast integration;
- normalized schemas;
- broad historical coverage;
- reduced ingestion complexity.

**Not selected as primary evidence because**

Signal would inherit provider normalization decisions and lose some direct control over provenance and reconciliation.

These sources remain useful as secondary evidence and fallback data.

### LLM extraction from filing PDFs

**Advantages**

- flexible across document structures;
- capable of extracting otherwise inaccessible information.

**Not selected for canonical structured facts because**

probabilistic extraction is unnecessary where regulatory structured data already exists.

LLM extraction remains appropriate for narrative evidence.

### Equal authority across multiple providers

**Advantages**

- maximum apparent data availability.

**Not selected because**

when sources disagree, the system needs an explicit trust hierarchy rather than arbitrary or hidden reconciliation.

## Consequences

### Positive

- direct provenance;
- stronger auditability;
- reproducible research;
- transparent source conflicts;
- improved restatement handling;
- reduced long-term dependency on individual financial-data vendors;
- clearer distinction between facts and inference.

### Negative

- jurisdiction-specific ingestion complexity;
- XBRL taxonomy mapping;
- filing edge cases;
- changing regulator formats;
- historical gaps;
- greater engineering effort than consuming one normalized API.

## Architectural invariant

> **Signal must be able to distinguish what a company reported, what Signal normalized, what Signal calculated, and what the reasoning system inferred.**

## Revisit when

The primary-source principle should remain.

Specific ingestion mechanisms may be revisited when:

- regulators change disclosure standards;
- new authoritative APIs become available;
- regulatory filing formats materially change;
- additional jurisdictions are introduced.
