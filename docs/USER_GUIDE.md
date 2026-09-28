# Signals — User Guide

This is a guide for **using Signals**, an Equity AI Research Assistant (US + India
focus), as an analyst — what each feature does and the exact commands to run it.
For how the system is built internally, see [README.md](../README.md).

This app runs as a single Docker image against the same cloud backends in local
dev as in production — **Neon (Postgres)**, **Qdrant Cloud** (semantic search),
**Neo4j Aura** (graph), and **S3** (documents). There is no SQLite mode: nothing
in this guide falls back to a local `data/*.db` file, so every command below talks
to the real shared cloud services. Keep that in mind — a `main.py init` or an
`ingest` run locally lands in the same live database production reads from (see
[Deployment](#deployment-aws-lightsail) below for how that's wired on Lightsail).

---

## One-time setup

**Step 1 — create a `.env` file** at the project root with the cloud credentials
below. `.env` is git-ignored and loaded automatically by every command run inside
the container (via `--env-file`) — you don't need to `export` or `source`
anything yourself:

```
ANTHROPIC_API_KEY=sk-ant-...

DATABASE_BACKEND=postgres
NEON=postgresql://<user>:<password>@<host>.neon.tech/neondb?sslmode=require

VECTOR_STORE_BACKEND=qdrant
QDRANT_URL=https://<your-cluster>.qdrant.io
QDRANT_API_KEY=<your-qdrant-api-key>

GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j+s://<your-instance>.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your-neo4j-password>

DOCUMENT_STORE_BACKEND=s3
S3_BUCKET_NAME=signals-app-documents-862938824222
AWS_REGION=us-east-2
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
```

Get the actual `NEON`/`QDRANT_*`/`NEO4J_*`/`AWS_*` values from whoever manages
this project's cloud accounts (or from your own already-working Lightsail
deployment config) — none of these are placeholders you can invent, and there's
no local substitute for any of them anymore.

**Step 2 — build the Docker image:**

```bash
# --platform linux/amd64 matches the production image (Lightsail runs amd64);
# harmless to include on Apple Silicon too, just slower to build/run under
# emulation than a native arm64 image would be if you drop the flag for
# purely-local use.
docker build --platform linux/amd64 -t signals-app:local .
```

**Step 3 — run the container locally, pointed at those same cloud backends:**

```bash
docker run -d --name signals-app -p 8080:8080 \
  --env-file <(grep -v '^#' .env | grep -v '^$') \
  signals-app:local

curl http://localhost:8080/health   # expect {"status": "ok"}
```

**Step 4 — initialize the database** (safe to re-run — it never deletes existing
data, only adds anything missing: seeds the metric vocabulary, e.g. net profit,
ROA/ROE inputs, GNPA %, and the admin account):

```bash
docker exec signals-app python main.py init
```

**A ready-to-use admin account is seeded automatically** — username `admin`,
password `admin` — so the web viewer (feature 7) is usable with zero signup.
Log in with it to reach admin-gated features (Admin → Import Data, the Ingest
queue, the Usage/cost page) instead of signing up for a new account. There's no
in-app way to change this password today — worth keeping in mind since this
instance now shares the same live database as production.

Every `main.py` subcommand below (`add-company`, `ingest`, `analyze`, `ask`, ...)
is run the same way — prefix it with `docker exec signals-app`, e.g.
`docker exec signals-app python main.py analyze HDFCBANK`. `serve` is the one
exception: it's already running as the container's own gunicorn process (Step 3),
so just open the browser instead of also running `serve` inside the container.

Stop/remove the container when you're done: `docker rm -f signals-app`. Nothing
local-only is destroyed by this — all data lives in Neon/Qdrant/Neo4j/S3, not in
the container.

---

## Features

| # | Feature | Command |
|---|---|---|
| 1 | [Register a company](#1-register-a-company) | `add-company`, `seed-companies` |
| 2 | [List your companies](#2-list-your-companies) | `list-companies` |
| 3 | [Archive / restore a company](#3-archive--restore-a-company) | `archive-company`, `restore-company` |
| 4 | [Ingest financial data](#4-ingest-financial-data) | `ingest` |
| 5 | [Analyze a company](#5-analyze-a-company-text-report) | `analyze` |
| 6 | [Generate charts](#6-generate-charts) | `analyze --charts` |
| 7 | [Browse in your browser](#7-browse-in-your-browser) | `serve` |
| 8 | [Ask the AI research assistant (CLI)](#8-ask-the-ai-research-assistant-cli) | `ask` |
| 9 | [Ask in your browser (chat)](#9-ask-in-your-browser-chat) | `serve` → `/chat` |
| 10 | [Register and ingest a US company](#10-register-and-ingest-a-us-company) | `add-company --country US`, `ingest-yfinance` |
| 11 | [Ingest US macro data (FRED)](#11-ingest-us-macro-data-fred) | `ingest-fred` |
| 12 | [Daily price history (NSE 500)](#12-daily-price-history-nse-500) | `scripts.backfill_price_history`, `scripts.fetch_daily_prices` |

---

### 1. Register a company

Before you can load any data for a company, it needs to exist in the system.

**Register one company:**

```bash
python main.py add-company HDFCBANK \
  --legal-name "HDFC Bank Limited" \
  --display-name "HDFC Bank" \
  --nse-symbol HDFCBANK \
  --bse-code 500180 \
  --sector "Financial Services" \
  --industry "Private Sector Bank"
```

- `company_id` (the first argument, `HDFCBANK` above) is the stable internal ID you'll
  use everywhere else — pick something short and recognizable, usually the NSE symbol
  (or, for a US company, the ticker itself — see [feature 10](#10-register-and-ingest-a-us-company)).
- `--legal-name` and `--display-name` are required. Everything else is optional but
  worth filling in — `--industry` in particular affects which ratios the system will
  compute for this company (e.g. only companies with "Bank" or "NBFC" in their industry
  get bank-specific ratios like GNPA %).
- `--country` (default `IN`) and `--currency` (default `INR`) control which market a
  company belongs to and how its figures are localized/displayed. `--fiscal-year-end-month`
  defaults to 3 (March close) for `--country IN` and 12 (calendar year) for `--country US`
  — pass it explicitly for a company with a different fiscal year end.
- Running this again for the same `company_id` updates the record — it doesn't create
  a duplicate.

**Or register the two seed demo companies (HDFC Bank + ICICI Bank) in one step:**

```bash
python main.py seed-companies
```

---

### 2. List your companies

```bash
python main.py list-companies
```

Add `--include-archived` to also see archived companies.

---

### 3. Archive / restore a company

Use this if a company gets delisted, merged, or you no longer want it appearing in
your active list. Archiving **never deletes any data** — it only flips a status flag,
so nothing needs to be re-ingested if you restore it later.

```bash
python main.py archive-company HDFCBANK --reason merged
python main.py restore-company HDFCBANK
```

Valid `--reason` values: `delisted`, `acquired`, `merged`, `renamed`, `duplicate`, `manual`.

An archived company can't have new data ingested into it until restored.

---

### 4. Ingest financial data

This loads a company's financials from a Screener.in Excel export into the database.
Screener.in only covers Indian listings — for a US company, use `ingest-yfinance`
instead (see [feature 10](#10-register-and-ingest-a-us-company)).

**Step 1 — get the file.** On [screener.in](https://www.screener.in), open the
company page and use **Export to Excel**.

**Step 2 — place it under `data/raw/<COMPANY_ID>/screener/`:**

```
data/raw/HDFCBANK/screener/HDFCBANK.xlsx
```

**Step 3 — ingest it:**

```bash
python main.py ingest data/raw/HDFCBANK/screener/HDFCBANK.xlsx
```

The system infers the company (`HDFCBANK`) and source (`screener`) from the folder
path automatically. You'll see a summary like:

```
Ingested ... (screener): parsed=280 inserted=280 skipped=0 reconciled=270
```

**Useful flags:**

- `--company-id <ID>` — override the company if your folder name doesn't match a
  registered `company_id` exactly (e.g. folder is `JioFinancial` but the registered ID
  is `JIOFIN`).
- `--statement-type consolidated|standalone` (default `consolidated`) — set this to
  match which figures you exported from Screener.
- `--source <source>` — override the detected source; only `screener` exists today.

You can re-ingest the same file (or a refreshed export) any time — nothing gets
overwritten. The old and new figures are both kept, and the system automatically
decides which one is canonical (with the reason recorded).

**A row got skipped — is that a problem?** You'll sometimes see warnings like:

```
No metric_alias for source=screener raw_label='Employee Cost' — skipping row
```

This means that specific line item isn't in the system's metric vocabulary yet. It's
not an error — everything else in the file still gets ingested. If a metric you care
about keeps getting skipped, that's worth flagging so it can be added.

---

### 5. Analyze a company (text report)

Once a company has data ingested, get a report of its trends, growth, and profitability:

```bash
python main.py analyze HDFCBANK
```

This prints:
- Annual trends for Net Profit, Total Assets, Advances, Deposits — each with year-over-year
  growth and a CAGR across the full period
- ROA and ROE for every year with enough data to compute
- Vendor-reported ratios (Gross NPA %, Net NPA %, CASA %, NIM) for the latest year,
  when available

Every figure is tagged `[FACT]` (a reported number) or `[CALCULATION]` (something the
system computed, with its formula and inputs shown).

Add `--statement-type standalone` if you ingested standalone figures and want that
view instead of consolidated (default).

---

### 6. Generate charts

Add `--charts` to the `analyze` command:

```bash
python main.py analyze HDFCBANK --charts
```

This saves PNG chart images to `data/charts/HDFCBANK/` — Net Profit trend, Total
Assets trend, ROA vs ROE, and Advances vs Deposits (whichever of these the company
actually has data for). Open them with any image viewer.

You don't need this flag if you're using the web viewer (feature 7) — charts show up
there automatically.

---

### 7. Browse in your browser

Start the local web viewer:

```bash
python main.py serve
```

Then open **http://127.0.0.1:5000** in a browser. At minimum you'll see:
- A Research home page (see feature 9) and a Companies list
- A page per company with the same report as `analyze`, plus the charts rendered
  inline, plus a toggle to switch between consolidated and standalone

The web app has grown well past this guide's original CLI-first scope since it
was written — it's not read-only any more (an Admin tab can import raw files,
same pipeline as `ingest` below), and there's more to it (Docs/Notes/Watchlist/
Investigations tabs, sign-up/login, an admin-only Usage/cost page) than these 9
features cover. This guide still gets you from zero to a working, ingested
company via the CLI; for the current full picture of what the web app does,
see [architecture.md](architecture.md).

Press `Ctrl+C` in the terminal to stop the server.

Optional flags: `--port 8080` to use a different port, `--host 0.0.0.0` to allow
other devices on your network to connect.

---

### 8. Ask the AI research assistant (CLI)

Ask a free-form research question about one or more companies:

```bash
python main.py ask "What are the key trends in HDFC Bank's profitability over the last 10 years?" \
  --company HDFCBANK
```

**For a peer comparison, repeat `--company`:**

```bash
python main.py ask "Compare HDFC Bank and IDFC First Bank — growth, profitability, and structural differences." \
  --company HDFCBANK --company IDFCFIRSTB
```

The assistant only uses the same retrieved FACT/CALCULATION figures the `analyze`
report is built from — it never invents a number. Its answer will:
- Tag every claim `[FACT]`, `[CALCULATION]`, or `[INFERENCE]`
- Never present an inference as if it were confirmed
- Explicitly say what it *can't* answer if the data doesn't cover it (e.g. it has no
  visibility into net interest margin or asset quality unless those were ingested)

**This feature needs an Anthropic API key** — see the `.env` setup in
[One-time setup](#one-time-setup). If the key isn't set, `ask` will tell you rather
than failing silently.

---

### 9. Ask in your browser (chat)

The same research assistant as feature 8, but as a chat interface instead of one-shot
CLI commands. Start the web viewer (feature 7) and open **http://127.0.0.1:5000/chat**.

- Tick one or more companies in the left-hand picker (one for a deep dive, several for
  a comparison — same idea as repeating `--company` on the CLI), choose consolidated or
  standalone, and type your question.
- Each answer appears with the same `[FACT]` / `[CALCULATION]` / `[INFERENCE]` tagging
  as the CLI, plus the standard trend charts for whichever companies that question was
  about — so the charts update as you ask about different companies.
- Each question is independent (the assistant doesn't remember earlier turns in the
  conversation) — the "chat" is a convenient way to ask several one-shot questions
  in a row, not a multi-turn conversation with memory.

Uses the same `.env` API key as the CLI — no separate setup.

---

### 10. Register and ingest a US company

Screener.in (feature 4) only covers Indian listings — for a US company, register it
with `--country US` and pull its financials live from Yahoo Finance instead of an
uploaded file:

```bash
python main.py add-company AAPL \
  --legal-name "Apple Inc." \
  --display-name "Apple" \
  --country US \
  --currency USD

python main.py ingest-yfinance AAPL AAPL
```

- The first `AAPL` is the `company_id`; the second is the Yahoo Finance ticker — they're
  often the same for a US company, but don't have to be.
- `--fiscal-year-end-month` wasn't passed above, so it defaulted to 12 (calendar year)
  because `--country US` was set (see [feature 1](#1-register-a-company)).
- `ingest-yfinance` fetches annual income statement, balance sheet, and cash flow data
  live — no file to download or place under `data/raw/`. Re-running it refreshes the
  figures the same "nothing overwritten, reconciliation decides what's canonical" way
  `ingest` does for a Screener file.
- Once ingested, `analyze AAPL`, `ask ... --company AAPL`, and the web viewer all work
  exactly the same as for an Indian company — figures display in USD millions
  automatically (driven by `--currency`).

---

### 11. Ingest US macro data (FRED)

The US counterpart to India's RBI/IMD/IITM macro data (rainfall, repo rate, ...) — the
Fed funds rate, Treasury yields, CPI, unemployment, GDP, and other economy-wide
indicators from FRED (Federal Reserve Economic Data), live-fetched, no file to download:

```bash
python main.py ingest-fred FEDFUNDS --unit PERCENT
python main.py ingest-fred CPIAUCSL --unit INDEX
python main.py ingest-fred UNRATE --unit PERCENT
```

- The first argument is the FRED series ID (visible in the series' URL on
  [fred.stlouisfed.org](https://fred.stlouisfed.org)).
- `--unit` is required — FRED's own export has no unit column, so you supply it (e.g.
  `PERCENT` for a rate, `INDEX` for CPI).
- Once ingested, a macro/regulatory question through `ask`, `/research/ask`, or `/chat`
  can draw on this series alongside India's RBI/IITM data — each is attributed to
  `"USA"` or `"INDIA"` in the evidence the assistant cites, so nothing gets conflated
  across countries.

**Any FRED series works** — `ingest-fred` isn't limited to the three examples above;
pass any series ID visible in a series' URL on
[fred.stlouisfed.org](https://fred.stlouisfed.org) (with the matching `--unit`).

**Batch job (starter set):** `scripts/batch_fetch_fred.py` loops over a small,
curated list of broad US indicators relevant regardless of which company/sector is
under review — useful for a first-time pull or a scheduled refresh instead of
ingesting series one at a time:

| Series ID | Meaning | Unit |
|---|---|---|
| `FEDFUNDS` | Federal funds rate | PERCENT |
| `DGS10` | 10-Year Treasury yield | PERCENT |
| `CPIAUCSL` | CPI (inflation) | INDEX |
| `UNRATE` | Unemployment rate | PERCENT |
| `GDP` | US GDP | USD_BILLION |

```bash
python -m scripts.batch_fetch_fred                    # every series above
python -m scripts.batch_fetch_fred --series FEDFUNDS,DGS10   # just these two
```

This list (`TRACKED_SERIES` in that script) is a plain Python list, not a database
table — add a series by adding one `FredSeries(...)` entry; each is independent, so
adding one never touches the others. This is also the job the Settings > Data
Operations > Schedule panel's "FRED macro data" row runs.

---

### 12. Daily price history (NSE 500)

Daily OHLCV (open/high/low/close/volume) price data for every Nifty 500 company,
fetched from Yahoo Finance — separate from `analyze`'s fundamentals, this is for
price charting. Not yet a `main.py` subcommand — run the scripts directly (as
modules, so their `storage`/`sources` imports resolve):

`daily_prices` lives in the same Neon Postgres database as everything else,
via `storage/price_repository_pg.py` and `storage/backend_bootstrap.py`'s
`open_price_db()` (ADR-021) — same `DATABASE_BACKEND=postgres` setup as the
rest of this guide, no separate local file.

**One-time (or occasional) backfill:**

```bash
docker exec signals-app python -m scripts.backfill_price_history --period 1y
```

Loops over every company tagged `Nifty 500` (already populated by `add-company`/
`seed-companies` or a full NSE import — see `companies/nse_import.py`) and upserts
its history into `daily_prices`. `--period` accepts `1y` (default), `5y`, `10y`, or
`max`; `--years N` backfills an exact N-year window (e.g. `--years 3`, for a
window `--period` has no name for); `--index-name "Nifty 50"` scopes to one NSE
tier (`--all-tiers` runs all five standard tiers back to back); add
`--company-id HDFCBANK` to backfill just one company; `--force` bypasses the
skip-if-already-covers-the-requested-window check. Safe to re-run any time
(e.g. after switching from `1y` to `10y`) — existing days are overwritten in
place, never duplicated, and (for `--years` runs) a company already covered
back to the requested start date is skipped outright rather than re-fetched.
The same six tier/country combinations are also available as one-click jobs
in Settings > Data Operations > Schedule ("History price" category). Expect
~10-15 minutes for the full 499-company run (deliberately rate-limited to
stay polite to Yahoo's endpoint); the Nifty Micro-Cap tier (~2,000 companies)
takes considerably longer.

**Scheduled runs target 20 years (or since listing, if shorter), pulled
incrementally:** the six EventBridge-triggered "History price" jobs (see the
Automated schedule table below) each pass `years=20` plus a per-tier
`time_budget_seconds` sized to that tier's own Saturday-morning slot
(`scheduling/jobs.py`'s `HISTORY_TIER_TIME_BUDGET_MINUTES`) — a single run
works backwards from whatever's already on file, stops once its time budget
is spent, and simply leaves the still-missing older days alone. Because
coverage is a real, persisted fact (existing `daily_prices` rows — no
separate checkpoint table), next week's run picks up exactly where this
week's left off and pushes the covered window further back, until the tier
reaches the full 20 years (or the company's listing date, whichever is
sooner), at which point each run goes back to being a fast no-op. A manual
"Run now" click, or `--years N` from the CLI directly, is unaffected — it
uses the exact window you pass and has no time budget unless you add
`--time-budget-minutes` yourself.

**Daily job (keeps it current):**

```bash
docker exec signals-app python -m scripts.fetch_daily_prices
```

Upserts the last 5 trading days for every company (not just "today") so a missed
run — a skipped weekend, the job not running for a few days — self-heals on the
next run instead of leaving a gap. Point your OS's scheduler (cron, Task
Scheduler, ...) at this to run it once daily after market close; this guide
doesn't set that up for you.

**Reading it back:** `GET /companies/<company_id>/price-feed.json?period=1y` (via
the running container, feature 7) returns `{"dates": [...], "open": [...],
"high": [...], "low": [...], "close": [...], "volume": [...]}` for that company.
There's no chart panel rendering this in the browser yet — today this is a raw
JSON feed only.

### 13. Database sharding (git storage) — not applicable

This section described sharding `data/equity_research.db` (a git-tracked SQLite
file split into ≤50MB parts to stay under GitHub's 100MB limit) for the old
local-SQLite dev workflow. It doesn't apply here: the database is Neon
(Postgres), never a local file, so there's nothing to shard or commit. Kept
below only in case an older checkout still has git-tracked `data/db_shards/`
parts to clean up; skip it otherwise.

**Reshard (legacy, SQLite-only):**

```bash
python -m scripts.db_shard
```

Snapshots the live db via SQLite's own online backup API and rewrites
`data/db_shards/` from scratch. Safe to run even while the app or an
ingestion job is writing to the db concurrently — it never locks or copies
the file at the OS level. Prints the part count and a checksum when done.
Add `--chunk-mb 40` to use a smaller part size than the 49MB default.

**Commit and push the new shards (a separate, deliberate step — not
something to fold into a schedule without deciding this explicitly first):**

```bash
git add data/db_shards/
git commit -m "Reshard: <what changed, e.g. 'Nifty 50 batch 2 ingested'>"
git push
```

This is the part worth pausing on: resharding just writes local files, but
committing and pushing sends whatever's currently in the live db to a
shared remote, unattended if scheduled. Confirm the remote/branch and
that unattended pushes are actually wanted before wiring this into cron —
running the reshard step alone, on its own, is harmless and can be
scheduled freely.

**Reassemble (after a fresh clone, or pulling someone else's reshard):**

```bash
python scripts/db_unshard.py
```

Refuses to overwrite an existing `data/equity_research.db` unless you pass
`--force` — this script can't tell "no db yet" apart from "db already open
by a running app" on its own, so it just declines rather than guessing.
Verifies the reassembled file's checksum against `data/db_shards/checksum.sha256`
before finishing.

**Staleness check:** as of this writing, `data/db_shards/` on disk matches
git's last committed shard exactly (`git status --short data/db_shards/`
shows nothing) — meaning it predates a fair amount of ingestion work done
since. Run the reshard command above before the next commit if you want
what's in git to reflect what's actually in the live db.

---

## A typical workflow, start to finish

```bash
# First time only (see One-time setup above): build the image, run the
# container against Neon/Qdrant/Neo4j/S3, then initialize the database.
docker build --platform linux/amd64 -t signals-app:local .
docker run -d --name signals-app -p 8080:8080 \
  --env-file <(grep -v '^#' .env | grep -v '^$') signals-app:local
docker exec signals-app python main.py init

# 1. Register the company
docker exec signals-app python main.py add-company HDFCBANK --legal-name "HDFC Bank Limited" \
  --display-name "HDFC Bank" --sector "Financial Services" --industry "Private Sector Bank"

# 2. Drop the Screener export at data/raw/HDFCBANK/screener/HDFCBANK.xlsx
#    inside the container (docker cp it in, or bind-mount data/raw/), then:
docker exec signals-app python main.py ingest data/raw/HDFCBANK/screener/HDFCBANK.xlsx

# 3. Read the report
docker exec signals-app python main.py analyze HDFCBANK --charts

# 4. Browse it with charts inline — the container is already serving on 8080
# → open http://localhost:8080/companies/HDFCBANK
# → or http://localhost:8080/chat to ask questions in the browser instead

# 5. Ask a research question from the CLI (needs ANTHROPIC_API_KEY in .env — see One-time setup)
docker exec signals-app python main.py ask "What stands out about HDFC Bank's last 10 years?" --company HDFCBANK
```

---

## Tips & troubleshooting

- **`No company registered with company_id=...`** — you need to run `add-company` (or
  `seed-companies`) before `ingest`, `analyze`, or `ask` will work for that company.
- **A metric never shows up in the report** — it may not have been ingested at all.
  Check the `ingest` output for `No metric_alias` warnings, or the fact may genuinely
  not exist in the source file (real Screener exports vary — a bank's file won't have
  the same line items as an NBFC's or a manufacturer's).
- **`ROA`/`ROE` missing for the earliest year** — these need the *prior* year's balance
  sheet figures to compute an average, so the first year in your data never has them.
- **Consolidated vs. standalone** — pick whichever matches what you exported from
  Screener, and use the same one consistently across `ingest`, `analyze`, and `ask` for
  a given company, or the report/assistant will report "no data" for the other view.
- **Nothing shows in `analyze` or `ask` after ingesting** — double check the
  `--statement-type` you ingested with matches the one you're viewing/asking with.

---

## Deployment (AWS Lightsail)

The app runs as a single container on AWS Lightsail Container Service
(`signals-app`, `us-east-2`), backed by Neon (Postgres), S3 (documents),
and Qdrant Cloud. Local SQLite/`data/` never ships in the image — see
`.dockerignore`.

### 1. Build the Docker image

Same build as [One-time setup](#one-time-setup) above — if you already built
and smoke-tested `signals-app:local` there against the real Neon/Qdrant/Neo4j/S3
backends (same `.env`, same `DATABASE_BACKEND=postgres`/`DOCUMENT_STORE_BACKEND=s3`),
that already is the pre-push verification; just re-tag it for the push step below:

```bash
# --platform linux/amd64 is required even on Apple Silicon -- Lightsail
# runs amd64, and Docker defaults to your host's architecture (arm64)
# otherwise, producing an image that won't start there.
docker build --platform linux/amd64 -t signals-app:pg-s3 .
docker run -d --name signals-test -p 8081:8080 \
  --env-file <(grep -v '^#' .env | grep -v '^$') \
  signals-app:pg-s3

curl http://localhost:8081/health   # expect {"status": "ok"}
docker rm -f signals-test           # once you're satisfied
```

### 2. Push the image and deploy to Lightsail

```bash
# Needs the lightsailctl plugin (aws lightsail push-container-image errors
# with a download link the first time if it's missing).
aws lightsail push-container-image \
  --service-name signals-app \
  --label signals-app \
  --image signals-app:pg-s3
# → prints the registered image reference, e.g. ":signals-app.signals-app.N"
# -- use that exact string (N increments every push) in containers.json below.

# containers.json's "image" field must be the ":signals-app.signals-app.N"
# reference from the push output above, and its "environment" object should
# carry forward every existing env var (see `aws lightsail get-container-
# services --service-name signals-app` to read the current ones back) plus:
#   DATABASE_BACKEND=postgres
#   DOCUMENT_STORE_BACKEND=s3
#   S3_BUCKET_NAME=signals-app-documents-862938824222
#   AWS_REGION=us-east-2
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (the signals-app-s3 IAM user's key)
# public-endpoint.json is the health-check config — reuse the same shape
# `get-container-services` already shows under currentDeployment.publicEndpoint.
aws lightsail create-container-service-deployment \
  --service-name signals-app \
  --containers file://containers.json \
  --public-endpoint file://public-endpoint.json

# Poll until state flips from DEPLOYING to RUNNING, then verify:
aws lightsail get-container-services --service-name signals-app --query 'containerServices[0].state'
curl https://signals-app.wmmbnsx82cwgc.us-east-2.cs.amazonlightsail.com/health
```

---

## Automated schedule (EventBridge, production)

As of 2026-09-13, every scheduled job below is wired to a real automated
trigger — an AWS EventBridge Connection (`signals-app-cron-trigger`, holds
the shared `X-Cron-Secret` header auth), one API Destination per job
(`https://signals-app.../admin/schedule/run-async/<job_id>`), and one
classic EventBridge Rule per job (`signals-app-<name>`, cron schedule,
targets the API destination via a scoped IAM role,
`signals-app-events-invoke-role`). Nothing here runs inside this app's own
process — it's all external AWS infrastructure calling the async trigger
route, same as `web/app.py`'s `admin_schedule_run_async()` docstring
describes. `SCHEDULED_JOBS.md`/`scheduling/jobs.py` remain the source of
truth for what each job actually does; this table is just when it fires.

**Known limitation — Daylight Saving Time**: every rule below uses a fixed
UTC cron expression (classic EventBridge Rules don't support timezones —
EventBridge *Scheduler* does, but doesn't support API Destination targets,
which is why Rules were used instead). All times are correct as written
for **EDT** (UTC-4, roughly mid-March to early November). Once DST ends,
every rule's hour shifts one hour early in ET terms and needs a manual
`+1 hour` UTC adjustment (e.g. `aws events put-rule --name <rule> --schedule-expression "cron(<min> <hour+1> ...)"` for each). Re-adjust back by `-1 hour` the following March.

| Category | Job | Cadence | Trigger (ET) | EventBridge rule |
|---|---|---|---|---|
| Daily price | India — close price & volume (Nifty 500) | Daily | Weekdays midnight | `signals-app-price-history-india-daily` |
| Daily price | India — close price & volume (Nifty Micro-Cap) | Monthly | 1st Sat, 4:00am | `signals-app-daily-price-india-microcap` |
| Daily price | USA — close price & volume | Weekly | Weekdays midnight | `signals-app-price-history-usa-daily` |
| History price | Nifty 50, 20y incremental | Manual→Weekly | Sat 7:00am | `signals-app-history-price-nifty50` |
| History price | Nifty Next 50, 20y incremental | Manual→Weekly | Sat 7:15am | `signals-app-history-price-next50` |
| History price | Nifty Midcap 150, 20y incremental | Manual→Weekly | Sat 7:30am | `signals-app-history-price-midcap150` |
| History price | Nifty Smallcap 250, 20y incremental | Manual→Weekly | Sat 8:00am | `signals-app-history-price-smallcap250` |
| History price | Nifty Micro-Cap, 20y incremental | Manual→Monthly | 1st Sat, 8:30am | `signals-app-history-price-microcap` |
| History price | USA, 20y incremental | Manual→Weekly | Sat 7:00am | `signals-app-history-price-usa` |
| Financials | Nifty 50 | Quarterly→Weekly | Sat 12:00pm | `signals-app-financials-nifty50` |
| Financials | Nifty Next 50 | Quarterly→Weekly | Sat 12:15pm | `signals-app-financials-next50` |
| Financials | Nifty Midcap 150 | Quarterly→Weekly | Sat 12:30pm | `signals-app-financials-midcap150` |
| Financials | Nifty Smallcap 250 | Quarterly→Weekly | Sat 12:50pm | `signals-app-financials-smallcap250` |
| Financials | Nifty Micro-Cap | Monthly | 1st Sat, 1:10pm | `signals-app-financials-microcap` |
| Financials | USA (SEC EDGAR) | Quarterly→Weekly | Sat 1:40pm | `signals-app-financials-usa` |
| Shareholding | Nifty 50 | Quarterly→Weekly | Sat 2:00pm | `signals-app-shareholding-nifty50` |
| Shareholding | Nifty Next 50 | Quarterly→Weekly | Sat 2:15pm | `signals-app-shareholding-next50` |
| Shareholding | Nifty Midcap 150 | Quarterly→Weekly | Sat 2:30pm | `signals-app-shareholding-midcap150` |
| Shareholding | Nifty Smallcap 250 | Quarterly→Weekly | Sat 2:50pm | `signals-app-shareholding-smallcap250` |
| Shareholding | Nifty Micro-Cap | Monthly | 1st Sat, 3:10pm | `signals-app-shareholding-microcap` |
| Corporate actions | Fetch — Nifty 50 | Quarterly→Weekly | Sat 3:30pm | `signals-app-corp-actions-fetch-nifty50` |
| Corporate actions | Ingest — Nifty 50 | Quarterly→Weekly | Sat 3:40pm | `signals-app-corp-actions-ingest-nifty50` |
| Corporate actions | Fetch — Nifty Next 50 | Quarterly→Weekly | Sat 3:50pm | `signals-app-corp-actions-fetch-next50` |
| Corporate actions | Ingest — Nifty Next 50 | Quarterly→Weekly | Sat 4:00pm | `signals-app-corp-actions-ingest-next50` |
| Corporate actions | Fetch — Nifty Midcap 150 | Quarterly→Weekly | Sat 4:10pm | `signals-app-corp-actions-fetch-midcap150` |
| Corporate actions | Ingest — Nifty Midcap 150 | Quarterly→Weekly | Sat 4:20pm | `signals-app-corp-actions-ingest-midcap150` |
| Corporate actions | Fetch — Nifty Smallcap 250 | Quarterly→Weekly | Sat 4:30pm | `signals-app-corp-actions-fetch-smallcap250` |
| Corporate actions | Ingest — Nifty Smallcap 250 | Quarterly→Weekly | Sat 4:40pm | `signals-app-corp-actions-ingest-smallcap250` |
| Corporate actions | Fetch — Nifty Micro-Cap | Monthly | 1st Sat, 4:50pm | `signals-app-corp-actions-fetch-microcap` |
| Corporate actions | Ingest — Nifty Micro-Cap | Monthly | 1st Sat, 5:00pm | `signals-app-corp-actions-ingest-microcap` |
| Macro | FRED macro data | Quarterly→Monthly | Last Sat of month, 6:00pm | `signals-app-fred-macro-monthly` |
| Macro | RBI / IITM macro data | Weekly | — (disabled, no runner) | — |
| Macro | Macro insights | Monthly | — (disabled, no runner) | — |
| Insights | Company insights | Monthly | — (on hold) | — |
| Documents | Document analysis | Quarterly | — (on hold) | — |
| Documents | Investor relations documents | Quarterly | — (on hold) | — |
| Maintenance | DB sharding | Daily | — (on hold) | — |
| Maintenance | Raw object catalog reconciliation | Weekly | Sun 5:00am | `signals-app-raw-object-reconciliation-weekly` |
| Maintenance | Research thread reconciliation (S3 <-> Postgres) | Weekly | Sun 5:15am | `signals-app-generated-report-reconciliation-weekly` |

31 of 39 registry jobs are automated (5 of those monthly instead of their
declared weekly/quarterly cadence, per an explicit operator decision to
keep NSE/SEC EDGAR load down for the largest tier). 4 are deliberately on
hold (Insights, Documents×2, DB sharding — not yet wanted on autopilot). 2
remain disabled at the code level (no runner implemented — see
`scheduling/jobs.py`'s own `reason` field for each). Every Saturday
job is staggered by 10-40 minutes from its neighbors specifically to avoid
firing 20+ concurrent NSE/SEC-EDGAR-hitting jobs at once — see each rule's
own trigger time above before adding a new one to this block, and don't
schedule a new heavy job into the same slot as an existing one without
checking for overlap.

---

## Related documentation

- **[README.md](../README.md)** — the original design proposal and scoping
  rationale: why the system is shaped the way it is.
- **[architecture.md](architecture.md)** — the current, accurate technical
  picture of what's actually built.
- **[FeatureList.md](FeatureList.md)** — what's shipped vs. still open.
