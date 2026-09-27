"""One-time (and periodically re-runnable) backfill: establish the initial
U.S. company universe for Signals — Russell 3000, with Russell 1000/Russell
2000/S&P 500 as overlapping classifications on the same canonical company
rows (see docs/pendingList.md's now-closed Russell-3000 roadmap item / the
"Signals — U.S. Company Universe" architecture directive this implements).

Membership source: FTSE Russell doesn't publish free official constituent
lists, so this uses iShares' own daily holdings CSVs for the 4 ETFs that
track these indices as a sufficiently-reliable free proxy, each carrying an
"As Of" date usable as `effective_from`:
  - IWV -> Russell 3000
  - IWB -> Russell 1000
  - IWM -> Russell 2000
  - IVV -> S&P 500

Design (see the approved plan for full reasoning):
  - Registration (creating a `companies` row) happens once per ticker, off
    the UNION of all 4 files' tickers — one canonical row per company
    regardless of how many indices it belongs to.
  - Tagging happens from EACH file independently (not just IWV), since IWV
    alone can't tell you whether a company is specifically Russell 1000 vs
    Russell 2000, and S&P 500 (from IVV) is a fully independent
    classification, never derived from Russell membership.
  - Re-running this later (reconstitution) refreshes still-current rows
    (new retrieved_at/effective_from) via tag_companies_index()'s
    provenance-aware upsert, and flips companies that dropped out of an
    index to status='historical' (effective_to=today) via
    mark_index_membership_historical() — never deletes a membership row,
    so a company's Russell-tier history survives reconstitution.
  - Financial/pricing history is NOT fetched here — run
    `scripts/batch_fetch_sec_edgar.py --index "Russell 3000" --annual-only
    --years N` and `scripts/backfill_price_history_usa.py --start
    2015-01-01` separately, after this script's full run is verified.

Reuses companies/us_universe.py's collision-resolution helpers (same ones
scripts/register_sp500_companies.py uses) so a US ticker that collides with
an existing non-US company_id (e.g. "PNC") is disambiguated the same way
everywhere in this app.

Usage:
  python -m scripts.register_russell3000_companies [--csv-dir PATH]
      [--indexes IWV,IWB,IWM,IVV] [--limit N] [--dry-run]

--csv-dir points at a directory of pre-downloaded iShares CSVs (named
<TICKER>_holdings.csv, e.g. IWV_holdings.csv) for offline/cached runs;
without it, this downloads fresh copies from iShares' public product pages.
"""

from __future__ import annotations

import argparse
import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yfinance as yf

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from companies.registry import register_company
from companies.us_universe import (
    existing_us_company_id,
    fiscal_year_end_month_from_yfinance_info,
    normalize_us_ticker,
    resolve_us_company_id,
)
from storage.backend_bootstrap import open_db
from storage.company_repository import (
    mark_index_membership_historical,
    select_company_ids_by_index,
    tag_companies_index,
)
from storage.repositories import add_index_definition

REQUEST_DELAY_SECONDS = 0.5

# iShares' public per-fund holdings CSV download endpoint (no auth). Verified
# stable format: a few header lines including "Fund Holdings as of,<date>",
# then a holdings table with (among others) "Ticker","Name","Sector" columns,
# then trailing disclaimer lines after a blank row.
_ISHARES_CSV_URLS = {
    "IWV": "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund",
    "IWB": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    "IWM": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    "IVV": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
}

_INDEX_NAME_BY_ETF = {
    "IWV": "Russell 3000",
    "IWB": "Russell 1000",
    "IWM": "Russell 2000",
    "IVV": "S&P 500",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SignalsResearchBot/1.0)"}


class HoldingsFile:
    def __init__(self, index_name: str, source: str, as_of: str, rows: list[dict]):
        self.index_name = index_name
        self.source = source
        self.as_of = as_of
        self.rows = rows  # each: {"ticker": ..., "name": ..., "sector": ...}


def _fetch_csv_text(etf_ticker: str, csv_dir: Path | None) -> str:
    if csv_dir is not None:
        path = csv_dir / f"{etf_ticker}_holdings.csv"
        return path.read_text(encoding="utf-8-sig")
    resp = requests.get(_ISHARES_CSV_URLS[etf_ticker], headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def _parse_holdings_csv(etf_ticker: str, text: str) -> HoldingsFile:
    """iShares' CSV has a few metadata lines, then the real header row
    ("Ticker","Name","Sector",...), then holdings, then a blank line and
    disclaimer text. Find the header row by scanning for "Ticker" as the
    first cell rather than assuming a fixed line number, since iShares has
    changed the number of leading metadata lines before without warning."""
    lines = text.splitlines()
    as_of = None
    header_idx = None
    for i, line in enumerate(lines):
        cells = next(csv.reader([line]), [])
        if not cells:
            continue
        if as_of is None and cells[0].strip().lower() == "fund holdings as of" and len(cells) > 1:
            as_of = cells[1].strip()
        if cells[0].strip() == "Ticker":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"{etf_ticker}: couldn't find a 'Ticker' header row in the downloaded CSV")
    if as_of is None:
        raise ValueError(f"{etf_ticker}: couldn't find a 'Fund Holdings as of' date in the downloaded CSV")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    rows = []
    for row in reader:
        ticker = (row.get("Ticker") or "").strip()
        name = (row.get("Name") or "").strip()
        if not ticker or not name:
            # Blank line / disclaimer text after the holdings table, or a
            # non-equity line item (cash, futures) with no real ticker.
            continue
        rows.append({"ticker": ticker, "name": name, "sector": (row.get("Sector") or "").strip() or None})
    return HoldingsFile(_INDEX_NAME_BY_ETF[etf_ticker], f"ishares_{etf_ticker.lower()}", as_of, rows)


# Row shape of a pre-consolidated "one row per company, one boolean column
# per index" workbook (an alternative to 4 separate per-ETF holdings CSVs --
# e.g. Master sheet: Company, Ticker, Sector, Exchange, S&P 500, Russell
# 1000, Russell 2000, Russell 3000, Index Tags, Holdings Date, Source /
# Quality). Each boolean index column becomes its own HoldingsFile, same as
# the per-ETF CSV path, so the rest of main() doesn't need to know which
# input format it came from.
_XLSX_INDEX_COLUMNS = {"S&P 500": "S&P 500", "Russell 1000": "Russell 1000", "Russell 2000": "Russell 2000", "Russell 3000": "Russell 3000"}


def _load_holdings_from_consolidated_xlsx(path: Path, sheet: str, indexes: list[str]) -> list[HoldingsFile]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    all_rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in all_rows[0]]
    col = {name: header.index(name) for name in ["Company", "Ticker", "Sector", "Exchange", "Holdings Date"] if name in header}
    missing_required = {"Company", "Ticker"} - col.keys()
    if missing_required:
        raise ValueError(f"{path}: sheet {sheet!r} is missing required column(s) {sorted(missing_required)}")

    by_index: dict[str, list[dict]] = {name: [] for name in indexes}
    as_of_by_index: dict[str, str | None] = {name: None for name in indexes}
    skipped_unlisted = 0
    for row in all_rows[1:]:
        ticker = (row[col["Ticker"]] or "").strip() if row[col["Ticker"]] else ""
        name = (row[col["Company"]] or "").strip() if row[col["Company"]] else ""
        if not ticker or not name:
            continue
        exchange = str(row[col["Exchange"]]).strip().upper() if "Exchange" in col and row[col["Exchange"]] else ""
        if "NO MARKET" in exchange or "UNLISTED" in exchange:
            # Delisted/unlisted artifacts (e.g. a merger CVR security) --
            # not a real tradeable US company, nothing to register against.
            skipped_unlisted += 1
            continue
        sector = (row[col["Sector"]] or "").strip() if "Sector" in col and row[col["Sector"]] else None
        holdings_date = str(row[col["Holdings Date"]]).strip() if "Holdings Date" in col and row[col["Holdings Date"]] else None
        for index_name in indexes:
            index_col = header.index(_XLSX_INDEX_COLUMNS[index_name]) if _XLSX_INDEX_COLUMNS[index_name] in header else None
            if index_col is None or not row[index_col]:
                continue
            by_index[index_name].append({"ticker": ticker, "name": name, "sector": sector})
            if as_of_by_index[index_name] is None and holdings_date:
                as_of_by_index[index_name] = holdings_date

    if skipped_unlisted:
        print(f"Skipped {skipped_unlisted} unlisted/no-market rows in {path.name}", flush=True)

    source = f"xlsx:{path.name}"
    return [
        HoldingsFile(index_name, source, as_of_by_index[index_name] or "unknown", by_index[index_name])
        for index_name in indexes
        if by_index[index_name]
    ]


def _existing_companies(conn) -> dict[str, str]:
    """company_id -> country for every company already on file. Plain
    cursor() + execute() (no `with cur:`) works against both backends --
    sqlite3.Cursor doesn't support the context-manager protocol at all,
    while psycopg2's does but doesn't need it here (nothing after this
    needs the cursor released early; conn.close() cleans it up)."""
    cur = conn.cursor()
    cur.execute("SELECT company_id, country FROM companies")
    return {row["company_id"]: row["country"] for row in cur.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-dir", type=Path, default=None, help="Pre-downloaded <TICKER>_holdings.csv directory")
    parser.add_argument("--indexes", default="IWV,IWB,IWM,IVV", help="Comma-separated iShares ETF tickers to process")
    parser.add_argument("--xlsx", type=Path, default=None,
                         help="A pre-consolidated workbook (Company/Ticker/Sector/Exchange + one boolean column "
                              "per index, e.g. 'Russell 1000') instead of per-ETF CSVs -- takes priority over --csv-dir")
    parser.add_argument("--xlsx-sheet", default="Master", help="Sheet name to read within --xlsx (default: Master)")
    parser.add_argument("--limit", type=int, default=None, help="Only register the first N missing companies (testing)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned inserts/tags, write nothing")
    args = parser.parse_args()

    if args.xlsx is not None:
        requested_indexes = [_INDEX_NAME_BY_ETF[t.strip().upper()] for t in args.indexes.split(",") if t.strip()]
        holdings_files = _load_holdings_from_consolidated_xlsx(args.xlsx, args.xlsx_sheet, requested_indexes)
        for holdings in holdings_files:
            print(f"{holdings.index_name}: {len(holdings.rows)} holdings, as of {holdings.as_of} (source={holdings.source})", flush=True)
        skipped_indexes = set(requested_indexes) - {h.index_name for h in holdings_files}
        if skipped_indexes:
            print(f"No holdings found for: {sorted(skipped_indexes)} (column missing or entirely blank in the workbook)", flush=True)
    else:
        etf_tickers = [t.strip().upper() for t in args.indexes.split(",") if t.strip()]
        unknown = set(etf_tickers) - set(_ISHARES_CSV_URLS)
        if unknown:
            raise SystemExit(f"Unknown --indexes entries: {sorted(unknown)} (known: {sorted(_ISHARES_CSV_URLS)})")

        holdings_files = []
        for etf_ticker in etf_tickers:
            text = _fetch_csv_text(etf_ticker, args.csv_dir)
            holdings = _parse_holdings_csv(etf_ticker, text)
            print(f"{etf_ticker} ({holdings.index_name}): {len(holdings.rows)} holdings, as of {holdings.as_of}", flush=True)
            holdings_files.append(holdings)

    conn = open_db()
    if not args.dry_run:
        for holdings in holdings_files:
            add_index_definition(conn, holdings.index_name)

    existing = _existing_companies(conn)

    # Union of tickers across all requested files -> the registration list.
    # Track a representative (name, sector) per ticker for the CSV-only
    # metadata register_company() needs (first file that mentions a ticker
    # wins -- fine, all 4 files list the same company under the same name).
    ticker_meta: dict[str, dict] = {}
    for holdings in holdings_files:
        for row in holdings.rows:
            ticker_meta.setdefault(normalize_us_ticker(row["ticker"]), row)

    to_register = []
    for ticker, meta in ticker_meta.items():
        resolved = resolve_us_company_id(ticker, existing)
        if resolved is None:
            continue
        company_id, fetch_symbol = resolved
        to_register.append((company_id, fetch_symbol, meta))
    if args.limit:
        to_register = to_register[: args.limit]

    total = len(to_register)
    print(
        f"{len(ticker_meta)} distinct tickers across {len(holdings_files)} index file(s), "
        f"{len(existing)} companies already on file, {total} to register",
        flush=True,
    )

    if args.dry_run:
        for company_id, fetch_symbol, meta in to_register:
            print(f"[dry-run] would register {company_id} (fetch_symbol={fetch_symbol}) -- {meta['name']}")
        for holdings in holdings_files:
            print(f"[dry-run] would tag {len(holdings.rows)} companies as {holdings.index_name!r} "
                  f"(source={holdings.source}, effective_from={holdings.as_of})")
        conn.close()
        return

    registered = errors = 0
    for i, (company_id, fetch_symbol, meta) in enumerate(to_register, 1):
        ticker = fetch_symbol or company_id
        try:
            info = yf.Ticker(ticker).info
        except Exception as exc:  # noqa: BLE001 -- one ticker's Yahoo lookup failing must not abort the batch
            info = {}
            print(f"[{i}/{total}] {company_id}: yfinance lookup failed ({exc}) -- registering with CSV data only", flush=True)
            errors += 1

        kwargs = dict(
            legal_name=info.get("longName") or meta["name"],
            display_name=meta["name"],
            country="US",
            currency="USD",
            fetch_symbol=fetch_symbol,
            fiscal_year_end_month=fiscal_year_end_month_from_yfinance_info(info),
            website=info.get("website"),
            sector=info.get("sector") or meta.get("sector"),
            industry=info.get("industry"),
        )
        try:
            register_company(conn, company_id, **kwargs)
            registered += 1
            existing[company_id] = "US"
            print(f"[{i}/{total}] {company_id}: registered ({kwargs['sector']})", flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad row must not abort the rest of the batch
            print(f"[{i}/{total}] {company_id}: registration failed ({exc})", flush=True)
            errors += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nRegistration done: {registered} registered, {errors} errors, out of {total} attempted.", flush=True)

    # Map every ticker in each file to the company_id it's ACTUALLY stored
    # under (existing_us_company_id, not resolve_us_company_id -- the
    # latter returns None for "nothing to register", which is true both
    # for an untouched existing US company AND for a ticker already
    # registered under its disambiguated "-US" id, so it can't tell tagging
    # which company_id to use). Skip any ticker that ISN'T actually in
    # `existing` yet -- e.g. a --limit-truncated registration pass, or a
    # ticker whose registration itself failed above -- tagging a company_id
    # that was never inserted would violate company_index_membership's FK
    # to companies (found exactly this via a real --limit 20 test run).
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for holdings in holdings_files:
        company_ids = []
        skipped_unregistered = 0
        for row in holdings.rows:
            ticker = normalize_us_ticker(row["ticker"])
            if ticker not in existing and f"{ticker}-US" not in existing:
                skipped_unregistered += 1
                continue
            company_ids.append(existing_us_company_id(ticker, existing))
        if skipped_unregistered:
            print(f"{holdings.index_name}: skipping {skipped_unregistered} not-yet-registered companies "
                  f"(outside this run's --limit, or registration failed)", flush=True)

        touched = tag_companies_index(
            conn, company_ids, holdings.index_name,
            source=holdings.source, retrieved_at=retrieved_at, effective_from=holdings.as_of,
        )

        if args.limit:
            # --limit means this run only ever saw a truncated slice of the
            # real file's tickers -- treating everything outside that slice
            # as "dropped" would incorrectly flip real, already-tagged
            # members to historical. Reconstitution bookkeeping only runs
            # on a full (--limit-free) pass.
            print(f"{holdings.index_name}: {touched} rows tagged/refreshed "
                  f"(--limit active, skipping dropped-company bookkeeping)", flush=True)
            continue

        current_rows = select_company_ids_by_index(conn, holdings.index_name)
        current_ids = {r["company_id"] for r in current_rows}
        dropped = list(current_ids - set(company_ids))
        closed_out = mark_index_membership_historical(conn, dropped, holdings.index_name, retrieved_at[:10])

        print(
            f"{holdings.index_name}: {touched} rows tagged/refreshed, "
            f"{closed_out} companies marked historical (dropped from this reconstitution)",
            flush=True,
        )

    conn.close()


if __name__ == "__main__":
    main()
