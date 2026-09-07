# ADR-016 — Scheduled Jobs Must Be Idempotent, Incremental, and Self-Healing

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Recurring data ingestion inevitably encounters:

- repeated execution;
- partial failures;
- temporary source outages;
- missing periods;
- interrupted runs;
- manual retries;
- overlapping schedule windows;
- previously completed observations.

A scheduler may invoke the same job more than once.

An operator may also intentionally rerun a job after a failure or use "Run now" for validation.

If repeated execution blindly reprocesses the entire historical dataset, scheduled workloads become:

- expensive;
- slow;
- source-intensive;
- difficult to operate;
- vulnerable to duplicate data;
- harder to recover after partial failure.

Signal already applies this principle in several current workflows.

Examples include:

- previously downloaded/ingested NSE observations being skipped;
- shareholding-detail retrieval using completion state so already-completed quarter details are not repeatedly fetched;
- recent price ingestion re-fetching a small recent window so missed trading-day data can be repaired.

## Decision

Recurring Signal jobs will be designed around three related properties:

1. **Idempotency**
2. **Incremental execution**
3. **Self-healing retries**

## 1. Idempotency

Running the same logical job more than once against the same source data must not corrupt canonical state or create unintended duplicates.

Conceptually:

```text
run(job, input)
run(job, same input)
run(job, same input)

        ↓

Equivalent canonical result
```

Idempotency may be implemented through mechanisms such as:

- stable business keys;
- database uniqueness constraints;
- UPSERT semantics;
- content/file identifiers;
- source observation identifiers;
- explicit completion metadata;
- deduplication rules.

Idempotency must rely on deterministic state where practical rather than on an LLM deciding whether data "looks duplicated."

## 2. Incremental execution

Scheduled jobs should process only the data that is new, changed, incomplete, or intentionally inside a repair window.

The default execution pattern should therefore resemble:

```text
Determine eligible scope
        ↓
Identify missing/new/incomplete work
        ↓
Process only eligible work
        ↓
Persist completion state
```

rather than:

```text
Fetch all historical data
        ↓
Reprocess everything
```

A full rebuild/backfill may exist as a separate operational mode when required.

## 3. Self-healing behavior

Recurring execution should naturally repair recoverable gaps.

For suitable datasets, Signal may deliberately overlap a small recent window.

Example:

```text
Daily price job

Today
  ↓
Refetch recent trading days
  ↓
UPSERT
  ↓
Previously missed observation repaired
```

This approach is preferable to assuming every scheduled run will succeed perfectly.

Transient failures should remain eligible for later retries.

Stable "no data exists" outcomes must be distinguishable from temporary fetch failures.

## Completion state

Where merely checking canonical records is insufficient, the system may persist explicit completion metadata.

For example, a source-detail fetch can record that:

```text
detail_fetched_at != NULL
```

indicates that the source was successfully checked.

This is materially different from:

```text
no detail row exists
```

which could mean either:

- the source contains no detail;
- the request failed;
- the parser failed;
- the detail was never attempted.

Completion metadata should therefore distinguish successful absence from unattempted or failed work.

## Failure handling

A partial batch failure must not invalidate successfully completed items.

Where practical:

```text
Batch
 ├── Company A → success
 ├── Company B → success
 ├── Company C → failed
 └── Company D → success
```

should result in Company C remaining retryable without requiring A, B, and D to be unnecessarily rebuilt.

## Backfills

Historical backfills are distinct from normal incremental scheduling.

They may deliberately process a broad range, but must still preserve:

- idempotency;
- source pacing;
- deterministic keys;
- replayability;
- auditability.

## Rationale

Scheduled systems must assume retries will happen.

Idempotency therefore is not merely an optimization; it is a reliability requirement.

Incrementality reduces:

- source requests;
- compute;
- latency;
- database writes;
- unnecessary downstream indexing.

Self-healing execution allows the normal recurring schedule to repair many small operational gaps without manual intervention.

## Alternatives considered

### Rebuild the entire dataset every run

Operationally simple but inefficient and increasingly impractical as history and company coverage grow.

Rejected as the default recurring model.

### Assume exactly-once scheduler execution

Rejected because real systems experience retries, restarts, manual execution, and ambiguous delivery.

### Skip any period for which some local data already exists

Rejected because partial data can be incorrectly treated as complete.

Completion must be defined at the appropriate semantic level.

### Retry all missing-looking observations indefinitely

Rejected because legitimate source absence can be mistaken for failure.

Stable absence and transient failure must be distinguishable.

## Consequences

### Positive

- safe manual retries;
- lower source load;
- lower execution cost;
- smaller batch duration;
- automatic repair of recent gaps;
- partial failures are easier to recover;
- improved operational reliability.

### Negative

- completion state may need additional schema;
- eligibility logic becomes more sophisticated;
- changing parsers or normalization logic may require explicit reprocessing/version invalidation;
- care is required when determining whether an existing observation is truly complete.

## Architectural invariant

> **Running the same scheduled job repeatedly must not corrupt or duplicate canonical data.**

And:

> **Recurring ingestion should preferentially process new, incomplete, or repairable data rather than rebuild complete history.**

And:

> **A transient failure remains retryable; a successfully verified absence is not treated as a perpetual failure.**

## Revisit when

Specific incremental windows and completion markers may evolve per source.

The idempotency requirement remains regardless of scheduler or deployment technology.
