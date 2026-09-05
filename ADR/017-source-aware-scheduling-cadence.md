# ADR-017 — Scheduling Cadence Follows Source Availability, Not Arbitrary Clock Frequency

**Status:** Accepted  
**Date:** 2026-09-05

## Context

A dataset's conceptual frequency does not necessarily determine the correct time to ingest it.

For example, a financial statement may be described as "quarterly," but that does not mean authoritative data is available on the final day of the quarter.

Different sources publish according to different operational and regulatory timelines.

Examples include:

- quarterly financial results becoming available only after the reporting period and filing window;
- shareholding disclosures following a different regulatory deadline;
- daily market prices becoming available after market activity;
- monthly macroeconomic series being published according to agency release calendars;
- historical revisions arriving after the original observation.

If Signal simply schedules:

```text
quarterly dataset → run every quarter-end
monthly dataset   → run first day of month
```

it can repeatedly query sources before authoritative information exists.

This increases:

- unnecessary requests;
- empty runs;
- false failure signals;
- operational noise;
- source load.

The current Signal scheduling plan already differentiates financial-result timing from shareholding timing because their regulatory publication windows differ.

## Decision

Signal will determine recurring ingestion cadence using the **publication lifecycle of the authoritative source**, not merely the nominal periodicity of the dataset.

The scheduling model therefore distinguishes:

```text
Data Periodicity
        ≠
Source Availability
        ≠
Ingestion Schedule
```

For example:

```text
Quarterly financial period
        ↓
Regulatory filing window
        ↓
Expected availability
        ↓
Scheduled ingestion
```

## Scheduling inputs

A source-aware schedule may consider:

- regulatory filing deadlines;
- expected publication lag;
- source release calendar;
- market close;
- historical source behavior;
- revision behavior;
- rate limits and pacing;
- operational load;
- company-specific fiscal calendars where required.

## Regulatory examples

For India, quarterly financial-result ingestion should not assume that quarter-end means the filing is already available.

The current scheduling policy allows for the SEBI reporting window and schedules financial ingestion after the likely filing period.

Shareholding disclosures have a different filing window and therefore can be scheduled closer to quarter-end.

The important architectural point is not the exact number of days.

It is that:

> **different datasets with the same nominal quarterly frequency can require different schedules because their authoritative publication lifecycles differ.**

## Fiscal-period awareness

Calendar timing must also not silently substitute for company fiscal-period semantics.

For sources where company fiscal quarters differ from simple calendar quarters, Signal should model or resolve the appropriate reporting period before treating a generic quarterly source as equivalent.

A scheduler cannot repair an incorrect period-mapping model.

Therefore:

```text
Correct source semantics
        ↓
Correct availability window
        ↓
Schedule
```

must precede automation.

## Late filings and missing data

The first scheduled check may occur after expected availability, but some companies or sources may still be late.

Signal should therefore support follow-up attempts using bounded retry/recheck policies rather than treating the first absence as permanent.

Conceptually:

```text
Expected availability reached
        ↓
Attempt ingestion
        ↓
Available?
   ┌────┴────┐
  Yes        No
   ↓          ↓
Store      Recheck later
```

This should integrate with the idempotent/incremental behavior defined in ADR-016.

## Release-calendar jobs

For macroeconomic datasets with known publication calendars, event- or release-aware scheduling may eventually be preferable to simple monthly polling.

The initial implementation may still use periodic windows, provided those windows are chosen around realistic source publication timing.

## Rationale

The goal of scheduled ingestion is not to run frequently.

The goal is to obtain authoritative data reliably after it becomes available.

Source-aware scheduling:

- reduces meaningless runs;
- respects regulator/source behavior;
- improves freshness;
- reduces false alarms;
- reduces network traffic;
- makes operational metrics more meaningful.

## Alternatives considered

### Schedule strictly by dataset frequency

Example:

```text
quarterly → every quarter-end
monthly → first day of every month
```

Rejected because nominal frequency does not imply publication availability.

### Poll every source daily

Would eventually capture most updates but creates unnecessary requests, operational noise, and avoidable source load.

Rejected as the default for slow-moving datasets.

### Hard-code fixed dates globally

Rejected because publication timing differs across jurisdictions, datasets, and sources.

### Let an LLM decide when to fetch

Rejected because source release rules and schedules are operational facts that should be represented deterministically.

## Consequences

### Positive

- fewer unnecessary fetches;
- better data freshness relative to real availability;
- fewer false failures;
- source-specific regulatory behavior is respected;
- easier operational capacity planning;
- better alignment between source semantics and ingestion.

### Negative

- schedule configuration becomes source-aware;
- regulatory timelines must be maintained;
- late filings require retry/recheck rules;
- expansion into new jurisdictions requires understanding local publication behavior.

## Architectural invariant

> **Signal schedules acquisition according to when authoritative data is expected to exist, not merely according to the period the data describes.**

And:

> **A scheduler must not compensate for unresolved source semantics or incorrect fiscal-period mapping.**

## Revisit when

Specific timing windows should be updated when:

- regulatory deadlines change;
- source publication behavior changes;
- release-calendar integration becomes available;
- observed ingestion telemetry supports a better schedule.

The source-aware scheduling principle should remain.
