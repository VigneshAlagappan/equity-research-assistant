"""Read-only helpers for comparing spike findings against this repo's real
Neon database. SELECT-only, single short-timeout queries, never imports or
touches storage/database.py or any production repository class — this spike
must never risk a write path.

Connection string is read from the `NEON` env var (loaded from the real
repo's .env by the caller), the exact same var storage/database.py reads —
but this module opens its own psycopg2 connection directly rather than going
through that module, so there is no route from this spike into any
production write code.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

_READ_ONLY_STATEMENT_TIMEOUT_MS = 15_000


def _load_neon_url() -> str:
    """Load NEON from the real repo's .env (not this worktree's — the spike
    worktree has no .env of its own, untracked files aren't copied by `git
    worktree add`). Falls back to an already-set env var if present."""
    if os.environ.get("NEON"):
        return os.environ["NEON"]
    # Real repo checkout this worktree was created from.
    real_repo_env = Path("/Users/radhamurugesan/work/vicky/repo/equity-research-assistant/.env")
    if real_repo_env.exists():
        from dotenv import dotenv_values
        vals = dotenv_values(real_repo_env)
        if vals.get("NEON"):
            return vals["NEON"]
    raise RuntimeError("NEON connection string not found in env or real repo .env")


def get_readonly_connection():
    """A psycopg2 connection explicitly set read-only at the session level
    (belt-and-suspenders on top of "we only ever call SELECT" below) with a
    short statement timeout so a slow/hanging query can't stall the spike."""
    conn = psycopg2.connect(_load_neon_url(), connect_timeout=15)
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {_READ_ONLY_STATEMENT_TIMEOUT_MS}")
    return conn


def fetch_company_ids(nse_symbols: list[str]) -> dict[str, dict]:
    """symbol -> {company_id, name, nse_symbol} for the given NSE symbols,
    from the real companies table. Single SELECT, read-only connection."""
    conn = get_readonly_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT company_id, display_name, nse_symbol FROM companies WHERE nse_symbol = ANY(%s)",
                (nse_symbols,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r[2]: {"company_id": r[0], "name": r[1], "nse_symbol": r[2]} for r in rows}


def fetch_canonical_financials(company_id: str, fiscal_year: str, quarter: str | None) -> list[dict]:
    """All canonical_financials rows for one company/period — read-only,
    single SELECT. Used only to compare against what a downloaded PDF
    reports; never written to."""
    conn = get_readonly_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT metric_key, statement_type, period_type, canonical_value, unit
                FROM canonical_financials
                WHERE company_id = %s AND fiscal_year = %s
                  AND (quarter = %s OR (%s IS NULL AND quarter IS NULL))
                ORDER BY statement_type, metric_key
                """,
                (company_id, fiscal_year, quarter, quarter),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    cols = ["metric_key", "statement_type", "period_type", "canonical_value", "unit"]
    return [dict(zip(cols, r)) for r in rows]


def table_columns(table_name: str) -> list[str]:
    """Quick schema probe (information_schema, read-only) — used once to
    confirm canonical_financials'/companies' real column names rather than
    guessing, since this spike must not import schemas/*.sql or any
    repository class."""
    conn = get_readonly_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
                (table_name,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
