"""Shared fixtures for Phase 2 tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A freshly initialized database with the metric vocabulary seeded, no companies."""
    conn = init_db(db_path=tmp_path / "test.db")
    ensure_metric_vocabulary(conn)
    yield conn
    conn.close()
