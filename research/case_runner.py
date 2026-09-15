"""Case Runner -- the one place that turns "start a long-running research
computation" into "an independent, DB-backed research_cases row plus a
background thread that owns it end to end," for every kind of case (Ask AI
today; the 2E-2H investigation pipeline can adopt the same harness later).

Deliberately generic over WHAT a case computes -- run_case_in_background()
takes the actual pipeline as a callable, so this module owns only the case
lifecycle (create -> in_progress -> completed/cancelled/failed) and the
cross-cutting concerns every case needs regardless of kind: its own DB
connection (never request-scoped `g`, never the calling thread's own
connection -- see storage.backend_bootstrap.open_db()'s own reasoning,
same one web/app.py's other -async routes already follow), a Sentry
transaction tagged case_id so every span underneath it (LLM calls,
Postgres/Qdrant/Neo4j queries -- research/assistant.py's own _sentry_span
calls) is grouped and searchable by case, and the InsufficientEvidenceError
/CaseCancelledError translation research/assistant.py's case_id-aware
answer_question() raises.

Never tied to the browser, the originating request, or that request's own
thread: run_case_in_background() starts a NEW daemon thread with its own
DB connection the moment it's called, and that thread runs to completion
regardless of what happens to the HTTP request that created it (the
browser closing, the network dropping, gunicorn finishing its handling of
that one request) -- exactly the guarantee "Continue in Background"
depends on. Case ISOLATION follows from the same design: every case gets
its own connection and its own thread-local call stack, and nothing in
this module (or research/assistant.py's evidence-gathering/LLM-call path)
keeps case-scoped data in a module-level/shared variable -- the only
cross-case shared state anywhere in the pipeline is genuinely global,
read-only-per-call caches (e.g. context/graph_neo4j.py's own knowledge-
graph resync fingerprint), never anything keyed to one case's question or
evidence.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Callable

from research.assistant import CaseCancelledError, InsufficientEvidenceError
from storage.repositories import (
    cancel_research_case,
    complete_research_case,
    create_research_case,
    fail_research_case,
)

logger = logging.getLogger(__name__)


class _NoOpTransaction:
    """Stands in for a real Sentry transaction when sentry_sdk isn't
    installed or config.settings.SENTRY_DSN isn't set (web/app.py's own
    gate) -- every call site below works identically either way."""

    def set_tag(self, *_args, **_kwargs) -> None:
        pass


@contextmanager
def _sentry_transaction(case_id: str, kind: str):
    try:
        import sentry_sdk
    except ImportError:
        yield _NoOpTransaction()
        return

    with sentry_sdk.start_transaction(op="research_case", name=f"case:{kind}") as transaction:
        transaction.set_tag("case_id", case_id)
        transaction.set_tag("case_kind", kind)
        yield transaction


def _capture_exception(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass


def start_case(
    conn, *, case_id: str, kind: str, question: str, company_ids: list[str],
    statement_type: str, owner_id: int | None,
):
    """Synchronous -- called in the real request, on the request's own
    connection, before the background thread starts. Returns the new
    research_cases row immediately so the route can respond with case_id
    right away, same "create then hand off" shape /investigate/generate-
    async and the -ask-async routes already established before this module
    existed."""
    return create_research_case(
        conn, case_id, kind=kind, question=question, company_ids=company_ids,
        statement_type=statement_type, owner_id=owner_id,
    )


def run_case_in_background(
    open_conn: Callable[[], object], case_id: str, kind: str, compute: Callable[[object], dict],
) -> None:
    """Starts the actual background thread. `open_conn` is a zero-arg
    factory (storage.backend_bootstrap.open_db, or scheduling.jobs.open_db)
    -- never a connection captured from the calling request/thread; a
    sqlite3 connection can't cross threads, and even where that isn't true
    (Postgres), reusing a request-scoped connection would tie this case's
    lifetime to the request's, exactly what this module exists to avoid.

    `compute(conn) -> dict` is the actual pipeline -- everything about the
    case's real work (which evidence sources, which LLM call, what gets
    persisted, what "result" shape the Cases UI eventually renders) stays
    in the caller (e.g. web/app.py's _compute_answer_question); this
    function only owns the surrounding lifecycle: create -> run -> exactly
    one of completed(answered)/completed(insufficient_data)/cancelled/failed.
    `compute` is expected to pass this same case_id down to research/
    assistant.py's answer_question(case_id=...) so current_activity
    updates and the sufficiency/cancellation checks actually fire."""

    def _run() -> None:
        conn = open_conn()
        try:
            with _sentry_transaction(case_id, kind):
                try:
                    result = compute(conn)
                except InsufficientEvidenceError as exc:
                    complete_research_case(
                        conn, case_id, outcome="insufficient_data",
                        result_json=json.dumps({"message": str(exc)}),
                    )
                except CaseCancelledError:
                    cancel_research_case(conn, case_id)
                except Exception as exc:  # noqa: BLE001 -- surface any failure to the case row, never a silently stuck case
                    logger.exception("Case %s (kind=%s) failed", case_id, kind)
                    _capture_exception(exc)
                    fail_research_case(conn, case_id, f"{type(exc).__name__}: {exc}")
                else:
                    complete_research_case(
                        conn, case_id, outcome="answered", result_json=json.dumps(result),
                        thread_id=result.get("thread_id"),
                    )
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
