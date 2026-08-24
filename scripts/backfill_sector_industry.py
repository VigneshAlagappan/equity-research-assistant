"""One-off backfill: fills NULL companies.sector / companies.industry from
Yahoo Finance (via the yfinance dependency already used by web/live_quote.py)
for every registered company missing either field that has an nse_symbol on
file — company_id isn't a reliable ticker (e.g. MUTHOOTFINANCE's real NSE
ticker is MUTHOOTFIN), so rows with no nse_symbol are skipped rather than
guessed at.

Never overwrites an existing value, same merge philosophy as
companies/nse_import.py's classification merge — Yahoo's own sector/industry
taxonomy doesn't exactly match NSE's (see sqlite_schema.sql's column
comments), so this only fills gaps, it doesn't reconcile disagreements.

Idempotent-safe to interrupt and re-run: every run only touches rows still
NULL, so a re-run just picks up wherever the last one left off. Expect
~1-2 hours wall-clock for the full backlog (one HTTP call per company, a
small delay between calls to stay polite to Yahoo's endpoint) — run this in
the background and tail its stdout.
"""

from __future__ import annotations

import time

import yfinance as yf

from storage.database import init_db

REQUEST_DELAY_SECONDS = 0.4


def main() -> None:
    conn = init_db()
    rows = conn.execute(
        "SELECT company_id, nse_symbol, sector, industry FROM companies "
        "WHERE (sector IS NULL OR industry IS NULL) AND nse_symbol IS NOT NULL "
        "ORDER BY company_id"
    ).fetchall()
    total = len(rows)
    print(f"{total} companies missing sector/industry with an nse_symbol on file", flush=True)

    updated = no_data = errors = 0
    for i, (company_id, nse_symbol, sector, industry) in enumerate(rows, 1):
        try:
            info = yf.Ticker(f"{nse_symbol}.NS").info
        except Exception as exc:
            errors += 1
            print(f"[{i}/{total}] {company_id:24s} ERROR {exc}", flush=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        new_sector = sector or (info.get("sector") or None)
        new_industry = industry or (info.get("industry") or None)
        if new_sector == sector and new_industry == industry:
            no_data += 1
            print(f"[{i}/{total}] {company_id:24s} no Yahoo sector/industry data", flush=True)
        else:
            conn.execute(
                "UPDATE companies SET sector = ?, industry = ? WHERE company_id = ?",
                (new_sector, new_industry, company_id),
            )
            conn.commit()
            updated += 1
            print(f"[{i}/{total}] {company_id:24s} sector={new_sector!r} industry={new_industry!r}", flush=True)

        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(f"\nDone. updated={updated} no_data={no_data} errors={errors} total={total}", flush=True)


if __name__ == "__main__":
    main()
