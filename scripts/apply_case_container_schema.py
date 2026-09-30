"""Applies the Cases-container schema (research_cases.origin/hidden_at/deleted_at
and the case_companies tag table) to the Postgres database named by NEON.

Every statement is additive and IF NOT EXISTS, taken verbatim from
schemas/postgres_schema.sql, so it is safe to re-run.

Default is a DRY RUN: applies the DDL, exercises storage/repositories_pg.py's
new case functions against a throwaway case, then ROLLS BACK -- nothing
persists. Pass --apply to run the DDL and COMMIT it (no throwaway case).

    python scripts/apply_case_container_schema.py           # dry run
    python scripts/apply_case_container_schema.py --apply   # for real
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

START = "ALTER TABLE research_cases ADD COLUMN IF NOT EXISTS origin"
END = "CREATE INDEX IF NOT EXISTS idx_case_companies_company ON case_companies(company_id);"


def _ddl() -> str:
    schema = (ROOT / "schemas" / "postgres_schema.sql").read_text()
    return schema[schema.index(START): schema.index(END) + len(END)]


def _exercise(conn) -> None:
    import storage.repositories_pg as pg

    with conn.cursor() as cur:
        cur.execute("SELECT company_id FROM companies ORDER BY company_id LIMIT 2")
        ids = [row["company_id"] for row in cur.fetchall()]
    if len(ids) < 2:
        print("skipping function checks: fewer than 2 companies registered")
        return
    row = pg.create_research_case(
        conn, "zz-schema-check", kind="ask", question="q", company_ids=ids + ["NOT-A-COMPANY"],
        statement_type="consolidated", owner_id=None, origin="conversation",
    )
    assert row["origin"] == "conversation"
    assert pg.list_case_company_ids(conn, "zz-schema-check") == ids, "unknown company should be skipped"
    assert pg.add_case_company(conn, "zz-schema-check", "NOT-A-COMPANY") is False
    assert pg.remove_case_company(conn, "zz-schema-check", ids[0]) is True
    assert [r["case_id"] for r in pg.list_cases_for_company(conn, ids[1])] == ["zz-schema-check"]
    assert pg.hide_research_case(conn, "zz-schema-check") is True
    assert pg.list_cases_for_company(conn, ids[1]) == []
    assert pg.delete_research_case(conn, "zz-schema-check") is True
    print("pg repository functions OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit the DDL (default: dry run, rolled back)")
    args = parser.parse_args()

    conn = psycopg2.connect(os.environ["NEON"], cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(_ddl())
        if args.apply:
            conn.commit()
            print("DDL committed")
        else:
            _exercise(conn)
            conn.rollback()
            print("DRY RUN rolled back -- nothing persisted. Re-run with --apply to commit.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
