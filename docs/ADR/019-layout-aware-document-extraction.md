# ADR-019 — Layout-Aware Document Extraction for Semantically Structured Documents

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal ingests narrative and evidentiary documents such as:

- annual reports;
- earnings presentations;
- conference-call transcripts;
- regulatory filing PDFs;
- investor presentations;
- management commentary;
- other company disclosures.

Many of these documents are visually structured.

Meaning is often encoded not only in text order, but also in:

- page position;
- headings;
- sections;
- columns;
- tables;
- footnotes;
- captions;
- chart labels;
- page boundaries;
- speaker labels;
- nearby explanatory text.

A naive extraction pipeline that treats a PDF as one flat text stream can lose important context.

Examples include:

- table headers becoming separated from values;
- footnotes being attached to the wrong section;
- two-column text being interleaved incorrectly;
- chart commentary being detached from the chart;
- section headings being lost;
- speaker attribution being separated from statements.

This weakens retrieval quality and can cause the reasoning layer to interpret evidence incorrectly even when the original document was clear.

## Decision

Signal will use **layout-aware extraction where document structure materially affects evidence meaning**.

The ingestion architecture should preserve useful structural metadata alongside extracted text where practical.

Conceptually:

```text
Document
   ↓
Layout-aware extraction
   ↓
Structural elements
   ├── page
   ├── section
   ├── heading
   ├── paragraph
   ├── table
   ├── caption
   ├── footnote
   └── speaker/context
          ↓
Semantic chunks
          ↓
Retrieval / evidence
```

The system must not assume that plain text order is always an adequate representation of document meaning.

## Extraction hierarchy

Signal should prefer the most deterministic and semantically reliable extraction mechanism available for the evidence type.

Conceptually:

```text
Structured regulatory data available?
        ↓ Yes
Use structured source for structured facts

        ↓ No / narrative evidence

Machine-readable document structure available?
        ↓ Yes
Preserve native structure

        ↓ No

Layout-aware document extraction
        ↓

OCR / visual interpretation only when required
```

This ADR does not change ADR-005.

Structured regulatory facts should continue to come from authoritative structured sources such as XBRL when available.

Layout-aware PDF extraction primarily serves narrative and document-based evidence.

## Chunking

Semantic chunks should be created with awareness of structural boundaries.

Where possible, chunks should avoid arbitrarily splitting:

- tables from their headers;
- a heading from its section;
- a management statement from its speaker;
- a footnote from its referenced content;
- a chart explanation from the associated page context.

Chunk metadata may include:

- document identifier;
- page number;
- section;
- heading;
- content type;
- speaker;
- table identifier;
- source coordinates or region where useful;
- extraction method;
- confidence;
- surrounding context.

## Tables

Tables require special treatment because flattening them into ordinary prose can destroy row/column meaning.

Where feasible, table extraction should retain:

- header hierarchy;
- row identity;
- column identity;
- units;
- footnotes;
- page/source provenance.

An LLM may help interpret an extracted table, but should not be the primary mechanism for reconstructing structured financial facts when a deterministic structured filing source exists.

## Charts and visual evidence

Charts may contain information not adequately represented in embedded text.

Where chart-derived evidence is required, Signal may use a visual or multimodal extraction path.

Such extraction must preserve provenance and should be classified appropriately because visually inferred values may have different confidence than directly reported structured facts.

## Extraction confidence

Where extraction quality is uncertain, the system should retain that uncertainty rather than silently presenting extracted content as equivalent to authoritative structured observations.

Evidence metadata may therefore distinguish:

- native text extraction;
- deterministic table extraction;
- layout reconstruction;
- OCR;
- multimodal interpretation.

## Rationale

Retrieval quality is constrained by ingestion quality.

A sophisticated vector database or reasoning model cannot reliably reconstruct document structure that was destroyed during extraction.

Therefore:

> **document structure should be preserved before semantic retrieval whenever that structure carries meaning.**

## Alternatives considered

### Plain PDF-to-text extraction for all documents

Simple and inexpensive but loses structure in complex filings and presentations.

Rejected as the universal strategy.

### Use an LLM to parse every document

Flexible but expensive and probabilistic, including for structure that can often be extracted deterministically.

Rejected as the default.

### OCR every page

Useful for scanned documents but unnecessary and less reliable for digitally generated PDFs with accessible text/layout objects.

Rejected as the first-choice path.

### Store only whole pages

Preserves layout boundary but produces coarse retrieval units and can reduce semantic precision.

Pages may remain an important provenance boundary, but retrieval chunks may need finer structure.

## Consequences

### Positive

- better semantic retrieval;
- stronger evidence provenance;
- fewer malformed table interpretations;
- better section and speaker context;
- improved downstream knowledge extraction;
- more reliable narrative reasoning.

### Negative

- more complex ingestion;
- multiple document-processing strategies may be required;
- extraction metadata becomes richer;
- layout libraries and document formats may vary in quality;
- visual extraction can increase compute cost.

## Architectural invariant

> **Signal should preserve document structure when that structure is necessary to interpret the evidence correctly.**

And:

> **Probabilistic document interpretation must not replace deterministic structured financial sources when authoritative structured data exists.**

## Revisit when

Extraction libraries, multimodal models, and document standards will evolve.

The requirement to preserve semantically meaningful structure remains independent of the implementation technology.
