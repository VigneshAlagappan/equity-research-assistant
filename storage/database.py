"""SQLite connection and schema initialization.

Repositories (README: Folder Structure -> storage/repositories.py) come in a
later phase. This module only owns connecting and creating the schema.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from config import settings
from config.settings import DEFAULT_SOURCES


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enforced and row access by name.

    db_path defaults to settings.DB_PATH, read at call time (not import time)
    so tests/callers can monkeypatch it — see ingestion/detector.py's RAW_DIR
    for the same pattern and why a bound default parameter doesn't work here.
    """
    db_path = db_path if db_path is not None else settings.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None, schema_path: Path | None = None) -> sqlite3.Connection:
    """Create all tables (if missing) and seed the sources table. Returns an open connection."""
    schema_path = schema_path if schema_path is not None else settings.SCHEMA_PATH
    schema_sql = schema_path.read_text()
    conn = get_connection(db_path)
    conn.executescript(schema_sql)
    _migrate_companies_website_column(conn)
    _migrate_companies_country_currency_columns(conn)
    _migrate_companies_fiscal_year_end_column(conn)
    _migrate_company_insights_history(conn)
    _migrate_users_theme_column(conn)
    _migrate_documents_table(conn)
    _migrate_documents_old_fk_references(conn)
    _migrate_document_chunks_fk_reference(conn)
    _migrate_documents_processing_status_columns(conn)
    _migrate_raw_file_paths_to_repo_relative(conn)
    _migrate_company_notes_updated_at(conn)
    _migrate_llm_call_log_columns(conn)
    _migrate_shareholding_observations_columns(conn)
    _migrate_investigations_as_of_column(conn)
    _migrate_investigation_companies(conn)
    _migrate_document_chunks_embedding_columns(conn)
    _migrate_generated_reports_question_embedding_columns(conn)
    _migrate_knowledge_relationships_target_index(conn)
    _seed_sources(conn)
    _migrate_source_trust_ranks(conn)
    _seed_sectors_and_industries(conn)
    _seed_index_definitions(conn)
    _seed_admin_user(conn)
    conn.commit()
    return conn


def _migrate_companies_website_column(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` above is a no-op on a companies table that
    already existed before these columns were added to the schema — ALTER
    TABLE is the only way to backfill them onto an existing database."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    if "website" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN website TEXT")
    if "valuation_model_file" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN valuation_model_file TEXT")
    if "macro_economic_sector" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN macro_economic_sector TEXT")
    if "basic_industry" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN basic_industry TEXT")


def _migrate_companies_country_currency_columns(conn: sqlite3.Connection) -> None:
    """Same reasoning as _migrate_companies_website_column — ALTER TABLE with
    a DEFAULT backfills every existing row to India/INR, which is correct
    for every company registered before multi-country support existed."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    if "country" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN country TEXT NOT NULL DEFAULT 'IN'")
    if "currency" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN currency TEXT NOT NULL DEFAULT 'INR'")


def _migrate_companies_fiscal_year_end_column(conn: sqlite3.Connection) -> None:
    """Same reasoning as _migrate_companies_country_currency_columns — ALTER
    TABLE with a DEFAULT backfills every existing row to 3 (March close),
    which is correct for every company registered before per-company fiscal
    years existed (all India, all Apr-Mar)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    if "fiscal_year_end_month" not in columns:
        conn.execute("ALTER TABLE companies ADD COLUMN fiscal_year_end_month INTEGER NOT NULL DEFAULT 3")


def _migrate_company_insights_history(conn: sqlite3.Connection) -> None:
    """company_insights originally had company_id as its PRIMARY KEY (one row
    per company, regenerate overwrote it). SQLite can't ALTER a primary key,
    so an existing database with that shape gets its table rebuilt here —
    existing rows are preserved, just no longer pinned to one-per-company.
    A no-op on a fresh database (already created with the new shape) or one
    already migrated (insight_id is already the primary key)."""
    columns = conn.execute("PRAGMA table_info(company_insights)").fetchall()
    if not columns:
        return
    primary_key_columns = [row["name"] for row in columns if row["pk"] > 0]
    if primary_key_columns == ["company_id"]:
        conn.execute("ALTER TABLE company_insights RENAME TO company_insights_old")
        conn.execute(
            """
            CREATE TABLE company_insights (
              insight_id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id TEXT NOT NULL REFERENCES companies(company_id),
              insight_text TEXT NOT NULL,
              statement_type TEXT NOT NULL,
              generated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO company_insights (company_id, insight_text, statement_type, generated_at)
            SELECT company_id, insight_text, statement_type, generated_at FROM company_insights_old
            """
        )
        conn.execute("DROP TABLE company_insights_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_company_insights_company_id ON company_insights(company_id, generated_at)"
        )


def _migrate_company_notes_updated_at(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a company_notes table that
    already existed before `updated_at` (edit support) was added."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(company_notes)")}
    if columns and "updated_at" not in columns:
        conn.execute("ALTER TABLE company_notes ADD COLUMN updated_at TEXT")


def _migrate_llm_call_log_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on an llm_call_log table that
    already existed before the Context Optimizer (context/optimizer.py) and
    Reuse-before-recompute (context/reuse.py) accounting columns were added
    — every research/assistant.py answer_question() call fails with
    "table llm_call_log has no column named ..." at the observability.record()
    step until these are backfilled, same pattern as the other _migrate_*
    functions above."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_call_log)")}
    if not columns:
        return
    if "context_tokens_before" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN context_tokens_before INTEGER")
    if "context_tokens_after" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN context_tokens_after INTEGER")
    if "context_items_dropped" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN context_items_dropped INTEGER")
    if "reuse_hit" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN reuse_hit INTEGER NOT NULL DEFAULT 0")
    if "reused_thread_id" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN reused_thread_id TEXT")
    if "cache_creation_input_tokens" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0")
    if "cache_read_input_tokens" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0")
    if "graph_hit" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN graph_hit INTEGER NOT NULL DEFAULT 0")
    if "graph_hit_thread_id" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN graph_hit_thread_id TEXT")
    if "graph_hit_score" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN graph_hit_score REAL")
    if "investigation_id" not in columns:
        conn.execute("ALTER TABLE llm_call_log ADD COLUMN investigation_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_call_log_investigation_id ON llm_call_log(investigation_id)")


def _migrate_shareholding_observations_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a shareholding_observations
    table that already existed before the FII/DII/Government/Public
    institutional-breakdown columns were added — same pattern as the other
    _migrate_* functions above. Empty `columns` means the table itself
    doesn't exist yet (a genuinely fresh DB, where CREATE TABLE just above
    already created it with every column) -- nothing to backfill."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(shareholding_observations)")}
    if not columns:
        return
    for column in ("fii_percent", "dii_percent", "government_percent", "public_non_institutional_percent"):
        if column not in columns:
            conn.execute(f"ALTER TABLE shareholding_observations ADD COLUMN {column} REAL")
    if "num_shareholders" not in columns:
        conn.execute("ALTER TABLE shareholding_observations ADD COLUMN num_shareholders INTEGER")


def _migrate_investigations_as_of_column(conn: sqlite3.Connection) -> None:
    """`as_of` (the point-in-time evidence cutoff an investigation was run
    under — research/temporal.py) was added after `investigations` shipped;
    ALTER TABLE backfills it as NULL, which is exactly the right value for
    every pre-existing investigation ("no cutoff, everything known at the
    time"). Same pattern as _migrate_companies_website_column."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(investigations)")}
    if columns and "as_of" not in columns:
        conn.execute("ALTER TABLE investigations ADD COLUMN as_of TEXT")


def _migrate_investigation_companies(conn: sqlite3.Connection) -> None:
    """`investigation_companies` (storage/investigation_repository.py) is
    created by the schema above, but an investigation saved before it existed
    has no rows in it and would silently vanish from its companies'
    Investigations sections. Backfill from the JSON `company_ids` column that
    has always been written. Idempotent (an anti-join finds nothing on the
    second run), so it can stay in init_db()'s unconditional migration list."""
    from storage.investigation_repository import backfill_investigation_companies

    backfill_investigation_companies(conn)


def _migrate_document_chunks_embedding_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a document_chunks table
    that already existed before the semantic-indexing status columns
    (retrieval/semantic_indexer.py) were added — ALTER TABLE backfills every
    existing chunk to embedding_status='pending' (embedding_model/embedded_at
    NULL), which is exactly right: every chunk indexed before the semantic
    layer existed has, correctly, never been embedded yet. Same pattern as
    _migrate_shareholding_observations_columns."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(document_chunks)")}
    if not columns:
        return
    if "embedding_status" not in columns:
        conn.execute("ALTER TABLE document_chunks ADD COLUMN embedding_status TEXT NOT NULL DEFAULT 'pending'")
    if "embedding_model" not in columns:
        conn.execute("ALTER TABLE document_chunks ADD COLUMN embedding_model TEXT")
    if "embedded_at" not in columns:
        conn.execute("ALTER TABLE document_chunks ADD COLUMN embedded_at TEXT")


def _migrate_generated_reports_question_embedding_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a generated_reports table
    that already existed before context/reuse.py's semantic reuse-matching
    layer was added -- ALTER TABLE backfills every existing report to
    question_embedding=NULL (embedding_model NULL too), which is exactly
    right: a report saved before this existed was, correctly, never
    embedded. find_reusable_report() falls back to word-overlap-only for a
    NULL-embedding report, same as if the embedding provider were down."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_reports)")}
    if not columns:
        return
    if "question_embedding" not in columns:
        conn.execute("ALTER TABLE generated_reports ADD COLUMN question_embedding TEXT")
    if "question_embedding_model" not in columns:
        conn.execute("ALTER TABLE generated_reports ADD COLUMN question_embedding_model TEXT")


def _migrate_knowledge_relationships_target_index(conn: sqlite3.Connection) -> None:
    """`CREATE INDEX IF NOT EXISTS` in schemas/sqlite_schema.sql only takes
    effect via `conn.executescript(schema_sql)` above on a database that
    doesn't already have knowledge_relationships from before this index was
    added — SQLite's schema script re-execution is a no-op for anything
    already created, index included, on a genuinely fresh database, but an
    existing database's on-disk schema was captured before this index
    existed and never automatically picks up an addition to the .sql file.
    Same idempotent `CREATE INDEX IF NOT EXISTS` shape as every other
    _migrate_* function here — the multi-hop BFS
    (context/knowledge_graph.py::find_multi_hop_claims()) needs this reverse-
    direction lookup on knowledge_relationships(target_entity_id) or every
    "who points AT this entity" neighbor query full-scans the table."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_relationships_target ON knowledge_relationships(target_entity_id)"
    )


def _migrate_users_theme_column(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a users table that already
    existed before `theme` was added — ALTER TABLE backfills it, same
    pattern as _migrate_companies_website_column."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if columns and "theme" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'schwab'")


def _migrate_documents_table(conn: sqlite3.Connection) -> None:
    """documents originally had raw_file_path as NOT NULL and no
    added_by_user column — a link-only Docs-tab entry (no uploaded file)
    needs raw_file_path to be nullable, and added_by_user distinguishes a
    manually-added document from an officially-sourced one. SQLite can't
    relax a NOT NULL constraint via ALTER TABLE, so rebuild; safe
    unconditionally because nothing has ever inserted into this table (a
    fresh CREATE TABLE IF NOT EXISTS already has the new shape, so this
    only ever fires once, against a pre-existing empty table)."""
    columns = conn.execute("PRAGMA table_info(documents)").fetchall()
    if not columns:
        return
    raw_file_path_col = next((c for c in columns if c["name"] == "raw_file_path"), None)
    has_added_by_user = any(c["name"] == "added_by_user" for c in columns)
    if raw_file_path_col is not None and raw_file_path_col["notnull"] == 0 and has_added_by_user:
        return
    conn.execute("ALTER TABLE documents RENAME TO documents_old")
    conn.execute(
        """
        CREATE TABLE documents (
          document_id INTEGER PRIMARY KEY,
          company_id TEXT REFERENCES companies(company_id),
          source TEXT REFERENCES sources(source_id),
          document_type TEXT,
          fiscal_year TEXT,
          quarter TEXT,
          published_at TEXT,
          retrieved_at TEXT,
          raw_file_path TEXT,
          file_hash TEXT,
          source_url TEXT,
          parser_version TEXT,
          added_by_user TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO documents (document_id, company_id, source, document_type, fiscal_year, quarter,
                                published_at, retrieved_at, raw_file_path, file_hash, source_url, parser_version)
        SELECT document_id, company_id, source, document_type, fiscal_year, quarter,
               published_at, retrieved_at, raw_file_path, file_hash, source_url, parser_version
        FROM documents_old
        """
    )
    conn.execute("DROP TABLE documents_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id, document_type)")


def _migrate_documents_old_fk_references(conn: sqlite3.Connection) -> None:
    """SQLite's `ALTER TABLE documents RENAME TO documents_old` above
    silently rewrites *every other table's* stored schema text too, pointing
    any `REFERENCES documents(...)` clause at `documents_old` instead — a
    documented side effect, not a bug in this migration, but a real bug in
    financial_observations never getting fixed back up afterward: once
    documents_old is dropped, any INSERT that touches financial_observations
    at all raises "no such table: main.documents_old" (SQLite resolves the
    referenced table at insert time regardless of whether the FK column's
    value is NULL), on every environment that ever ran the migration above.
    Detected by checking the stored schema text directly (PRAGMA table_info
    can't see a stale FK target); a no-op on any database that never had the
    rename happen at all (fresh installs).

    document_chunks has the exact same stale reference — fixed separately in
    _migrate_document_chunks_fk_reference() below, once
    research/document_chunker.py (Step 2D) became the first thing to ever
    write to that table and actually hit the bug."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='financial_observations'"
    ).fetchone()
    if row is None or "documents_old" not in row["sql"]:
        return

    # canonical_financials/reconciliation_log both hold a real, live
    # `REFERENCES financial_observations(...)` with real dependent rows
    # (reconciliation really does set chosen_observation_id) — renaming
    # financial_observations itself (the naive fix) makes SQLite silently
    # rewrite *their* schema text to point at whatever the new name is,
    # reintroducing this exact bug one hop further out. Building the fixed
    # table under a temp name first, dropping the old one, then renaming
    # the temp table into place sidesteps that: nothing else references
    # "financial_observations_fixed" by that name, so nothing else's schema
    # gets touched, and the final rename lands back on the exact name
    # canonical_financials/reconciliation_log already correctly reference.
    # foreign_keys is off for the swap since canonical_financials has real
    # rows referencing observation_ids that momentarily don't exist between
    # the DROP and the final rename (the same ids exist again immediately
    # after — nothing is actually orphaned).
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE financial_observations_fixed (
          observation_id INTEGER PRIMARY KEY,
          company_id TEXT NOT NULL REFERENCES companies(company_id),
          metric_key TEXT NOT NULL REFERENCES metrics_dictionary(metric_key),
          period_type TEXT NOT NULL,
          fiscal_year TEXT NOT NULL,
          quarter TEXT,
          statement_type TEXT,
          value REAL NOT NULL,
          unit TEXT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'INR',
          source TEXT NOT NULL REFERENCES sources(source_id),
          source_document_id INTEGER REFERENCES documents(document_id),
          source_file TEXT,
          source_url TEXT,
          retrieved_at TEXT NOT NULL,
          parser_version TEXT NOT NULL,
          normalization_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO financial_observations_fixed
        SELECT observation_id, company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
               value, unit, currency, source, source_document_id, source_file, source_url, retrieved_at,
               parser_version, normalization_version, created_at
        FROM financial_observations
        """
    )
    conn.execute("DROP TABLE financial_observations")
    conn.execute("ALTER TABLE financial_observations_fixed RENAME TO financial_observations")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_lookup ON financial_observations(company_id, metric_key, fiscal_year)")


def _migrate_document_chunks_fk_reference(conn: sqlite3.Connection) -> None:
    """document_chunks has the same stale `REFERENCES documents_old(...)`
    _migrate_documents_old_fk_references() already fixes for
    financial_observations (see that function's docstring for the root
    cause) — left unfixed until now because nothing ever wrote to
    document_chunks before research/document_chunker.py (Step 2D).
    Simpler than the financial_observations fix: nothing else holds a live
    FK reference to document_chunks the way canonical_financials/
    reconciliation_log do to financial_observations, so no cascading
    rewrite to guard against — just rebuild under a temp name and rename
    into place. document_chunks_fts (the FTS5 virtual table) references
    document_chunks by name, not a real FK — its own stored definition is
    untouched by this, and resolves correctly against the rebuilt table
    once it's renamed back to "document_chunks"."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_chunks'"
    ).fetchone()
    if row is None or "documents_old" not in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE document_chunks_fixed (
          chunk_id INTEGER PRIMARY KEY,
          document_id INTEGER REFERENCES documents(document_id),
          company_id TEXT,
          section_heading TEXT,
          page_number INTEGER,
          chunk_index INTEGER,
          text TEXT NOT NULL,
          embedding BLOB,
          created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO document_chunks_fixed
        SELECT chunk_id, document_id, company_id, section_heading, page_number, chunk_index, text, embedding, created_at
        FROM document_chunks
        """
    )
    conn.execute("DROP TABLE document_chunks")
    conn.execute("ALTER TABLE document_chunks_fixed RENAME TO document_chunks")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_documents_processing_status_columns(conn: sqlite3.Connection) -> None:
    """Same reasoning as _migrate_companies_website_column — ALTER TABLE
    backfills every existing document row to 'pending', which is correct:
    no document was ever processed by the (new) Ingest queue before this
    column existed, so every one of them is genuinely still pending."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if not columns:
        return
    if "processing_status" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'pending'")
    if "processed_at" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN processed_at TEXT")
    if "error_message" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN error_message TEXT")


def _migrate_raw_file_paths_to_repo_relative(conn: sqlite3.Connection) -> None:
    """documents.raw_file_path and company_note_attachments.raw_file_path
    used to be stored as absolute paths (config.settings.to_repo_relative/
    from_repo_relative's docstring explains why that's wrong) — an absolute
    path bakes in the repo folder's name/location at write time, and breaks
    every stored reference the moment the repo is renamed or moved (as this
    one already has been: "indian-equity-research-assistant" ->
    "equity-research-assistant"). Rewrites any row already stored absolute
    to the same repo-relative form new writes use.

    Robust to ANY historical absolute prefix, not just the one rename this
    app has actually been through: every path this app ever wrote under
    DOCUMENTS_DIR necessarily contains "data/documents/" as a structural
    fragment (that folder layout itself was never renamed, only the outer
    repo directory), so slicing the stored string from the first occurrence
    of "data/documents/" onward recovers the correct relative path
    regardless of what came before it. A row not matching that shape is
    left untouched — most likely already relative, or a genuinely unusual
    value not worth guessing at.
    """
    marker = "data/documents/"
    # SQLite labels an INTEGER PRIMARY KEY's rowid alias by its declared
    # column name in query results, not literally "rowid" — select each
    # table's real primary key column by name rather than relying on that.
    pk_columns = {"documents": "document_id", "company_note_attachments": "attachment_id"}
    for table, pk_column in pk_columns.items():
        rows = conn.execute(
            f"SELECT {pk_column}, raw_file_path FROM {table} "
            f"WHERE raw_file_path IS NOT NULL AND raw_file_path LIKE '/%'"
        ).fetchall()
        for row in rows:
            raw_path = row["raw_file_path"]
            marker_at = raw_path.find(marker)
            if marker_at == -1:
                continue
            conn.execute(
                f"UPDATE {table} SET raw_file_path = ? WHERE {pk_column} = ?",
                (raw_path[marker_at:], row[pk_column]),
            )
    conn.commit()


def _migrate_source_trust_ranks(conn: sqlite3.Connection) -> None:
    """sources is seeded with INSERT OR IGNORE (_seed_sources, below), so a
    row already seeded on a prior run never picks up a later trust_rank
    correction on its own. config/settings.py promoted nse/bse from
    trust_rank 2 to 0 (NSE XBRL as the target source of truth for
    structured financial facts, ahead of the hand-curated 'proprietary'
    tier it used to sit below) — propagate that to any database that
    already has the old value. trust_rank/description are code-owned
    config with no admin-editable UI, so an unconditional UPDATE (not a
    conditional backfill) is the correct way to keep an existing database
    in sync with DEFAULT_SOURCES, same as this function will need to do
    again the next time a rank changes."""
    conn.executemany(
        "UPDATE sources SET trust_rank = :trust_rank, description = :description "
        "WHERE source_id = :source_id AND source_id IN ('nse', 'bse')",
        DEFAULT_SOURCES,
    )


def _seed_sources(conn: sqlite3.Connection) -> None:
    """Insert the default source rows, leaving any existing rows untouched."""
    conn.executemany(
        """
        INSERT OR IGNORE INTO sources (source_id, name, trust_rank, description)
        VALUES (:source_id, :name, :trust_rank, :description)
        """,
        DEFAULT_SOURCES,
    )


def _seed_sectors_and_industries(conn: sqlite3.Connection) -> None:
    """Backfill the sectors/industries lookup tables from whatever values
    companies already use — INSERT OR IGNORE, so this only ever adds rows
    once (on a database that predates these tables) and never touches an
    admin's own later add/rename/delete."""
    now = utcnow_iso()
    conn.execute(
        "INSERT OR IGNORE INTO sectors (name, created_at) "
        "SELECT DISTINCT sector, ? FROM companies WHERE sector IS NOT NULL AND sector != ''",
        (now,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO industries (name, created_at) "
        "SELECT DISTINCT industry, ? FROM companies WHERE industry IS NOT NULL AND industry != ''",
        (now,),
    )


def _seed_index_definitions(conn: sqlite3.Connection) -> None:
    """Backfill index_definitions from config.settings.INDEX_NAMES (imported
    here, not at module level, to avoid a database.py <-> repositories.py
    import cycle — repositories.py already imports utcnow_iso from this
    module). INSERT OR IGNORE, same reasoning as _seed_sectors_and_industries."""
    from config.settings import INDEX_NAMES

    now = utcnow_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO index_definitions (name, created_at) VALUES (?, ?)",
        [(name, now) for name in INDEX_NAMES],
    )


def _seed_admin_user(conn: sqlite3.Connection) -> None:
    """One built-in admin account (username "admin", password "admin") so the
    app is usable on first run without a separate bootstrap step. INSERT OR
    IGNORE — a later password change (if ever added) wouldn't be clobbered
    back to "admin" on every app start."""
    conn.execute(
        """
        INSERT OR IGNORE INTO users (username, password_hash, is_admin, created_at)
        VALUES ('admin', ?, 1, ?)
        """,
        (generate_password_hash("admin"), utcnow_iso()),
    )


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every user table/virtual-table name in the database, sorted."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(row["name"] for row in rows)


def utcnow_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string, for created_at/updated_at columns."""
    return datetime.now(timezone.utc).isoformat()
