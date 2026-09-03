"""Detect which source adapter applies to a raw file, from its path convention.

Files live at data/raw/<COMPANY>/<source>/<file> (README: Ingestion Approach
by Source). Detection reads that convention rather than sniffing content.

Non-company sources (RBI, IMD, MOSPI, ...) have no company_id, so they don't
fit that convention — data/raw/_macro/<source>/<file> instead, with `_macro`
a sentinel folder name (leading underscore, never a valid ticker) so it's
distinguishable from a real company at a glance and in code (README: Data
Layers -> Non-company sources).
"""

from __future__ import annotations

from pathlib import Path

from config import settings
from sources.base import SourceAdapter
from sources.nse_xbrl import NSEXbrlAdapter
from sources.proprietary import ProprietaryAdapter
from sources.screener import ScreenerAdapter

# source_id -> adapter class. Extend this dict as new adapters land (README:
# Implementation Sequence adds NSE/BSE in step 6, Investor Relations in step 7).
ADAPTER_CLASSES: dict[str, type[SourceAdapter]] = {
    "screener": ScreenerAdapter,
    "proprietary": ProprietaryAdapter,
    "nse": NSEXbrlAdapter,
}

MACRO_SENTINEL = "_macro"


class PathConventionError(ValueError):
    """Raised when a file path doesn't follow data/raw/<COMPANY>/<source>/<file>."""


def _relative_parts(file_path: Path, raw_dir: Path | None) -> tuple[str, ...]:
    raw_dir = raw_dir if raw_dir is not None else settings.RAW_DIR
    try:
        relative = file_path.resolve().relative_to(raw_dir.resolve())
    except ValueError:
        raise PathConventionError(
            f"{file_path} is not under {raw_dir} (expected data/raw/<COMPANY>/<source>/<file>)"
        ) from None
    return relative.parts


def is_macro_path(file_path: Path, raw_dir: Path | None = None) -> bool:
    """True if file_path sits under data/raw/_macro/... rather than a company folder."""
    parts = _relative_parts(file_path, raw_dir)
    return len(parts) >= 1 and parts[0] == MACRO_SENTINEL


def detect_macro_source_from_path(file_path: Path, raw_dir: Path | None = None) -> str:
    """Infer the macro source_id from data/raw/_macro/<source>/<file>."""
    parts = _relative_parts(file_path, raw_dir)
    if len(parts) < 3 or parts[0] != MACRO_SENTINEL:
        raise PathConventionError(
            f"{file_path} does not match data/raw/{MACRO_SENTINEL}/<source>/<file> (got {'/'.join(parts)})"
        )
    return parts[1]


def detect_from_path(file_path: Path, raw_dir: Path | None = None) -> tuple[str, str]:
    """Infer (company_id, source_id) from a path under data/raw/<COMPANY>/<source>/<file>.

    raw_dir defaults to settings.RAW_DIR, read at call time (not import time)
    so tests can monkeypatch it.
    """
    parts = _relative_parts(file_path, raw_dir)
    if parts and parts[0] == MACRO_SENTINEL:
        raise PathConventionError(
            f"{file_path} is a macro path, not a company one — use detect_macro_source_from_path()/"
            f"ingest_macro_file() instead"
        )

    if len(parts) < 3:
        raise PathConventionError(
            f"{file_path} does not match data/raw/<COMPANY>/<source>/<file> (got {'/'.join(parts)})"
        )

    company_id, source_id = parts[0], parts[1]
    if source_id not in ADAPTER_CLASSES:
        raise PathConventionError(f"No adapter registered for source_id={source_id!r} (path: {file_path})")
    return company_id, source_id
