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

All six modules (company_repository, fact_store, indicator_repository,
investigation_repository, price_repository, raw_object_repository,
repositories) are swapped wholesale -- every function in each is ported
1:1 to its _pg sibling. storage.repositories used to need a HYBRID module
instead of a clean swap: 33 of its functions touched tables schemas/
postgres_schema.sql excluded (financial_observations, reconciliation_log,
and the 7 audit/observability tables), citing Neon's free-tier storage
cap. That cap no longer applied by the time production had already grown
past it (771MB, current plan) -- all of those tables were added to
schemas/postgres_schema.sql and storage/repositories_pg.py on 2026-09-13
(see that file's own docstring and docs/ADR/021), closing the gap and
making storage.repositories a clean wholesale swap too.
"""

from __future__ import annotations

import sys

from config.settings import DATABASE_BACKEND

_WHOLESALE_SWAP_MODULES = (
    ("storage.company_repository", "storage.company_repository_pg"),
    ("storage.fact_store", "storage.fact_store_pg"),
    ("storage.indicator_repository", "storage.indicator_repository_pg"),
    ("storage.investigation_repository", "storage.investigation_repository_pg"),
    ("storage.price_repository", "storage.price_repository_pg"),
    ("storage.raw_object_repository", "storage.raw_object_repository_pg"),
    ("storage.repositories", "storage.repositories_pg"),
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

    for original_name, pg_name in _WHOLESALE_SWAP_MODULES:
        sys.modules[original_name] = importlib.import_module(pg_name)

    _installed = True


def open_db():
    """The one correct way for ANY entry point (web/app.py's get_db(),
    scripts/run_job.py's CLI, scripts/fetch_daily_prices*.py's main_conn,
    scheduling/jobs.py's runners) to open a connection meant for
    storage.company_repository / storage.repositories / etc. calls --
    install() first (a no-op if already installed or DATABASE_BACKEND
    isn't postgres), then return a connection on whichever backend is
    actually configured.

    Never use storage.database.init_db() directly for this purpose once a
    module's repository functions might have been swapped to Postgres --
    passing a plain sqlite3 connection into a Postgres-flavored function
    (or vice versa) either raises an AttributeError (sqlite3 connections
    have no `.execute()`) or, just as
    broken, `'sqlite3.Cursor' object does not support the context manager
    protocol` (company_repository_pg.py's `with conn.cursor() as cur:`
    pattern, called with a sqlite3 cursor that doesn't support `with`).
    Found exactly this bug in scripts/fetch_daily_prices.py and
    scripts/fetch_daily_prices_usa.py, both of which hardcoded init_db()
    for their `main_conn` while calling company_repository functions that
    resolve to the Postgres flavor once DATABASE_BACKEND=postgres -- fixed
    by routing them through this function instead."""
    install()
    from config.settings import DATABASE_BACKEND

    if DATABASE_BACKEND == "postgres":
        from storage.database import init_postgres_db

        return init_postgres_db()
    from storage.database import init_db

    return init_db()


def open_price_db():
    """The price-history counterpart of open_db(). Under DATABASE_BACKEND=
    postgres, daily_prices lives in the same Postgres database as
    everything else (see schemas/postgres_schema.sql's comment on that
    table) -- no separate price database exists there, so this opens
    another connection to the same Postgres instance open_db() does,
    rather than storage.price_database's SQLite-only price_history.db.
    Callers that previously called storage.price_database.init_price_db()
    directly (web/app.py's get_price_db(), scripts/fetch_daily_prices*.py,
    scripts/backfill_price_history*.py, ingestion/onboarding.py) must route
    through this instead, same reasoning open_db()'s own docstring gives
    for storage.repositories/company_repository callers -- storage.
    price_repository resolves to price_repository_pg's Postgres-flavored
    functions once DATABASE_BACKEND=postgres, and a plain sqlite3
    connection breaks those the same way it breaks company_repository_pg.
    """
    install()
    from config.settings import DATABASE_BACKEND

    if DATABASE_BACKEND == "postgres":
        from storage.database import init_postgres_db

        return init_postgres_db()
    from storage.price_database import init_price_db

    return init_price_db()
