"""Audit-log wrapper for multi-company batch fetch jobs (NSE XBRL
financials, NSE shareholding pattern, and future Nifty 500/USA batches --
see SCHEDULED_JOBS.md / NIFTY500_USA_XBRL_BATCHES.md). Records start/end +
status per run and per company to batch_job_runs/batch_job_items
(storage/repositories.py), so "did this batch actually work, and if not,
which companies and why" is a durable, queryable record instead of
something you have to go digging through a scratch log file for.

Usage:
    with BatchRun(conn, "nse_shareholding_fetch", "Nifty 50 remaining (9)") as run:
        for company_id in companies:
            with run.item(company_id) as item:
                result = do_the_actual_fetch(company_id)
                item.detail = f"downloaded={result.n} reconciled={result.reconciled}"
                # an uncaught exception inside this block is caught by
                # item.__exit__, recorded as that company's failure (the
                # exception text as detail), and swallowed so the loop
                # moves on to the next company -- one bad company doesn't
                # kill the run, same as ingestion/pipeline.py's own
                # per-file error handling.

An exception that escapes the `with run.item(...)` block entirely (i.e.
outside the per-company try) still propagates out of `with BatchRun(...)`
normally -- only per-item failures are swallowed, never a bug in the loop
itself."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from storage.repositories import (
    finish_batch_job_item,
    finish_batch_job_run,
    start_batch_job_item,
    start_batch_job_run,
)


class _Item:
    """Handed to the caller's `with run.item(...) as item:` block -- set
    `item.detail` before the block ends to record a human-readable summary
    (e.g. "downloaded=9 reconciled=594") alongside the ok/failed status."""

    def __init__(self) -> None:
        self.detail: str | None = None


class BatchRun:
    def __init__(self, conn: sqlite3.Connection, job_name: str, scope_label: str | None = None) -> None:
        self._conn = conn
        self._job_name = job_name
        self._scope_label = scope_label
        self.run_id: int | None = None

    def __enter__(self) -> "BatchRun":
        self.run_id = start_batch_job_run(self._conn, self._job_name, self._scope_label)
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        # A per-item failure is already recorded on that item and doesn't
        # reach here -- this only fires for a bug in the loop itself
        # (unhandled outside `with run.item(...)`), which the run really
        # did fail on.
        finish_batch_job_run(
            self._conn, self.run_id,
            status="failed" if exc_type is not None else "completed",
            notes=str(exc) if exc_type is not None else None,
        )
        return False  # never suppress -- let a real bug surface to the caller

    @contextmanager
    def item(self, company_id: str | None) -> Iterator[_Item]:
        item_id = start_batch_job_item(self._conn, self.run_id, company_id)
        holder = _Item()
        try:
            yield holder
        except Exception as exc:  # noqa: BLE001 -- one bad company shouldn't kill the whole batch
            finish_batch_job_item(self._conn, item_id, status="failed", detail=str(exc))
        else:
            finish_batch_job_item(self._conn, item_id, status="ok", detail=holder.detail)
