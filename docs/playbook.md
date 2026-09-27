# Playbook: Extending Signals' Data Coverage

Operational how-to for four recurring extension tasks: adding a new
geography, adding a company universe, adding a macro data source, and
adding a reliable financial-filings source. Each section is a checklist
against the real patterns already in this codebase (file paths/functions
verified, not illustrative) — extend the existing pattern, don't invent a
parallel one.

This playbook assumes and reuses [architecture.md](architecture.md) (module
map, data model) and the durable architecture guardrails below. It doesn't
replace either — check both before a change that doesn't fit neatly into
one of the four checklists here.

## Guardrails that apply to everything below

1. **Nothing is disposable.** A manually triggered fetch/registration/
   calculation must go through the same durable pipeline
   (`Acquire → Preserve Raw Source → Register Provenance → Normalize →
   Persist → Index → Associate → Make Reusable`) automation will later
   reuse — never a throwaway script whose output only lives in your
   terminal. Test: "if Signals needs this again in six months, can it
   reuse what it already acquired?"
2. **New geography is annual-first.** Establish annual financials +
   historical pricing before quarterly/higher-frequency data. Don't assume
   a new geography behaves like an existing one.
3. **Macro data is geography-aware.** Tag geography/region/currency/source
   on every series; never substitute a foreign indicator for missing local
   data without saying so.
4. **Local evidence, global hypotheses.** Geography constrains *evidence*,
   not the system's ability to *propose* a cross-geography hypothesis — but
   a hypothesis must be labeled as one, never silently promoted to fact.
5. **Preserve epistemic state.** Keep `FACT → CALCULATION → HYPOTHESIS →
   EVIDENCE → INFERENCE` distinct.
6. **Architecture check** before shipping: durability, provenance,
   reusability, geography applicability, time applicability, epistemic
   state, user agency, extensibility (can a *third* geography reuse this
   without duplicating the whole pattern?).

(Full text and rationale: the "Signals — Data & Geography Architecture
Guardrails" directive, 2026-09-25 — kept in this repo's own project memory,
not a file here; ask if you need the complete original wording.)

---

## 1. Adding a new geography

Reference implementation: India (original) → United States (2026-09,
S&P 500 then Russell 3000). Follow the same order for a third market:

```
Company Universe → Classification/Taxonomy → Annual Financial Foundation
  → Historical Pricing → Documents On Demand → Learn Geography Nuances
  → Add Higher-Frequency Data Later
```

1. **Company universe** — see §2 below. Get a broad, correctly-classified
   company list on file before anything else.
2. **Annual financial foundation** — a `SourceAdapter` (file-based) or
   live-fetch adapter (see §4) hitting that geography's authoritative
   filing source, `trust_rank=0`, annual-only filter applied output-side
   (`period_types={"annual"}`, `min_fiscal_year` — see
   `ingestion/pipeline.py::ingest_sec_edgar_company()` for the exact
   pattern: fetch → `store_raw_object()` → parse → filter → validate →
   `insert_financial_observations()` → reconcile → publish
   `DatasetIngestedEvent`).
3. **Historical pricing** — independent of financials (own script, own
   schedule — see `sources/yfinance_prices.py`'s `resolve_yfinance_ticker
   (ticker, country=...)` ticker-suffix/override pattern and
   `scripts/backfill_price_history_usa.py`).
4. **Documents on demand** — do **not** bulk-download filings for the
   whole universe. The existing Docs-tab manual-upload/paste-URL flow
   (`web/app.py::company_add_document()` → `save_company_document()` →
   `ingestion/coordinator.py::process_documents()`) is already
   geography-agnostic — verify a company from the new geography works
   through it with zero code changes before building anything bespoke.
5. **Learn geography-specific nuances** before adding depth: fiscal-year
   convention (`companies.fiscal_year_end_month`), accounting-standard
   quirks, local terminology (e.g. India's GNPA/CASA vocabulary doesn't
   transplant to a US bank — `financials/ratios.py`'s known gap), currency/
   units, filing conventions, regulator/exchange structure.
6. **Higher-frequency data later** — quarterly financials, scheduled
   monitoring, deeper document coverage — only once annual-first evidence
   shows it's actually needed for real research questions, not "for
   completeness."

**What's still ad hoc, not a general interface**: country dispatch today is
`if country == "IN" / "US"` branching inside individual modules (ticker
override dicts in `sources/yfinance_prices.py`/`sources/sec_edgar.py`,
duplicated per module), not a polymorphic `GeographyAdapter`. A third
geography is the natural forcing function to extract that — do it as part
of the work, not as a preparatory refactor with no consumer yet.

---

## 2. Adding a company universe (bulk registration)

Reference: `scripts/register_sp500_companies.py` (S&P 500),
`scripts/register_russell3000_companies.py` (Russell 3000/1000/2000 —
mirrors iShares ETF holdings, one company row shared across overlapping
index tags).

1. **Pick a membership source.** Must be authoritative or "sufficiently
   reliable" — never infer membership from market cap/ticker/sector. No
   free official list? A well-known ETF-holdings proxy (iShares/SPDR) with
   a dated "as of" field is an accepted substitute; document the
   substitution.
2. **Resolve collisions before registering**, via
   `companies/us_universe.py`:
   - `normalize_us_ticker(raw)` — strip `.`/space share-class separators
     (`BRK.B`/`BRK B` → `BRKB`). *(A real bug fixed 2026-09-26: the
     original version only stripped `.`, missing space-separated tickers
     like `"BRK B"` — 8 companies failed on the first full production run
     until this was fixed. Check both separators for any new source.)*
   - `resolve_us_company_id(ticker, existing)` — 3-way: not registered →
     use ticker as-is; already registered under this id → skip (idempotent
     re-run); collides with an existing *different-country* `company_id` →
     disambiguate as `"{ticker}-US"`, real ticker goes in `fetch_symbol`.
   - `existing_us_company_id(ticker, existing)` — the read-only counterpart
     for a **second pass** (e.g. tagging) that needs to know which
     `company_id` a ticker is *actually* stored under, not whether
     something needs registering. Don't reuse `resolve_us_company_id` for
     this — it returns `None` for both "already fine" and "already
     disambiguated," which a tagging pass can't tell apart.
3. **One canonical row per company, regardless of index overlap.** Register
   off the *union* of tickers across every index file you're processing,
   not once per file.
4. **Tag membership per index, not once.** A company in both Russell 1000
   and Russell 3000 needs two rows in `company_index_membership` — one
   membership list (e.g. the broadest) isn't sufficient to know finer-tier
   membership.
5. **Tag with provenance**, via `tag_companies_index(conn, company_ids,
   index_name, source=..., retrieved_at=..., effective_from=...)` — passing
   `source` switches it from plain `INSERT OR IGNORE` to an upsert that
   refreshes a still-current row and revives a historical one on re-entry.
   Omit `source` only for bare additive tagging with no provenance need
   (e.g. `scripts/tag_nifty_microcap.py`'s style).
6. **Reconstitution: mark dropped, never delete.** On a re-run, compute
   `dropped = current_tagged_ids - this_runs_company_ids` and call
   `mark_index_membership_historical(conn, dropped, index_name,
   effective_to=today)`. A row's full lifecycle (current → historical →
   current again) stays visible through `status`/`effective_from`/
   `effective_to`, without deleting anything or duplicating rows per
   re-entry.
7. **`--limit`/test runs**: if registration is capped, the *tagging* pass
   must also skip any ticker not actually in `existing` yet — tagging a
   `company_id` that was never inserted violates
   `company_index_membership`'s FK to `companies` (a real bug hit during
   the first `--limit 20` test run). Also skip the drop-detection step
   entirely under `--limit` — a truncated run's company set isn't the real
   universe, so "not in this run" doesn't mean "actually dropped."
8. **Add the index name(s)** to `config/settings.py::INDEX_NAMES` — SQLite
   auto-seeds `index_definitions` from this list on next `init_db()`, but
   **Postgres does not** (`init_postgres_db()` never calls
   `_seed_index_definitions()`). Call `add_index_definition(conn,
   index_name)` explicitly from the registration script itself so it works
   on both backends.
9. **Verify before scaling up**: dry-run (`--dry-run` if the script
   supports it) → small real run (~20 companies) → check collision
   handling, dual-membership (no duplicate `companies` rows), provenance
   populated, no data mutation beyond scope → full run.

---

## 3. Adding a macro data source

Two supported shapes — pick based on how the provider distributes data:

**A. CSV-staged** (RBI/IMD/IITM/MOSPI/IRDA pattern —
`sources/macro.py::MacroDataAdapter`)
1. Land files under `data/raw/_macro/<source_id>/`, one file per series.
2. Required columns: `period,value,unit`, optional `region` (blank =
   national). `period` is `"YYYY"` (annual), `"YYYY-MM"` (monthly), or
   `"YYYY-MM-DD"` (weekly/fortnightly/quarterly/dated — cadence isn't
   inferable from shape alone; the caller/validator is responsible for it).
3. `series_key` is inferred from the filename stem unless overridden — same
   "read the path convention" rule `ingestion/detector.py` uses for
   company files.
4. Add `<source_id>` to `sources/macro.py::MACRO_SOURCE_IDS` and to
   `config/settings.py::DEFAULT_SOURCES` (own row, own `trust_rank` —
   usually `None`, since macro sources today have exactly one provider per
   series, nothing to reconcile against).

**B. Live-fetched** (FRED pattern — `sources/fred.py`)
1. No raw file — the API response *is* the source. Mirror `fred.py`'s
   shape (`fetch()`-style method emitting `MacroNormalizedObservation`
   directly), not `SourceAdapter.parse(file_path, ...)` — there's no file
   to point it at.
2. Still classify each observation's period through
   `sources/macro.py::infer_period_type()` — don't write a parallel period
   parser; reuse the shared one so `ingestion/validation.py`'s
   cross-checks apply uniformly regardless of source.
3. Add a pipeline entry point mirroring `ingestion/pipeline.py
   ::ingest_fred_series()` (sibling to `ingest_yfinance_company()` — a live
   API source gets its own `ingest_*` function, not a branch inside
   `ingest_file()`, which assumes a raw file exists).
4. Register the source_id in `config/settings.py::DEFAULT_SOURCES` and
   `sources/macro.py::MACRO_SOURCE_IDS`, same as the CSV path.

**Either shape**: tag geography per guardrail #3 above — a new macro source
for a new geography needs its applicability (country/region/currency)
identifiable, not just a bare series name.

---

## 4. Adding a reliable financial-filings source

Two supported shapes, mirroring §3:

**A. File-based** (`sources/base.py::SourceAdapter` — Screener, NSE XBRL,
NSE PDF annual reports, proprietary workbooks)
1. Subclass `SourceAdapter`, implement
   `parse(file_path, company_id, **kwargs) -> list[NormalizedObservation]`.
   Set `source_id` to match a row you'll add to
   `config/settings.py::DEFAULT_SOURCES`.
2. Register the class in `ingestion/detector.py::ADAPTER_CLASSES` — files
   are routed to it by path convention
   (`data/raw/<COMPANY>/<source_id>/<file>`); an unregistered `source_id`
   raises `PathConventionError` rather than silently failing later.
3. Emit `NormalizedObservation` rows with real provenance:
   `source`, `source_file`, `source_url`, `parser_version`, `retrieved_at`.
   Set `source_document_id` if this observation traces back to a
   registered Docs-tab document (see `sources/nse_pdf_extractor.py` for
   the only adapter doing this today).

**B. Live-fetched** (`sources/sec_edgar.py`/`sources/yfinance_financials.py`
pattern — no `SourceAdapter` subclass, since `parse(file_path, ...)`
doesn't fit an API call with no file)
1. Expose a `fetch(company_id, cik_or_ticker, **kwargs) ->
   list[NormalizedObservation]` method instead.
2. Persist the raw API response via `storage/raw_object_store.py
   ::store_raw_object()` (hash-deduped, ADR-022 catalog) *before* parsing —
   don't skip straight to parsed rows even for a live source; the raw
   response is what makes a re-processing pass or an audit possible later.
3. Add an `ingest_<source>_company()` function in `ingestion/pipeline.py`
   mirroring `ingest_sec_edgar_company()`'s shape: resolve identifier →
   fetch raw → `store_raw_object()` → parse → (annual-only filter here if
   relevant, output-side) → validate → `insert_financial_observations()` →
   reconcile → publish `DatasetIngestedEvent`.

**Both shapes — set `trust_rank` deliberately.** `trust_rank=0` means
"target source of truth, wins reconciliation and blocks backfill from
weaker sources for the same period" — reserve it for the geography's own
regulator/exchange filing system (NSE/BSE XBRL for India, SEC EDGAR for
US), matching this app's existing "official filing beats every secondary
provider" policy. A licensed/secondary data API (Screener, yfinance) gets a
higher (weaker) `trust_rank`, same tier across geographies unless there's a
specific reason to rank one above another.

---

## Quick-reference: files you'll touch

| Task | Primary files |
|---|---|
| New geography | All four sections below, plus `companies.fiscal_year_end_month` defaults, `financials/ratios.py` vocabulary review |
| New company universe | `companies/us_universe.py` (or an equivalent per-geography resolver), a `scripts/register_*.py` script, `config/settings.py::INDEX_NAMES`, `storage/company_repository.py` + `_pg.py` twin if a new repository function is needed |
| New macro source | `sources/macro.py` or a new live-fetch module beside `sources/fred.py`, `config/settings.py::DEFAULT_SOURCES`, `ingestion/pipeline.py` (live-fetch only) |
| New filing source | A new `sources/*.py` adapter, `ingestion/detector.py::ADAPTER_CLASSES` (file-based only), `ingestion/pipeline.py` (live-fetch only), `config/settings.py::DEFAULT_SOURCES` |

After any of the above: update `docs/architecture.md` (module map + Known
gaps if you closed one) and `docs/FeatureList.md`/`docs/pendingList.md` per
that pair's own rule (`FeatureList.md` = what's actually shipped and
verified; roadmap items that aren't built yet stay in `pendingList.md`,
never both).
