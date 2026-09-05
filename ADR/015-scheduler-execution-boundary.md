# ADR-015 — Scheduler Owns Timing; Jobs Own Business Logic

**Status:** Accepted  
**Date:** 2026-09-05

## Context

Signal runs recurring ingestion and maintenance workloads across multiple domains, including:

- financial filings;
- shareholding data;
- market prices;
- macroeconomic datasets;
- historical backfills;
- downstream post-ingestion processing.

These workloads may be started in several ways:

- automatically by a scheduler;
- manually through a "Run now" action;
- through a CLI or administrative workflow;
- later, potentially through an external worker or orchestration platform.

If scheduling concerns become embedded inside ingestion and domain logic, the same business operation can develop multiple execution paths.

For example:

```text
Scheduled financial fetch
        ↓
scheduler-specific implementation

Manual financial fetch
        ↓
different implementation
```

This creates duplicate logic, inconsistent behavior, and makes testing or future scheduler replacement more difficult.

The current Signal job design already moves toward reusable per-company and batch execution functions, with schedule rows and manual execution calling the same underlying job behavior.

## Decision

Signal will separate **when a job executes** from **what the job does**.

The scheduler owns:

- cadence;
- next-run calculation;
- enable/disable state;
- triggering;
- schedule metadata;
- execution initiation.

The job implementation owns:

- company/universe resolution;
- fetching;
- parsing;
- normalization;
- validation;
- persistence;
- downstream event creation;
- job-specific success/failure semantics.

Conceptually:

```text
              ┌── Scheduled trigger
              │
Execution ────┼── Manual "Run now"
              │
              └── CLI / future worker
                       ↓
                Shared Job Contract
                       ↓
                 Business Logic
```

A scheduled execution and a manually triggered execution of the same job should use the same underlying job contract.

## Scheduler neutrality

Application/domain logic must not depend directly on a specific scheduler implementation.

The system may initially use an in-process scheduler, cron-like mechanism, or application-managed schedule table.

That implementation may later be replaced by:

- operating-system cron;
- APScheduler;
- Celery Beat;
- a cloud scheduler;
- a workflow orchestrator;
- another scheduling platform.

Such a change should not require rewriting ingestion or domain behavior.

## Job identity

Each logical recurring workload should have a stable job identity.

Job identity is separate from scheduling configuration.

For example:

```text
job_name = nse_xbrl_fetch_nifty50
```

may have:

```text
cadence = quarterly
enabled = true
```

Changing the cadence must not create a new business implementation.

Similarly, separate company universes may have separate job identities when independent audit history or execution control is useful.

## Manual execution

"Run now" is treated as another trigger of the same job.

It must not be implemented as a separate shortcut with different ingestion rules.

This ensures that:

- testing a manual run exercises production job logic;
- operators can replay a scheduled operation;
- audit behavior is consistent;
- failures can be reproduced more easily.

## Rationale

Scheduling infrastructure changes more frequently than domain invariants.

Separating the two preserves:

- scheduler portability;
- testability;
- reusable ingestion logic;
- consistent behavior;
- simpler debugging;
- future worker extraction.

## Alternatives considered

### Embed schedule definitions directly in ingestion code

Simple initially, but couples business logic to execution infrastructure.

Rejected because a scheduler change would affect domain behavior.

### Implement separate manual and scheduled paths

Rejected because duplicate execution paths can diverge.

### Make each scheduled job a standalone script containing all logic

Useful for prototypes, but creates repeated company-resolution, persistence, audit, and error-handling code.

Thin entry points are acceptable; duplicated domain logic is not.

## Consequences

### Positive

- one execution path per logical job;
- scheduler technology can change;
- manual replay is straightforward;
- easier testing;
- reduced duplicate code;
- clearer operational ownership.

### Negative

- explicit job interfaces/contracts are required;
- scheduler metadata and job definitions must remain separately modeled;
- execution context must be passed consistently into jobs.

## Architectural invariant

> **The scheduler decides when work runs. The job decides how the work is performed.**

And:

> **Scheduled, manual, and administrative execution of the same logical workload should converge on the same underlying job implementation.**

## Revisit when

Scheduler technology may be replaced whenever operational requirements justify it.

The separation between triggering and business execution should remain.
