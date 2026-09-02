"""The event bus: publish DatasetIngestedEvents, dispatch them to every
registered worker, and replay already-stored events on demand (README:
Signals Dataset-Centric Ingestion -- "Publish once. Process many -- now or
later.").

In-process and DB-backed, not a message broker -- consistent with this
app's standing modular-monolith guardrail (no microservices/new
infrastructure unless justified by real scale/isolation needs). publish()
persists the event to the Event Store (storage/repositories.py's
dataset_events table) and then calls every registered worker exactly once,
synchronously, each wrapped in its own try/except so one worker's bug can
never block the event store write or a sibling worker.

A worker is a plain `def run(conn, event) -> WorkerResult` registered via
register_worker(name, version, run). It decides relevance for itself --
inspect event.dataset_type/event.scope and return WorkerResult(status=
"skipped") for anything not its concern; there's no separate relevance
predicate to register, one calling convention for every worker.

Idempotency: publish() always runs fresh (every real ingestion mints a new
event_id, so there is nothing to dedupe against on that path). replay() is
where idempotency matters -- it skips a worker for an event it already
logged ok/skipped for (storage/repositories.py's
UNIQUE(event_id, worker_name, worker_version)), unless force=True. Bumping
a worker's WORKER_VERSION is the normal way to force reprocessing after a
logic change: the new version has no log row yet, so replay() (or even a
future publish(), though that never happens for a past event) runs it
fresh while the old version's history stays untouched.
"""

from __future__ import annotations

import json
import logging
from storage.db_types import DBConnection, Row
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

from ingestion.events import DatasetIngestedEvent
from storage.database import utcnow_iso
from storage.repositories import (
    finish_worker_log,
    get_worker_log,
    insert_dataset_event,
    list_dataset_events,
    start_worker_log,
)

logger = logging.getLogger(__name__)

WorkerHandler = Callable[[DBConnection, DatasetIngestedEvent], "WorkerResult"]


@dataclass(frozen=True)
class WorkerResult:
    """What a worker's run() returns. status is one of "ok"/"skipped"/
    "failed" -- "skipped" means "not relevant to me", not an error.
    output_reference is the small, human-readable pointer persisted to the
    Worker Processing Log (e.g. "reconciled_count=12"); `data` is an
    in-process-only bag for a synchronous caller that needs something back
    right away (e.g. ingestion/pipeline.py reading reconciled_count) --
    it is never persisted, so it's never something a replay can depend on."""

    status: str
    output_reference: str | None = None
    error: str | None = None
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerOutcome:
    worker_name: str
    worker_version: str
    result: WorkerResult


@dataclass(frozen=True)
class _Registration:
    name: str
    version: str
    handler: WorkerHandler


_REGISTRY: dict[str, _Registration] = {}


def register_worker(name: str, version: str, handler: WorkerHandler) -> None:
    """Register a worker under `name`. Re-registering the same name simply
    replaces the entry -- idempotent on repeated module import, never an
    accumulating list of duplicate handlers."""
    _REGISTRY[name] = _Registration(name, version, handler)


def registered_workers() -> list[_Registration]:
    return list(_REGISTRY.values())


def _run_one(conn: DBConnection, registration: _Registration, event: DatasetIngestedEvent) -> WorkerOutcome:
    log_id = start_worker_log(
        conn, event_id=event.event_id, ingestion_id=event.ingestion_id,
        worker_name=registration.name, worker_version=registration.version,
    )
    try:
        result = registration.handler(conn, event)
    except Exception as exc:  # noqa: BLE001 -- one worker's bug must never break ingestion or a sibling worker
        logger.warning(
            "Worker %s (v%s) failed on event %s: %s",
            registration.name, registration.version, event.event_id, exc, exc_info=True,
        )
        finish_worker_log(conn, log_id, status="failed", error_message=str(exc))
        return WorkerOutcome(registration.name, registration.version, WorkerResult(status="failed", error=str(exc)))

    finish_worker_log(
        conn, log_id, status=result.status, output_reference=result.output_reference, error_message=result.error,
    )
    return WorkerOutcome(registration.name, registration.version, result)


def publish(conn: DBConnection, event: DatasetIngestedEvent) -> list[WorkerOutcome]:
    """Persist `event` to the Event Store, then dispatch it to every
    registered worker exactly once. Fills event_id/ingested_at if the
    caller left them blank."""
    if not event.event_id:
        event = replace(event, event_id=str(uuid.uuid4()))
    if not event.ingested_at:
        event = replace(event, ingested_at=utcnow_iso())

    insert_dataset_event(conn, event)
    return [_run_one(conn, registration, event) for registration in registered_workers()]


def _event_from_row(row: Row) -> DatasetIngestedEvent:
    return DatasetIngestedEvent(
        dataset_id=row["dataset_id"],
        dataset_type=row["dataset_type"],
        source=row["source"],
        storage_reference=json.loads(row["storage_reference_json"]),
        ingestion_id=row["ingestion_id"],
        event_type=row["event_type"],
        scope=json.loads(row["scope_json"]),
        period=row["period"],
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        event_id=row["event_id"],
        ingested_at=row["ingested_at"],
    )


def replay(
    conn: DBConnection,
    *,
    event_id: str | None = None,
    dataset_type: str | None = None,
    worker_name: str | None = None,
    since: str | None = None,
    force: bool = False,
) -> list[WorkerOutcome]:
    """Re-dispatch already-stored events to registered workers, without
    touching source files or re-fetching anything -- a worker re-derives
    purely from what its storage_reference points at, which is already
    durable. Covers worker failure recovery (an event whose worker logged
    'failed'), backfilling a newly-added worker over history (it has no log
    row yet for any past event, so nothing is skipped), and reprocessing
    after a worker's logic changed (bump its WORKER_VERSION -- the new
    version has no log row either).

    Idempotent by default: a worker already logged 'ok' or 'skipped' for a
    given (event_id, worker_name, worker_version) is left alone. force=True
    re-runs it anyway -- rare; prefer bumping the worker's version so old
    history is preserved under its own row instead of overwritten.
    """
    events = list_dataset_events(conn, event_id=event_id, dataset_type=dataset_type, since=since)
    outcomes: list[WorkerOutcome] = []
    for row in events:
        event = _event_from_row(row)
        for registration in registered_workers():
            if worker_name is not None and registration.name != worker_name:
                continue
            if not force:
                existing = get_worker_log(conn, event.event_id, registration.name, registration.version)
                if existing is not None and existing["status"] in ("ok", "skipped"):
                    continue
            outcomes.append(_run_one(conn, registration, event))
    return outcomes
