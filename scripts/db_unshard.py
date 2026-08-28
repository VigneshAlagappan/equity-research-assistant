"""Reassembles data/equity_research.db from the sharded parts under
data/db_shards/ (scripts/db_shard.py) — run this once after cloning the repo
or pulling a change to the shard parts, before starting the app.

Refuses to overwrite an existing data/equity_research.db unless --force is
passed — this repo's real, live db lives at that exact path, and this script
has no way to tell "no db yet" apart from "db already open by a running app"
on its own.

Usage: python scripts/db_unshard.py [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
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
    parser.add_argument("--force", action="store_true", help="Overwrite data/equity_research.db if it already exists")
    args = parser.parse_args()

    parts = sorted(SHARD_DIR.glob(f"{PART_PREFIX}*"))
    if not parts:
        sys.exit(f"No shard parts found under {SHARD_DIR}/ — nothing to reassemble")

    checksum_path = SHARD_DIR / CHECKSUM_FILE
    if not checksum_path.exists():
        sys.exit(f"{checksum_path} is missing — can't verify the reassembled file")
    expected = checksum_path.read_text().strip()

    if DB_PATH.exists() and not args.force:
        sys.exit(f"{DB_PATH} already exists — pass --force to overwrite it")

    tmp_path = DB_PATH.with_name(DB_PATH.name + ".reassembling")
    with tmp_path.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())

    actual = _sha256(tmp_path)
    if actual != expected:
        tmp_path.unlink()
        sys.exit(f"Checksum mismatch: expected {expected}, got {actual} — shard parts may be incomplete/corrupted")

    tmp_path.replace(DB_PATH)
    print(f"Reassembled {DB_PATH} ({DB_PATH.stat().st_size / 1024 / 1024:.1f} MB), checksum verified.")


if __name__ == "__main__":
    main()
