"""Repository layer: insert observations, reconcile them into canonical_financials.

Conflicting/duplicate observations are never overwritten (README: Source /
Provenance & Reconciliation) — every insert is a new financial_observations
row; reconciliation only decides which one canonical_financials points at,
and records that decision (and every candidate it considered) separately.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable

from sources.base import NormalizedObservation
from sources.macro import MacroNormalizedObservation
from sources.rbi_bank_infrastructure import BankInfrastructureObservation
from storage.database import utcnow_iso

NORMALIZATION_VERSION = "v1"


def insert_financial_observations(
    conn: sqlite3.Connection, observations: Iterable[NormalizedObservation]
) -> list[int]:
    """Insert each observation as a new row. Returns the assigned observation_ids."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO financial_observations (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                value, unit, currency, source, source_document_id, source_file, source_url,
                retrieved_at, parser_version, normalization_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter,
                obs.statement_type, obs.value, obs.unit, obs.currency, obs.source, obs.source_file,
                obs.source_url, obs.retrieved_at or now, obs.parser_version, NORMALIZATION_VERSION, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def reconcile(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None,
    statement_type: str | None,
) -> int | None:
    """Reconcile every observation for one (company, metric, period) key.

    Phase 2 has exactly one source per company, so this degenerates to the
    trivial pass-through the README describes ("only source available"). The
    same logic already generalizes to multiple sources (picks the lowest
    sources.trust_rank, most-recent observation as tiebreak) for when NSE/BSE
    ingestion lands in a later phase — it isn't special-cased to "one source".
    Returns the canonical_id, or None if there are no observations for this key.
    """
    rows = conn.execute(
        """
        SELECT fo.observation_id, fo.value, fo.unit, fo.source, fo.retrieved_at, s.trust_rank
        FROM financial_observations fo
        JOIN sources s ON s.source_id = fo.source
        WHERE fo.company_id = ? AND fo.metric_key = ? AND fo.period_type = ?
          AND fo.fiscal_year = ? AND fo.quarter IS ? AND fo.statement_type IS ?
        ORDER BY fo.retrieved_at ASC, fo.observation_id ASC
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchall()
    if not rows:
        return None

    # Most recent observation per source (rows are ordered oldest-to-newest,
    # tie-broken on the strictly-increasing observation_id since wall-clock
    # retrieved_at can collide at the OS clock's resolution between two
    # back-to-back ingestions — so the last write per source wins, and a
    # re-ingested file reliably supersedes the old one).
    latest_per_source: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest_per_source[row["source"]] = row
    candidates = list(latest_per_source.values())

    def sort_key(row: sqlite3.Row) -> tuple[int, str, int]:
        rank = row["trust_rank"] if row["trust_rank"] is not None else 999
        return (rank, row["retrieved_at"], row["observation_id"])

    chosen = min(candidates, key=sort_key)
    reason = (
        "only source available"
        if len(candidates) == 1
        else f"source '{chosen['source']}' preferred by trust_rank"
    )

    # SQLite's UNIQUE constraint (and therefore ON CONFLICT) never fires when
    # a NULL participates in the unique columns — quarter/statement_type are
    # NULL for annual observations, so "INSERT ... ON CONFLICT DO UPDATE"
    # would silently insert a duplicate canonical row every re-run instead of
    # updating the existing one. Do the upsert explicitly with a NULL-safe
    # (IS) lookup instead.
    now = utcnow_iso()
    existing = conn.execute(
        """
        SELECT canonical_id FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ?
          AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchone()

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO canonical_financials (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                canonical_value, unit, chosen_observation_id, reconciliation_reason,
                normalization_version, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                chosen["value"], chosen["unit"], chosen["observation_id"], reason,
                NORMALIZATION_VERSION, now,
            ),
        )
        canonical_id = cursor.lastrowid
    else:
        canonical_id = existing["canonical_id"]
        conn.execute(
            """
            UPDATE canonical_financials SET
                canonical_value = ?, unit = ?, chosen_observation_id = ?,
                reconciliation_reason = ?, normalization_version = ?, decided_at = ?
            WHERE canonical_id = ?
            """,
            (chosen["value"], chosen["unit"], chosen["observation_id"], reason,
             NORMALIZATION_VERSION, now, canonical_id),
        )

    for row in candidates:
        was_chosen = row["observation_id"] == chosen["observation_id"]
        conn.execute(
            """
            INSERT INTO reconciliation_log (canonical_id, observation_id, considered_at, was_chosen, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                canonical_id, row["observation_id"], now, 1 if was_chosen else 0,
                reason if was_chosen else f"not chosen (source={row['source']}, trust_rank={row['trust_rank']})",
            ),
        )
    conn.commit()
    return canonical_id


def get_canonical_value(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None = None,
    statement_type: str | None = "consolidated",
) -> sqlite3.Row | None:
    """Fetch the single reconciled value for one (company, metric, period) key."""
    return conn.execute(
        """
        SELECT * FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ?
          AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchone()


def get_canonical_series(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> list[sqlite3.Row]:
    """Fetch every reconciled value for a metric, oldest to newest.

    Ordered by fiscal_year, then quarter (Q1..Q4) for quarterly series — plain
    lexicographic fiscal_year ordering works because "FY2024" < "FY2025" sorts
    correctly as text.
    """
    return conn.execute(
        """
        SELECT * FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ? AND statement_type IS ?
        ORDER BY fiscal_year ASC, quarter ASC
        """,
        (company_id, metric_key, period_type, statement_type),
    ).fetchall()


def reconcile_batch(conn: sqlite3.Connection, observations: Iterable[NormalizedObservation]) -> int:
    """Reconcile every distinct (company, metric, period) key touched by a batch of observations."""
    keys = {
        (obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter, obs.statement_type)
        for obs in observations
    }
    return sum(1 for key in keys if reconcile(conn, *key) is not None)


def reconcile_company(conn: sqlite3.Connection, company_id: str) -> int:
    """Re-derive canonical_financials for every (metric, period) key this
    company has any observation for, from what's already stored — no new
    upload needed. For when metric_aliases or a source's trust_rank changes
    after the fact and existing data should reflect it, not just future
    ingests (which already re-reconcile the periods they touch via
    reconcile_batch — this is the same logic, just re-run on demand over
    everything on file instead of only a fresh batch's own periods)."""
    keys = conn.execute(
        """
        SELECT DISTINCT metric_key, period_type, fiscal_year, quarter, statement_type
        FROM financial_observations WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()
    return sum(
        1
        for row in keys
        if reconcile(
            conn, company_id, row["metric_key"], row["period_type"],
            row["fiscal_year"], row["quarter"], row["statement_type"],
        )
        is not None
    )


WATCHLIST_ITEM_TYPES = ("company", "thread")


def add_watchlist_item(conn: sqlite3.Connection, item_type: str, item_ref: str) -> int:
    """Pin a company or research thread. Re-pinning an already-pinned item is a no-op
    (README: single shared watchlist, so there's no per-user pin to distinguish)."""
    if item_type not in WATCHLIST_ITEM_TYPES:
        raise ValueError(f"item_type must be one of {WATCHLIST_ITEM_TYPES}, got {item_type!r}")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watchlist_items (item_type, item_ref, pinned_at) VALUES (?, ?, ?)",
        (item_type, item_ref, utcnow_iso()),
    )
    conn.commit()
    if cursor.lastrowid and cursor.rowcount:
        return cursor.lastrowid
    return conn.execute(
        "SELECT item_id FROM watchlist_items WHERE item_type = ? AND item_ref = ?",
        (item_type, item_ref),
    ).fetchone()["item_id"]


def remove_watchlist_item(conn: sqlite3.Connection, item_type: str, item_ref: str) -> None:
    conn.execute(
        "DELETE FROM watchlist_items WHERE item_type = ? AND item_ref = ?", (item_type, item_ref)
    )
    conn.commit()


def list_watchlist_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most recently pinned first."""
    return conn.execute(
        "SELECT * FROM watchlist_items ORDER BY pinned_at DESC"
    ).fetchall()


def is_watchlisted(conn: sqlite3.Connection, item_type: str, item_ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM watchlist_items WHERE item_type = ? AND item_ref = ?", (item_type, item_ref)
    ).fetchone()
    return row is not None


def get_company_insights(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    """Most recently generated insight for this company, if any."""
    return conn.execute(
        "SELECT * FROM company_insights WHERE company_id = ? ORDER BY generated_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()


def list_company_insights(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every generated insight for this company, most recent first."""
    return conn.execute(
        "SELECT * FROM company_insights WHERE company_id = ? ORDER BY generated_at DESC",
        (company_id,),
    ).fetchall()


def save_company_insights(conn: sqlite3.Connection, company_id: str, insight_text: str, statement_type: str) -> None:
    """Every generate/regenerate inserts a new row — company_insights keeps
    history against generated_at rather than overwriting (README: Overview tab,
    user-triggered, never auto-regenerated)."""
    conn.execute(
        "INSERT INTO company_insights (company_id, insight_text, statement_type, generated_at) VALUES (?, ?, ?, ?)",
        (company_id, insight_text, statement_type, utcnow_iso()),
    )
    conn.commit()


def list_company_notes(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every personal note logged against this company, most recent first."""
    return conn.execute(
        "SELECT * FROM company_notes WHERE company_id = ? ORDER BY created_at DESC",
        (company_id,),
    ).fetchall()


def save_company_note(conn: sqlite3.Connection, company_id: str, note_text: str) -> sqlite3.Row:
    """Append-only — no edit/delete yet, same as company_insights history."""
    cursor = conn.execute(
        "INSERT INTO company_notes (company_id, note_text, created_at) VALUES (?, ?, ?)",
        (company_id, note_text, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM company_notes WHERE note_id = ?", (cursor.lastrowid,)).fetchone()


def update_company_note(conn: sqlite3.Connection, company_id: str, note_id: int, note_text: str) -> sqlite3.Row | None:
    """None if no note with that id exists for this company — the route
    treats that as a 404, not a silent no-op."""
    cursor = conn.execute(
        "UPDATE company_notes SET note_text = ?, updated_at = ? WHERE note_id = ? AND company_id = ?",
        (note_text, utcnow_iso(), note_id, company_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return conn.execute("SELECT * FROM company_notes WHERE note_id = ?", (note_id,)).fetchone()


def delete_company_note(conn: sqlite3.Connection, company_id: str, note_id: int) -> bool:
    cursor = conn.execute("DELETE FROM company_notes WHERE note_id = ? AND company_id = ?", (note_id, company_id))
    conn.commit()
    return cursor.rowcount > 0


def list_note_attachments(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE note_id = ? ORDER BY uploaded_at", (note_id,)
    ).fetchall()


def list_note_attachments_for_company(conn: sqlite3.Connection, company_id: str) -> dict[int, list[sqlite3.Row]]:
    """Every attachment for every note this company has, grouped by note_id —
    one query for the whole Notes tab instead of one per note."""
    rows = conn.execute(
        """
        SELECT a.* FROM company_note_attachments a
        JOIN company_notes n ON n.note_id = a.note_id
        WHERE n.company_id = ?
        ORDER BY a.uploaded_at
        """,
        (company_id,),
    ).fetchall()
    by_note: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_note.setdefault(row["note_id"], []).append(row)
    return by_note


def get_note_attachment(conn: sqlite3.Connection, note_id: int, attachment_id: int) -> sqlite3.Row | None:
    """Scoped by note_id too, same reasoning as get_company_document."""
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE attachment_id = ? AND note_id = ?",
        (attachment_id, note_id),
    ).fetchone()


def save_note_attachment(
    conn: sqlite3.Connection, note_id: int, filename: str, raw_file_path: str, size_bytes: int
) -> sqlite3.Row:
    cursor = conn.execute(
        "INSERT INTO company_note_attachments (note_id, filename, raw_file_path, size_bytes, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (note_id, filename, raw_file_path, size_bytes, utcnow_iso()),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE attachment_id = ?", (cursor.lastrowid,)
    ).fetchone()


def delete_note_attachment(conn: sqlite3.Connection, note_id: int, attachment_id: int) -> sqlite3.Row | None:
    """Returns the deleted row (so the caller can remove its on-disk file
    too) or None if it didn't exist under this note_id."""
    row = get_note_attachment(conn, note_id, attachment_id)
    if row is None:
        return None
    conn.execute(
        "DELETE FROM company_note_attachments WHERE attachment_id = ? AND note_id = ?", (attachment_id, note_id)
    )
    conn.commit()
    return row


def list_company_periods(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every (fiscal_year, quarter) this company actually has quarterly
    financials for — the Docs tab's real period list, in place of a
    hardcoded range. Companies without quarterly-granularity ingestion
    (e.g. Proprietary-adapter-only, or sources/yfinance_financials.py's
    annual-only pilot) get an empty list, not a fabricated one."""
    return conn.execute(
        """
        SELECT DISTINCT fiscal_year, quarter FROM canonical_financials
        WHERE company_id = ? AND period_type = 'quarterly' AND quarter IS NOT NULL
        ORDER BY fiscal_year, quarter
        """,
        (company_id,),
    ).fetchall()


def list_company_annual_years(conn: sqlite3.Connection, company_id: str) -> list[str]:
    """Every fiscal_year this company has *annual* financials for — separate
    from list_company_periods (quarterly) so the Docs tab's year groups can
    exist for an annual-only company (no quarterly ingestion at all, e.g.
    every company sources/yfinance_financials.py has ingested so far) instead
    of the year list being entirely gated on quarterly data existing."""
    rows = conn.execute(
        "SELECT DISTINCT fiscal_year FROM canonical_financials WHERE company_id = ? AND period_type = 'annual'",
        (company_id,),
    ).fetchall()
    return [row["fiscal_year"] for row in rows]


def list_company_documents(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every document on file for this company — official and manually-added
    alike (added_by_user is NULL for the former)."""
    return conn.execute(
        "SELECT * FROM documents WHERE company_id = ? ORDER BY fiscal_year, quarter",
        (company_id,),
    ).fetchall()


def get_company_document(conn: sqlite3.Connection, company_id: str, document_id: int) -> sqlite3.Row | None:
    """Scoped by company_id too — a document's id alone shouldn't be enough
    to serve someone else's file from a mistyped/guessed URL."""
    return conn.execute(
        "SELECT * FROM documents WHERE document_id = ? AND company_id = ?",
        (document_id, company_id),
    ).fetchone()


def save_company_document(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    document_type: str,
    fiscal_year: str,
    quarter: str | None,
    added_by_user: str,
    raw_file_path: str | None = None,
    source_url: str | None = None,
) -> sqlite3.Row:
    """Manually-added documents only, via the Docs tab's Add form —
    officially-sourced rows (added_by_user NULL) would come from a future
    data-provider ingestion path, which doesn't exist yet."""
    now = utcnow_iso()
    cursor = conn.execute(
        """
        INSERT INTO documents (company_id, document_type, fiscal_year, quarter,
                                raw_file_path, source_url, added_by_user, retrieved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, document_type, fiscal_year, quarter, raw_file_path, source_url, added_by_user, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (cursor.lastrowid,)).fetchone()


def list_documents_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Every document across every company at this processing_status —
    unlike list_company_documents, not scoped to one company, since the
    Admin -> Ingest queue view spans the whole document archive."""
    return conn.execute(
        "SELECT * FROM documents WHERE processing_status = ? ORDER BY retrieved_at DESC",
        (status,),
    ).fetchall()


def mark_document_processing_status(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    status: str,
    file_hash: str | None = None,
    processed_at: str | None = None,
    error_message: str | None = None,
) -> sqlite3.Row | None:
    """file_hash is only overwritten when explicitly given (e.g. computed
    fresh during Ingest queue processing) — passing None leaves whatever's
    already stored on the row untouched, not wiped. error_message is always
    written (including None, to clear a stale one on a later success)."""
    if file_hash is not None:
        conn.execute(
            "UPDATE documents SET processing_status = ?, processed_at = ?, file_hash = ?, error_message = ? WHERE document_id = ?",
            (status, processed_at, file_hash, error_message, document_id),
        )
    else:
        conn.execute(
            "UPDATE documents SET processing_status = ?, processed_at = ?, error_message = ? WHERE document_id = ?",
            (status, processed_at, error_message, document_id),
        )
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()


def list_ingestion_queue_items(
    conn: sqlite3.Connection, *, status: str | None = None, item_kind: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM ingestion_queue_items WHERE 1=1"
    params: list[object] = []
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if item_kind is not None:
        query += " AND item_kind = ?"
        params.append(item_kind)
    query += " ORDER BY discovered_at DESC"
    return conn.execute(query, params).fetchall()


def get_ingestion_queue_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ingestion_queue_items WHERE item_id = ?", (item_id,)).fetchone()


def get_ingestion_queue_item_by_path(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ingestion_queue_items WHERE file_path = ?", (file_path,)).fetchone()


def upsert_ingestion_queue_item(
    conn: sqlite3.Connection,
    *,
    item_kind: str,
    file_path: str,
    content_hash: str | None,
    company_id: str | None,
    source_id: str | None,
    status: str,
    status_reason: str | None,
) -> sqlite3.Row:
    """Insert a newly-discovered file, or refresh an existing row's
    detection/hash — discovery is a full rescan every time, not an
    append-only log, so re-running it must update in place, not duplicate.
    Deliberately does NOT touch status/last_processed_content_hash/
    processed_at for a row that's already PROCESSED with an unchanged
    content_hash — see ingestion/coordinator.py's discover_pending_
    financial_items, which decides that before calling this."""
    now = utcnow_iso()
    existing = get_ingestion_queue_item_by_path(conn, file_path)
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO ingestion_queue_items (
                item_kind, file_path, content_hash, company_id, source_id,
                status, status_reason, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_kind, file_path, content_hash, company_id, source_id, status, status_reason, now),
        )
        item_id = cursor.lastrowid
    else:
        conn.execute(
            """
            UPDATE ingestion_queue_items SET
                item_kind = ?, content_hash = ?, company_id = ?, source_id = ?,
                status = ?, status_reason = ?
            WHERE item_id = ?
            """,
            (item_kind, content_hash, company_id, source_id, status, status_reason, existing["item_id"]),
        )
        item_id = existing["item_id"]
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def update_ingestion_queue_item_result(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    status: str,
    error_message: str | None = None,
    processed_at: str | None = None,
    last_processed_content_hash: str | None = None,
) -> sqlite3.Row | None:
    """Records the outcome of one processing attempt — always stamps
    last_attempt_at; processed_at/last_processed_content_hash are only set
    on success (callers pass them then), left untouched on failure."""
    conn.execute(
        """
        UPDATE ingestion_queue_items SET
            status = ?, error_message = ?, last_attempt_at = ?,
            processed_at = COALESCE(?, processed_at),
            last_processed_content_hash = COALESCE(?, last_processed_content_hash)
        WHERE item_id = ?
        """,
        (status, error_message, utcnow_iso(), processed_at, last_processed_content_hash, item_id),
    )
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def save_investigation(
    conn: sqlite3.Connection,
    *,
    investigation_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    strongest_explanation: str | None,
    unanswered_questions: list[str],
    additional_evidence_needed: list[str],
) -> None:
    conn.execute(
        "INSERT INTO investigations (investigation_id, question, company_ids, statement_type, "
        "strongest_explanation, unanswered_questions, additional_evidence_needed, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (investigation_id, question, json.dumps(company_ids), statement_type, strongest_explanation,
         json.dumps(unanswered_questions), json.dumps(additional_evidence_needed), utcnow_iso()),
    )
    conn.commit()


def save_investigation_hypothesis(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: str,
    investigation_id: str,
    statement: str,
    mechanism: str | None,
    category: str,
    rationale: str | None,
    unknowns: list[str],
    generation_order: int,
    verdict: str | None = None,
    confidence_basis: str | None = None,
    synthesis_rank: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO investigation_hypotheses (hypothesis_id, investigation_id, statement, mechanism, "
        "category, rationale, unknowns, generation_order, verdict, confidence_basis, synthesis_rank, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hypothesis_id, investigation_id, statement, mechanism, category, rationale, json.dumps(unknowns),
         generation_order, verdict, confidence_basis, synthesis_rank, utcnow_iso()),
    )
    conn.commit()


def save_investigation_hypothesis_evidence(
    conn: sqlite3.Connection, hypothesis_id: str, evidence: list[dict]
) -> None:
    """evidence: list of {stance, kind, label, value, citation} dicts —
    plain dicts, not a dataclass, same reasoning save_report_evidence()
    already gives for research_thread_evidence."""
    conn.executemany(
        "INSERT INTO investigation_hypothesis_evidence (hypothesis_id, stance, kind, label, value, citation) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(hypothesis_id, e["stance"], e["kind"], e["label"], e.get("value"), e.get("citation")) for e in evidence],
    )
    conn.commit()


def get_investigation(conn: sqlite3.Connection, investigation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = ?", (investigation_id,)
    ).fetchone()


def list_investigations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM investigations ORDER BY generated_at DESC").fetchall()


def list_investigation_hypotheses(conn: sqlite3.Connection, investigation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_hypotheses WHERE investigation_id = ? ORDER BY "
        "CASE WHEN synthesis_rank IS NULL THEN 1 ELSE 0 END, synthesis_rank, generation_order",
        (investigation_id,),
    ).fetchall()


def list_investigation_hypothesis_evidence(conn: sqlite3.Connection, hypothesis_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_hypothesis_evidence WHERE hypothesis_id = ? ORDER BY id", (hypothesis_id,)
    ).fetchall()


def get_or_create_knowledge_entity(
    conn: sqlite3.Connection, entity_type: str, name: str, company_id: str | None = None
) -> sqlite3.Row:
    """Entities are shared/deduped across every claim that mentions them
    (UNIQUE(entity_type, name, company_id)) — unlike knowledge_claims,
    which is always append-only, the same "Product: iPhone" entity
    shouldn't get a new row every time a new document mentions it again."""
    existing = conn.execute(
        "SELECT * FROM knowledge_entities WHERE entity_type = ? AND name = ? AND company_id IS ?",
        (entity_type, name, company_id),
    ).fetchone()
    if existing is not None:
        return existing
    cursor = conn.execute(
        "INSERT INTO knowledge_entities (entity_type, name, company_id, created_at) VALUES (?, ?, ?, ?)",
        (entity_type, name, company_id, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM knowledge_entities WHERE entity_id = ?", (cursor.lastrowid,)).fetchone()


def insert_knowledge_claim(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    company_id: str | None,
    claim_type: str,
    category: str | None,
    claim_text: str,
    speaker: str | None,
    fiscal_year: str | None,
    quarter: str | None,
    extraction_confidence: float | None,
) -> sqlite3.Row:
    """Always a fresh INSERT, never an UPDATE — a new document's claims are
    additive, same "never overwrite" discipline financial_observations
    already follows (schemas/sqlite_schema.sql's Knowledge Builder section)."""
    cursor = conn.execute(
        """
        INSERT INTO knowledge_claims (
            document_id, company_id, claim_type, category, claim_text, speaker,
            fiscal_year, quarter, extraction_confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, company_id, claim_type, category, claim_text, speaker,
         fiscal_year, quarter, extraction_confidence, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM knowledge_claims WHERE claim_id = ?", (cursor.lastrowid,)).fetchone()


def insert_knowledge_relationship(
    conn: sqlite3.Connection, *, claim_id: int | None, source_entity_id: int, relationship_type: str, target_entity_id: int
) -> None:
    conn.execute(
        "INSERT INTO knowledge_relationships (claim_id, source_entity_id, relationship_type, target_entity_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (claim_id, source_entity_id, relationship_type, target_entity_id, utcnow_iso()),
    )
    conn.commit()


def insert_knowledge_evidence(conn: sqlite3.Connection, *, claim_id: int, document_id: int, quote: str | None) -> None:
    conn.execute(
        "INSERT INTO knowledge_evidence (claim_id, document_id, quote, created_at) VALUES (?, ?, ?, ?)",
        (claim_id, document_id, quote, utcnow_iso()),
    )
    conn.commit()


def list_knowledge_claims_for_document(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_claims WHERE document_id = ? ORDER BY claim_id", (document_id,)
    ).fetchall()


def list_knowledge_claims_for_company(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_claims WHERE company_id = ? ORDER BY fiscal_year, quarter, claim_id",
        (company_id,),
    ).fetchall()


def list_knowledge_entities_for_companies(
    conn: sqlite3.Connection, company_ids: list[str], *, entity_types: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Distinct entities already extracted for one or more companies —
    the repository-layer replacement for what research/investigation_planner.py
    and research/hypothesis_generator.py used to run as raw SQL against
    knowledge_entities directly. `entity_types`/`limit` are optional filters
    the single-company caller (hypothesis_generator.py's own context lookup)
    needs; the multi-company caller (investigation_planner.py's mentioned-entity
    match) leaves both unset."""
    if not company_ids:
        return []
    placeholders = ",".join("?" for _ in company_ids)
    sql = f"SELECT DISTINCT entity_type, name FROM knowledge_entities WHERE company_id IN ({placeholders})"
    params: list[object] = list(company_ids)
    if entity_types:
        type_placeholders = ",".join("?" for _ in entity_types)
        sql += f" AND entity_type IN ({type_placeholders})"
        params.extend(entity_types)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_all_knowledge_entities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every entity — only for context/graph_neo4j.py's
    full-graph resync, never for a per-request path (use
    list_knowledge_entities_for_companies for that)."""
    return conn.execute("SELECT entity_id, entity_type, name, company_id FROM knowledge_entities").fetchall()


def list_all_knowledge_claims(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every claim — see list_all_knowledge_entities."""
    return conn.execute(
        "SELECT claim_id, document_id, company_id, claim_type, category, claim_text, speaker, "
        "fiscal_year, quarter, extraction_confidence FROM knowledge_claims"
    ).fetchall()


def list_all_knowledge_relationships(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every relationship — see list_all_knowledge_entities."""
    return conn.execute(
        "SELECT relationship_id, claim_id, source_entity_id, relationship_type, target_entity_id "
        "FROM knowledge_relationships"
    ).fetchall()


def list_all_knowledge_evidence(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every evidence row — see list_all_knowledge_entities."""
    return conn.execute("SELECT evidence_id, claim_id, document_id, quote FROM knowledge_evidence").fetchall()


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _sanitize_fts_query(query: str) -> str:
    """FTS5's MATCH syntax treats hyphens/colons/quotes as query operators —
    an arbitrary query passed straight through can raise a syntax error
    instead of just finding nothing. Tokenizing to plain alphanumeric words
    and double-quoting each one individually keeps every token literal
    (safe from operator interpretation).

    OR-joined, not FTS5's default AND — deliberately: a short 2-3 word
    search phrase is fine either way, but research/investigation_planner.py
    (Step 2F) composes a much longer blob (a question plus a hypothesis's
    own statement and mechanism) to search with, and requiring literally
    every one of those words to co-occur in one small chunk means almost
    nothing ever matches. OR plus bm25 ranking (search_document_chunks()'s
    `ORDER BY rank`) already rewards a chunk sharing MORE query terms over
    one sharing just one, without demanding all of them — a better fit for
    "where was something *similar* discussed" than a rigid AND would be."""
    tokens = _FTS_TOKEN_RE.findall(query)
    return " OR ".join(f'"{t}"' for t in tokens)


def replace_document_chunks(conn: sqlite3.Connection, document_id: int, chunks: list[dict]) -> None:
    """Deletes this document's existing chunks (if it's being reprocessed)
    and inserts the fresh set. Unlike knowledge_claims, a chunk has no
    standalone provenance value once superseded — it's a mechanical index
    over the document's CURRENT text for search, not a historical claim —
    so replacing it on reprocess is correct here, not a violation of the
    "never overwrite" rule that governs genuinely historical facts/claims."""
    now = utcnow_iso()
    existing_ids = [
        row["chunk_id"] for row in
        conn.execute("SELECT chunk_id FROM document_chunks WHERE document_id = ?", (document_id,)).fetchall()
    ]
    if existing_ids:
        conn.executemany("DELETE FROM document_chunks_fts WHERE rowid = ?", [(i,) for i in existing_ids])
        conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO document_chunks (document_id, company_id, page_number, chunk_index, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk["document_id"], chunk["company_id"], chunk["page_number"], chunk["chunk_index"], chunk["text"], now),
        )
        conn.execute(
            "INSERT INTO document_chunks_fts (rowid, text) VALUES (?, ?)", (cursor.lastrowid, chunk["text"])
        )
    conn.commit()


def search_document_chunks(
    conn: sqlite3.Connection, query: str, *, company_id: str | None = None, limit: int = 10
) -> list[sqlite3.Row]:
    """FTS5 keyword search over indexed chunks, joined back to `documents`
    for full provenance — Step 2D's "every returned passage must retain
    document, company, date, quarter, page/section, source." Returns []
    (not an error) for a query with no usable search tokens."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    sql = (
        "SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text, "
        "       d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at "
        "FROM document_chunks_fts fts "
        "JOIN document_chunks dc ON dc.chunk_id = fts.rowid "
        "JOIN documents d ON d.document_id = dc.document_id "
        "WHERE document_chunks_fts MATCH ?"
    )
    params: list[object] = [fts_query]
    if company_id is not None:
        sql += " AND dc.company_id = ?"
        params.append(company_id)
    sql += " ORDER BY fts.rank LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_knowledge_evidence_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_evidence WHERE claim_id = ? ORDER BY evidence_id", (claim_id,)
    ).fetchall()


def find_knowledge_claims_about_entity(conn: sqlite3.Connection, entity_type: str, entity_name: str) -> list[sqlite3.Row]:
    """Every claim (any company) whose extracted relationships touch this
    entity — the SQLite path for context/knowledge_graph.py's cross-entity
    query, a real join-based traversal, not a stub."""
    return conn.execute(
        """
        SELECT DISTINCT c.*
        FROM knowledge_entities e
        JOIN knowledge_relationships r ON r.source_entity_id = e.entity_id OR r.target_entity_id = e.entity_id
        JOIN knowledge_claims c ON c.claim_id = r.claim_id
        WHERE e.entity_type = ? AND e.name = ?
        ORDER BY c.fiscal_year, c.quarter, c.claim_id
        """,
        (entity_type, entity_name),
    ).fetchall()


def list_knowledge_relationships_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, se.entity_type AS source_type, se.name AS source_name,
               te.entity_type AS target_type, te.name AS target_name
        FROM knowledge_relationships r
        JOIN knowledge_entities se ON se.entity_id = r.source_entity_id
        JOIN knowledge_entities te ON te.entity_id = r.target_entity_id
        WHERE r.claim_id = ?
        """,
        (claim_id,),
    ).fetchall()


def _row_to_generated_report(row: sqlite3.Row) -> dict:
    return {
        "thread_id": row["thread_id"],
        "question": row["question"],
        "company_ids": json.loads(row["company_ids"]),
        "statement_type": row["statement_type"],
        "report_markdown": row["report_markdown"],
        "generated_at": row["generated_at"],
    }


def save_generated_report(
    conn: sqlite3.Connection,
    thread_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    report_markdown: str,
) -> None:
    """Persist a full Signals report (research/signals_report.py, via
    /research/thread/generate) so it survives a server restart — unlike the
    short /research/ask answers, which stay ephemeral by design."""
    conn.execute(
        "INSERT INTO generated_reports "
        "(thread_id, question, company_ids, statement_type, report_markdown, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, question, json.dumps(company_ids), statement_type, report_markdown, utcnow_iso()),
    )
    conn.commit()


def get_generated_report(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM generated_reports WHERE thread_id = ?", (thread_id,)).fetchone()
    return _row_to_generated_report(row) if row is not None else None


def list_generated_reports(conn: sqlite3.Connection) -> list[dict]:
    """Every generated report, newest first."""
    rows = conn.execute("SELECT * FROM generated_reports ORDER BY generated_at DESC").fetchall()
    return [_row_to_generated_report(row) for row in rows]


def save_report_evidence(conn: sqlite3.Connection, thread_id: str, evidence: list[dict]) -> None:
    """Persist the deterministic Evidence (research/evidence.py) that grounded a
    generated_reports row, in retrieval order, for the Investigations evidence
    rail. Each dict has kind/company_id/label/value/citation, matching
    Evidence's fields — callers pass plain dicts rather than Evidence objects
    so this storage-layer module doesn't need to import from research/."""
    conn.executemany(
        "INSERT INTO research_thread_evidence "
        "(thread_id, sort_order, kind, company_id, label, value, citation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (thread_id, i, ev["kind"], ev["company_id"], ev["label"], ev["value"], ev["citation"])
            for i, ev in enumerate(evidence)
        ],
    )
    conn.commit()


def list_report_evidence(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    """A generated report's grounding evidence, in the order it was retrieved in."""
    rows = conn.execute(
        "SELECT * FROM research_thread_evidence WHERE thread_id = ? ORDER BY sort_order", (thread_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def save_report_followups(conn: sqlite3.Connection, thread_id: str, followups: list[str]) -> None:
    """Persist the follow-up question suggestions a Signals report ended with
    (research/signals_report.py's ===FOLLOWUP_QUESTIONS=== parsing), so the
    Follow-up research rail's buttons are real and re-clickable."""
    conn.executemany(
        "INSERT INTO research_thread_followups (thread_id, sort_order, followup_text) VALUES (?, ?, ?)",
        [(thread_id, i, text) for i, text in enumerate(followups)],
    )
    conn.commit()


def list_report_followups(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT followup_text FROM research_thread_followups WHERE thread_id = ? ORDER BY sort_order", (thread_id,)
    ).fetchall()
    return [row["followup_text"] for row in rows]


def insert_llm_call_log(
    conn: sqlite3.Connection,
    *,
    task_name: str,
    company_ids: str,
    question: str | None,
    thread_id: str | None,
    complexity_tier: str,
    complexity_level: int,
    complexity_reason: str,
    model_used: str,
    provider_used: str,
    fallback_used: bool,
    attempts_json: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    stop_reason: str,
    context_tokens_before: int | None = None,
    context_tokens_after: int | None = None,
    context_items_dropped: int | None = None,
    reuse_hit: bool = False,
    reused_thread_id: str | None = None,
) -> None:
    """Persist one llm/router.py route() outcome, or one context/reuse.py
    reuse hit (llm/observability.py) — the Context Optimization + Model
    Routing + Fallback layer's audit trail."""
    conn.execute(
        "INSERT INTO llm_call_log "
        "(created_at, task_name, company_ids, question, thread_id, complexity_tier, complexity_level, "
        "complexity_reason, model_used, provider_used, fallback_used, attempts_json, input_tokens, "
        "output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before, "
        "context_tokens_after, context_items_dropped, reuse_hit, reused_thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow_iso(), task_name, company_ids, question, thread_id, complexity_tier, complexity_level,
            complexity_reason, model_used, provider_used, int(fallback_used), attempts_json, input_tokens,
            output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before,
            context_tokens_after, context_items_dropped, int(reuse_hit), reused_thread_id,
        ),
    )
    conn.commit()


def list_llm_call_log(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Most recent LLM calls, newest first — for a future cost/observability view."""
    rows = conn.execute(
        "SELECT * FROM llm_call_log ORDER BY call_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_llm_usage_summary(conn: sqlite3.Connection) -> dict:
    """All-time totals plus a by-task and by-model breakdown of llm_call_log
    — backs the /admin/usage page (web/app.py). A reuse hit (context/reuse.py)
    costs 0 tokens/0 dollars by construction (llm/observability.py's
    record_reuse), so it's counted separately (reused_calls) rather than
    diluting the per-model cost breakdown with free rows."""
    totals = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(reuse_hit), 0) AS reused_calls "
        "FROM llm_call_log"
    ).fetchone()

    by_task = conn.execute(
        "SELECT task_name, COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
        "FROM llm_call_log GROUP BY task_name ORDER BY cost_usd DESC"
    ).fetchall()

    by_model = conn.execute(
        "SELECT model_used, COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
        "FROM llm_call_log WHERE reuse_hit = 0 GROUP BY model_used ORDER BY cost_usd DESC"
    ).fetchall()

    return {
        "calls": totals["calls"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cost_usd": totals["cost_usd"],
        "reused_calls": totals["reused_calls"],
        "by_task": [dict(row) for row in by_task],
        "by_model": [dict(row) for row in by_model],
    }


def get_latest_data_timestamp(conn: sqlite3.Connection, company_ids: list[str]) -> str | None:
    """Most recent financial_observations.created_at / documents.retrieved_at
    across these companies — the freshness signal context/reuse.py checks a
    cached generated_reports row against before reusing it. A generated
    report older than this timestamp was built from data that has since
    changed, so it must not be silently reused (README §17: "an old cached
    result must not silently masquerade as current data")."""
    placeholders = ",".join("?" * len(company_ids))
    row = conn.execute(
        f"""
        SELECT MAX(ts) AS latest FROM (
            SELECT MAX(created_at) AS ts FROM financial_observations WHERE company_id IN ({placeholders})
            UNION ALL
            SELECT MAX(retrieved_at) AS ts FROM documents WHERE company_id IN ({placeholders})
        )
        """,
        (*company_ids, *company_ids),
    ).fetchone()
    return row["latest"] if row is not None else None


def delete_generated_report(conn: sqlite3.Connection, thread_id: str) -> bool:
    """Remove a generated report and its evidence/followups. Child rows have to
    go first — research_thread_evidence/research_thread_followups reference
    generated_reports(thread_id) with FK enforcement on (storage/database.py),
    so deleting the parent first would raise IntegrityError."""
    conn.execute("DELETE FROM research_thread_evidence WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM research_thread_followups WHERE thread_id = ?", (thread_id,))
    cursor = conn.execute("DELETE FROM generated_reports WHERE thread_id = ?", (thread_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_company_index_tags(conn: sqlite3.Connection, company_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT index_name FROM company_index_membership WHERE company_id = ? ORDER BY index_name",
        (company_id,),
    ).fetchall()
    return [row["index_name"] for row in rows]


def get_all_company_index_tags(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every company_id's index tags, in one query — for a page that needs
    every company's tags at once (e.g. to filter/search a large company list
    before pagination), where calling get_company_index_tags() per row would
    mean one query per company instead of one query total."""
    rows = conn.execute(
        "SELECT company_id, index_name FROM company_index_membership ORDER BY company_id, index_name"
    ).fetchall()
    tags_by_company: dict[str, list[str]] = {}
    for row in rows:
        tags_by_company.setdefault(row["company_id"], []).append(row["index_name"])
    return tags_by_company


def set_company_index_tags(conn: sqlite3.Connection, company_id: str, index_names: list[str]) -> None:
    """Replace this company's whole tag set — simpler and safe at this scale
    (a handful of tags per company) than diffing adds/removes."""
    known = {row["name"] for row in conn.execute("SELECT name FROM index_definitions")}
    unknown = set(index_names) - known
    if unknown:
        raise ValueError(f"Unknown index name(s): {sorted(unknown)}; must be one of {sorted(known)}")
    conn.execute("DELETE FROM company_index_membership WHERE company_id = ?", (company_id,))
    conn.executemany(
        "INSERT INTO company_index_membership (company_id, index_name) VALUES (?, ?)",
        [(company_id, name) for name in index_names],
    )
    conn.commit()


# ============================================================
# Sector / Industry / Index-tag vocabularies (Admin tab: "Sectors,
# Industries & Tags") — editable lookup tables, not pure freeform text or a
# hardcoded Python list. See schemas/sqlite_schema.sql's comment on these
# three tables for why they're TEXT-primary-keyed (a rename is one UPDATE
# on the name itself, not an id lookup).
# ============================================================


def list_sectors(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM sectors ORDER BY name")]


def count_companies_by_sector(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT sector, COUNT(*) AS n FROM companies WHERE sector IS NOT NULL GROUP BY sector"
    ).fetchall()
    return {row["sector"]: row["n"] for row in rows}


def add_sector(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sectors (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_sector(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    """Renames the lookup row and every company currently using it, in one
    transaction — a rename must never leave some companies pointing at a
    name that no longer exists in the lookup table."""
    conn.execute("UPDATE sectors SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute("UPDATE companies SET sector = ? WHERE sector = ?", (new_name, old_name))
    conn.commit()


def delete_sector(conn: sqlite3.Connection, name: str) -> None:
    """Clears `sector` to NULL on every company using it, then removes the
    lookup row — deleting a sector means "this no longer applies to
    anyone," not a silent reassignment to some other guessed value."""
    conn.execute("UPDATE companies SET sector = NULL WHERE sector = ?", (name,))
    conn.execute("DELETE FROM sectors WHERE name = ?", (name,))
    conn.commit()


def list_industries(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM industries ORDER BY name")]


def count_companies_by_industry(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT industry, COUNT(*) AS n FROM companies WHERE industry IS NOT NULL GROUP BY industry"
    ).fetchall()
    return {row["industry"]: row["n"] for row in rows}


def add_industry(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO industries (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_industry(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    conn.execute("UPDATE industries SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute("UPDATE companies SET industry = ? WHERE industry = ?", (new_name, old_name))
    conn.commit()


def delete_industry(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("UPDATE companies SET industry = NULL WHERE industry = ?", (name,))
    conn.execute("DELETE FROM industries WHERE name = ?", (name,))
    conn.commit()


def list_index_definitions(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM index_definitions ORDER BY name")]


def count_companies_by_index_tag(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT index_name, COUNT(*) AS n FROM company_index_membership GROUP BY index_name"
    ).fetchall()
    return {row["index_name"]: row["n"] for row in rows}


def add_index_definition(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO index_definitions (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_index_definition(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    conn.execute("UPDATE index_definitions SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute(
        "UPDATE company_index_membership SET index_name = ? WHERE index_name = ?", (new_name, old_name)
    )
    conn.commit()


def delete_index_definition(conn: sqlite3.Connection, name: str) -> None:
    """Removes every company's membership in this tag, then the tag itself
    — same "delete means it no longer applies to anyone" rule as
    delete_sector/delete_industry above."""
    conn.execute("DELETE FROM company_index_membership WHERE index_name = ?", (name,))
    conn.execute("DELETE FROM index_definitions WHERE name = ?", (name,))
    conn.commit()


# Optional columns on the Companies list (Admin tab: "Company List Columns").
# Fixed vocabulary, same pattern as INDEX_NAMES/ARCHIVE_REASONS — "Company"
# itself is always shown and isn't part of this set.
COMPANY_LIST_COLUMNS = [
    {"key": "sector", "label": "Sector"},
    {"key": "industry", "label": "Industry"},
    {"key": "price", "label": "Price"},
    {"key": "status", "label": "Status"},
    {"key": "tags", "label": "Index tags & IDs"},
]
_COMPANY_LIST_COLUMN_KEYS = {c["key"] for c in COMPANY_LIST_COLUMNS}


def get_company_list_column_settings(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which optional columns Admin has made available on the Companies
    list. A column with no row yet defaults to enabled — new columns show
    up available-by-default rather than silently hidden until an admin
    visits the settings page."""
    rows = conn.execute("SELECT column_key, enabled FROM company_list_column_settings").fetchall()
    overrides = {row["column_key"]: bool(row["enabled"]) for row in rows}
    return {key: overrides.get(key, True) for key in _COMPANY_LIST_COLUMN_KEYS}


def set_company_list_column_settings(conn: sqlite3.Connection, enabled_keys: Iterable[str]) -> None:
    """Replace the whole set — same replace-all pattern as set_company_index_tags."""
    enabled_keys = set(enabled_keys)
    unknown = enabled_keys - _COMPANY_LIST_COLUMN_KEYS
    if unknown:
        raise ValueError(f"Unknown column key(s): {sorted(unknown)}; must be one of {sorted(_COMPANY_LIST_COLUMN_KEYS)}")
    conn.executemany(
        "INSERT INTO company_list_column_settings (column_key, enabled) VALUES (?, ?) "
        "ON CONFLICT(column_key) DO UPDATE SET enabled = excluded.enabled",
        [(key, 1 if key in enabled_keys else 0) for key in _COMPANY_LIST_COLUMN_KEYS],
    )
    conn.commit()


def insert_macro_observations(
    conn: sqlite3.Connection, observations: Iterable[MacroNormalizedObservation]
) -> list[int]:
    """Insert each macro observation as a new row — append-only, same as
    insert_financial_observations (README: raw observations are never
    overwritten). No reconciliation step yet: with one source per series
    today there's nothing to reconcile, the same trivial pass-through
    financial_observations started with before NSE/BSE (step 6)."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO macro_observations (
                series_key, region, period_type, period, value, unit,
                source, source_file, source_url, retrieved_at, parser_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.series_key, obs.region, obs.period_type, obs.period, obs.value, obs.unit,
                obs.source, obs.source_file, obs.source_url, obs.retrieved_at or now, obs.parser_version, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def insert_bank_infrastructure_observations(
    conn: sqlite3.Connection, observations: Iterable[BankInfrastructureObservation]
) -> list[int]:
    """Insert each bank-infrastructure observation as a new row —
    append-only, same as insert_macro_observations."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO bank_infrastructure_observations (
                bank_name, metric, period_type, period, value, unit,
                source, source_file, parser_version, retrieved_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.bank_name, obs.metric, obs.period_type, obs.period, obs.value, obs.unit,
                obs.source, obs.source_file, obs.parser_version, now, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def get_bank_infrastructure_series(
    conn: sqlite3.Connection, bank_name: str, metric: str
) -> list[sqlite3.Row]:
    """One bank's one metric across every period ingested, oldest first."""
    return conn.execute(
        "SELECT * FROM bank_infrastructure_observations WHERE bank_name = ? AND metric = ? ORDER BY period",
        (bank_name, metric),
    ).fetchall()


def get_macro_series(
    conn: sqlite3.Connection, series_key: str, region: str | None = None
) -> list[sqlite3.Row]:
    """Every observation for one macro series, oldest to newest. region=None
    means all-India/national (matches how the adapter stores an omitted
    region column) rather than "any region" — pass a specific region string
    to fetch a regional series instead."""
    return conn.execute(
        "SELECT * FROM macro_observations WHERE series_key = ? AND region IS ? ORDER BY period ASC",
        (series_key, region),
    ).fetchall()


def list_macro_series_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every distinct (series_key, source, earliest, latest) at the national
    level (region IS NULL) — research/macro_evidence.py's own catalog
    discovery used to run this as raw SQL directly."""
    return conn.execute(
        "SELECT series_key, source, MIN(period) AS earliest, MAX(period) AS latest "
        "FROM macro_observations WHERE region IS NULL GROUP BY series_key, source"
    ).fetchall()


# ============================================================
# Users (README: sign-up is email-based, no verification; the one seeded
# admin account logs in by username instead — see schemas/sqlite_schema.sql)
# ============================================================

VALID_THEMES = {"light", "white", "green", "dark", "schwab"}
DEFAULT_THEME = "schwab"


def create_user(conn: sqlite3.Connection, email: str, password_hash: str) -> int:
    """Register a new sign-up user. Raises sqlite3.IntegrityError if the
    email is already taken — callers check that first via get_user_by_email
    for a friendly error message, this is the last-word uniqueness guard.

    theme is passed explicitly (not left to the column's own DEFAULT) so the
    actual default a new signup gets doesn't silently depend on which
    version of the schema this particular database was first created
    under — a long-lived database's stored column default doesn't retroactively
    follow a later change to schemas/sqlite_schema.sql the way a fresh
    install's would."""
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, is_admin, theme, created_at) VALUES (?, ?, 0, ?, ?)",
        (email, password_hash, DEFAULT_THEME, utcnow_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_login(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    """Login accepts either a sign-up user's email or the admin account's
    username — one field, matched against whichever column is populated."""
    return conn.execute(
        "SELECT * FROM users WHERE email = ? OR username = ?", (identifier, identifier)
    ).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def update_user_theme(conn: sqlite3.Connection, user_id: int, theme: str) -> None:
    if theme not in VALID_THEMES:
        raise ValueError(f"theme must be one of {sorted(VALID_THEMES)}, got {theme!r}")
    conn.execute("UPDATE users SET theme = ? WHERE user_id = ?", (theme, user_id))
    conn.commit()
