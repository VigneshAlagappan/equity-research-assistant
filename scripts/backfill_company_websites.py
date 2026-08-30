"""One-time backfill: fetch each US company's official website from Yahoo
Finance (yfinance's Ticker.info, company-profile metadata — not a price or a
financial-statement fact, so outside this project's usual "yfinance is
price/volume only" boundary) and store it on companies.website. The Indian
companies already carry theirs; the dozen US ones registered via
ingest_yfinance_company never got one, so the Companies list's
company-website link icon (web/templates/index.html) silently has nothing
to point at for any of them.

Usage (run as a module, same reasoning as scripts/backfill_price_history.py
-- a plain `python scripts/backfill_company_websites.py` fails on the
`companies`/`storage` imports below):
    python -m scripts.backfill_company_websites
    python -m scripts.backfill_company_websites --company-id AAPL
"""

from __future__ import annotations

import argparse
import time

import yfinance as yf

from companies.registry import update_company_website
from storage.database import init_db

REQUEST_DELAY_SECONDS = 0.6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default=None, help="Limit the run to a single company_id")
    args = parser.parse_args()

    conn = init_db()
    query = "SELECT company_id FROM companies WHERE country != 'IN' AND website IS NULL"
    params: tuple = ()
    if args.company_id:
        query += " AND company_id = ?"
        params = (args.company_id,)
    query += " ORDER BY company_id"
    rows = conn.execute(query, params).fetchall()

    total = len(rows)
    print(f"{total} companies missing a website", flush=True)

    updated = no_data = errors = 0
    for i, (company_id,) in enumerate(rows, 1):
        try:
            website = yf.Ticker(company_id).info.get("website")
        except Exception as exc:
            errors += 1
            print(f"[{i}/{total}] {company_id}: error ({exc})", flush=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not website:
            no_data += 1
            print(f"[{i}/{total}] {company_id}: no website on file", flush=True)
        else:
            update_company_website(conn, company_id, website)
            updated += 1
            print(f"[{i}/{total}] {company_id}: {website}", flush=True)
        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    print(f"Done: {updated} updated, {no_data} had no website on file, {errors} errors", flush=True)


if __name__ == "__main__":
    main()
