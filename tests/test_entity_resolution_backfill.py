"""main.py's `entity-resolution-backfill` CLI command (context/entity_resolution.py,
Step 2B follow-up) — same test-double shape as tests/test_vector_backfill.py:
a tmp_path SQLite database (monkeypatched config.settings.DB_PATH), main.py's
real build_parser()/cmd function, never the real data/equity_research.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import main
from companies.registry import seed_companies
from storage.database import init_db
from storage.repositories import (
    get_or_create_knowledge_entity,
    insert_knowledge_relationship,
    list_batch_job_items,
    list_batch_job_runs,
)


@pytest.fixture
def backfill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    conn = init_db(db_path=db_path)
    seed_companies(conn)
    yield conn
    conn.close()


def _seed_duplicates(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """HDFCBANK's canonical Company entity, plus a matching duplicate
    ("HDFC Bank Limited", HDFCBANK's own legal_name) and a genuinely
    different, subsidiary-shaped one ("HDFC Securities Limited") that must
    be left alone. Returns (canonical_id, matching_dup_id, left_alone_id)."""
    canonical = get_or_create_knowledge_entity(conn, "Company", "HDFCBANK", "HDFCBANK")
    matching_dup = get_or_create_knowledge_entity(conn, "Company", "HDFC Bank Limited", "HDFCBANK")
    left_alone = get_or_create_knowledge_entity(conn, "Company", "HDFC Securities Limited", "HDFCBANK")
    risk = get_or_create_knowledge_entity(conn, "Risk", "Interest Rate Volatility", "HDFCBANK")
    # A relationship touching the matching duplicate — this is what --apply
    # must repoint onto the canonical entity before deleting the duplicate.
    insert_knowledge_relationship(
        conn, claim_id=None, source_entity_id=matching_dup["entity_id"], relationship_type="EXPOSED_TO",
        target_entity_id=risk["entity_id"],
    )
    return canonical["entity_id"], matching_dup["entity_id"], left_alone["entity_id"]


def _run_backfill(*, company_id: str | None = None, apply: bool = False) -> None:
    flags = ["entity-resolution-backfill"]
    if company_id:
        flags += ["--company-id", company_id]
    if apply:
        flags.append("--apply")
    args = main.build_parser().parse_args(flags)
    args.func(args)


def test_dry_run_changes_nothing_but_still_records_the_plan(backfill_env: sqlite3.Connection) -> None:
    conn = backfill_env
    canonical_id, matching_dup_id, left_alone_id = _seed_duplicates(conn)

    _run_backfill(company_id="HDFCBANK")

    # Nothing written — the duplicate and its relationship are both untouched.
    still_there = conn.execute(
        "SELECT entity_id FROM knowledge_entities WHERE entity_id = ?", (matching_dup_id,)
    ).fetchone()
    assert still_there is not None
    rel = conn.execute(
        "SELECT source_entity_id FROM knowledge_relationships WHERE source_entity_id = ?", (matching_dup_id,)
    ).fetchone()
    assert rel is not None

    # But the plan IS recorded, human-reviewable via show-batch-run.
    runs = list_batch_job_runs(conn)
    assert len(runs) == 1
    assert runs[0]["job_name"] == "entity_resolution_backfill"
    items = list_batch_job_items(conn, runs[0]["run_id"])
    item = next(i for i in items if i["company_id"] == "HDFCBANK")
    assert "merged=1 (HDFC Bank Limited)" in item["detail"]
    assert "left_alone=1 (HDFC Securities Limited)" in item["detail"]


def test_apply_merges_the_matching_duplicate_and_repoints_relationships(backfill_env: sqlite3.Connection) -> None:
    conn = backfill_env
    canonical_id, matching_dup_id, left_alone_id = _seed_duplicates(conn)

    _run_backfill(company_id="HDFCBANK", apply=True)

    # The matching duplicate is gone...
    assert conn.execute(
        "SELECT entity_id FROM knowledge_entities WHERE entity_id = ?", (matching_dup_id,)
    ).fetchone() is None
    # ...and its relationship now points at the canonical entity instead.
    rel = conn.execute(
        "SELECT source_entity_id FROM knowledge_relationships WHERE relationship_type = 'EXPOSED_TO'"
    ).fetchone()
    assert rel["source_entity_id"] == canonical_id

    # The genuinely different subsidiary-shaped entity is completely untouched.
    assert conn.execute(
        "SELECT entity_id FROM knowledge_entities WHERE entity_id = ?", (left_alone_id,)
    ).fetchone() is not None


def test_apply_leaves_a_non_matching_duplicate_alone(backfill_env: sqlite3.Connection) -> None:
    """Regression guard against over-merging, even with --apply: a
    subsidiary/auditor/noise row sharing the company's company_id scope is
    never merged, matching or not."""
    conn = backfill_env
    _seed_duplicates(conn)

    _run_backfill(company_id="HDFCBANK", apply=True)

    rows = conn.execute(
        "SELECT name FROM knowledge_entities WHERE entity_type = 'Company' AND company_id = 'HDFCBANK'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert names == {"HDFCBANK", "HDFC Securities Limited"}


def test_no_duplicates_is_reported_as_such(backfill_env: sqlite3.Connection) -> None:
    conn = backfill_env
    get_or_create_knowledge_entity(conn, "Company", "ICICIBANK", "ICICIBANK")

    _run_backfill(company_id="ICICIBANK")

    runs = list_batch_job_runs(conn)
    items = list_batch_job_items(conn, runs[0]["run_id"])
    item = next(i for i in items if i["company_id"] == "ICICIBANK")
    assert item["detail"] == "no duplicate Company-type entities found"
