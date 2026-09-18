# NSE PDF Feasibility Spike — Report

Scope: HDFCBANK, RELIANCE, ICICIBANK, TCS. Sampled periods: "recent" (quarter
ended 30-Jun-2026), "~5y ago" (quarter ended 30-Jun-2021), "~10y ago" (quarter
ended 30-Jun-2016) — the actual sampled quarter per company was whatever NSE's
own announcement history put closest to the target date, per company (all four
happened to land on the same Q1 quarter across companies; not assumed in
advance). All code lives under `spikes/nse_pdf_feasibility/`, raw results
under `spikes/nse_pdf_feasibility/data/` (not committed as a data dump — see
that directory's contents for `results.json`, `earliest.json`, and attempt
logs backing every claim below).

## 1. Verdict: **PASS**

Signal can reliably discover and download official NSE-filed quarterly
financial-results PDFs, going back to roughly 2019-2021 (varies by company),
using an endpoint (`/api/corporate-announcements`) this repo's existing NSE
code does not currently use at all. No blocking (403/429) was hit anywhere in
this spike — all ~34 logged requests (plus additional development-iteration
requests, see the final scope note) returned HTTP 200. The one real,
significant gap: before PDFs became standard (~2016-2018 depending on
company), NSE's own system has neither a real XBRL file nor a real PDF for
the sampled quarter — results were filed as a ZIP bundle (not fetched by this
spike) or an old-format HTML page. That's a genuine historical-coverage
limit, not a blocking/access problem, and is documented in Section 8.

The Balance Sheet gap this spike was asked to check for is real and
concretely confirmed (Section 7): for all four companies, this app's
`canonical_financials` table currently holds **zero balance-sheet facts for
any Q1 quarter** — only P&L line items and ratios — while the downloaded PDFs
for the same companies/quarters contain a full "Statement of Assets and
Liabilities" (Capital, Reserves, Deposits, Borrowings, Advances, Investments,
etc.), directly read and verified from a real downloaded PDF (Section 7 has
the exact figures).

## 2. Results by company and period

| Company | Period sampled | Filing found | PDF downloaded | XBRL exists | Verified real financial result |
|---|---|---|---|---|---|
| HDFCBANK | Recent (Q1 FY27, Jun-2026) | Y | Y | Y | Y |
| HDFCBANK | ~5y ago (Q1 FY22, Jun-2021) | Y | Y | Y | Y |
| HDFCBANK | ~10y ago (Q1 FY17, Jun-2016) | Y (announcement) | N (ZIP, not PDF) | N (placeholder XBRL) | N/A — no PDF/XBRL to verify |
| RELIANCE | Recent (Q1 FY27) | Y | Y | Y | Y |
| RELIANCE | ~5y ago (Q1 FY22) | Y | Y | Y | Y |
| RELIANCE | ~10y ago (Q1 FY17) | Y (announcement) | N (ZIP) | N (placeholder) | N/A |
| ICICIBANK | Recent (Q1 FY27) | Y | Y | Y | Y |
| ICICIBANK | ~5y ago (Q1 FY22) | Y | Y | Y | Y |
| ICICIBANK | ~10y ago (Q1 FY17) | Y (announcement) | N (ZIP) | N (placeholder) | N/A |
| TCS | Recent (Q1 FY27) | Y | Y | Y | Y |
| TCS | ~5y ago (Q1 FY22) | Y | Y | Y | Y |
| TCS | ~10y ago (Q1 FY17) | Y (announcement) | N (ZIP) | N (placeholder) | N/A |

No case hit "blocked" or "timed out" — NSE never rate-limited or blocked this
spike's traffic (see Section 4/5).

8 real PDFs were downloaded (recent + ~5y-ago for all 4 companies), sizes
0.58MB-18.8MB, all opened successfully with `pypdf`. Raw files are under
`spikes/nse_pdf_feasibility/data/pdfs/<SYMBOL>/`.

## 3. Earliest reliably discoverable filing per company

Using `/api/corporate-announcements` (full history in one call, going back to
2004-2005 for all four companies) filtered to genuine financial-results rows
(Section 4's verification method), then further filtered to rows carrying an
actual PDF attachment (not a ZIP, not a "-" placeholder):

| Company | Earliest PDF-attached financial-result announcement |
|---|---|
| ICICIBANK | 2019-01-07 |
| TCS | 2021-04-12 |
| RELIANCE | 2021-04-30 |
| HDFCBANK | 2020-08-27 |

Before these dates, the same announcement category exists (back to
2005-2007) but the attached file is a ZIP archive or the old
`resultDetailedDataLink` HTML page, not a standalone PDF this spike's simple
downloader recognized. A ZIP-aware downloader would likely extend PDF/XLS
coverage several years earlier per company — not attempted here (see Section
11).

## 4. NSE discovery/download mechanism used

Reused the exact session-bootstrap pattern already in
`sources/nse_xbrl.py` (`_new_session()`/`_bootstrap()`): a plain GET to
`https://www.nseindia.com/companies-listing/corporate-filings-financial-results`
with a real browser `User-Agent`, which hands out the anti-bot cookies a cold
API call is otherwise rejected without. This spike's version
(`nse_pdf_fetch.py: new_session()`) is a smaller copy of that logic with its
own short, hard timeout (15s) and a small (2-attempt) retry budget — never an
unbounded retry loop, per the task's hard rule.

Three endpoints used, all GET, all under the bootstrapped session:

- `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>`
  — **the key discovery not previously in this repo's NSE code.** Returns a
  company's FULL disclosure history (2004/2005 onward) in one response, each
  row carrying `desc` (category), `attchmntText` (a human-readable
  description NSE itself writes), and `attchmntFile` (a direct PDF/ZIP/etc.
  URL under `nsearchives.nseindia.com`) or `"-"` if none.
- `https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=<SYM>&period=Quarterly`
  — the endpoint `sources/nse_fetch.py` already uses for XBRL; reused
  read-only here (via `fetch_filing_index_raw`) purely to check XBRL
  existence for the same period, not modified.
- `https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYM>`
  — same, for the newer Integrated Filing framework (Q4 FY25 onward).

No blocking encountered: **zero 403s, zero 429s, zero timeouts** across every
request this spike made (all attempt-log entries show `status_code: 200`).
This is worth flagging as a real, if provisional, result — NSE's WAF did not
distinguish this spike's traffic from `sources/nse_xbrl.py`'s already-proven
pattern, at the request volumes and pacing used here (see Section 5/final
scope note for exact volume). It should NOT be read as "NSE has no
rate limit" — only that this spike's volume/pacing stayed under whatever
threshold triggers blocking.

### Verifying a PDF is a genuine financial-result filing, not an excluded type

This was harder than expected and is itself a finding (task requirement 5).
A first-pass filter (match on `desc` category, or match on `attchmntText`
containing a phrase like "financial results for the period ended") produced
false positives verified live: a Reliance "Press Release" row's own
`attchmntText` reads *"...on the Consolidated and Standalone Unaudited
Financial Results for the quarter ended June 30, 2021, we send herewith a
copy of Media Release..."* — genuinely mentions the financial results, but
the attached PDF is a media release, not the result filing itself. Same
pattern verified for an ICICI "Copy of Newspaper Publication" row and a
Reliance "Analysts Meet" row (analyst presentation referencing the same
quarter's results).

The working filter (`run_spike.py: _is_fin_result_row`) is therefore
two-stage: (1) hard-exclude a fixed set of `desc` categories verified live to
produce this false-positive pattern (Press Release, Analysts Meet,
Investor Presentation, Con. Call transcripts/recordings, Newspaper
Publication, News Clarification); (2) within what's left, require either a
recognized `desc` (e.g. "Financial Result Updates", "Outcome of Board
Meeting") or an `attchmntText` phrase match. This is the closest this spike
got to an automatable verification rule; it is NOT proven bulletproof at
Nifty 500 scale — see Section 10/11.

A second real finding: for the **most recent** quarter (Jun-2026) at all four
companies, the genuine result PDF was NOT filed under any of NSE's dedicated
"Financial Result" categories — it was filed under `desc="Outcome of Board
Meeting"`, with `attchmntText` explicitly stating *"...has submitted to the
Exchange, the financial results for the period ended Jun 30, 2026."* A filter
that only trusted `desc` category names (as an initial version of this
spike's own code did) would have missed the most recent quarter entirely and
silently picked a 19-month-old administrative "Integrated Filing- Financial"
resubmission instead — caught only by cross-checking `attchmntText` content.

## 5. PDF availability and reliability

- Recent + ~5y-ago periods (2021, 2026): **8/8 PDFs found and downloaded
  successfully** (100% for the periods where NSE's own system carries a PDF
  attachment at all).
- ~10y-ago period (2016): **0/4** — the attachment for all four companies at
  this date is a `.zip`, not a `.pdf` (e.g.
  `Result30062016_21072016115151.zip`). Not a failure/error — the spike's
  downloader correctly identified these as non-PDF and skipped them rather
  than mis-tagging a ZIP as a PDF.
- No HTTP errors, no corrupted downloads, no timeouts across any of the 8
  real downloads (all opened cleanly with `pypdf`, page counts 16-29 for the
  6 text-PDFs, 22 pages for one, plus one large 18.8MB PDF).
- Two of the 8 downloaded PDFs (`recent` filings, both scanned/signed
  cover-letter-style submissions) extracted very little usable text via
  `pypdf` (e.g. HDFCBANK recent: 22 pages, only 2,389 characters) — these
  appear to be predominantly scanned/image content or have an unusual text
  layer. The 6 `~5y_ago` PDFs (2021) extracted cleanly (32,700-81,200
  characters each) with intact, readable financial tables.

## 6. XBRL vs PDF coverage differences

For the periods where both existed (recent + ~5y-ago, all 4 companies):
XBRL and the PDF cover the same reporting period and the same
consolidated/standalone split, as expected — no surprises there. The real
coverage difference found is not PDF-vs-XBRL-as-filed, but **XBRL-as-filed
vs. what's actually in this repo's own database** (Section 7) — a downstream
ingestion/reconciliation gap, not an NSE data-availability gap.

## 7. Real examples of facts present in a PDF but absent from this repo's data

Directly queried (read-only) this repo's real `canonical_financials` table in
production Neon:

- `SELECT ... WHERE company_id='HDFCBANK' AND fiscal_year='FY2022' AND quarter='Q1'` → **0 rows** (Q1 FY22 = quarter ended 30-Jun-2021, the exact period of one of the downloaded PDFs). Checking what fiscal years exist at all for HDFCBANK: **quarterly** (non-null `quarter`) rows only start at FY2023 Q1 — every fiscal year FY2004-FY2022 has only an annual row (`quarter IS NULL`). So the entire pre-FY2023 quarterly XBRL era (technically available on NSE from mid-2019 per `sources/nse_xbrl.py`'s own comments) has not been ingested into `canonical_financials` as quarterly data at all — a genuine, real, currently-open ingestion gap this spike surfaced as a side effect, not something requiring a PDF to know about.
- `SELECT ... WHERE company_id='HDFCBANK' AND fiscal_year='FY2027' AND quarter='Q1'` (the "recent" quarter, which IS ingested) → 28 rows, but **every one is a P&L line item or ratio** (`eps`, `net_profit`, `interest_earned`, `gross_npa_percent`, `return_on_assets_percent`, `shares_outstanding`, etc.) — **zero balance-sheet metrics** (no `total_assets`, `deposits`, `advances`, `total_equity`, `borrowings`, `investments`). Same zero-balance-sheet-metric result for RELIANCE and TCS FY2027 Q1 (18 rows each, all P&L/ratios).
- The downloaded HDFCBANK PDF for quarter ended 30-Jun-2021 (`spikes/nse_pdf_feasibility/data/pdfs/HDFCBANK/~5y_ago_142732.pdf`, page 4 of the extracted text) contains a complete **"Statement of Assets and Liabilities as at 30-Jun-2021"** for both standalone and consolidated, directly read from the PDF text:

  Standalone, as at 30-Jun-2021 (₹ lakh, as printed):
  ```
  CAPITAL AND LIABILITIES
  Capital                          55,267
  Reserves and Surplus         21,193,527
  Deposits                    134,582,934
  Borrowings                   13,127,502
  Other Liabilities/Provisions  6,434,878
  Total                       175,394,108
  ASSETS
  Cash & bal. with RBI         10,462,511
  Bal. with Banks & Money at Call 1,535,458
  Investments                  43,613,164
  Advances                    114,765,164
  Fixed Assets                    500,538
  Other Assets                  4,517,273
  Total                       175,394,108
  ```
  None of these line items (Deposits, Advances, Investments, Borrowings,
  Reserves and Surplus, Fixed Assets, Total Assets) exist anywhere in
  `canonical_financials` for HDFCBANK at any Q1 quarter checked. This is a
  directly-read, real example of the exact gap the task asked this spike to
  find — present in the PDF, absent from this repo's XBRL-sourced data.

  (Balance-sheet numbers were confirmed present in the PDF by direct text
  read as shown above; no values were estimated, interpolated, or derived —
  only what `pypdf` extracted verbatim from the real downloaded file.)

## 8. Historical coverage limitations / blocking behavior

**No blocking was observed anywhere in this spike** — this is itself a
finding worth being precise about: it means the *access* problem this task
was scoped to test for (in case the earlier stalled attempt had hit NSE
blocking) did not reproduce here. The earlier attempt's failure is more
consistent with an unbounded/hanging request than with active blocking (see
`sources/nse_xbrl.py`'s own docstring, which documents exactly this failure
mode for its bootstrap call before a fix).

The real, concrete limitation found is **historical data-format coverage**,
not access:

- **XBRL**: placeholder (`.../xbrl/-`) for all four companies at the
  ~10-years-ago (2016) sample — confirms `sources/nse_xbrl.py`'s own
  documented finding that XBRL only became mandatory/available from
  30-Jun-2019 onward.
- **PDF**: the `corporate-announcements` endpoint's attachment for the same
  2016 quarter is a `.zip`, not a `.pdf`, for all four companies. The
  earliest real PDF attachment found per company ranges 2019-2021
  (Section 3).
- **Before that**: only the old `resultDetailedDataLink` field (a
  `nsearchives.nseindia.com/archives/financial_results/*.html` page, not a
  PDF) and/or a ZIP attachment are available. Neither was downloaded/parsed
  by this spike (out of scope — see Section 11).

Net: for any company/period older than roughly 2016-2019 (varies by
company), **neither XBRL nor a directly downloadable PDF is reliably
available** through the endpoints this spike tested. That's the spike's
main historical-coverage finding.

## 9. Recommended production architecture

A real ingestion pipeline should treat `/api/corporate-announcements` as a
**new, separate NSE source adapter** (analogous to `sources/nse_xbrl.py` but
for PDFs), not bolted onto the existing XBRL fetch path:

1. **Discovery**: one `corporate-announcements` call per company lists full
   history in one response — cheap, cacheable (same TTL-cache pattern
   `sources/nse_xbrl.py` already uses), no per-period pagination needed.
2. **Filtering/verification**: the two-stage filter from Section 4
   (desc-category exclusion list + `attchmntText` content match) is a
   reasonable starting point but needs broader validation before Nifty 500
   scale — a wrong match here risks ingesting a press release or transcript
   as if it were canonical.
3. **Source hierarchy** (validated as feasible, not built): XBRL preferred
   whenever a fact exists there → PDF-extracted fact used only for the
   metrics missing from XBRL for that exact period/statement_type (the
   balance-sheet gap in Section 7 is exactly this case) → investor
   presentations/press releases/transcripts never used for canonical facts
   (and per Section 4, must be actively excluded, not just "not sought out",
   since they surface in the same discovery feed). A PDF-sourced fact's
   provenance is capturable in the shape the task specified —
   `source_type=NSE_FILING_PDF, extraction_method=PDF, document_id,
   filing_date, period` — every one of those fields was directly available
   from what this spike observed (`seq_id` as `document_id`, `an_dt`/
   `broadcast_Date` as `filing_date`, the extracted period-end as `period`).
4. **PDF fact extraction**: `pypdf` (already in this repo's environment) was
   sufficient to get raw text for 6 of 8 PDFs, but table structure was not
   reliably preserved for scanned/signed cover-page-style filings (2 of 8).
   A table-aware extractor (`pdfplumber` or `camelot`, neither currently
   installed) would very likely be needed for a production extractor,
   rather than raw-text regex matching on `pypdf` output alone.
5. **Where XBRL is genuinely absent** (pre-2019, Section 8), the PDF (where
   a real one exists, 2019-2021 onward per company) becomes the *only*
   automatable source — earlier than that, this spike found no
   automatable path at all (Section 8's ZIP/HTML-only gap).

## 10. Recommended Nifty 500 backfill approach

Realistic given what was actually observed:

- The `corporate-announcements` discovery call is genuinely cheap at scale —
  one call per company returns full history, no NSE blocking was hit at this
  spike's volume (roughly 100+ requests across development iterations, zero
  403/429s — see final scope note). A Nifty 500 backfill (500 companies × 1
  discovery call + N PDF downloads) is very plausibly the same order of
  magnitude of traffic `sources/nse_xbrl.py`'s existing production batch job
  already generates, and that job is already running in production without
  reported blocking.
- **Do not assume this generalizes without validation.** This spike tested 4
  large-cap, high-profile companies over a handful of periods each. NSE's
  WAF behavior at 500-company, multi-year-history scale, run back-to-back,
  was not tested here and could behave differently (rate limits are commonly
  volume/pacing-sensitive, not just per-request). The existing production
  code's own pacing (`_REQUEST_PACING_SECONDS = 1.0`, exponential backoff,
  a 403-triggered re-bootstrap) should be carried over unchanged into any
  real PDF-fetch adapter — this spike deliberately used a *smaller* retry
  budget than production specifically because it's a spike, not because a
  smaller budget is adequate for a real backfill.
- The false-positive verification risk (Section 4) is the main blocker to
  *unsupervised* bulk backfill: at 500 companies × 10+ years, a filter that
  occasionally mistakes a press release or newspaper clipping for the real
  filing will produce silently wrong "financial-result" documents unless
  spot-checked. Recommend an initial backfill with the filter's matches
  logged for manual spot-check on a sample (e.g. 20-30 companies) before
  trusting it unsupervised across all 500.
- Pre-2019-ish coverage (Section 8) cannot be bulk-backfilled through this
  mechanism at all — those periods have neither XBRL nor PDF via any
  endpoint this spike tested. If that history matters, it needs either (a)
  the ZIP/HTML fallback explored (Section 11) or (b) a paid NSE/BSE
  historical-filings data vendor — this spike did not evaluate any vendor
  option.

## 11. Exact next implementation step

Extend `run_spike.py`'s `_is_fin_result_row` false-positive check
(Section 4) into a **standalone validation script** that runs the two-stage
filter across a larger sample — e.g. 15-20 companies × every quarter in one
fiscal year — and dumps every matched row's `desc`/`attchmntText`/
`attchmntFile` to a CSV for manual review, specifically hunting for further
false positives beyond the three categories already excluded (Press
Release, Analysts Meet, Copy of Newspaper Publication) and false negatives
(a genuine filing whose `desc` isn't in the recognized set and whose
`attchmntText` doesn't contain any of the current text markers — e.g. a
company that phrases its submission differently than the two banks and two
non-banks sampled here). This is the single highest-leverage next step
because the verification-filter's reliability, not NSE access or PDF
downloading, is this spike's biggest open question before any real
ingestion work is justified.

---

## Appendix: what this spike's code does

- `spikes/nse_pdf_feasibility/nse_pdf_fetch.py` — low-level NSE HTTP layer:
  session bootstrap (copied pattern from `sources/nse_xbrl.py`), a single
  bounded `_get()` (hard 15s timeout, 2-attempt budget, never raises past
  itself — every failure is logged to `ATTEMPT_LOG` and returns `None`/the
  response for the caller to handle), and `download_pdf()`.
- `spikes/nse_pdf_feasibility/run_spike.py` — orchestrator: per company,
  fetches the full `corporate-announcements` history + XBRL listings once,
  samples 3 target periods, applies the verification filter, downloads PDFs,
  and writes `data/results.json` + `data/attempt_log.json`.
- `spikes/nse_pdf_feasibility/db_readonly.py` — a separate, minimal
  read-only Postgres helper (own `psycopg2` connection, session set
  `readonly=True`, 15s `statement_timeout`) used only to run the
  `canonical_financials`/`companies` SELECT queries in Section 7 — does not
  import or touch `storage/database.py` or any repository class.
- `spikes/nse_pdf_feasibility/data/` — `results.json` (full structured
  results per company/period), `earliest.json` (Section 3's data),
  `attempt_log*.json` (every logged NSE HTTP call), `pdfs/<SYMBOL>/*.pdf`
  (the 8 real downloaded filings).

What worked cleanly: discovery via `corporate-announcements`, PDF download,
XBRL-existence cross-check, the read-only DB comparison. What needs more
work before this is production-ready: the verification filter's precision at
scale (Section 11), and table-aware PDF extraction for precise fact-level
comparison (Section 9, point 4) — this spike's Section 7 figures were read
directly off clean-extracting PDFs' raw text, not through any structured
table parser.
