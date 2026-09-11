"""One-off backfill: tags every Indian company outside the Nifty 500 with the
'Nifty Micro-Cap' index name, so the fetch/schedule machinery that already
partitions work by company_index_membership (scripts/batch_fetch_nse.py's
`--index`, web/app.py's Schedule panel tier closures) has a fifth tier to run
against, alongside Nifty 50 / Next 50 / Midcap 150 / Smallcap 250.

'Nifty Micro-Cap' isn't an NSE-defined index -- it's this app's own catch-all
label for "every registered Indian company NSE's own Nifty 500 tiering
doesn't already cover" (2054 companies as of this session, computed live
below rather than hardcoded since companies/nse_import.py keeps adding to the
universe). Additive only: tag_companies_index() uses INSERT OR IGNORE on
company_index_membership's (company_id, index_name) primary key, so this
never touches a company's other index tags (BSE indices, Nifty sub-variants)
the way set_company_index_tags()'s "replace the whole tag set" would.

Idempotent-safe to interrupt and re-run, same philosophy as
scripts/backfill_sector_industry.py -- a re-run only re-tags companies still
missing the label (INSERT OR IGNORE no-ops on the rest).

Usage: python -m scripts.tag_nifty_microcap
"""

from __future__ import annotations

from storage.company_repository import select_india_companies_not_in_index, tag_companies_index
from storage.database import init_db
from storage.repositories import add_index_definition

INDEX_NAME = "Nifty Micro-Cap"
EXCLUDE_INDEX_NAME = "Nifty 500"


def main() -> None:
    conn = init_db()
    add_index_definition(conn, INDEX_NAME)

    rows = select_india_companies_not_in_index(conn, EXCLUDE_INDEX_NAME)
    company_ids = [r["company_id"] for r in rows]
    total = len(company_ids)
    print(f"{total} Indian companies outside {EXCLUDE_INDEX_NAME}", flush=True)

    newly_tagged = tag_companies_index(conn, company_ids, INDEX_NAME)
    already_tagged = total - newly_tagged
    conn.close()

    print(f"Tagged {newly_tagged} companies as {INDEX_NAME} ({already_tagged} already tagged)", flush=True)


if __name__ == "__main__":
    main()
