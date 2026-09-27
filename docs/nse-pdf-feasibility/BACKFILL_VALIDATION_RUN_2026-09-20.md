# NSE filing-document backfill — first real run validation (2026-09-20)

Real (non-dry-run) invocation of `scripts/backfill_nse_filing_documents.py`
against production Neon + S3 for two validation companies (BANKBARODA,
AAVAS), followed by a read-only verification pass. No writes were made
during verification; all queries below ran with `set_session(readonly=True)`
against the live `NEON` connection string, plus S3 `HeadObject` calls only
(no `GetObject`).

## Reported run output (from the background process)

```
BANKBARODA: announcements=1423 classified=[concall_transcript=23, investor_presentation=47, quarterly_result_filing=68]
            unclassified=1285 stored_new=137 stored_duplicate=1 download_errors=0
AAVAS:      announcements=1003 classified=[concall_transcript=53, investor_presentation=36, quarterly_result_filing=28]
            unclassified=886  stored_new=117 stored_duplicate=0 download_errors=0
```

## 1. DB + S3 verification (read-only)

Queried `raw_objects` directly (`source='nse', entity IN ('BANKBARODA','AAVAS')`):

- **BANKBARODA**: 137 rows, all `state='stored'` — breakdown
  concall_transcript=23, investor_presentation=46, quarterly_result_filing=68.
  Note: classified count for investor_presentation was 47 but only 46 are
  new rows — this matches `stored_duplicate=1` exactly (the one duplicate
  landed in that type), so the numbers reconcile.
- **AAVAS**: 117 rows, all `state='stored'` — concall_transcript=53,
  investor_presentation=36, quarterly_result_filing=28. Matches
  `stored_new=117, stored_duplicate=0` exactly.
- **Safety boundary**: queried for any row for either company with
  `state NOT IN ('stored')` — **zero rows**. No row has been advanced to
  `validated`/`parsed`/`ingested`/`reconciled`. The pipeline boundary held.
- **S3 spot-check**: took one row per (company, object_type) — 6 objects —
  and ran `HeadObject` against `signals-app-documents-862938824222`. All 6
  resolved with real, non-trivial sizes (444 KB – 4.5 MB PDFs), confirming
  actual bytes exist at the cataloged `s3_key`, not just DB rows.

## 2. Unclassified-row spot check

Re-ran discovery (`discover_company_filings`, one read-only network call per
company against NSE's public `corporate-announcements` API — no downloads)
to inspect the actual unclassified rows (these aren't persisted anywhere,
so they can only be inspected live).

Desc-bucket breakdown of the unclassified sets:

- **BANKBARODA** (1285 unclassified): dominated by `Updates` (356),
  `General Updates` (143), `Interest Rates Updates` (91), `Copy of
  Newspaper Publication` (55), `Credit Rating` (53), `Analysts/...Con. Call
  Updates` (52, of which a sample showed these are schedule/meet-link
  intimations, not transcripts), `Loss of Share Certificates` (48),
  `Trading Window` (39), `Shareholders meeting` (34), `Change in
  Management` (28), etc.
- **AAVAS** (886 unclassified): dominated by `Analysts/...Con. Call
  Updates` (314 — confirmed by sampling to be near-100% forward-looking
  "Intimation of Investor(s)/Analyst(s) Meet/Call scheduled to be held on
  <date>" notices, correctly excluded by the schedule-notice guard),
  `Updates` (93), `Outcome of Board Meeting` (49 — sampled 15, all
  genuine non-results board business: committee meetings, director
  reappointment, disclosure filings), `Schedule of Analysts/...Con. Call`
  (48), `Copy of Newspaper Publication` (45), `Press Release` (41),
  `Credit Rating` (32), etc.

Sampled examples confirm these are genuinely dividend/AGM/credit-rating/
management-change/newspaper-notice/scheduling content, not one of the
three target document types — the classifier's "skip rather than guess"
trade-off is behaving as designed for the large majority of the
unclassified volume.

**One real (narrow) recall-gap finding**: BANKBARODA has exactly 1
unclassified row reading `desc='Updates'`, text `"Integrated Filing - Q3
December 2024"` — a genuine SEBI Integrated Filing (a bundled regulatory
submission that includes financial results among other sections). The
mixed-bag disambiguator (`_disambiguate_mixed_bag_by_content`) only ever
resolves a mixed-bag desc to `investor_presentation` or `concall_transcript`
— it never attempts `quarterly_result_filing`, so this class of row is
structurally unreachable regardless of its text. Volume is 1 of 1285 for
this company (not seen at all in AAVAS's set) — not worth a code change on
this pass, but worth a line in the module docstring's "known limitations"
if Integrated Filing rows show up more often at Nifty-500 scale.

No pipeline code was changed in this verification pass (no bug rose to the
level of needing a fix).

## 3. Nifty-500 rollout — what's needed next

- **Registry coverage**: 2553 of 2591 companies in the DB already carry an
  `nse_symbol` — Nifty-500 membership is a small subset of that, so symbol
  resolution is not expected to be a blocker.
- **Request volume**: each company costs 1 discovery call (full history,
  no pagination) + 1 download per confidently-classified filing. This run's
  two companies classified 138 (BANKBARODA) and 117 (AAVAS) filings —
  call it ~127 downloads/company on average, though both are large, long-
  listed companies, so a Nifty-500-wide average is plausibly lower for
  younger/smaller constituents. Rough estimate: 500 discovery calls + up to
  ~60–65k download requests.
- **Runtime**: measured directly from `fetched_at` timestamps this run —
  BANKBARODA's 137 stores spanned ~6m24s, AAVAS's 117 spanned ~5m19s,
  i.e. ~2.7–2.8s per stored document end-to-end (network fetch + S3 write),
  consistent with `sources/nse_fetch.py`'s deliberate
  `_REQUEST_PACING_SECONDS = 1.0` courtesy delay plus large-PDF download/
  upload time. At that rate, ~60k downloads for Nifty-500 is on the order
  of **~45–50 hours of serial runtime** — this needs to run as an
  unattended multi-day batch, not an interactive session.
- **Resumability**: `store_raw_object()` dedups by content hash before
  writing anything, so a re-run (after an interruption, crash, or partial
  company failure) is safe and cheap — already-stored filings cost one
  DB lookup each, not a re-download. This makes a checkpoint/resume
  strategy unnecessary to build separately; a company-level retry loop on
  top of the existing `BatchRun` per-item tracking is sufficient.
- **S3 storage growth**: the 6 sampled objects ranged 444 KB–4.5 MB
  (~2.7 MB average). At ~127 files/company × 500 companies × ~2.7 MB,
  that's roughly 150–200 GB of additional S3 storage — a real but
  inexpensive increment (S3 standard storage cost, not Neon's tier-capped
  Postgres storage that the FTS backfill had to budget around).
- **Failure handling**: both companies in this run reported
  `download_errors=0`, which is optimistic for a 500-company run against a
  WAF-protected host — before scaling, add an explicit "companies with
  download_errors > 0" report at the end of a batch so partial failures get
  a manual re-run rather than silently completing with a smaller-than-
  expected `stored_new`.
- **Concurrency**: not recommended to parallelize downloads across
  companies without care — the 1 req/sec pacing is a deliberate anti-WAF
  courtesy delay, and multiple concurrent sessions hitting the same host
  risk exactly the blocking this module's session bootstrap already works
  around. If the ~48-hour serial runtime is unacceptable, the safer lever
  is running a small number of independent worker processes (each its own
  session/cookie jar) rather than raising request rate within one session.
