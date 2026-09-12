"""Manual CLI trigger for any job in scheduling/jobs.py's registry — the
third of the three call paths that module's docstring describes (CLI, web
"Run now", cron trigger), all sharing the exact same runner function.

Unlike every individual batch_fetch_*.py script's own main() (which
hardcodes init_db(), always local SQLite), this opens its connection
through scheduling.jobs.open_db() — so a manual run against a
Postgres-backed deployment (DATABASE_BACKEND=postgres in the environment)
actually reaches Postgres, not a throwaway local SQLite file.

Usage:
    python -m scripts.run_job --list
    python -m scripts.run_job financials_india_next50
"""

from __future__ import annotations

import argparse
import sys

# Must run before any other import in this file (or any module this file
# transitively imports, e.g. scheduling.jobs -> scripts.batch_fetch_nse ->
# companies.registry) touches storage.repositories/company_repository/etc.
# -- see storage/backend_bootstrap.py's own docstring for why, and
# web/app.py's own top-of-file comment for the same requirement there.
# Importing scheduling.jobs first and relying on open_db() to install the
# bootstrap later is too late: by then company_repository (etc.) is
# already bound to its pre-swap module object in every module that did
# `from storage import company_repository as repo` at import time.
import storage.backend_bootstrap

storage.backend_bootstrap.install()

from scheduling.jobs import SCHEDULED_JOBS, get_job, open_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id", nargs="?", help="one of the job_ids from --list")
    parser.add_argument("--list", action="store_true", help="list every job_id and exit")
    args = parser.parse_args()

    if args.list or not args.job_id:
        for job in SCHEDULED_JOBS:
            status = "runnable" if job.runner else f"disabled ({job.reason})"
            print(f"{job.job_id:40s} {job.cadence:10s} {status}")
        if not args.job_id:
            sys.exit(0 if args.list else 1)
        return

    job = get_job(args.job_id)
    if job is None:
        raise SystemExit(f"no such job_id: {args.job_id!r} (see --list)")
    if job.runner is None:
        raise SystemExit(f"{args.job_id!r} has no runner wired up yet: {job.reason}")

    conn = open_db()
    try:
        run_id = job.runner(conn)
        print(f"{job.label}: run #{run_id} finished — see Audit Log -> Job Runs for details.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
