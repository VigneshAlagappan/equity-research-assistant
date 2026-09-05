"""Splits data/equity_research.db into <=50MB parts under data/db_shards/
for git storage — GitHub hard-blocks any single file over 100MB, and this
repo's db (well past the 50MB warning threshold already) is tracked in git
directly today. Doesn't touch the live db file itself — uses SQLite's own
online backup API (sqlite3.Connection.backup()), safe to run even while the
app/ingestion workers are writing to it concurrently, since it never locks
or copies the file at the OS level. Only how the db is *stored in git*
changes; the app keeps reading/writing data/equity_research.db exactly as
before.

Usage: python scripts/db_shard.py [--chunk-mb 49]
Reassemble with scripts/db_unshard.py after a fresh clone/pull.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from ingestion.batch_log import BatchRun
from storage.database import init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "equity_research.db"
SHARD_DIR = REPO_ROOT / "data" / "db_shards"
PART_PREFIX = "equity_research.db.part-"
CHECKSUM_FILE = "checksum.sha256"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_db_shard(chunk_mb: int = 49) -> dict:
    """The actual backup/split/checksum work, factored out of the old bare
    main() so both the CLI below and the Settings > Data Operations >
    Schedule panel's "Run now" button (web/app.py) drive the identical
    local shard-file write -- one capability, two triggers. Deliberately
    does *only* that: no `git add`/commit/push here, matching what
    db_shard.py has always done (see SCHEDULED_JOBS.md section 7) -- the
    commit+push step for an unattended daily run is a separate, still-
    undecided authorization this function must not blur past.

    Returns a small summary dict ({"parts", "checksum", "total_bytes"}) --
    the caller (run_db_shard_job below) records it as one BatchRun item's
    detail line."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} does not exist")

    chunk_bytes = chunk_mb * 1024 * 1024

    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(snapshot_path))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()

        checksum = _sha256(snapshot_path)

        if SHARD_DIR.exists():
            shutil.rmtree(SHARD_DIR)
        SHARD_DIR.mkdir(parents=True)

        part_count = 0
        with snapshot_path.open("rb") as f:
            while True:
                data = f.read(chunk_bytes)
                if not data:
                    break
                part_path = SHARD_DIR / f"{PART_PREFIX}{part_count:04d}"
                part_path.write_bytes(data)
                part_count += 1

    (SHARD_DIR / CHECKSUM_FILE).write_text(checksum + "\n")

    total_size = sum((SHARD_DIR / f"{PART_PREFIX}{i:04d}").stat().st_size for i in range(part_count))
    print(f"Sharded {DB_PATH.name} ({total_size / 1024 / 1024:.1f} MB) into {part_count} part(s) under {SHARD_DIR}/")
    print(f"Checksum: {checksum}")
    return {"parts": part_count, "checksum": checksum, "total_bytes": total_size}


def run_db_shard_job(conn=None, chunk_mb: int = 49) -> int:
    """Wraps run_db_shard() in a BatchRun so a Schedule-panel trigger shows
    up in Audit Log > Job Runs like every other job -- kept as a separate
    function rather than folding the BatchRun bookkeeping into run_db_shard()
    itself, so the CLI path below stays exactly as lightweight as it always
    was (no db connection needed just to shard a file) and only the web
    route pays for the audit-log wrapping. This job isn't per-company, so it
    logs a single `run.item(None)` rather than one item per company like the
    NSE batch jobs. Returns the BatchRun's run_id."""
    owns_conn = conn is None
    if conn is None:
        conn = init_db()
    try:
        # scope_label can't be the eventual "N parts" count the spec's
        # sketch suggested -- BatchRun.__enter__ starts the run row (and
        # therefore fixes scope_label) before run_db_shard() below has run
        # and produced that count, and there's no update-scope-label helper
        # in storage/repositories.py to revise it after the fact. A static
        # label naming what's being sharded is the closest equivalent;
        # the actual part count/size/checksum still end up in the item's
        # detail line below, which is what Audit Log > Job Runs displays.
        with BatchRun(conn, "db_shard", scope_label=f"{DB_PATH.name} -> {SHARD_DIR}/") as run:
            with run.item(None) as item:
                summary = run_db_shard(chunk_mb=chunk_mb)
                mb = summary["total_bytes"] / 1024 / 1024
                item.detail = f"{summary['parts']} parts, ~{mb:.0f}MB, checksum {summary['checksum'][:12]}..."
        return run.run_id
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-mb", type=int, default=49, help="Max size per shard part, in MB (default 49)")
    args = parser.parse_args()

    try:
        run_db_shard(chunk_mb=args.chunk_mb)
    except FileNotFoundError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
