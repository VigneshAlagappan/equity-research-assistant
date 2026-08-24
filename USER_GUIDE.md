# User Guide

This is a guide for **using** the Indian Equity Research Assistant as an analyst —
what each feature does and the exact commands to run it. For how the system is
built internally, see [README.md](README.md).

All commands are run from the project root, with the virtual environment active:

```bash
source .venv/bin/activate
```

---

## One-time setup

Before using any feature, initialize the database once:

```bash
python main.py init
```

This creates the SQLite database, the folders under `data/`, and seeds the metric
vocabulary (net profit, ROA/ROE inputs, GNPA %, etc.). Safe to re-run — it never
deletes existing data.

**If you plan to use the AI research assistant** (`ask`, or the chat page in the web
viewer — features 8 and 9 below), create a `.env` file at the project root with your
Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is git-ignored and loaded automatically by every `python main.py ...` command —
you don't need to `export` or `source` anything yourself.

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
  use everywhere else — pick something short and recognizable, usually the NSE symbol.
- `--legal-name` and `--display-name` are required. Everything else is optional but
  worth filling in — `--industry` in particular affects which ratios the system will
  compute for this company (e.g. only companies with "Bank" or "NBFC" in their industry
  get bank-specific ratios like GNPA %).
- Running this again for the same `company_id` updates the record — it doesn't create
  a duplicate.

**Or register the two POC demo companies (HDFC Bank + ICICI Bank) in one step:**

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

## A typical workflow, start to finish

```bash
source .venv/bin/activate
python main.py init

# 1. Register the company
python main.py add-company HDFCBANK --legal-name "HDFC Bank Limited" \
  --display-name "HDFC Bank" --sector "Financial Services" --industry "Private Sector Bank"

# 2. Drop the Screener export at data/raw/HDFCBANK/screener/HDFCBANK.xlsx, then:
python main.py ingest data/raw/HDFCBANK/screener/HDFCBANK.xlsx

# 3. Read the report
python main.py analyze HDFCBANK --charts

# 4. Browse it with charts inline
python main.py serve
# → open http://127.0.0.1:5000/companies/HDFCBANK
# → or http://127.0.0.1:5000/chat to ask questions in the browser instead

# 5. Ask a research question from the CLI (needs ANTHROPIC_API_KEY in .env — see One-time setup)
python main.py ask "What stands out about HDFC Bank's last 10 years?" --company HDFCBANK
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
