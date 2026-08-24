# Proprietary AnnualReports archive — import status

Tracks progress importing `~/work/AnnualReports/` (53 sector folders, hand-
maintained "Equity Analysis" xlsx workbooks + PDFs) into this app's
`data/raw/<company_id>/proprietary/` and `data/documents/<company_id>/`
conventions. See `scripts/import_banks_sector.py` and
`scripts/import_finance_sector.py` for the completed, per-file mapping and
reasoning (letterhead checks, dedup hashes, entity mergers) — this file is
just the top-level status list and the deferred backlog.

## Done

- **Banks/** — fully imported, nothing deferred. 20 companies received data
  (financials and/or documents). Two items initially held back over entity-
  identity concerns (Equitas Financial vs. Equitas SFB; Ujjivan Financial vs.
  Ujjivan Small Finance Bank) were resolved once the user confirmed both
  pairs are the same company — filed as part of `import_banks_sector.py`.
- **Finance/** — imported except the deferred items below. 30 companies
  touched (29 successfully populated with financials and/or documents).
- **19 tiny sectors** (`import_tiny_sectors.py`) — Bearings, Breweries,
  Cigrettes, Dry Cells, Electrical, Engg, Insurance, Media, Plantations,
  Pumps, Space, Consumer Goods, Health Care Life Science, Sugar, Textile,
  Construction Materials, Consumer Durables, Fertilizers, Glass. All 49
  files triaged; ~20 companies received data.
- **9 large sectors** (`import_large_sectors.py`) — Rating Agency, Cements,
  Hospitality, Realty, Misc, Conglomerate, IT, Tyres, Power (including its
  misfiled "Logistics Transport Courier Shipping" sub-folder — Adani Ports,
  Blue Dart, Great Eastern Shipping, TCI — routed by content, not by the
  folder it was sitting in). ~518 files triaged; ~55 companies touched,
  including CRISIL, CARE Ratings, ICRA, Reliance Industries, Adani
  Enterprises, Vedanta, TCS, Wipro, Google/Alphabet, MRF, CEAT, Apollo
  Tyres, Balkrishna Industries (15 investor presentations alone), NTPC,
  Adani Power, Tata Power, Kolte-Patil (11 straight annual reports), Zomato/
  Eternal and Swiggy (their recent IPO filings). 11 more old-template xlsx
  failures on the same known issue (India Cements, Royal Orchid Hotels,
  Oberoi Realty, HDIL, DB Realty, Adani Enterprise's "-old", 2 of Reliance's
  6 revisions, Sasken, TCS, Suzlon's "-old", Gujarat Industries Power) — all
  now listed in the table below. One cross-company mixup caught: an xlsx
  literally named "MRF 2014.xlsx" turned out byte-identical to one named
  "Ceat - 2014.xlsx" — company-name field blank in both, genuinely
  unresolvable, so neither was filed from that file.
- **16 medium sectors + 2 folded-in small ones** (`import_medium_sectors.py`)
  — Agri, Auto OEM, Auto Suppliers, Ceramics Tiles Granite Quartz, Chemicals,
  Education, Energy, Engines, FMCG, Food processing, Infra, Jewellery,
  Leisure, Minerals Natural Resources, Pharma, Telecom, plus Domestic
  Appliance and Lubricants (7 files each — missed in the initial "tiny"
  pass, added in a follow-up). ~312 files triaged; ~50 companies touched.
  6 xlsx failed on the same older-template issue as Muthoot Finance/Tata
  Investment below (Novartis, NMDC × 2 files, BHEL's "-old.xlsx", Delta
  Corp's "Delta.xlsx") — their PDFs/other xlsx revisions still filed fine.
- **Finance/L&TFinance Holding/** (`import_ltfh.py`) — 68 files, the
  largest of the 7 originally-deferred Finance subfolders. There was no
  technical blocker here — it just hadn't been scoped yet. `LTFINANCEHOLDING`
  is archived with `successor_company_id=LTF` ("L&T Finance", same rename
  pattern as IDFC Bank -> IDFC First Bank), so all 44 filed documents
  (annual reports FY2007-FY2022, investor presentations, quarterly results,
  transcripts) route there, including 5 pre-holding-company subsidiary
  reports from "Subsidary Reports/L&T Finance/". A different subsidiary,
  "Subsidary Reports/L&T Infra/" (infrastructure lending, a separate
  business line), was NOT filed — distinct enough from LTF's own lending
  business to need its own decision rather than folding in by default.

## Deferred — resume later

### Finance/ — remaining excluded subfolders

`L&TFinance Holding/` is done (see above). The rest:

| Folder | Files | Status |
|---|---|---|
| `MFIN/` | 25 | **Done, with a caveat.** Registered as a new macro source: `config/settings.py` DEFAULT_SOURCES gained an `"mfin"` row, `sources/macro.py`'s `MACRO_SOURCE_IDS` now includes it. All 25 PDFs copied to `data/raw/_macro/mfin/` (prefixed `otherpubs__`/`AR__`/`MicroScape__` by their original subfolder, to avoid name collisions and keep provenance visible). **Caveat**: none of this produced `macro_observations` rows — `MacroDataAdapter` only parses `period,value,unit` CSVs, and these are qualitative PDFs with no numeric series to extract, so `ingest_macro_file()` was never run on them. They're archived reference files under a recognized macro source, not queryable structured data. |
| `SRG housing finance/` | 23 | **Ignored** — mix of genuine ARs/results and prospectus/third-party research (SRGHFL); not revisited since being deferred. |
| `Capital First/` | 16 | **Permanently ignored** — user confirmed: it and IDFC merged into IDFC First Bank, skip the folder rather than route it there. |
| `IDFC/` | 2 | **Permanently ignored** — same user decision as Capital First. |
| `HDFC/` | 1 | **Permanently ignored** — user confirmed: HDFC Ltd merged into HDFC Bank, skip rather than route it there (matches the earlier decision to exclude HDFC Ltd's Integrated Report from Banks/HDFC Bank). |
| `IL&FS/` | 4 | **Permanently ignored** — user confirmed skip. |

### xlsx that failed to parse across all sectors so far (technical, not a mapping decision)

Same root cause every time: an older workbook template with no "Company
Name" row before the year-header row, which `sources/proprietary.py`'s
`ProprietaryAdapter` requires. PDFs/other xlsx revisions for these companies
were still filed fine where they existed.

| Sector | Company | File |
|---|---|---|
| Finance | MUTHOOTFIN | `Equity Analysis - Muthoot Finance.xlsx` |
| Finance | TATAINVEST | `Equity Analysis - Tata Investment.xlsx` (only planned item — nothing filed for this company) |
| Medium/Pharma | NOVARTIND | `Equity Analysis - Novartis.xlsx` (only planned item) |
| Medium/Minerals | NMDC | `Equity Analysis - NMDC.xlsx` and `Equity Analysis - NMDC - May2013.xlsx` (the `-2014` revision parsed fine) |
| Medium/Infra | BHEL | `Equity Analysis - BHEL - old.xlsx` (3 other revisions parsed fine) |
| Medium/Leisure | DELTACORP | `Leisure/Delta/Equity Analysis - Delta.xlsx` (5 other revisions parsed fine) |
| Large/Cements | INDIACEM | `Equity Analysis - India Cements.xlsx` (only planned item) |
| Large/Hospitality | ROHLTD | `Royal Orchids Analysis.xlsx` — actually a different failure mode: no "3 - Forecast" tab at all (only planned item) |
| Large/Realty | OBEROIRLTY | `Equity Analysis - Oberoi Realty.xlsx` (only planned item) |
| Large/Realty | HDIL | `Equity Analysis - HDIL.xlsx` (only planned item) |
| Large/Realty | DBREALTY | `Equity Analysis - DB Reality.xlsx` (only planned item) |
| Large/Conglomerate | ADANIENT | `0. old/Equity Analysis - Adani Enterprise - old.xlsx` (current-version file parsed fine) |
| Large/Conglomerate | RELIANCE | `0. old/Equity Analysis - RIL.xlsx` and `0. old/Equity Analysis - RIL - 12Jan.xlsx` (4 other revisions parsed fine) |
| Large/IT | SASKEN | `Equity Analysis - Sasken.xlsx` (only planned item) |
| Large/IT | TCS | `Equity Analysis - TCS.xlsx` (only planned item) |
| Large/Power | SUZLON | `Equity Analysis - Suzlon - old.xlsx` (present at 2 paths, both fail; current-version file parsed fine) |
| Large/Power | GIPCL | `Gujrat/Equity Analysis - Gujrat Industries Power Ltd.xlsx` (only planned item) |

Fixing these needs `ProprietaryAdapter`'s year-header detection extended to
recognize the older layout, not another import-mapping decision.

## Not started

All 53 top-level sector folders have now been triaged (empty ones needed no
action), and L&TFinance Holding is done. What's left is entirely the
**deferred backlog**: Finance/MFIN (pending a "what does macro data mean
here" decision), Finance/SRG housing finance (still just deferred, not yet
revisited), three permanently-ignored Finance folders (Capital First, IDFC,
HDFC — all pre-merger predecessor entities by user decision), one
permanently-ignored folder (IL&FS), and the growing list of old-template
xlsx failures. Resuming means picking one of those, or extending
`ProprietaryAdapter` to handle the older workbook template so the ~15 failed
xlsx across every pass can be retried.

### Scale survey (file counts only — no registry matching or classification yet)

| Folder | Files | xlsx | PDF | Subfolders |
|---|---|---|---|---|
| Power | 98 | 25 | 65 | 13 |
| Tyres | 88 | 18 | 68 | 12 |
| Conglomerate | 66 | 11 | 50 | 12 |
| IT | 65 | 7 | 55 | 12 |
| Hospitality | 54 | 7 | 9 | 4 |
| Misc | 49 | 5 | 42 | 18 |
| Realty | 49 | 5 | 44 | 10 |
| Rating Agency | 34 | 9 | 23 | 6 |
| Cements | 35 | 4 | 27 | 10 |
| Education | 30 | 0 | 30 | 6 |
| Leisure | 29 | 13 | 15 | 10 |
| Retailer | 26 | 0 | 9 | 7 |
| Chemicals | 27 | 14 | 10 | 8 |
| Infra | 24 | 11 | 13 | 8 |
| Engines | 20 | 5 | 14 | 5 |
| Telecom | 19 | 6 | 13 | 7 |
| Pharma | 19 | 1 | 15 | 5 |
| Auto Suppliers | 18 | 7 | 8 | 6 |
| Energy | 17 | 10 | 3 | 5 |
| Ceramics Tiles Granite Quartz | 15 | 5 | 8 | 3 |
| FMCG | 14 | 0 | 12 | 6 |
| Agri | 12 | 7 | 5 | 3 |
| Auto OEM | 12 | 4 | 7 | 3 |
| Food processing | 11 | 5 | 3 | 4 |
| Jewellery | 10 | 5 | 4 | 2 |
| Minerals Natural Resources | 21 | 8 | 9 | 7 |
| Steel | 9 | 4 | 5 | 2 |
| Domestic Appliance | 7 | 2 | 5 | 2 |
| Lubricants | 7 | 3 | 3 | 2 |
| Glass | 6 | 1 | 3 | 1 |
| Construction Materials | 5 | 0 | 5 | 1 |
| Consumer Durables | 5 | 0 | 4 | 6 |
| Fertilizers | 5 | 1 | 4 | 1 |
| Textile | 4 | 2 | 2 | 1 |
| Health Care Life Science | 3 | 3 | 0 | 1 |
| Sugar | 3 | 0 | 3 | 1 |
| Cigrettes | 2 | 0 | 1 | 1 |
| Consumer Goods | 2 | 0 | 1 | 1 |
| Engg | 2 | 2 | 0 | 1 |
| Insurance | 2 | 0 | 1 | 1 |
| Plantations | 2 | 0 | 2 | 1 |
| Space | 2 | 0 | 1 | 1 |
| Bearings | 1 | 0 | 0 | 0 |
| Breweries | 1 | 0 | 1 | 1 |
| Dry Cells | 1 | 0 | 0 | 0 |
| Electrical | 1 | 0 | 1 | 1 |
| Media | 1 | 0 | 1 | 1 |
| Pumps | 1 | 0 | 1 | 1 |
| Construction | 0 | 0 | 0 | 1 (empty) |
| Finance and Services | 0 | 0 | 0 | 0 (empty) |
| Tea estates | 0 | 0 | 0 | 0 (empty) |

**Total: ~934 files across the 51 remaining folders** (vs. 405 in Banks +
Finance combined). Rough shape: 3 folders are empty, ~19 have 10 files or
fewer (quick passes, similar effort to a single mid-sized Banks company),
~16 are medium (11-30 files, roughly one sector-day each based on Banks/
Finance pace), and 9 are large (30-98 files — Power and Tyres in particular
are bigger than Banks was on their own).
