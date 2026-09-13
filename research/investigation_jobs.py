"""Progress tracking for an in-flight /investigate/generate-async run --
one small JSON file per investigation_id under config.settings.
INVESTIGATION_JOBS_DIR, so the background thread that runs the
investigation (in one gunicorn worker) and the /investigate/status poll
(which may land in a different worker) agree on how far it's got, without
sharing process memory. See INVESTIGATION_JOBS_DIR's own comment for the
single-container-instance assumption this relies on.

Deliberately NOT a database table: this is short-lived, disposable
progress state for one page's polling loop, not an audit trail -- the
investigation's own real, permanent record is the investigations/
investigation_hypotheses rows run_investigation() already writes on
success (research/investigation.py::_persist), same distinction
batch_job_runs draws between "the durable audit log" and job state.
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

    path = settings.INVESTIGATION_JOBS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(investigation_id: str) -> Path:
    # investigation_id is always our own uuid4().hex[:12] (research/
    # investigation.py) -- no path-separator/traversal risk from an
    # attacker-controlled value reaching here.
    return _jobs_dir() / f"{investigation_id}.json"


def mark_running(investigation_id: str) -> None:
    _job_path(investigation_id).write_text(
        json.dumps({"status": STATUS_RUNNING, "updated_at": time.time()})
    )


def mark_done(investigation_id: str, url: str) -> None:
    _job_path(investigation_id).write_text(
        json.dumps({"status": STATUS_DONE, "url": url, "updated_at": time.time()})
    )


def mark_error(investigation_id: str, message: str) -> None:
    _job_path(investigation_id).write_text(
        json.dumps({"status": STATUS_ERROR, "error": message, "updated_at": time.time()})
    )


def get_status(investigation_id: str) -> dict | None:
    """None if this investigation_id was never tracked here (unknown id,
    or a job old enough that nothing ever wrote a file for it) -- the
    route turns that into a 404, distinct from a real error status."""
    path = _job_path(investigation_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
