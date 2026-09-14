"""Progress tracking for an in-flight /chat, /research/ask, or
/companies/<id>/ask-async run -- same shape as research/investigation_
jobs.py (see that module's own docstring for the full reasoning: one
small JSON file per job under config.settings.ASK_JOBS_DIR, so the
background thread answering the question and the poll route agree on
status without sharing process memory across gunicorn's forked workers).

The one real difference from investigation_jobs.py: an investigation's
"done" state just needs a URL to redirect to (the investigation page
renders from its own permanent database row). Ask AI's answer -- markdown
rendered to HTML, charts, thread_id/thread_url -- has nowhere else to
live once computed, so "done" carries the whole JSON response payload
the route would otherwise have returned synchronously, not just a
pointer to it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def _jobs_dir() -> Path:
    from config import settings

    path = settings.ASK_JOBS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    # job_id is always our own uuid4().hex[:12] -- no path-separator/
    # traversal risk from an attacker-controlled value reaching here.
    return _jobs_dir() / f"{job_id}.json"


def mark_running(job_id: str) -> None:
    _job_path(job_id).write_text(json.dumps({"status": STATUS_RUNNING, "updated_at": time.time()}))


def mark_done(job_id: str, result: dict) -> None:
    _job_path(job_id).write_text(json.dumps({"status": STATUS_DONE, "result": result, "updated_at": time.time()}))


def mark_error(job_id: str, message: str) -> None:
    _job_path(job_id).write_text(json.dumps({"status": STATUS_ERROR, "error": message, "updated_at": time.time()}))


def get_status(job_id: str) -> dict | None:
    """None if this job_id was never tracked here (unknown id, or old
    enough that nothing ever wrote a file for it) -- the route turns that
    into a 404, distinct from a real error status."""
    path = _job_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
