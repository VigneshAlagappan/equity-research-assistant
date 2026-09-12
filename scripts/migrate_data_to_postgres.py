"""One-time (but safely re-runnable) bulk data migration:
SQLite (`data/equity_research.db`) -> Postgres/Neon (`schemas/postgres_schema.sql`).

Migrates all 42 ported tables in FK-safe topological order, using
`psycopg2.extras.execute_values()` batch inserts with `ON CONFLICT DO NOTHING`
on each table's real primary key so a retry after a partial failure is safe
and idempotent (rows already present are simply skipped, not duplicated).

`companies` is self-referencing (`predecessor_company_id`/`successor_company_id`
both REFERENCE companies(company_id)) — handled with a two-pass insert: first
pass inserts every row with those two columns NULLed out, second pass UPDATEs
them in once every company row exists, so insertion order among companies
never matters.

Tables with an `INTEGER GENERATED ALWAYS AS IDENTITY` primary key are inserted
with `OVERRIDING SYSTEM VALUE` so the original SQLite rowids are preserved
(required — other tables' foreign keys point at these exact ids).

Usage:
    python scripts/migrate_data_to_postgres.py [--verify-only]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 3000

SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "equity_research.db"

# FK-safe topological order (derived from schemas/postgres_schema.sql's
# REFERENCES clauses -- see task notes / PR description for the derivation).
TABLE_ORDER = [
    "sources",
    "companies",
    "company_identifier_history",
    "metrics_dictionary",
    "metric_aliases",
    "documents",
    # financial_observations excluded (2026-09-11): dropped from Postgres,
    # stays SQLite-only -- see schemas/postgres_schema.sql's own note.
    "canonical_financials",
    "macro_observations",
    "bank_infrastructure_observations",
    "document_chunks",
    "watchlist_items",
    "company_news",
    "generated_reports",
    "research_thread_evidence",
    "research_thread_followups",
    "company_insights",
    "system_insights",
    "company_notes",
    "company_note_attachments",
    "company_index_membership",
    "sectors",
    "industries",
    "index_definitions",
    "company_list_column_settings",
    "overview_ratio_settings",
    "knowledge_entities",
    "knowledge_claims",
    "knowledge_relationships",
    "knowledge_evidence",
    "investigations",
    "investigation_companies",
    "investigation_hypotheses",
    "investigation_hypothesis_evidence",
    "stock_actions",
    "corporate_actions_raw",
    "corporate_actions",
    "shareholding_observations",
    "shareholding_holders",
    "users",
    "indicator_rule_config",
    "indicator_evaluations",
]

# Primary key columns per table (composite where applicable). Tables with no
# real PK/unique constraint (research_thread_evidence, research_thread_followups)
# map to None -- those get a plain INSERT (no ON CONFLICT target available).
PRIMARY_KEYS: dict[str, tuple[str, ...] | None] = {
    "sources": ("source_id",),
    "companies": ("company_id",),
    "company_identifier_history": ("id",),
    "metrics_dictionary": ("metric_key",),
    "metric_aliases": ("alias_id",),
    "documents": ("document_id",),
    "canonical_financials": ("canonical_id",),
    "macro_observations": ("observation_id",),
    "bank_infrastructure_observations": ("observation_id",),
    "document_chunks": ("chunk_id",),
    "watchlist_items": ("item_id",),
    "company_news": ("id",),
    "generated_reports": ("thread_id",),
    "research_thread_evidence": None,
    "research_thread_followups": None,
    "company_insights": ("insight_id",),
    "system_insights": ("insight_id",),
    "company_notes": ("note_id",),
    "company_note_attachments": ("attachment_id",),
    "company_index_membership": ("company_id", "index_name"),
    "sectors": ("name",),
    "industries": ("name",),
    "index_definitions": ("name",),
    "company_list_column_settings": ("column_key",),
    "overview_ratio_settings": ("ratio_key",),
    "knowledge_entities": ("entity_id",),
    "knowledge_claims": ("claim_id",),
    "knowledge_relationships": ("relationship_id",),
    "knowledge_evidence": ("evidence_id",),
    "investigations": ("investigation_id",),
    "investigation_companies": ("investigation_id", "company_id"),
    "investigation_hypotheses": ("hypothesis_id",),
    "investigation_hypothesis_evidence": ("id",),
    "stock_actions": ("action_id",),
    "corporate_actions_raw": ("raw_id",),
    "corporate_actions": ("action_id",),
    "shareholding_observations": ("observation_id",),
    "shareholding_holders": ("holder_id",),
    "users": ("user_id",),
    "indicator_rule_config": ("config_id",),
    "indicator_evaluations": ("evaluation_id",),
}

# Tables whose PK is an INTEGER GENERATED ALWAYS AS IDENTITY column -- these
# need `OVERRIDING SYSTEM VALUE` to accept the explicit ids carried over from
# SQLite (required so FK references from other tables keep resolving).
IDENTITY_TABLES = {
    "company_identifier_history", "metric_aliases", "documents", "financial_observations",
    "canonical_financials", "macro_observations", "bank_infrastructure_observations",
    "document_chunks", "watchlist_items", "company_news", "company_insights",
    "company_notes", "company_note_attachments", "knowledge_entities", "knowledge_claims",
    "knowledge_relationships", "knowledge_evidence", "investigation_hypothesis_evidence",
    "stock_actions", "corporate_actions_raw", "corporate_actions", "shareholding_observations",
    "shareholding_holders", "users", "indicator_rule_config", "indicator_evaluations",
}

# companies is self-referencing; migrate_table() handles it specially.
COMPANIES_DEFERRED_COLUMNS = ("predecessor_company_id", "successor_company_id")


def sanitize_value(value):
    """Postgres TEXT columns reject embedded NUL (0x00) bytes outright
    ("A string literal cannot contain NUL (0x00) characters"), while SQLite
    permits them. Source text extracted from PDFs/XBRL can occasionally
    contain stray NULs, so strip them from any string value generically
    (not scoped to one table/column) before it reaches execute_values().
    """
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "")
    return value


def get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    connection_string = os.environ["NEON"]
    return psycopg2.connect(connection_string, cursor_factory=psycopg2.extras.RealDictCursor)


def pg_columns(pg_cur, table: str) -> list[str]:
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r["column_name"] for r in pg_cur.fetchall()]


def sqlite_columns(sqlite_cur, table: str) -> list[str]:
    sqlite_cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in sqlite_cur.fetchall()]


def migrate_table(sqlite_conn, pg_conn, table: str) -> tuple[int, int]:
    """Returns (rows_read, rows_written_or_attempted)."""
    s_cur = sqlite_conn.cursor()
    pg_cur = pg_conn.cursor()

    s_cols = sqlite_columns(s_cur, table)
    p_cols = pg_columns(pg_cur, table)
    cols = [c for c in s_cols if c in p_cols]
    skipped_sqlite_only = [c for c in s_cols if c not in p_cols]
    if skipped_sqlite_only:
        print(f"  [{table}] sqlite-only columns skipped: {skipped_sqlite_only}")

    defer_cols = set()
    if table == "companies":
        defer_cols = set(COMPANIES_DEFERRED_COLUMNS) & set(cols)

    insert_cols = [c for c in cols if c not in defer_cols]

    pk = PRIMARY_KEYS.get(table)
    overriding = "OVERRIDING SYSTEM VALUE " if table in IDENTITY_TABLES else ""
    conflict_clause = f"ON CONFLICT ({', '.join(pk)}) DO NOTHING" if pk else ""

    col_list_sql = ", ".join(f'"{c}"' for c in insert_cols)
    # OVERRIDING SYSTEM VALUE must come before VALUES in Postgres syntax:
    # INSERT INTO t (...) OVERRIDING SYSTEM VALUE VALUES %s ON CONFLICT ...
    sql = f'INSERT INTO {table} ({col_list_sql}) {overriding}VALUES %s {conflict_clause}'

    s_cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
    total_read = 0
    total_attempted = 0
    batch: list[tuple] = []

    def flush(batch_rows):
        nonlocal total_attempted
        if not batch_rows:
            return
        values = [
            tuple(sanitize_value(row[c]) for c in insert_cols) for row in batch_rows
        ]
        psycopg2.extras.execute_values(pg_cur, sql, values, page_size=min(1000, len(values)))
        total_attempted += len(values)

    while True:
        rows = s_cur.fetchmany(BATCH_SIZE)
        if not rows:
            break
        total_read += len(rows)
        flush(rows)
        pg_conn.commit()

    if defer_cols:
        # Second pass: fill in the self-referencing columns now that every
        # company row exists.
        s_cur.execute(f"SELECT company_id, {', '.join(sorted(defer_cols))} FROM companies")
        rows = s_cur.fetchall()
        set_clause = ", ".join(f'"{c}" = %s' for c in sorted(defer_cols))
        update_sql = f"UPDATE companies SET {set_clause} WHERE company_id = %s"
        updates = []
        for row in rows:
            vals = [row[c] for c in sorted(defer_cols)]
            if any(v is not None for v in vals):
                updates.append((*vals, row["company_id"]))
        if updates:
            psycopg2.extras.execute_batch(pg_cur, update_sql, updates, page_size=500)
        pg_conn.commit()

    return total_read, total_attempted


def reset_identity_sequences(pg_conn) -> list[str]:
    """`OVERRIDING SYSTEM VALUE` (used above to preserve SQLite's original
    rowids so other tables' FKs keep resolving) inserts explicit ids WITHOUT
    advancing each table's underlying identity sequence -- so after a bulk
    migration the sequence is left wherever it was before (typically at 1),
    while real rows now occupy ids far past that. Left unfixed, the very
    next ordinary INSERT (no explicit id) on any of these tables collides
    with an already-migrated row (`UniqueViolation` on the PK). Must run
    once after the bulk load so future inserts resume from MAX(pk) + 1."""
    results = []
    with pg_conn.cursor() as cur:
        for table in IDENTITY_TABLES:
            pk = PRIMARY_KEYS.get(table)
            if not pk or len(pk) != 1:
                continue
            pk_col = pk[0]
            cur.execute(
                "SELECT setval(pg_get_serial_sequence(%s, %s), "
                "COALESCE((SELECT MAX(\"" + pk_col + "\") FROM " + table + "), 1), "
                "(SELECT MAX(\"" + pk_col + "\") FROM " + table + ") IS NOT NULL)",
                (table, pk_col),
            )
            new_val = cur.fetchone()["setval"]
            results.append(f"{table}.{pk_col}: sequence -> {new_val}")
    pg_conn.commit()
    return results


def verify_counts(sqlite_conn, pg_conn) -> list[tuple[str, int, int, bool]]:
    results = []
    s_cur = sqlite_conn.cursor()
    p_cur = pg_conn.cursor()
    for table in TABLE_ORDER:
        s_cur.execute(f"SELECT COUNT(*) FROM {table}")
        s_count = s_cur.fetchone()[0]
        p_cur.execute(f"SELECT COUNT(*) FROM {table}")
        p_count = p_cur.fetchone()["count"]
        results.append((table, s_count, p_count, s_count == p_count))
    return results


def verify_bytea_roundtrip(sqlite_conn, pg_conn) -> str:
    s_cur = sqlite_conn.cursor()
    s_cur.execute(
        "SELECT chunk_id, embedding FROM document_chunks WHERE embedding IS NOT NULL LIMIT 5"
    )
    rows = s_cur.fetchall()
    if not rows:
        return "no non-NULL embedding rows in source -- nothing to spot-check"
    p_cur = pg_conn.cursor()
    mismatches = []
    checked = 0
    for row in rows:
        p_cur.execute("SELECT embedding FROM document_chunks WHERE chunk_id = %s", (row["chunk_id"],))
        p_row = p_cur.fetchone()
        pg_bytes = bytes(p_row["embedding"]) if p_row and p_row["embedding"] is not None else None
        sqlite_bytes = bytes(row["embedding"])
        checked += 1
        if pg_bytes != sqlite_bytes:
            mismatches.append(row["chunk_id"])
    if mismatches:
        return f"MISMATCH on chunk_ids {mismatches} out of {checked} checked"
    return f"OK -- {checked} non-NULL embedding rows byte-for-byte identical"


def verify_fk_spot_checks(pg_conn) -> list[str]:
    p_cur = pg_conn.cursor()
    checks = [
        (
            "document_chunks.document_id -> documents",
            "SELECT COUNT(*) AS c FROM document_chunks dc "
            "LEFT JOIN documents d ON d.document_id = dc.document_id "
            "WHERE dc.document_id IS NOT NULL AND d.document_id IS NULL",
        ),
        (
            "knowledge_claims.document_id -> documents",
            "SELECT COUNT(*) AS c FROM knowledge_claims kc "
            "LEFT JOIN documents d ON d.document_id = kc.document_id WHERE d.document_id IS NULL",
        ),
        (
            "investigation_hypotheses.investigation_id -> investigations",
            "SELECT COUNT(*) AS c FROM investigation_hypotheses ih "
            "LEFT JOIN investigations i ON i.investigation_id = ih.investigation_id "
            "WHERE i.investigation_id IS NULL",
        ),
    ]
    results = []
    for label, sql in checks:
        p_cur.execute(sql)
        orphan_count = p_cur.fetchone()["c"]
        results.append(f"{label}: {orphan_count} orphans")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true", help="Skip migration, only run verification.")
    args = parser.parse_args()

    sqlite_conn = get_sqlite_conn()
    pg_conn = get_pg_conn()

    start = time.time()
    if not args.verify_only:
        print(f"Migrating {len(TABLE_ORDER)} tables from {SQLITE_PATH} -> Neon Postgres...")
        for table in TABLE_ORDER:
            t0 = time.time()
            attempts = 0
            while True:
                attempts += 1
                try:
                    read, attempted = migrate_table(sqlite_conn, pg_conn, table)
                    break
                except psycopg2.OperationalError as e:
                    print(f"  {table}: connection error on attempt {attempts} ({e}); reconnecting and retrying...")
                    try:
                        pg_conn.close()
                    except Exception:
                        pass
                    pg_conn = get_pg_conn()
                    if attempts >= 5:
                        raise
            print(f"  {table}: read {read}, inserted/attempted {attempted} ({time.time() - t0:.1f}s)")
        print(f"Migration pass complete in {time.time() - start:.1f}s")

        print("\n=== Resetting identity sequences (post OVERRIDING SYSTEM VALUE) ===")
        for line in reset_identity_sequences(pg_conn):
            print(" ", line)

    print("\n=== Row count verification (source vs dest) ===")
    results = verify_counts(sqlite_conn, pg_conn)
    total_src = total_dst = 0
    all_ok = True
    for table, s_count, p_count, ok in results:
        total_src += s_count
        total_dst += p_count
        all_ok = all_ok and ok
        flag = "OK" if ok else "MISMATCH"
        print(f"  {table:40s} sqlite={s_count:>8d}  postgres={p_count:>8d}  {flag}")
    print(f"  {'TOTAL':40s} sqlite={total_src:>8d}  postgres={total_dst:>8d}  {'OK' if all_ok else 'MISMATCH'}")

    print("\n=== BYTEA round-trip check (document_chunks.embedding) ===")
    print(" ", verify_bytea_roundtrip(sqlite_conn, pg_conn))

    print("\n=== FK spot-checks (should all be 0 orphans) ===")
    for line in verify_fk_spot_checks(pg_conn):
        print(" ", line)

    sqlite_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    main()
