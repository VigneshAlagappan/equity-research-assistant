"""One-time migration: copy data/documents/** into the S3 bucket named by
config.settings.S3_BUCKET_NAME, using storage_object_key as the S3 key
(already backfilled to equal raw_file_path for every existing row — see
storage/database.py::_migrate_documents_storage_columns).

Additive/verify-only: does NOT delete local files, does NOT flip
DOCUMENT_STORE_BACKEND anywhere. Idempotent — skips any row whose
content_hash is already set (our marker for "already migrated and
verified"), so a re-run after a partial failure just resumes.

Updates BOTH the local SQLite documents table and the Postgres (Neon)
documents table so they stay consistent, since documents rows exist in
both after the earlier full data migration.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from config import settings
from storage.document_store import S3DocumentStore

SQLITE_PATH = settings.BASE_DIR / "data" / "equity_research.db"


def get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(os.environ["NEON"], cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_pending_rows(sqlite_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return sqlite_conn.execute(
        "SELECT document_id, company_id, raw_file_path, storage_object_key, content_hash "
        "FROM documents WHERE storage_object_key IS NOT NULL AND content_hash IS NULL "
        "ORDER BY document_id"
    ).fetchall()


def migrate_one(store: S3DocumentStore, row: sqlite3.Row) -> tuple[bool, str]:
    """Returns (success, message)."""
    local_path = settings.from_repo_relative(row["storage_object_key"])
    if not local_path.is_file():
        return False, f"local file missing: {local_path}"

    content = local_path.read_bytes()
    local_hash = hashlib.sha256(content).hexdigest()

    try:
        store.store(row["storage_object_key"], content)
    except Exception as exc:
        return False, f"upload failed: {exc}"

    try:
        remote_bytes = store.retrieve(row["storage_object_key"])
    except Exception as exc:
        return False, f"post-upload verify fetch failed: {exc}"

    remote_hash = hashlib.sha256(remote_bytes).hexdigest()
    if remote_hash != local_hash:
        return False, f"hash mismatch after upload (local={local_hash} remote={remote_hash})"

    return True, local_hash


def update_hash(sqlite_conn: sqlite3.Connection, pg_conn, document_id: int, content_hash: str) -> None:
    sqlite_conn.execute(
        "UPDATE documents SET content_hash = ? WHERE document_id = ?", (content_hash, document_id)
    )
    sqlite_conn.commit()
    if pg_conn is not None:
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET content_hash = %s WHERE document_id = %s",
                (content_hash, document_id),
            )
        pg_conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only migrate this many rows (dry run).")
    parser.add_argument("--skip-postgres", action="store_true", help="Do not update Postgres (dry-run mode).")
    args = parser.parse_args()

    sqlite_conn = get_sqlite_conn()
    pg_conn = None if args.skip_postgres else get_pg_conn()
    store = S3DocumentStore()

    rows = fetch_pending_rows(sqlite_conn)
    if args.limit:
        rows = rows[: args.limit]

    print(f"{len(rows)} document(s) to migrate to s3://{settings.S3_BUCKET_NAME}")

    ok_count = 0
    fail_count = 0
    start = time.time()
    for i, row in enumerate(rows, 1):
        success, msg = migrate_one(store, row)
        if success:
            update_hash(sqlite_conn, pg_conn, row["document_id"], msg)
            ok_count += 1
            if i % 25 == 0 or i == len(rows):
                print(f"  [{i}/{len(rows)}] ok={ok_count} fail={fail_count} ({time.time()-start:.0f}s elapsed)")
        else:
            fail_count += 1
            print(f"  [{i}/{len(rows)}] FAILED document_id={row['document_id']} company={row['company_id']}: {msg}")

    print(f"\nDone in {time.time()-start:.1f}s: {ok_count} migrated, {fail_count} failed, out of {len(rows)}")

    sqlite_conn.close()
    if pg_conn is not None:
        pg_conn.close()


if __name__ == "__main__":
    main()
