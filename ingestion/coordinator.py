"""Ingestion Coordinator — Admin -> Ingest queue's orchestration layer.

Settings/Admin -> Ingest UI
      |
      v
Ingestion Coordinator (this module)
      |
      +---------------------------+---------------------------+
      |                           |                           |
Existing financial/macro      Existing document           Future Knowledge
ingestion pipeline            registration (documents      Builder (Step 2A,
(ingestion/pipeline.py,       table, already exists)        not built here)
unchanged)

This module adds NO new financial-parsing/normalization logic — it only
discovers what hasn't been processed yet and dispatches to the existing
ingest_file()/ingest_macro_file()/ingest_bank_infrastructure_file()
pipeline (ingestion/pipeline.py), same as main.py's `ingest` command
already does. Flask routes (web/app.py) call into this module, not into
ingestion/pipeline.py or storage/repositories.py directly, so ingestion
business logic never lives in a route handler.

Two kinds of pending item, tracked differently (see schemas/sqlite_schema.sql):
  - Financial/macro FILES under data/raw/: not modeled anywhere until now —
    ingestion_queue_items is a new table purely for discovery/status tracking.
  - DOCUMENTS: already modeled in the `documents` table (Docs tab uploads/
    links) — their queue state lives directly on that table
    (processing_status/processed_at), not duplicated into a second identity.

"Processing" a document in Step 1 has nothing to extract yet (Step 2A's
Knowledge Builder isn't built) — it just computes/refreshes the file's
content hash (documents.file_hash, already a column) and marks it
processed, so the Ingest queue's contract (discover -> review -> process ->
tracked) is real and stable before any real extraction logic lands on top
of it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import ingestion.workers  # noqa: F401 -- registers built-in workers (knowledge_builder, chunk_indexer, ...)
from companies.registry import get_company
from config import settings
from ingestion.batch_log import BatchRun
from ingestion.detector import (
    PathConventionError,
    detect_from_path,
    detect_macro_source_from_path,
    is_macro_path,
)
from ingestion.event_bus import publish
from ingestion.events import DatasetIngestedEvent
from ingestion.pipeline import ingest_bank_infrastructure_file, ingest_file, ingest_macro_file
from storage.database import utcnow_iso
from storage.repositories import (
    get_document,
    get_ingestion_queue_item,
    get_ingestion_queue_item_by_path,
    list_documents_by_status,
    list_ingestion_queue_items,
    mark_document_processing_status,
    set_document_processing_status,
    set_ingestion_queue_item_status,
    upsert_ingestion_queue_item,
    update_ingestion_queue_item_result,
)

logger = logging.getLogger(__name__)

_BANK_INFRASTRUCTURE_PREFIXES = ("ATM", "NEFTRTGS")

#: data/raw/_macro/mfin/ — archive-only reference PDFs (config.settings.DEFAULT_SOURCES'
#: "mfin" entry, sources/macro.py's MACRO_SOURCE_IDS comment): never has a
#: period/value/unit shape, nothing calls ingest_macro_file() on it, so it's
#: never queued for processing at all — discovering it as a "failed" macro
#: file would just be noise in the Ingest queue.
_MFIN_SENTINEL = "mfin"


@dataclass
class ProcessOutcome:
    item_id: int
    ok: bool
    detail: str = ""


@dataclass
class ProcessSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    outcomes: list[ProcessOutcome] = field(default_factory=list)


def _content_hash(file_path: Path) -> str | None:
    """sha256 of the file's bytes — the stable identifier
    schemas/sqlite_schema.sql's ingestion_queue_items comment calls for.
    None (not raised) if the file's vanished/unreadable between discovery
    and hashing — treated as SKIPPED by the caller, not a hard failure."""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return None


def discover_pending_financial_items(conn) -> int:
    """Rescan data/raw/ for files not yet reflected (or changed since last
    reflected) in ingestion_queue_items. Returns how many rows were
    inserted/updated.

    A file's detection (company/source, PENDING vs NEEDS_REVIEW) is only
    re-derived when there's a real reason to: it's brand new, its content
    changed since the last discovery pass, or it's stuck NEEDS_REVIEW
    (worth re-checking — e.g. the company may have been registered since).
    A real processing outcome — PROCESSED or FAILED — is never silently
    overwritten by rediscovery alone on an otherwise-unchanged file: that's
    both the "never silently reprocess an unchanged file" contract and what
    keeps a FAILED row's error_message around for Retry Failed to act on,
    instead of a routine rescan quietly resetting it back to PENDING.
    """
    raw_dir = settings.RAW_DIR  # read live, not at import time — see storage/database.py's get_connection for why
    if not raw_dir.exists():
        return 0

    touched = 0
    for path in sorted(raw_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue  # OS/editor artifacts (.DS_Store, .gitkeep, ...) are never ingestible data
        # Stored relative to BASE_DIR, not absolute — an absolute path bakes
        # in the repo folder's current name/location (config/settings.py's
        # to_repo_relative/from_repo_relative docstring explains why).
        relative_path = settings.to_repo_relative(path)
        content_hash = _content_hash(path)
        existing = get_ingestion_queue_item_by_path(conn, relative_path)

        unchanged = existing is not None and existing["content_hash"] == content_hash
        if unchanged and existing["status"] != "NEEDS_REVIEW":
            continue  # nothing to re-derive — PENDING/PROCESSED/FAILED/PROCESSING/SKIPPED all stand as-is

        if is_macro_path(path):
            if _MFIN_SENTINEL in path.parts:
                continue  # config.settings.DEFAULT_SOURCES / sources/macro.py's MACRO_SOURCE_IDS
                          # comment: mfin is archive-only reference PDFs, never queued for ingestion
            item_kind = (
                "bank_infrastructure_file" if path.name.upper().startswith(_BANK_INFRASTRUCTURE_PREFIXES)
                else "macro_file"
            )
            try:
                source_id = detect_macro_source_from_path(path)
                status, reason = "PENDING", None
            except PathConventionError as exc:
                source_id, status, reason = None, "NEEDS_REVIEW", str(exc)
            company_id = None
        else:
            item_kind = "financial_file"
            try:
                company_id, source_id = detect_from_path(path)
                status, reason = "PENDING", None
            except PathConventionError as exc:
                company_id, source_id, status, reason = None, None, "NEEDS_REVIEW", str(exc)
            else:
                if get_company(conn, company_id) is None:
                    status, reason = "NEEDS_REVIEW", f"company {company_id!r} is not registered"

        if existing is not None and existing["status"] in ("PROCESSED", "FAILED") and not unchanged:
            reason = reason or f"file changed since it was last {existing['status'].lower()}"

        upsert_ingestion_queue_item(
            conn, item_kind=item_kind, file_path=relative_path, content_hash=content_hash,
            company_id=company_id, source_id=source_id, status=status, status_reason=reason,
        )
        touched += 1

    return touched


def discover_pending_documents(conn) -> int:
    """Documents added via the Docs tab that haven't been marked processed
    yet — no filesystem scan needed, they're already rows in `documents`;
    this just reports how many are sitting at 'pending' today."""
    return len(list_documents_by_status(conn, "pending"))


def _process_financial_queue_item(conn, item) -> ProcessOutcome:
    file_path = settings.from_repo_relative(item["file_path"])
    if not file_path.exists():
        update_ingestion_queue_item_result(conn, item["item_id"], status="FAILED", error_message="file no longer exists on disk")
        return ProcessOutcome(item["item_id"], ok=False, detail="file no longer exists on disk")

    if item["status"] == "NEEDS_REVIEW":
        return ProcessOutcome(
            item["item_id"], ok=False,
            detail=item["status_reason"] or "needs review before it can be processed",
        )

    try:
        if item["item_kind"] == "bank_infrastructure_file":
            result = ingest_bank_infrastructure_file(conn, file_path, source_id=item["source_id"])
            detail = f"parsed={result.parsed_count} inserted={result.inserted_count}"
        elif item["item_kind"] == "macro_file":
            result = ingest_macro_file(conn, file_path, source_id=item["source_id"])
            detail = f"parsed={result.parsed_count} inserted={result.inserted_count} skipped={result.skipped_count}"
        else:
            result = ingest_file(conn, file_path, company_id=item["company_id"], source_id=item["source_id"])
            detail = f"parsed={result.parsed_count} inserted={result.inserted_count} skipped={result.skipped_count}"
    except Exception as exc:  # noqa: BLE001 — any adapter/pipeline failure lands here as a retryable FAILED item
        logger.warning("Ingest queue item %s failed: %s", item["item_id"], exc, exc_info=True)
        update_ingestion_queue_item_result(conn, item["item_id"], status="FAILED", error_message=str(exc))
        return ProcessOutcome(item["item_id"], ok=False, detail=str(exc))

    content_hash = _content_hash(file_path)
    update_ingestion_queue_item_result(
        conn, item["item_id"], status="PROCESSED", processed_at=utcnow_iso(),
        last_processed_content_hash=content_hash,
    )
    return ProcessOutcome(item["item_id"], ok=True, detail=detail)


def process_financial_items(conn, item_ids: list[int]) -> ProcessSummary:
    """Process a specific set of ingestion_queue_items rows (Ingest Selected).

    A NEEDS_REVIEW row is rejected before ever being stamped PROCESSING —
    checking that inside _process_financial_queue_item() alone isn't
    enough, since by the time it re-reads the row here it would already
    see the PROCESSING status this function just wrote, not NEEDS_REVIEW.
    """
    summary = ProcessSummary()
    for item_id in item_ids:
        item = get_ingestion_queue_item(conn, item_id)
        if item is None:
            continue
        summary.attempted += 1
        if item["status"] == "NEEDS_REVIEW":
            outcome = ProcessOutcome(
                item_id, ok=False, detail=item["status_reason"] or "needs review before it can be processed"
            )
            summary.outcomes.append(outcome)
            summary.failed += 1
            continue
        if item["status"] == "ARCHIVED":
            outcome = ProcessOutcome(item_id, ok=False, detail="archived — unarchive first")
            summary.outcomes.append(outcome)
            summary.failed += 1
            continue
        update_ingestion_queue_item_result(conn, item_id, status="PROCESSING")
        item = get_ingestion_queue_item(conn, item_id)  # re-read after the PROCESSING stamp
        outcome = _process_financial_queue_item(conn, item)
        summary.outcomes.append(outcome)
        summary.succeeded += int(outcome.ok)
        summary.failed += int(not outcome.ok)
    return summary


def process_all_pending_financial_items(conn) -> ProcessSummary:
    """Ingest All Pending — every PENDING row, in discovery order."""
    pending = list_ingestion_queue_items(conn, status="PENDING")
    return process_financial_items(conn, [row["item_id"] for row in pending])


def retry_failed_financial_items(conn) -> ProcessSummary:
    """Retry Failed — re-attempts every FAILED row as-is (same file_path/
    detection), without re-running discovery first."""
    failed = list_ingestion_queue_items(conn, status="FAILED")
    return process_financial_items(conn, [row["item_id"] for row in failed])


def archive_financial_items(conn, item_ids: list[int]) -> int:
    """Archive Selected — "no need for review/processing right now": parks a
    row out of the Pending/Failed/Needs Review working set (excluded from
    Ingest All Pending and Retry Failed, which both filter by status) without
    deleting it or losing its history. A file that never matches its
    adapter's naming convention (e.g. a misnamed IITM rainfall file) would
    otherwise sit in Failed forever, re-failing every Retry Failed run.
    Reversible via unarchive_financial_items() — this is a parking lot, not
    a delete."""
    archived = 0
    for item_id in item_ids:
        item = get_ingestion_queue_item(conn, item_id)
        if item is None or item["status"] == "ARCHIVED":
            continue
        set_ingestion_queue_item_status(conn, item_id, "ARCHIVED")
        archived += 1
    return archived


def unarchive_financial_items(conn, item_ids: list[int]) -> int:
    """Unarchive — back to PENDING, so it's picked up by Ingest All Pending/
    the normal queue again. Same "hand it back to the working set" contract
    on the documents side (unarchive_documents)."""
    unarchived = 0
    for item_id in item_ids:
        item = get_ingestion_queue_item(conn, item_id)
        if item is None or item["status"] != "ARCHIVED":
            continue
        set_ingestion_queue_item_status(conn, item_id, "PENDING")
        unarchived += 1
    return unarchived


def process_documents(conn, document_ids: list[int]) -> ProcessSummary:
    """Register each document (content hash refreshed) and publish a
    `document` DATASET_INGESTED event for it — "processing" a document now
    means both, not just the Step 1 status flip. The event fans out to two
    independent workers (ingestion/workers/knowledge_builder_worker.py,
    ingestion/workers/chunk_indexer_worker.py) instead of this function
    calling Step 2A extraction and Step 2D chunking inline; this function
    just reads their outcomes back to decide the document's status, same
    as before. A document with no extractable text (non-PDF, unfetchable
    link) still succeeds with zero claims, same "absence isn't an error"
    rule research/documents.py already follows; an extraction that raises
    (LLM unavailable, unparseable response) marks the document FAILED with
    the error recorded, retryable via retry_failed_documents().

    Each document's outcome is also recorded to batch_job_runs/batch_job_items
    (ingestion/batch_log.py) — the same durable, queryable audit trail
    scripts/batch_fetch_nse.py and main.py's vector-backfill/graph-backfill
    use — so every caller (Admin -> Ingest queue, retry_failed_documents(),
    process_all_pending_documents()) gets this for free without its own
    wiring, since they all funnel through this one function."""
    summary = ProcessSummary()
    with BatchRun(conn, "document_processing", f"{len(document_ids)} document(s)") as run:
        for document_id in document_ids:
            # get_company_document (storage/repositories.py) is company-scoped;
            # the queue view isn't, so get_document() (also company-agnostic)
            # is used here instead.
            row = get_document(conn, document_id)
            if row is None:
                continue
            summary.attempted += 1
            with run.item(row["company_id"]) as item:
                file_hash = None
                if row["raw_file_path"]:
                    path = settings.from_repo_relative(row["raw_file_path"])
                    if path.exists():
                        file_hash = _content_hash(path)

                event = DatasetIngestedEvent(
                    dataset_id=f"document:{document_id}",
                    dataset_type="document",
                    source=row["source"] or "document",
                    scope={"document_id": document_id, "company_id": row["company_id"]},
                    storage_reference={"table": "documents", "document_id": document_id},
                    ingestion_id=str(uuid.uuid4()),
                )
                outcomes = {outcome.worker_name: outcome.result for outcome in publish(conn, event)}

                kb_result = outcomes.get("knowledge_builder")
                if kb_result is None or kb_result.status == "failed":
                    error = kb_result.error if kb_result is not None else "knowledge_builder worker not registered"
                    logger.warning("Knowledge extraction failed for document %s: %s", document_id, error)
                    mark_document_processing_status(conn, document_id, status="failed", error_message=error)
                    summary.failed += 1
                    summary.outcomes.append(ProcessOutcome(document_id, ok=False, detail=error))
                    raise RuntimeError(error)  # re-raised so run.item() records this item as failed too

                # Chunking/indexing (Step 2D) is best-effort on top of an already-
                # successful extraction — deterministic and low-risk (no LLM call),
                # but a failure here shouldn't undo a real, already-persisted
                # knowledge-extraction success. Same graceful-degradation spirit as
                # the Neo4j/Ollama fallbacks elsewhere in this app; the two workers
                # dispatch independently, so a chunk_indexer failure never touches
                # the knowledge_builder outcome above.
                chunk_result = outcomes.get("chunk_indexer")
                if chunk_result is not None and chunk_result.status == "failed":
                    logger.warning(
                        "Chunk indexing failed for document %s (extraction still succeeded): %s",
                        document_id, chunk_result.error,
                    )
                chunk_count = (
                    chunk_result.data.get("chunk_count", 0) if chunk_result and chunk_result.status == "ok" else 0
                )
                claims_created = kb_result.data.get("claims_created", 0)

                mark_document_processing_status(
                    conn, document_id, status="processed", file_hash=file_hash, processed_at=utcnow_iso(),
                    error_message=None,
                )
                summary.succeeded += 1
                detail = f"document_id={document_id} registered, {claims_created} claim(s) extracted, {chunk_count} chunk(s) indexed"
                summary.outcomes.append(ProcessOutcome(document_id, ok=True, detail=detail))
                item.detail = detail
    return summary


def process_all_pending_documents(conn) -> ProcessSummary:
    pending = list_documents_by_status(conn, "pending")
    return process_documents(conn, [row["document_id"] for row in pending])


def retry_failed_documents(conn) -> ProcessSummary:
    """Retry Failed — every document stuck at processing_status='failed',
    same reasoning as retry_failed_financial_items()."""
    failed = list_documents_by_status(conn, "failed")
    return process_documents(conn, [row["document_id"] for row in failed])


def archive_documents(conn, document_ids: list[int]) -> int:
    """Archive Selected — same "no need for review/processing right now"
    parking lot as archive_financial_items(), for documents that keep
    failing extraction (or just aren't worth processing yet): excluded from
    Ingest All Pending/Retry Failed (both filter by processing_status)
    without losing the row or its history. Reversible via
    unarchive_documents().

    Recorded to batch_job_runs/batch_job_items (ingestion/batch_log.py) —
    same audit trail as process_documents() above — one item per document
    actually archived."""
    archived = 0
    with BatchRun(conn, "document_archive", f"{len(document_ids)} document(s)") as run:
        for document_id in document_ids:
            row = get_document(conn, document_id)
            if row is None or row["processing_status"] == "archived":
                continue
            with run.item(row["company_id"]) as item:
                set_document_processing_status(conn, document_id, "archived")
                archived += 1
                item.detail = f"document_id={document_id} archived (was {row['processing_status']})"
    return archived


def unarchive_documents(conn, document_ids: list[int]) -> int:
    """Unarchive — back to pending, so it's picked up by Ingest All Pending/
    the normal queue again. Recorded to batch_job_runs/batch_job_items same
    as archive_documents() above."""
    unarchived = 0
    with BatchRun(conn, "document_unarchive", f"{len(document_ids)} document(s)") as run:
        for document_id in document_ids:
            row = get_document(conn, document_id)
            if row is None or row["processing_status"] != "archived":
                continue
            with run.item(row["company_id"]) as item:
                set_document_processing_status(conn, document_id, "pending")
                unarchived += 1
                item.detail = f"document_id={document_id} unarchived, back to pending"
    return unarchived
