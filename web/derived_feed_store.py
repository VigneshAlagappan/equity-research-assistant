"""Stored, calculated Financials/Charts feeds (derived_financial_feeds).

build_charts_feed()/build_valuation_feed() make 100-475 queries per call, all
recomputed on every page load. This wraps them: the finished feed is stored
per (company, feed kind, statement type, period type) and served back with one
primary-key read while its fingerprint still matches; a mismatch rebuilds it
and writes it back.

The fingerprint covers every input the payload depends on:
  * the source of the calculation modules (a code change rebuilds everything)
  * latest shares outstanding on file (the reference figure: a split/bonus or
    a new share count changes every restated per-share row)
  * canonical_financials and corporate_actions change markers (new filings,
    restated values, new dividends/splits that shares outstanding alone
    wouldn't reveal)
  * the company's classification (sector drives which rows are shown; the
    fiscal-year-end month drives period placement; currency)
  * the latest price date (charts feed only -- Close/Volume/P-E/P-B rows)

Stored values are calculations, not facts -- this is a read-through store and
never replaces canonical_financials. Any storage failure (table missing,
read-only connection) falls back to computing directly, so a page can never
break because of it.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
from pathlib import Path

import storage.price_repository as price_repo
import storage.repositories as repo
from companies.registry import get_company
from storage.database import utcnow_iso
from storage.db_types import DBConnection

logger = logging.getLogger(__name__)

_CALC_SOURCES = ("charts_feed.py", "valuation_feed.py", "share_adjustment.py")


def _json_default(value):
    # Postgres NUMERIC columns come back as Decimal (price rows etc.).
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _calc_version() -> str:
    h = hashlib.sha1()
    for name in _CALC_SOURCES:
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()[:12]


_CALC_VERSION = _calc_version()


def fingerprint(conn: DBConnection, company_id: str, price_conn: DBConnection | None = None) -> str:
    inputs = repo.get_feed_fingerprint_inputs(conn, company_id)
    company = get_company(conn, company_id)
    cls = (
        [company["fiscal_year_end_month"], company["sector"], company["macro_economic_sector"], company["currency"]]
        if company is not None else None
    )
    latest_price = None
    if price_conn is not None:
        row = price_repo.get_latest_close(price_conn, company_id)
        latest_price = row["trade_date"] if row else None
    return json.dumps([_CALC_VERSION, inputs, cls, latest_price], sort_keys=True, default=str)


def get_or_build(
    conn: DBConnection, company_id: str, feed_kind: str, statement_type: str, period_type: str,
    build, price_conn: DBConnection | None = None,
) -> dict:
    """`build` is a zero-arg callable producing the feed dict."""
    try:
        fp = fingerprint(conn, company_id, price_conn)
        try:
            stored = repo.get_derived_feed(conn, company_id, feed_kind, statement_type, period_type)
        except Exception:
            conn.rollback()
            repo.ensure_derived_feeds_table(conn)
            stored = None
        if stored is not None and stored["fingerprint"] == fp:
            return json.loads(stored["payload"])
    except Exception:
        logger.warning("Derived feed lookup failed for %s; computing directly", company_id, exc_info=True)
        _safe_rollback(conn)
        return build()

    feed = build()
    try:
        repo.upsert_derived_feed(
            conn, company_id, feed_kind, statement_type, period_type, fp,
            json.dumps(feed, default=_json_default), utcnow_iso(),
        )
    except Exception:
        logger.warning("Could not store derived %s feed for %s", feed_kind, company_id, exc_info=True)
        _safe_rollback(conn)
    return feed


def _safe_rollback(conn: DBConnection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def refresh_company(conn: DBConnection, company_id: str, price_conn: DBConnection | None = None) -> int:
    """Bring every stored feed for one company up to date (charts annual/
    quarterly and valuation, consolidated and standalone); returns how many
    were actually rebuilt -- 0 means everything was already current."""
    from web.charts_feed import build_charts_feed
    from web.valuation_feed import build_valuation_feed

    rebuilt = [0]

    def counted(build):
        def run():
            rebuilt[0] += 1
            return build()
        return run

    for statement_type in ("consolidated", "standalone"):
        for period_type in ("annual", "quarterly"):
            get_or_build(
                conn, company_id, "charts", statement_type, period_type,
                counted(lambda st=statement_type, pt=period_type: build_charts_feed(
                    conn, company_id, statement_type=st, period_type=pt, price_conn=price_conn)),
                price_conn=price_conn,
            )
        get_or_build(
            conn, company_id, "valuation", statement_type, "annual",
            counted(lambda st=statement_type: build_valuation_feed(conn, company_id, statement_type=st)),
        )
    return rebuilt[0]
