"""Database backend switch — DATABASE_BACKEND=postgres redirects every
existing `from storage.repositories import X` / `from storage.company_
repository import X` (etc.) call site in the app to the Postgres-flavored
implementation, with ZERO changes to those call sites. This is the
mechanism the architecture investigation's Section I asked for: "Changing
DATABASE_BACKEND=sqlite to postgres should NOT require business/research
logic changes."

How: Python caches every imported module in sys.modules, keyed by its
dotted path. Registering a different module object under that same key
(`sys.modules["storage.repositories"] = <something else>`) makes every
subsequent `from storage.repositories import X`, anywhere in the process,
resolve against that different object instead — including in files that
haven't been imported yet. This MUST run before the first import of any of
the five modules below (web/app.py, gunicorn's entry point, does this as
literally its first statement, before its own storage imports).

Four of the five modules (company_repository, fact_store, indicator_
repository, investigation_repository) are swapped wholesale -- every
function in each was ported 1:1 to its _pg sibling, confirmed via a
function-name diff against the live module (see this session's migration
notes). storage.repositories is NOT a clean 1:1 swap: 33 of its 172
functions operate ONLY on the 8 tables that stay SQLite-only forever
(batch_job_runs/items, dataset_events, worker_processing_log,
retrieval_diagnostics, llm_call_log, ingestion_queue_items,
reconciliation_log) -- repositories_pg.py deliberately doesn't define
these (see that file's own "NOT PORTED" section). A full-module swap would
make every `from storage.repositories import start_batch_job_run` (etc.)
raise ImportError at process startup -- web/app.py's Schedule/Audit-Log/
Ingest-queue/LLM-cost admin pages all import several of these directly.

So storage.repositories gets a HYBRID module instead: every name from
repositories_pg (Postgres-backed) PLUS the original SQLite implementations
of those 33 audit-log functions, re-exported unchanged. This makes imports
succeed either way -- but it does NOT, by itself, make those 33 functions
work correctly if called with a Postgres connection (they run `?`-
placeholder SQLite SQL, which is a syntax error against psycopg2). That
half of the fix is at the CALL SITE: web/app.py's admin/audit routes pass
a dedicated always-SQLite connection (get_logs_db(), not get_db()) to
these specific 33 functions, regardless of DATABASE_BACKEND -- see that
file's own comment where get_logs_db() is defined. This module only
guarantees "the name exists and does something correct when given the
right kind of connection"; it can't fix what connection a caller passes.
"""

from __future__ import annotations

import sys

from config.settings import DATABASE_BACKEND

# The 33 real audit-log functions storage.repositories has that
# storage.repositories_pg deliberately doesn't (verified via a function-name
# diff: 172 total, 139 ported, 33 audit-log-only + 2 internal-only names
# --_normalize_source_file, a private helper, and search_document_chunks,
# reachable only through storage.fact_store.default_fact_store() which is
# swapped independently below -- neither of those two needs re-exporting
# here since nothing outside storage/repositories.py imports them directly).
_SQLITE_ONLY_REPOSITORY_FUNCTIONS = (
    "start_batch_job_run", "finish_batch_job_run", "start_batch_job_item", "finish_batch_job_item",
    "get_last_successful_batch_item_times", "get_latest_batch_item_for_company",
    "list_running_batch_job_runs", "list_batch_job_runs", "list_distinct_batch_job_names",
    "get_latest_batch_job_run", "list_batch_job_items", "get_batch_job_run_live_progress",
    "insert_dataset_event", "get_dataset_event", "list_dataset_events",
    "start_worker_log", "finish_worker_log", "get_worker_log", "list_worker_processing_log",
    "insert_retrieval_diagnostic", "list_retrieval_diagnostics",
    "insert_llm_call_log", "list_llm_call_log", "get_llm_usage_summary", "get_investigation_cost_summary",
    "list_ingestion_queue_items", "get_ingestion_queue_item", "get_ingestion_queue_item_by_path",
    "upsert_ingestion_queue_item", "update_ingestion_queue_item_result", "set_ingestion_queue_item_status",
    "list_reconciliation_log", "list_reconciliation_log_by_company",
)

_WHOLESALE_SWAP_MODULES = (
    ("storage.company_repository", "storage.company_repository_pg"),
    ("storage.fact_store", "storage.fact_store_pg"),
    ("storage.indicator_repository", "storage.indicator_repository_pg"),
    ("storage.investigation_repository", "storage.investigation_repository_pg"),
)

_installed = False


def install() -> None:
    """No-op when DATABASE_BACKEND is unset/"sqlite" (the default) -- the
    live app's behavior is then byte-for-byte what it was before this
    module existed. Safe to call more than once (e.g. from both a script
    and web/app.py in the same process) -- only installs once."""
    global _installed
    if _installed or DATABASE_BACKEND != "postgres":
        return

    import importlib
    import importlib.util

    for original_name, pg_name in _WHOLESALE_SWAP_MODULES:
        sys.modules[original_name] = importlib.import_module(pg_name)

    # storage.repositories itself must be imported (under its real name)
    # BEFORE the hybrid replaces it in sys.modules, so the audit-log
    # functions below are captured from the genuine SQLite module, not
    # from whatever's already sitting in sys.modules at this point.
    sqlite_repositories = importlib.import_module("storage.repositories")
    pg_repositories = importlib.import_module("storage.repositories_pg")

    hybrid = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("storage.repositories", loader=None)
    )
    for name in dir(pg_repositories):
        if not name.startswith("_"):
            setattr(hybrid, name, getattr(pg_repositories, name))
    for name in _SQLITE_ONLY_REPOSITORY_FUNCTIONS:
        setattr(hybrid, name, getattr(sqlite_repositories, name))
    sys.modules["storage.repositories"] = hybrid

    _installed = True
