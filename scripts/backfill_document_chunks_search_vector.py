"""One-time backfill: populate document_chunks.search_vector for every row
still NULL on Neon (Postgres) -- code (storage/fact_store_pg.py::
search_document_chunks) and the search_vector/GIN index (schemas/postgres_
schema.sql) were already built and proven correct on a 2,088-row subset in
an earlier session; the full 95K-row backfill was blocked by Neon's
free-tier 512MB storage cap until the plan was upgraded.

Batched (BATCH_SIZE rows per UPDATE, committed between batches) rather
than one giant transaction -- keeps memory/lock time bounded and gives
real progress visibility on a run this size. Idempotent/resumable: only
ever touches rows still NULL, so a re-run after an interruption picks up
exactly where it left off.

Usage: python -m scripts.backfill_document_chunks_search_vector
"""

from __future__ import annotations

import time

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

import os

BATCH_SIZE = 2000


def get_conn():
    return psycopg2.connect(os.environ["NEON"])


def main() -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM document_chunks WHERE search_vector IS NULL")
    remaining = cur.fetchone()[0]
    print(f"{remaining} row(s) to backfill", flush=True)

    total_done = 0
    start = time.time()
    while True:
        cur.execute(
            """
            UPDATE document_chunks
            SET search_vector = to_tsvector('english', text)
            WHERE chunk_id IN (
                SELECT chunk_id FROM document_chunks
                WHERE search_vector IS NULL
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (BATCH_SIZE,),
        )
        batch_count = cur.rowcount
        conn.commit()
        if batch_count == 0:
            break
        total_done += batch_count
        elapsed = time.time() - start
        print(f"  backfilled {total_done}/{remaining} ({elapsed:.0f}s elapsed)", flush=True)

    print(f"\nDone. {total_done} row(s) backfilled in {time.time() - start:.1f}s", flush=True)

    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    print("Final DB size:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM document_chunks WHERE search_vector IS NULL")
    print("Rows still NULL (should be 0):", cur.fetchone()[0])

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
