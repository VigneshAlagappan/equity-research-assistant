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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-mb", type=int, default=49, help="Max size per shard part, in MB (default 49)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} does not exist")

    chunk_bytes = args.chunk_mb * 1024 * 1024

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


if __name__ == "__main__":
    main()
