"""ingestion/event_bus.py -- publish persists to the Event Store and
dispatches to every registered worker; replay re-dispatches stored events
idempotently unless force=True; a worker that raises degrades to a failed
log row without breaking the caller or a sibling worker."""

from __future__ import annotations

import sqlite3

import pytest

from ingestion.event_bus import (
    WorkerResult,
    _REGISTRY,
    publish,
    register_worker,
    replay,
)
from ingestion.events import DatasetIngestedEvent
from storage.repositories import get_dataset_event, list_worker_processing_log


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every real worker module registers itself at import time
    (ingestion/workers/__init__.py) -- isolate each test's own fake workers
    from that global registry and from each other, then restore it."""
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _event(**overrides) -> DatasetIngestedEvent:
    defaults = dict(
        dataset_id="test:ACME",
        dataset_type="widgets",
        source="test_source",
        storage_reference={"table": "widgets"},
        ingestion_id="ingest-1",
        scope={"company_id": "ACME"},
    )
    defaults.update(overrides)
    return DatasetIngestedEvent(**defaults)


def test_publish_persists_event_and_runs_every_worker(db_conn: sqlite3.Connection) -> None:
    calls = []

    def worker_a(conn, event):
        calls.append(("a", event.dataset_type))
        return WorkerResult(status="ok", output_reference="did a thing", data={"n": 1})

    def worker_b(conn, event):
        calls.append(("b", event.dataset_type))
        return WorkerResult(status="skipped")

    register_worker("worker_a", "1", worker_a)
    register_worker("worker_b", "1", worker_b)

    outcomes = publish(db_conn, _event())

    assert calls == [("a", "widgets"), ("b", "widgets")]
    assert {o.worker_name: o.result.status for o in outcomes} == {"worker_a": "ok", "worker_b": "skipped"}
    assert outcomes[0].result.data == {"n": 1}

    row = get_dataset_event(db_conn, _only_published_event_id(db_conn))
    assert row["dataset_type"] == "widgets"
    assert row["dataset_id"] == "test:ACME"

    logs = list_worker_processing_log(db_conn, event_id=row["event_id"])
    assert {log["worker_name"]: log["status"] for log in logs} == {"worker_a": "ok", "worker_b": "skipped"}
    assert logs[0]["ingestion_id"] == "ingest-1"


def _only_published_event_id(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT event_id FROM dataset_events").fetchone()["event_id"]


def test_a_failing_worker_does_not_block_a_sibling_worker(db_conn: sqlite3.Connection) -> None:
    def bad_worker(conn, event):
        raise RuntimeError("boom")

    calls = []

    def good_worker(conn, event):
        calls.append(True)
        return WorkerResult(status="ok")

    register_worker("bad_worker", "1", bad_worker)
    register_worker("good_worker", "1", good_worker)

    outcomes = publish(db_conn, _event())

    assert calls == [True]  # good_worker still ran
    statuses = {o.worker_name: o.result.status for o in outcomes}
    assert statuses == {"bad_worker": "failed", "good_worker": "ok"}
    assert "boom" in next(o.result.error for o in outcomes if o.worker_name == "bad_worker")

    event_id = _only_published_event_id(db_conn)
    log = list_worker_processing_log(db_conn, event_id=event_id, worker_name="bad_worker")[0]
    assert log["status"] == "failed"
    assert "boom" in log["error_message"]


def test_worker_irrelevance_is_a_skip_not_an_error(db_conn: sqlite3.Connection) -> None:
    def picky_worker(conn, event):
        if event.dataset_type != "the_only_type_i_care_about":
            return WorkerResult(status="skipped")
        return WorkerResult(status="ok")

    register_worker("picky_worker", "1", picky_worker)

    outcomes = publish(db_conn, _event(dataset_type="something_else"))
    assert outcomes[0].result.status == "skipped"


def test_replay_skips_a_worker_already_ok_unless_forced(db_conn: sqlite3.Connection) -> None:
    calls = []

    def counting_worker(conn, event):
        calls.append(1)
        return WorkerResult(status="ok")

    register_worker("counting_worker", "1", counting_worker)
    publish(db_conn, _event())
    event_id = _only_published_event_id(db_conn)
    assert len(calls) == 1

    # Not forced -- already logged 'ok', so replay is a no-op.
    outcomes = replay(db_conn, event_id=event_id)
    assert outcomes == []
    assert len(calls) == 1

    # Forced -- runs again, same log row (retry_count increments), no duplicate row.
    outcomes = replay(db_conn, event_id=event_id, force=True)
    assert len(outcomes) == 1
    assert len(calls) == 2
    logs = list_worker_processing_log(db_conn, event_id=event_id, worker_name="counting_worker")
    assert len(logs) == 1
    assert logs[0]["retry_count"] == 1


def test_replay_backfills_a_newly_registered_worker_over_history(db_conn: sqlite3.Connection) -> None:
    """A worker added after an event was already published has never run
    against it -- replay() should pick it up with no --force needed, since
    there's no existing log row to skip."""
    register_worker("worker_a", "1", lambda conn, event: WorkerResult(status="ok"))
    publish(db_conn, _event())
    event_id = _only_published_event_id(db_conn)

    new_calls = []

    def worker_b(conn, event):
        new_calls.append(1)
        return WorkerResult(status="ok")

    register_worker("worker_b", "1", worker_b)

    outcomes = replay(db_conn, event_id=event_id)
    assert len(new_calls) == 1
    assert [o.worker_name for o in outcomes] == ["worker_b"]


def test_replay_filters_by_dataset_type_and_worker_name(db_conn: sqlite3.Connection) -> None:
    calls = []
    register_worker("worker_a", "1", lambda conn, event: calls.append("a") or WorkerResult(status="ok"))
    register_worker("worker_b", "1", lambda conn, event: calls.append("b") or WorkerResult(status="ok"))

    publish(db_conn, _event(dataset_type="type_x", ingestion_id="ingest-x"))
    publish(db_conn, _event(dataset_type="type_y", ingestion_id="ingest-y"))
    calls.clear()

    outcomes = replay(db_conn, dataset_type="type_x", worker_name="worker_a", force=True)
    assert calls == ["a"]
    assert len(outcomes) == 1
    assert outcomes[0].worker_name == "worker_a"
