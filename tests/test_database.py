"""Phase 1 scaffold tests: schema creation and source seeding."""

from __future__ import annotations

from pathlib import Path

from config.settings import DEFAULT_SOURCES, SCHEMA_PATH
from storage.database import get_connection, init_db, list_tables

EXPECTED_TABLES = {
    "canonical_financials",
    "companies",
    "company_identifier_history",
    "document_chunks",
    "document_chunks_fts",
    "documents",
    "financial_observations",
    "metric_aliases",
    "metrics_dictionary",
    "reconciliation_log",
    "sources",
}


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    conn = init_db(db_path=tmp_path / "test.db")
    try:
        assert EXPECTED_TABLES <= set(list_tables(conn))
    finally:
        conn.close()


def test_init_db_seeds_sources(tmp_path: Path) -> None:
    conn = init_db(db_path=tmp_path / "test.db")
    try:
        rows = conn.execute("SELECT source_id, trust_rank FROM sources").fetchall()
        seeded = {row["source_id"]: row["trust_rank"] for row in rows}
        expected = {s["source_id"]: s["trust_rank"] for s in DEFAULT_SOURCES}
        assert seeded == expected
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = init_db(db_path=db_path)
    tables_after_first_run = set(list_tables(conn))
    conn.close()

    conn = init_db(db_path=db_path)  # should not raise, and not add/drop tables
    try:
        assert set(list_tables(conn)) == tables_after_first_run
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = init_db(db_path=tmp_path / "test.db")
    try:
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1
    finally:
        conn.close()


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.exists()


def test_migrate_raw_file_paths_to_repo_relative(tmp_path: Path) -> None:
    """A pre-fix row stored with an absolute raw_file_path (any historical
    repo folder name — the migration doesn't special-case which one) is
    rewritten to the same repo-relative form new writes use, on the very
    next init_db() call, not just at insert time."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path=db_path)
    conn.execute(
        "INSERT INTO companies (company_id, legal_name, display_name, status, created_at, updated_at) "
        "VALUES ('HDFCBANK', 'HDFC Bank Limited', 'HDFC Bank', 'active', '2024-01-01', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO documents (company_id, document_type, raw_file_path, retrieved_at) VALUES "
        "('HDFCBANK', 'annual_report', "
        "'/Users/someone/old-repo-name/data/documents/HDFCBANK/report.pdf', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO company_notes (company_id, note_text, created_at) VALUES ('HDFCBANK', 'x', '2024-01-01')"
    )
    note_id = conn.execute("SELECT note_id FROM company_notes").fetchone()["note_id"]
    conn.execute(
        "INSERT INTO company_note_attachments (note_id, filename, raw_file_path, size_bytes, uploaded_at) VALUES "
        "(?, 'memo.txt', '/Users/someone/old-repo-name/data/documents/HDFCBANK/note_attachments/memo.txt', 11, '2024-01-01')",
        (note_id,),
    )
    conn.commit()
    conn.close()

    conn = init_db(db_path=db_path)  # re-running init_db is what triggers the migration
    doc = conn.execute("SELECT raw_file_path FROM documents").fetchone()
    assert doc["raw_file_path"] == "data/documents/HDFCBANK/report.pdf"
    attachment = conn.execute("SELECT raw_file_path FROM company_note_attachments").fetchone()
    assert attachment["raw_file_path"] == "data/documents/HDFCBANK/note_attachments/memo.txt"
    conn.close()


def test_init_db_default_path_resolves_settings_dynamically(tmp_path: Path, monkeypatch) -> None:
    """Regression test: init_db()/get_connection() used to bind DB_PATH as a
    default-parameter value at import time, so monkeypatching config.settings.DB_PATH
    afterward had no effect — the same stale-binding bug fixed in
    ingestion/detector.py for RAW_DIR. web/app.py relies on the no-args default
    resolving to whatever settings.DB_PATH is *now*, not at process start.
    """
    db_path = tmp_path / "monkeypatched.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_path)

    conn = init_db()  # no db_path passed — must resolve settings.DB_PATH at call time
    try:
        assert db_path.exists()
        assert EXPECTED_TABLES <= set(list_tables(conn))
    finally:
        conn.close()


def test_get_connection_reopens_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path=db_path).close()
    conn = get_connection(db_path)
    try:
        assert "companies" in list_tables(conn)
    finally:
        conn.close()
