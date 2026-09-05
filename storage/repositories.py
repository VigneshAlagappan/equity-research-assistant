"""Repository layer: insert observations, reconcile them into canonical_financials.

Conflicting/duplicate observations are never overwritten (README: Source /
Provenance & Reconciliation) — every insert is a new financial_observations
row; reconciliation only decides which one canonical_financials points at,
and records that decision (and every candidate it considered) separately.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from config.settings import to_repo_relative
from ingestion.events import DatasetIngestedEvent
from sources.base import NormalizedObservation
from sources.macro import MacroNormalizedObservation
from sources.rbi_bank_infrastructure import BankInfrastructureObservation
from storage.database import utcnow_iso
from storage.investigation_repository import insert_investigation_companies

NORMALIZATION_VERSION = "v1"


def _normalize_source_file(value: str | None) -> str | None:
    """Source adapters (sources/*.py) pass whatever file_path they were
    given straight through as source_file — usually already-absolute
    (ingestion/coordinator.py resolves data/raw/ files against BASE_DIR
    before handing them to a source adapter), which bakes today's repo
    location into the row forever (config/settings.py's to_repo_relative()
    docstring: this repo has already been renamed once, silently breaking
    every previously-stored absolute path). Only real filesystem paths get
    relativized — a synthetic identifier like "fred:FEDFUNDS" or
    "yfinance:AAPL" (never absolute) is left untouched rather than risking
    to_repo_relative() misinterpreting it as a path."""
    if value and Path(value).is_absolute():
        return to_repo_relative(value)
    return value


def insert_financial_observations(
    conn: sqlite3.Connection, observations: Iterable[NormalizedObservation]
) -> list[int]:
    """Insert each observation as a new row. Returns the assigned observation_ids."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO financial_observations (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                value, unit, currency, source, source_document_id, source_file, source_url,
                retrieved_at, parser_version, normalization_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter,
                obs.statement_type, obs.value, obs.unit, obs.currency, obs.source, _normalize_source_file(obs.source_file),
                obs.source_url, obs.retrieved_at or now, obs.parser_version, NORMALIZATION_VERSION, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


#: Source policy (2026-08 NSE XBRL directive): once a reporting period has a
#: validated NSE observation on file, NSE XBRL is the sole source of truth
#: for structured financial facts in that period — a metric it didn't report
#: must stay blank, never fall back to legacy. This is the source_id
#: reconcile() treats that way; extending to BSE happens once a BSE adapter
#: exists (they're tied on trust_rank already — see config/settings.py).
XBRL_SOURCE_ID = "nse"


def _period_is_xbrl_migrated(
    conn: sqlite3.Connection,
    company_id: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None,
    statement_type: str | None,
) -> bool:
    """Whether ANY validated XBRL_SOURCE_ID observation exists for this exact
    (company, period_type, fiscal_year, quarter, statement_type) scope —
    regardless of which metric_key it's for. "Validated" today just means
    "present as an nse-sourced observation": there's no separate filing
    -validation gate yet (Open Decisions), so ingestion itself is the only
    checkpoint. standalone/consolidated are independently migrated since
    statement_type is part of the scope, same as everywhere else in this
    module — an XBRL consolidated filing never affects standalone
    reconciliation for the same period, and vice versa."""
    row = conn.execute(
        """
        SELECT 1 FROM financial_observations
        WHERE company_id = ? AND source = ? AND period_type = ?
          AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
        LIMIT 1
        """,
        (company_id, XBRL_SOURCE_ID, period_type, fiscal_year, quarter, statement_type),
    ).fetchone()
    return row is not None


def reconcile(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None,
    statement_type: str | None,
) -> int | None:
    """Reconcile every observation for one (company, metric, period) key.

    Multiple sources are handled by picking the lowest sources.trust_rank,
    most-recent observation as tiebreak. One case is special-cased ahead of
    trust_rank, not just ordered by it: once _period_is_xbrl_migrated() is
    true for this key's period scope, only XBRL_SOURCE_ID observations are
    eligible candidates for ANY metric in that scope — even one XBRL simply
    didn't report — rather than silently falling back to a legacy source.
    That's a real behavioral carve-out (not something a trust_rank number
    alone can express), because trust_rank only ranks candidates that exist
    for this exact metric; it can't say "this period is XBRL's now, so a
    metric XBRL is silent on should go blank, not legacy". See
    config/settings.py's DEFAULT_SOURCES comment for the source policy this
    implements.

    Returns the canonical_id, or None if there are no eligible observations
    for this key (either genuinely none on file, or every candidate was
    rejected as legacy-in-a-migrated-period) — in the latter case, any
    stale canonical_financials row from before migration is deleted rather
    than left showing a pre-migration legacy value.
    """
    rows = conn.execute(
        """
        SELECT fo.observation_id, fo.value, fo.unit, fo.source, fo.retrieved_at, s.trust_rank
        FROM financial_observations fo
        JOIN sources s ON s.source_id = fo.source
        WHERE fo.company_id = ? AND fo.metric_key = ? AND fo.period_type = ?
          AND fo.fiscal_year = ? AND fo.quarter IS ? AND fo.statement_type IS ?
        ORDER BY fo.retrieved_at ASC, fo.observation_id ASC
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchall()

    def _delete_stale_canonical_row() -> None:
        # reconciliation_log rows from the pre-migration reconciliation still
        # reference this canonical_id (nullable FK, no ON DELETE CASCADE) —
        # null those references out first so the DELETE below doesn't trip
        # the foreign-key constraint; the log rows themselves are kept (audit
        # trail), just no longer pointing at a canonical row that no longer
        # exists.
        conn.execute(
            """
            UPDATE reconciliation_log SET canonical_id = NULL
            WHERE canonical_id IN (
                SELECT canonical_id FROM canonical_financials
                WHERE company_id = ? AND metric_key = ? AND period_type = ?
                  AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
            )
            """,
            (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
        )
        conn.execute(
            """
            DELETE FROM canonical_financials
            WHERE company_id = ? AND metric_key = ? AND period_type = ?
              AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
            """,
            (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
        )
        conn.commit()

    if not rows:
        return None

    # Most recent observation per source (rows are ordered oldest-to-newest,
    # tie-broken on the strictly-increasing observation_id since wall-clock
    # retrieved_at can collide at the OS clock's resolution between two
    # back-to-back ingestions — so the last write per source wins, and a
    # re-ingested file reliably supersedes the old one).
    latest_per_source: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest_per_source[row["source"]] = row
    candidates = list(latest_per_source.values())

    all_candidates = candidates  # kept for the audit-log loop below even once `candidates` is narrowed
    migrated = _period_is_xbrl_migrated(conn, company_id, period_type, fiscal_year, quarter, statement_type)
    if migrated:
        xbrl_candidates = [row for row in candidates if row["source"] == XBRL_SOURCE_ID]
        if not xbrl_candidates:
            # Period is on XBRL now, but this metric wasn't in the filing —
            # blank, not backfilled from whatever legacy candidates exist.
            _delete_stale_canonical_row()
            now = utcnow_iso()
            for row in candidates:
                conn.execute(
                    """
                    INSERT INTO reconciliation_log (canonical_id, observation_id, considered_at, was_chosen, note)
                    VALUES (NULL, ?, ?, 0, ?)
                    """,
                    (
                        row["observation_id"], now,
                        f"not chosen (source={row['source']}): period migrated to validated "
                        f"{XBRL_SOURCE_ID!r} XBRL and this metric wasn't in the filing — left blank, not legacy-filled",
                    ),
                )
            conn.commit()
            return None
        candidates = xbrl_candidates

    def sort_key(row: sqlite3.Row) -> tuple[int, str, int]:
        rank = row["trust_rank"] if row["trust_rank"] is not None else 999
        return (rank, row["retrieved_at"], row["observation_id"])

    chosen = min(candidates, key=sort_key)
    if migrated:
        reason = f"source '{chosen['source']}' — period validated on NSE XBRL"
    else:
        reason = (
            "only source available"
            if len(candidates) == 1
            else f"source '{chosen['source']}' preferred by trust_rank"
        )

    # SQLite's UNIQUE constraint (and therefore ON CONFLICT) never fires when
    # a NULL participates in the unique columns — quarter/statement_type are
    # NULL for annual observations, so "INSERT ... ON CONFLICT DO UPDATE"
    # would silently insert a duplicate canonical row every re-run instead of
    # updating the existing one. Do the upsert explicitly with a NULL-safe
    # (IS) lookup instead.
    now = utcnow_iso()
    existing = conn.execute(
        """
        SELECT canonical_id FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ?
          AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchone()

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO canonical_financials (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                canonical_value, unit, chosen_observation_id, reconciliation_reason,
                normalization_version, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id, metric_key, period_type, fiscal_year, quarter, statement_type,
                chosen["value"], chosen["unit"], chosen["observation_id"], reason,
                NORMALIZATION_VERSION, now,
            ),
        )
        canonical_id = cursor.lastrowid
    else:
        canonical_id = existing["canonical_id"]
        conn.execute(
            """
            UPDATE canonical_financials SET
                canonical_value = ?, unit = ?, chosen_observation_id = ?,
                reconciliation_reason = ?, normalization_version = ?, decided_at = ?
            WHERE canonical_id = ?
            """,
            (chosen["value"], chosen["unit"], chosen["observation_id"], reason,
             NORMALIZATION_VERSION, now, canonical_id),
        )

    for row in all_candidates:
        was_chosen = row["observation_id"] == chosen["observation_id"]
        note_for_rejected = (
            f"not chosen (source={row['source']}, trust_rank={row['trust_rank']}): "
            f"period validated on NSE XBRL — legacy sources aren't eligible for this period"
            if migrated and row["source"] != XBRL_SOURCE_ID
            else None
        )
        conn.execute(
            """
            INSERT INTO reconciliation_log (canonical_id, observation_id, considered_at, was_chosen, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                canonical_id, row["observation_id"], now, 1 if was_chosen else 0,
                reason if was_chosen else (
                    note_for_rejected or f"not chosen (source={row['source']}, trust_rank={row['trust_rank']})"
                ),
            ),
        )
    conn.commit()
    return canonical_id


def get_canonical_value(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str,
    fiscal_year: str,
    quarter: str | None = None,
    statement_type: str | None = "consolidated",
) -> sqlite3.Row | None:
    """Fetch the single reconciled value for one (company, metric, period) key."""
    return conn.execute(
        """
        SELECT * FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ?
          AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
        """,
        (company_id, metric_key, period_type, fiscal_year, quarter, statement_type),
    ).fetchone()


def get_canonical_series(
    conn: sqlite3.Connection,
    company_id: str,
    metric_key: str,
    period_type: str = "annual",
    statement_type: str | None = "consolidated",
) -> list[sqlite3.Row]:
    """Fetch every reconciled value for a metric, oldest to newest.

    Ordered by fiscal_year, then quarter (Q1..Q4) for quarterly series — plain
    lexicographic fiscal_year ordering works because "FY2024" < "FY2025" sorts
    correctly as text.
    """
    return conn.execute(
        """
        SELECT * FROM canonical_financials
        WHERE company_id = ? AND metric_key = ? AND period_type = ? AND statement_type IS ?
        ORDER BY fiscal_year ASC, quarter ASC
        """,
        (company_id, metric_key, period_type, statement_type),
    ).fetchall()


def list_canonical_financials_for_companies(conn: sqlite3.Connection, company_ids: list[str]) -> list[sqlite3.Row]:
    """Every canonical_financials row for the given companies in one query,
    joined with metrics_dictionary for a human-readable display_name/
    category -- the bulk read context/graph_neo4j.py's sync_financials()/
    sync_financials_if_changed() need to project canonical_financials into
    Neo4j (decided_at included for the latter's change-detection
    fingerprint). Deliberately company-scoped, not a list-everything query:
    canonical_financials runs to 1000+ rows per company, and
    get_canonical_series() above is per-metric/per-company by design
    (retrieval/structured_search.py's normal access pattern) -- this is the
    one place a bulk multi-metric read is actually needed."""
    if not company_ids:
        return []
    placeholders = ",".join("?" * len(company_ids))
    return conn.execute(
        f"""
        SELECT cf.company_id, cf.metric_key, cf.period_type, cf.fiscal_year, cf.quarter,
               cf.statement_type, cf.canonical_value, cf.unit, cf.decided_at,
               md.display_name, md.category
        FROM canonical_financials cf
        LEFT JOIN metrics_dictionary md ON md.metric_key = cf.metric_key
        WHERE cf.company_id IN ({placeholders})
        """,
        company_ids,
    ).fetchall()


def list_latest_shares_outstanding(conn: sqlite3.Connection) -> dict[str, tuple[float, str]]:
    """Latest reconciled (shares-outstanding value in Cr, its fiscal year e.g.
    "FY2014") per company, one query for every company at once — used to
    compute market cap on the Companies list and to drive the company page's
    Overview ratio grid, where a get_canonical_series() call per row (of
    ~2,500 on the list) would be an N+1. Sparse: only companies with at
    least one shares_outstanding observation appear.

    Across BOTH period_type='annual' and 'quarterly' rows, not annual-only —
    a company's quarterly XBRL filing reports its own share count every
    quarter, and that can move between annual filings (ESOP allotments,
    buybacks, rights issues); restricting to annual-only meant a company
    with a fresher quarterly filing than its last annual one still showed
    the stale annual figure (verified live: ICICIBANK's FY2026 annual row
    is 716.115 Cr shares, but its Q1 FY2027 quarterly row -- filed after
    that annual figure -- already shows 717.47 Cr). ORDER BY fiscal_year
    DESC, quarter DESC naturally picks whichever period is chronologically
    latest regardless of which period_type it came from: quarter is NULL on
    an annual row, and SQLite sorts NULL last in a DESC ordering, so an
    annual and same-fiscal-year Q4 row (normally identical in value anyway)
    only matters as a tie-break -- a later fiscal_year string always wins
    first regardless.

    The fiscal year comes along because "latest on file" and "current" are
    not the same thing here: a company whose financial-statement ingestion
    stalled years ago (common in this dataset — as of 2026-08, most
    companies' newest shares_outstanding row is FY2013/FY2014) still has
    exactly one "latest" row, just a stale one. A caller computing market
    cap from it needs the fiscal year to decide whether that's current
    enough to show at all, not just the value — see web/app.py's
    _is_shares_outstanding_current()."""
    rows = conn.execute(
        """
        SELECT company_id, canonical_value, fiscal_year FROM (
            SELECT company_id, canonical_value, fiscal_year,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_id ORDER BY fiscal_year DESC, quarter DESC
                   ) AS rn
            FROM canonical_financials
            WHERE metric_key = 'shares_outstanding' AND statement_type = 'consolidated'
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["company_id"]: (row["canonical_value"], row["fiscal_year"]) for row in rows}


# ------------------------------------------------------------------
# Metric vocabulary (metrics_dictionary / metric_aliases) — used by
# normalization/financials.py to seed/resolve the vendor-label -> metric_key
# mapping, and by financials/ratios.py to check a metric's applicable_sectors.
# ------------------------------------------------------------------


def seed_metric_vocabulary(
    conn: sqlite3.Connection, metrics: Iterable[tuple], aliases: Iterable[tuple]
) -> None:
    """Bulk-insert metrics_dictionary/metric_aliases rows, leaving existing
    ones untouched (INSERT OR IGNORE)."""
    conn.executemany(
        """
        INSERT OR IGNORE INTO metrics_dictionary
            (metric_key, display_name, category, applicable_sectors, default_unit)
        VALUES (?, ?, ?, ?, ?)
        """,
        metrics,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO metric_aliases (source, raw_label, metric_key) VALUES (?, ?, ?)",
        aliases,
    )
    conn.commit()


def get_metric_key_for_alias(conn: sqlite3.Connection, source: str, raw_label: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT metric_key FROM metric_aliases WHERE source = ? AND raw_label = ?",
        (source, raw_label),
    ).fetchone()


def get_metric_dictionary_entry(conn: sqlite3.Connection, metric_key: str) -> sqlite3.Row | None:
    """Full metrics_dictionary row for one metric_key (default_unit,
    applicable_sectors, ...) — normalization/financials.py reads
    default_unit from it, financials/ratios.py reads applicable_sectors."""
    return conn.execute(
        "SELECT * FROM metrics_dictionary WHERE metric_key = ?", (metric_key,)
    ).fetchone()


def compute_reconciliation_keys(
    conn: sqlite3.Connection, observations: Iterable[NormalizedObservation]
) -> list[tuple[str, str, str, str, str | None, str | None]]:
    """Every distinct (company, metric, period_type, fiscal_year, quarter,
    statement_type) key a batch of observations touches, expanded to
    include XBRL's migration side effect.

    When the batch includes an XBRL_SOURCE_ID observation, that also
    "migrates" its (company, period_type, fiscal_year, quarter,
    statement_type) scope (reconcile()'s docstring) — every OTHER metric
    already on file for that same period must be re-reconciled too, not
    just the metrics this particular filing happened to report, or a
    legacy metric XBRL is silent on would keep showing its stale
    pre-migration canonical value instead of going blank.

    Pulled out of reconcile_batch() so ingestion/pipeline.py can compute the
    same key set to publish on a DATASET_INGESTED event's
    storage_reference — the Financial Derivation Worker
    (ingestion/workers/financial_derivation.py) reconciles from those keys
    alone, without needing the observations themselves re-passed through
    the event or re-ingested on replay.
    """
    observations = list(observations)
    keys = {
        (obs.company_id, obs.metric_key, obs.period_type, obs.fiscal_year, obs.quarter, obs.statement_type)
        for obs in observations
    }

    period_scopes = {
        (obs.company_id, obs.period_type, obs.fiscal_year, obs.quarter, obs.statement_type)
        for obs in observations if obs.source == XBRL_SOURCE_ID
    }
    for company_id, period_type, fiscal_year, quarter, statement_type in period_scopes:
        sibling_metrics = conn.execute(
            """
            SELECT DISTINCT metric_key FROM financial_observations
            WHERE company_id = ? AND period_type = ? AND fiscal_year = ? AND quarter IS ? AND statement_type IS ?
            """,
            (company_id, period_type, fiscal_year, quarter, statement_type),
        ).fetchall()
        for row in sibling_metrics:
            keys.add((company_id, row["metric_key"], period_type, fiscal_year, quarter, statement_type))

    return list(keys)


def reconcile_batch(conn: sqlite3.Connection, observations: Iterable[NormalizedObservation]) -> int:
    """Reconcile every distinct (company, metric, period) key touched by a
    batch of observations. See compute_reconciliation_keys() for how that
    key set (including XBRL's migration side effect) is derived."""
    keys = compute_reconciliation_keys(conn, observations)
    return sum(1 for key in keys if reconcile(conn, *key) is not None)


def reconcile_company(conn: sqlite3.Connection, company_id: str) -> int:
    """Re-derive canonical_financials for every (metric, period) key this
    company has any observation for, from what's already stored — no new
    upload needed. For when metric_aliases or a source's trust_rank changes
    after the fact and existing data should reflect it, not just future
    ingests (which already re-reconcile the periods they touch via
    reconcile_batch — this is the same logic, just re-run on demand over
    everything on file instead of only a fresh batch's own periods)."""
    keys = conn.execute(
        """
        SELECT DISTINCT metric_key, period_type, fiscal_year, quarter, statement_type
        FROM financial_observations WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()
    return sum(
        1
        for row in keys
        if reconcile(
            conn, company_id, row["metric_key"], row["period_type"],
            row["fiscal_year"], row["quarter"], row["statement_type"],
        )
        is not None
    )


def list_reconciliation_log(
    conn: sqlite3.Connection,
    *,
    company_id: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Recent reconciliation_log entries, newest first — the Admin Audit Log
    panel's raw decision trail (every candidate reconcile() considered for
    a (company, metric, period) key, chosen or not, with why). Joined
    through financial_observations for company_id/metric_key/period/source,
    since reconciliation_log itself only stores observation_id/canonical_id
    — canonical_id can be NULL (a candidate rejected outright, or a stale
    canonical row deleted after a period migrated to XBRL — see reconcile()'s
    _delete_stale_canonical_row), so joining via canonical_financials would
    silently drop exactly the rows an XBRL-migration audit most needs to
    show."""
    query = """
        SELECT rl.log_id, rl.considered_at, rl.was_chosen, rl.note,
               fo.company_id, fo.metric_key, fo.period_type, fo.fiscal_year,
               fo.quarter, fo.statement_type, fo.source
        FROM reconciliation_log rl
        JOIN financial_observations fo ON fo.observation_id = rl.observation_id
        WHERE 1=1
    """
    params: list[object] = []
    if company_id:
        query += " AND fo.company_id = ?"
        params.append(company_id)
    if source:
        query += " AND fo.source = ?"
        params.append(source)
    query += " ORDER BY rl.considered_at DESC, rl.log_id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def list_reconciliation_log_by_company(
    conn: sqlite3.Connection, company_ids: Iterable[str], *, limit_per_company: int = 20
) -> dict[str, list[sqlite3.Row]]:
    """Recent reconciliation_log entries for a batch of companies at once,
    capped per company — the Admin Audit Log table's per-row expandable
    detail (list_xbrl_migration_status()'s companion), fetched for a whole
    page of companies in one query rather than one query per row (the N+1
    admin.html's own Companies-panel comment already flags as a real
    problem at ~2,600 rows). ROW_NUMBER()-per-company keeps this bounded
    even for a company with a long history (IDFCFIRSTB already has 130+
    entries and growing), same windowing approach list_latest_shares_outstanding()
    already uses for "top N per company" from one query."""
    ids = list(company_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT rl.log_id, rl.considered_at, rl.was_chosen, rl.note,
                   fo.company_id, fo.metric_key, fo.period_type, fo.fiscal_year,
                   fo.quarter, fo.statement_type, fo.source,
                   ROW_NUMBER() OVER (
                       PARTITION BY fo.company_id ORDER BY rl.considered_at DESC, rl.log_id DESC
                   ) AS rn
            FROM reconciliation_log rl
            JOIN financial_observations fo ON fo.observation_id = rl.observation_id
            WHERE fo.company_id IN ({placeholders})
        )
        WHERE rn <= ?
        ORDER BY company_id, considered_at DESC
        """,
        (*ids, limit_per_company),
    ).fetchall()
    by_company: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_company.setdefault(row["company_id"], []).append(row)
    return by_company


def list_xbrl_migration_status(conn: sqlite3.Connection) -> list[dict]:
    """Per NSE-listed active company: the latest quarterly period on file
    from ANY source vs the latest one specifically validated on NSE XBRL —
    the Admin Audit Log panel's "what's pending" view (source policy: NSE
    XBRL as the target source of truth for structured financial facts).

    A company is "pending" when legacy data extends past where XBRL
    coverage currently reaches (fo.source='nse' has never overtaken it);
    "not_started" when there's no XBRL coverage on file at all yet, but
    there is legacy quarterly data to eventually migrate; "no_data" when
    there's no quarterly data of any kind. Scoped to quarterly — that's
    XBRL's actual reporting cadence today (sources/nse_xbrl.py) — and to
    companies with an nse_symbol, since only those are fetchable from NSE's
    API at all (scripts/fetch_nse_xbrl.py).

    fiscal_year||quarter (e.g. "FY2025Q3") sorts correctly as plain text
    since fiscal_year is always "FY" + 4 digits and quarter is always a
    single "Q1".."Q4" — MAX()/< on that concatenation is a valid
    chronological comparison without parsing it apart.
    """
    coverage_rows = conn.execute(
        """
        SELECT company_id,
               MAX(CASE WHEN source = 'nse' THEN fiscal_year || quarter END) AS latest_xbrl_period,
               MAX(fiscal_year || quarter) AS latest_any_period
        FROM financial_observations
        WHERE period_type = 'quarterly'
        GROUP BY company_id
        """
    ).fetchall()
    coverage_by_company = {row["company_id"]: row for row in coverage_rows}

    companies = conn.execute(
        """
        SELECT company_id, display_name, nse_symbol FROM companies
        WHERE nse_symbol IS NOT NULL AND nse_symbol != '' AND status = 'active'
        """
    ).fetchall()

    _STATUS_ORDER = {"pending": 0, "not_started": 1, "no_data": 2, "up_to_date": 3}
    results: list[dict] = []
    for company in companies:
        coverage = coverage_by_company.get(company["company_id"])
        latest_xbrl = coverage["latest_xbrl_period"] if coverage else None
        latest_any = coverage["latest_any_period"] if coverage else None
        if latest_any is None:
            migration_status = "no_data"
        elif latest_xbrl is None:
            migration_status = "not_started"
        elif latest_xbrl < latest_any:
            migration_status = "pending"
        else:
            migration_status = "up_to_date"
        results.append(
            {
                "company_id": company["company_id"],
                "display_name": company["display_name"],
                "nse_symbol": company["nse_symbol"],
                "latest_xbrl_period": latest_xbrl,
                "latest_legacy_period": latest_any,
                "migration_status": migration_status,
            }
        )
    results.sort(key=lambda r: (_STATUS_ORDER[r["migration_status"]], r["display_name"] or ""))
    return results


WATCHLIST_ITEM_TYPES = ("company", "thread")


def add_watchlist_item(conn: sqlite3.Connection, item_type: str, item_ref: str) -> int:
    """Pin a company or research thread. Re-pinning an already-pinned item is a no-op
    (README: single shared watchlist, so there's no per-user pin to distinguish)."""
    if item_type not in WATCHLIST_ITEM_TYPES:
        raise ValueError(f"item_type must be one of {WATCHLIST_ITEM_TYPES}, got {item_type!r}")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watchlist_items (item_type, item_ref, pinned_at) VALUES (?, ?, ?)",
        (item_type, item_ref, utcnow_iso()),
    )
    conn.commit()
    if cursor.lastrowid and cursor.rowcount:
        return cursor.lastrowid
    return conn.execute(
        "SELECT item_id FROM watchlist_items WHERE item_type = ? AND item_ref = ?",
        (item_type, item_ref),
    ).fetchone()["item_id"]


def remove_watchlist_item(conn: sqlite3.Connection, item_type: str, item_ref: str) -> None:
    conn.execute(
        "DELETE FROM watchlist_items WHERE item_type = ? AND item_ref = ?", (item_type, item_ref)
    )
    conn.commit()


def list_watchlist_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most recently pinned first."""
    return conn.execute(
        "SELECT * FROM watchlist_items ORDER BY pinned_at DESC"
    ).fetchall()


def is_watchlisted(conn: sqlite3.Connection, item_type: str, item_ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM watchlist_items WHERE item_type = ? AND item_ref = ?", (item_type, item_ref)
    ).fetchone()
    return row is not None


def get_company_insights(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    """Most recently generated insight for this company, if any."""
    return conn.execute(
        "SELECT * FROM company_insights WHERE company_id = ? ORDER BY generated_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()


def list_company_insights(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every generated insight for this company, most recent first."""
    return conn.execute(
        "SELECT * FROM company_insights WHERE company_id = ? ORDER BY generated_at DESC",
        (company_id,),
    ).fetchall()


def save_company_insights(conn: sqlite3.Connection, company_id: str, insight_text: str, statement_type: str) -> None:
    """Every generate/regenerate inserts a new row — company_insights keeps
    history against generated_at rather than overwriting (README: Overview tab,
    user-triggered, never auto-regenerated)."""
    conn.execute(
        "INSERT INTO company_insights (company_id, insight_text, statement_type, generated_at) VALUES (?, ?, ?, ?)",
        (company_id, insight_text, statement_type, utcnow_iso()),
    )
    conn.commit()


def save_system_insight(
    conn: sqlite3.Connection, *, insight_id: str, company_ids: list[str], insight_text: str,
    source_claim_ids: list[int],
) -> None:
    """One row per system-generated insight (Tools tab), status='new' until
    the user retains/archives it via update_system_insight_status()."""
    conn.execute(
        "INSERT INTO system_insights (insight_id, company_ids, insight_text, source_claim_ids, status, generated_at) "
        "VALUES (?, ?, ?, ?, 'new', ?)",
        (insight_id, json.dumps(company_ids), insight_text, json.dumps(source_claim_ids), utcnow_iso()),
    )
    conn.commit()


def list_system_insights(conn: sqlite3.Connection, *, statuses: tuple[str, ...] = ("new", "retained")) -> list[dict]:
    """Insights in any of `statuses`, newest first — defaults to everything
    the Insights panel shows by default (archived hidden unless asked for)."""
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM system_insights WHERE status IN ({placeholders}) ORDER BY generated_at DESC",
        statuses,
    ).fetchall()
    return [
        {
            "insight_id": r["insight_id"], "company_ids": json.loads(r["company_ids"]),
            "insight_text": r["insight_text"],
            "source_claim_ids": json.loads(r["source_claim_ids"]) if r["source_claim_ids"] else [],
            "status": r["status"], "generated_at": r["generated_at"], "status_changed_at": r["status_changed_at"],
        }
        for r in rows
    ]


def update_system_insight_status(conn: sqlite3.Connection, insight_id: str, status: str) -> None:
    if status not in ("new", "retained", "archived"):
        raise ValueError(f"status must be one of new|retained|archived, got {status!r}")
    conn.execute(
        "UPDATE system_insights SET status = ?, status_changed_at = ? WHERE insight_id = ?",
        (status, utcnow_iso(), insight_id),
    )
    conn.commit()


def list_recent_high_confidence_claims(
    conn: sqlite3.Connection, *, claim_types: tuple[str, ...], limit: int = 10
) -> list[sqlite3.Row]:
    """Candidate claims for system-insight generation (research/system_insights.py)
    — the "give me everything interesting across companies" read
    find_claims_about_entity() can't do (it requires naming one entity
    first). Highest-confidence, most-recent claims of the given types,
    across every company."""
    placeholders = ",".join("?" for _ in claim_types)
    return conn.execute(
        f"SELECT * FROM knowledge_claims WHERE claim_type IN ({placeholders}) "
        "ORDER BY extraction_confidence DESC, created_at DESC LIMIT ?",
        (*claim_types, limit),
    ).fetchall()


def list_company_ids_with_financial_data(conn: sqlite3.Connection) -> list[str]:
    """Distinct companies with at least one canonical_financials row — the
    cheap prefilter analytics/patterns.py uses instead of iterating every
    registered company (most of which have nothing ingested yet)."""
    return [r["company_id"] for r in conn.execute("SELECT DISTINCT company_id FROM canonical_financials").fetchall()]


def list_company_notes(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every personal note logged against this company, most recent first."""
    return conn.execute(
        "SELECT * FROM company_notes WHERE company_id = ? ORDER BY created_at DESC",
        (company_id,),
    ).fetchall()


def save_company_note(conn: sqlite3.Connection, company_id: str, note_text: str) -> sqlite3.Row:
    """Append-only — no edit/delete yet, same as company_insights history."""
    cursor = conn.execute(
        "INSERT INTO company_notes (company_id, note_text, created_at) VALUES (?, ?, ?)",
        (company_id, note_text, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM company_notes WHERE note_id = ?", (cursor.lastrowid,)).fetchone()


def update_company_note(conn: sqlite3.Connection, company_id: str, note_id: int, note_text: str) -> sqlite3.Row | None:
    """None if no note with that id exists for this company — the route
    treats that as a 404, not a silent no-op."""
    cursor = conn.execute(
        "UPDATE company_notes SET note_text = ?, updated_at = ? WHERE note_id = ? AND company_id = ?",
        (note_text, utcnow_iso(), note_id, company_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return conn.execute("SELECT * FROM company_notes WHERE note_id = ?", (note_id,)).fetchone()


def delete_company_note(conn: sqlite3.Connection, company_id: str, note_id: int) -> bool:
    cursor = conn.execute("DELETE FROM company_notes WHERE note_id = ? AND company_id = ?", (note_id, company_id))
    conn.commit()
    return cursor.rowcount > 0


def list_note_attachments(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE note_id = ? ORDER BY uploaded_at", (note_id,)
    ).fetchall()


def list_note_attachments_for_company(conn: sqlite3.Connection, company_id: str) -> dict[int, list[sqlite3.Row]]:
    """Every attachment for every note this company has, grouped by note_id —
    one query for the whole Notes tab instead of one per note."""
    rows = conn.execute(
        """
        SELECT a.* FROM company_note_attachments a
        JOIN company_notes n ON n.note_id = a.note_id
        WHERE n.company_id = ?
        ORDER BY a.uploaded_at
        """,
        (company_id,),
    ).fetchall()
    by_note: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_note.setdefault(row["note_id"], []).append(row)
    return by_note


def get_note_attachment(conn: sqlite3.Connection, note_id: int, attachment_id: int) -> sqlite3.Row | None:
    """Scoped by note_id too, same reasoning as get_company_document."""
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE attachment_id = ? AND note_id = ?",
        (attachment_id, note_id),
    ).fetchone()


def save_note_attachment(
    conn: sqlite3.Connection, note_id: int, filename: str, raw_file_path: str, size_bytes: int
) -> sqlite3.Row:
    cursor = conn.execute(
        "INSERT INTO company_note_attachments (note_id, filename, raw_file_path, size_bytes, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (note_id, filename, raw_file_path, size_bytes, utcnow_iso()),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM company_note_attachments WHERE attachment_id = ?", (cursor.lastrowid,)
    ).fetchone()


def delete_note_attachment(conn: sqlite3.Connection, note_id: int, attachment_id: int) -> sqlite3.Row | None:
    """Returns the deleted row (so the caller can remove its on-disk file
    too) or None if it didn't exist under this note_id."""
    row = get_note_attachment(conn, note_id, attachment_id)
    if row is None:
        return None
    conn.execute(
        "DELETE FROM company_note_attachments WHERE attachment_id = ? AND note_id = ?", (attachment_id, note_id)
    )
    conn.commit()
    return row


def list_company_periods(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every (fiscal_year, quarter) this company actually has quarterly
    financials for — the Docs tab's real period list, in place of a
    hardcoded range. Companies without quarterly-granularity ingestion
    (e.g. Proprietary-adapter-only, or sources/yfinance_financials.py's
    annual-only pilot) get an empty list, not a fabricated one."""
    return conn.execute(
        """
        SELECT DISTINCT fiscal_year, quarter FROM canonical_financials
        WHERE company_id = ? AND period_type = 'quarterly' AND quarter IS NOT NULL
        ORDER BY fiscal_year, quarter
        """,
        (company_id,),
    ).fetchall()


def list_company_annual_years(conn: sqlite3.Connection, company_id: str) -> list[str]:
    """Every fiscal_year this company has *annual* financials for — separate
    from list_company_periods (quarterly) so the Docs tab's year groups can
    exist for an annual-only company (no quarterly ingestion at all, e.g.
    every company sources/yfinance_financials.py has ingested so far) instead
    of the year list being entirely gated on quarterly data existing."""
    rows = conn.execute(
        "SELECT DISTINCT fiscal_year FROM canonical_financials WHERE company_id = ? AND period_type = 'annual'",
        (company_id,),
    ).fetchall()
    return [row["fiscal_year"] for row in rows]


def list_company_documents(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every document on file for this company — official and manually-added
    alike (added_by_user is NULL for the former)."""
    return conn.execute(
        "SELECT * FROM documents WHERE company_id = ? ORDER BY fiscal_year, quarter",
        (company_id,),
    ).fetchall()


def get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    """Not company-scoped — for the Ingest queue's coordinator, which
    already has document_id from ingestion_queue_items/documents-by-status
    and has no separate company_id to filter by (unlike
    get_company_document(), which exists for the company-facing routes)."""
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()


def get_company_document(conn: sqlite3.Connection, company_id: str, document_id: int) -> sqlite3.Row | None:
    """Scoped by company_id too — a document's id alone shouldn't be enough
    to serve someone else's file from a mistyped/guessed URL."""
    return conn.execute(
        "SELECT * FROM documents WHERE document_id = ? AND company_id = ?",
        (document_id, company_id),
    ).fetchone()


def save_company_document(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    document_type: str,
    fiscal_year: str,
    quarter: str | None,
    added_by_user: str,
    raw_file_path: str | None = None,
    source_url: str | None = None,
) -> sqlite3.Row:
    """Manually-added documents only, via the Docs tab's Add form —
    officially-sourced rows (added_by_user NULL) would come from a future
    data-provider ingestion path, which doesn't exist yet."""
    now = utcnow_iso()
    cursor = conn.execute(
        """
        INSERT INTO documents (company_id, document_type, fiscal_year, quarter,
                                raw_file_path, source_url, added_by_user, retrieved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, document_type, fiscal_year, quarter, raw_file_path, source_url, added_by_user, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (cursor.lastrowid,)).fetchone()


def list_documents_by_status(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    """Every document across every company at this processing_status (or,
    with status=None, at any status) — unlike list_company_documents, not
    scoped to one company, since the Admin -> Ingest queue view spans the
    whole document archive. Same optional-status convention as
    list_ingestion_queue_items."""
    if status is None:
        return conn.execute("SELECT * FROM documents ORDER BY retrieved_at DESC").fetchall()
    return conn.execute(
        "SELECT * FROM documents WHERE processing_status = ? ORDER BY retrieved_at DESC",
        (status,),
    ).fetchall()


def mark_document_processing_status(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    status: str,
    file_hash: str | None = None,
    processed_at: str | None = None,
    error_message: str | None = None,
) -> sqlite3.Row | None:
    """file_hash is only overwritten when explicitly given (e.g. computed
    fresh during Ingest queue processing) — passing None leaves whatever's
    already stored on the row untouched, not wiped. error_message is always
    written (including None, to clear a stale one on a later success)."""
    if file_hash is not None:
        conn.execute(
            "UPDATE documents SET processing_status = ?, processed_at = ?, file_hash = ?, error_message = ? WHERE document_id = ?",
            (status, processed_at, file_hash, error_message, document_id),
        )
    else:
        conn.execute(
            "UPDATE documents SET processing_status = ?, processed_at = ?, error_message = ? WHERE document_id = ?",
            (status, processed_at, error_message, document_id),
        )
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()


def set_document_processing_status(conn: sqlite3.Connection, document_id: int, status: str) -> sqlite3.Row | None:
    """Archive/Unarchive — a manual status change, not a processing outcome,
    so unlike mark_document_processing_status() this never touches
    processed_at/error_message/file_hash; the row's history stays exactly as
    it was, just parked (or unparked) out of the working set."""
    conn.execute("UPDATE documents SET processing_status = ? WHERE document_id = ?", (status, document_id))
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()


def list_ingestion_queue_items(
    conn: sqlite3.Connection, *, status: str | None = None, item_kind: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM ingestion_queue_items WHERE 1=1"
    params: list[object] = []
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if item_kind is not None:
        query += " AND item_kind = ?"
        params.append(item_kind)
    query += " ORDER BY discovered_at DESC"
    return conn.execute(query, params).fetchall()


def get_ingestion_queue_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ingestion_queue_items WHERE item_id = ?", (item_id,)).fetchone()


def get_ingestion_queue_item_by_path(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ingestion_queue_items WHERE file_path = ?", (file_path,)).fetchone()


def upsert_ingestion_queue_item(
    conn: sqlite3.Connection,
    *,
    item_kind: str,
    file_path: str,
    content_hash: str | None,
    company_id: str | None,
    source_id: str | None,
    status: str,
    status_reason: str | None,
) -> sqlite3.Row:
    """Insert a newly-discovered file, or refresh an existing row's
    detection/hash — discovery is a full rescan every time, not an
    append-only log, so re-running it must update in place, not duplicate.
    Deliberately does NOT touch status/last_processed_content_hash/
    processed_at for a row that's already PROCESSED with an unchanged
    content_hash — see ingestion/coordinator.py's discover_pending_
    financial_items, which decides that before calling this."""
    now = utcnow_iso()
    existing = get_ingestion_queue_item_by_path(conn, file_path)
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO ingestion_queue_items (
                item_kind, file_path, content_hash, company_id, source_id,
                status, status_reason, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_kind, file_path, content_hash, company_id, source_id, status, status_reason, now),
        )
        item_id = cursor.lastrowid
    else:
        conn.execute(
            """
            UPDATE ingestion_queue_items SET
                item_kind = ?, content_hash = ?, company_id = ?, source_id = ?,
                status = ?, status_reason = ?
            WHERE item_id = ?
            """,
            (item_kind, content_hash, company_id, source_id, status, status_reason, existing["item_id"]),
        )
        item_id = existing["item_id"]
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def update_ingestion_queue_item_result(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    status: str,
    error_message: str | None = None,
    processed_at: str | None = None,
    last_processed_content_hash: str | None = None,
) -> sqlite3.Row | None:
    """Records the outcome of one processing attempt — always stamps
    last_attempt_at; processed_at/last_processed_content_hash are only set
    on success (callers pass them then), left untouched on failure."""
    conn.execute(
        """
        UPDATE ingestion_queue_items SET
            status = ?, error_message = ?, last_attempt_at = ?,
            processed_at = COALESCE(?, processed_at),
            last_processed_content_hash = COALESCE(?, last_processed_content_hash)
        WHERE item_id = ?
        """,
        (status, error_message, utcnow_iso(), processed_at, last_processed_content_hash, item_id),
    )
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def set_ingestion_queue_item_status(conn: sqlite3.Connection, item_id: int, status: str) -> sqlite3.Row | None:
    """Archive/Unarchive — a manual status change, not a processing attempt,
    so unlike update_ingestion_queue_item_result() this never touches
    last_attempt_at/error_message; the row's history stays exactly as it
    was, just parked (or unparked) out of the working set."""
    conn.execute("UPDATE ingestion_queue_items SET status = ? WHERE item_id = ?", (status, item_id))
    conn.commit()
    return get_ingestion_queue_item(conn, item_id)


def save_investigation(
    conn: sqlite3.Connection,
    *,
    investigation_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    strongest_explanation: str | None,
    unanswered_questions: list[str],
    additional_evidence_needed: list[str],
    as_of: str | None = None,
) -> None:
    """Writes the investigation row AND its `investigation_companies`
    associations in one transaction — the JSON `company_ids` column stays the
    ordered as-asked list the investigation view renders, while the join table
    is what "Company -> Investigations" queries (see
    storage/investigation_repository.py for why both exist). A cross-company
    investigation is one record here, associated with several companies, never
    duplicated per company."""
    conn.execute(
        "INSERT INTO investigations (investigation_id, question, company_ids, statement_type, "
        "strongest_explanation, unanswered_questions, additional_evidence_needed, generated_at, as_of) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (investigation_id, question, json.dumps(company_ids), statement_type, strongest_explanation,
         json.dumps(unanswered_questions), json.dumps(additional_evidence_needed), utcnow_iso(), as_of),
    )
    insert_investigation_companies(conn, investigation_id, company_ids)
    conn.commit()


def save_investigation_hypothesis(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: str,
    investigation_id: str,
    statement: str,
    mechanism: str | None,
    category: str,
    rationale: str | None,
    unknowns: list[str],
    generation_order: int,
    verdict: str | None = None,
    confidence_basis: str | None = None,
    synthesis_rank: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO investigation_hypotheses (hypothesis_id, investigation_id, statement, mechanism, "
        "category, rationale, unknowns, generation_order, verdict, confidence_basis, synthesis_rank, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hypothesis_id, investigation_id, statement, mechanism, category, rationale, json.dumps(unknowns),
         generation_order, verdict, confidence_basis, synthesis_rank, utcnow_iso()),
    )
    conn.commit()


def save_investigation_hypothesis_evidence(
    conn: sqlite3.Connection, hypothesis_id: str, evidence: list[dict]
) -> None:
    """evidence: list of {stance, kind, label, value, citation} dicts —
    plain dicts, not a dataclass, same reasoning save_report_evidence()
    already gives for research_thread_evidence."""
    conn.executemany(
        "INSERT INTO investigation_hypothesis_evidence (hypothesis_id, stance, kind, label, value, citation) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(hypothesis_id, e["stance"], e["kind"], e["label"], e.get("value"), e.get("citation")) for e in evidence],
    )
    conn.commit()


def get_investigation(conn: sqlite3.Connection, investigation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = ?", (investigation_id,)
    ).fetchone()


def list_investigations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM investigations ORDER BY generated_at DESC").fetchall()


def list_investigation_hypotheses(conn: sqlite3.Connection, investigation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_hypotheses WHERE investigation_id = ? ORDER BY "
        "CASE WHEN synthesis_rank IS NULL THEN 1 ELSE 0 END, synthesis_rank, generation_order",
        (investigation_id,),
    ).fetchall()


def list_investigation_hypothesis_evidence(conn: sqlite3.Connection, hypothesis_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM investigation_hypothesis_evidence WHERE hypothesis_id = ? ORDER BY id", (hypothesis_id,)
    ).fetchall()


def get_or_create_knowledge_entity(
    conn: sqlite3.Connection, entity_type: str, name: str, company_id: str | None = None
) -> sqlite3.Row:
    """Entities are shared/deduped across every claim that mentions them
    (UNIQUE(entity_type, name, company_id)) — unlike knowledge_claims,
    which is always append-only, the same "Product: iPhone" entity
    shouldn't get a new row every time a new document mentions it again."""
    existing = conn.execute(
        "SELECT * FROM knowledge_entities WHERE entity_type = ? AND name = ? AND company_id IS ?",
        (entity_type, name, company_id),
    ).fetchone()
    if existing is not None:
        return existing
    cursor = conn.execute(
        "INSERT INTO knowledge_entities (entity_type, name, company_id, created_at) VALUES (?, ?, ?, ?)",
        (entity_type, name, company_id, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM knowledge_entities WHERE entity_id = ?", (cursor.lastrowid,)).fetchone()


def insert_knowledge_claim(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    company_id: str | None,
    claim_type: str,
    category: str | None,
    claim_text: str,
    speaker: str | None,
    fiscal_year: str | None,
    quarter: str | None,
    extraction_confidence: float | None,
) -> sqlite3.Row:
    """Always a fresh INSERT, never an UPDATE — a new document's claims are
    additive, same "never overwrite" discipline financial_observations
    already follows (schemas/sqlite_schema.sql's Knowledge Builder section)."""
    cursor = conn.execute(
        """
        INSERT INTO knowledge_claims (
            document_id, company_id, claim_type, category, claim_text, speaker,
            fiscal_year, quarter, extraction_confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, company_id, claim_type, category, claim_text, speaker,
         fiscal_year, quarter, extraction_confidence, utcnow_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM knowledge_claims WHERE claim_id = ?", (cursor.lastrowid,)).fetchone()


def insert_knowledge_relationship(
    conn: sqlite3.Connection, *, claim_id: int | None, source_entity_id: int, relationship_type: str, target_entity_id: int
) -> None:
    conn.execute(
        "INSERT INTO knowledge_relationships (claim_id, source_entity_id, relationship_type, target_entity_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (claim_id, source_entity_id, relationship_type, target_entity_id, utcnow_iso()),
    )
    conn.commit()


def insert_knowledge_evidence(conn: sqlite3.Connection, *, claim_id: int, document_id: int, quote: str | None) -> None:
    conn.execute(
        "INSERT INTO knowledge_evidence (claim_id, document_id, quote, created_at) VALUES (?, ?, ?, ?)",
        (claim_id, document_id, quote, utcnow_iso()),
    )
    conn.commit()


def list_knowledge_claims_for_document(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_claims WHERE document_id = ? ORDER BY claim_id", (document_id,)
    ).fetchall()


def list_knowledge_claims_for_company(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_claims WHERE company_id = ? ORDER BY fiscal_year, quarter, claim_id",
        (company_id,),
    ).fetchall()


def list_knowledge_entities_for_companies(
    conn: sqlite3.Connection, company_ids: list[str], *, entity_types: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Distinct entities already extracted for one or more companies —
    the repository-layer replacement for what research/investigation_planner.py
    and research/hypothesis_generator.py used to run as raw SQL against
    knowledge_entities directly. `entity_types`/`limit` are optional filters
    the single-company caller (hypothesis_generator.py's own context lookup)
    needs; the multi-company caller (investigation_planner.py's mentioned-entity
    match) leaves both unset."""
    if not company_ids:
        return []
    placeholders = ",".join("?" for _ in company_ids)
    sql = f"SELECT DISTINCT entity_type, name FROM knowledge_entities WHERE company_id IN ({placeholders})"
    params: list[object] = list(company_ids)
    if entity_types:
        type_placeholders = ",".join("?" for _ in entity_types)
        sql += f" AND entity_type IN ({type_placeholders})"
        params.extend(entity_types)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_all_knowledge_entities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every entity — only for context/graph_neo4j.py's
    full-graph resync, never for a per-request path (use
    list_knowledge_entities_for_companies for that)."""
    return conn.execute("SELECT entity_id, entity_type, name, company_id FROM knowledge_entities").fetchall()


def list_all_knowledge_claims(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every claim — see list_all_knowledge_entities."""
    return conn.execute(
        "SELECT claim_id, document_id, company_id, claim_type, category, claim_text, speaker, "
        "fiscal_year, quarter, extraction_confidence FROM knowledge_claims"
    ).fetchall()


def list_all_knowledge_relationships(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every relationship — see list_all_knowledge_entities."""
    return conn.execute(
        "SELECT relationship_id, claim_id, source_entity_id, relationship_type, target_entity_id "
        "FROM knowledge_relationships"
    ).fetchall()


def list_all_knowledge_evidence(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Unfiltered read of every evidence row — see list_all_knowledge_entities."""
    return conn.execute("SELECT evidence_id, claim_id, document_id, quote FROM knowledge_evidence").fetchall()


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _sanitize_fts_query(query: str) -> str:
    """FTS5's MATCH syntax treats hyphens/colons/quotes as query operators —
    an arbitrary query passed straight through can raise a syntax error
    instead of just finding nothing. Tokenizing to plain alphanumeric words
    and double-quoting each one individually keeps every token literal
    (safe from operator interpretation).

    OR-joined, not FTS5's default AND — deliberately: a short 2-3 word
    search phrase is fine either way, but research/investigation_planner.py
    (Step 2F) composes a much longer blob (a question plus a hypothesis's
    own statement and mechanism) to search with, and requiring literally
    every one of those words to co-occur in one small chunk means almost
    nothing ever matches. OR plus bm25 ranking (search_document_chunks()'s
    `ORDER BY rank`) already rewards a chunk sharing MORE query terms over
    one sharing just one, without demanding all of them — a better fit for
    "where was something *similar* discussed" than a rigid AND would be."""
    tokens = _FTS_TOKEN_RE.findall(query)
    return " OR ".join(f'"{t}"' for t in tokens)


def replace_document_chunks(conn: sqlite3.Connection, document_id: int, chunks: list[dict]) -> None:
    """Deletes this document's existing chunks (if it's being reprocessed)
    and inserts the fresh set. Unlike knowledge_claims, a chunk has no
    standalone provenance value once superseded — it's a mechanical index
    over the document's CURRENT text for search, not a historical claim —
    so replacing it on reprocess is correct here, not a violation of the
    "never overwrite" rule that governs genuinely historical facts/claims."""
    now = utcnow_iso()
    existing_ids = [
        row["chunk_id"] for row in
        conn.execute("SELECT chunk_id FROM document_chunks WHERE document_id = ?", (document_id,)).fetchall()
    ]
    if existing_ids:
        conn.executemany("DELETE FROM document_chunks_fts WHERE rowid = ?", [(i,) for i in existing_ids])
        conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO document_chunks "
            "(document_id, company_id, page_number, chunk_index, text, section_heading, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk["document_id"], chunk["company_id"], chunk["page_number"], chunk["chunk_index"],
                chunk["text"], chunk.get("section_heading"), now,
            ),
        )
        conn.execute(
            "INSERT INTO document_chunks_fts (rowid, text) VALUES (?, ?)", (cursor.lastrowid, chunk["text"])
        )
    conn.commit()


def search_document_chunks(
    conn: sqlite3.Connection, query: str, *, company_id: str | None = None, limit: int = 10
) -> list[sqlite3.Row]:
    """FTS5 keyword search over indexed chunks, joined back to `documents`
    for full provenance — Step 2D's "every returned passage must retain
    document, company, date, quarter, page/section, source." Returns []
    (not an error) for a query with no usable search tokens."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    sql = (
        "SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text, "
        "       d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at "
        "FROM document_chunks_fts fts "
        "JOIN document_chunks dc ON dc.chunk_id = fts.rowid "
        "JOIN documents d ON d.document_id = dc.document_id "
        "WHERE document_chunks_fts MATCH ?"
    )
    params: list[object] = [fts_query]
    if company_id is not None:
        sql += " AND dc.company_id = ?"
        params.append(company_id)
    sql += " ORDER BY fts.rank LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_document_chunks(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    """Every chunk belonging to one document, joined back to `documents` for
    the same full provenance search_document_chunks() returns — the read
    side retrieval/semantic_indexer.py uses to (re-)embed a document's
    chunks without re-deriving them from the PDF a second time (section 5:
    "the same logical chunk must have both a keyword-search representation
    and a semantic-search representation — do not create a second
    independent chunking implementation")."""
    return conn.execute(
        """
        SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text,
               dc.embedding_status, dc.embedding_model,
               d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at
        FROM document_chunks dc
        JOIN documents d ON d.document_id = dc.document_id
        WHERE dc.document_id = ?
        ORDER BY dc.chunk_index
        """,
        (document_id,),
    ).fetchall()


def get_document_chunks_by_ids(conn: sqlite3.Connection, chunk_ids: list[int]) -> list[sqlite3.Row]:
    """Hydrate a set of chunk_ids (typically a VectorStore search's hits,
    retrieval/semantic_search.py) back into full provenance rows — same
    joined shape search_document_chunks() returns, so semantic and keyword
    hits become the identical DocumentPassage shape downstream. The vector
    store itself is never trusted for provenance (retrieval/vector_store.py:
    "the vector store staying authoritative for nothing"); this table always
    is. Returns [] for an empty/no-longer-existing chunk_id list rather than
    raising — a stale vector pointing at a since-deleted chunk is dropped
    silently, the same "absence isn't an error" convention used everywhere
    else in this pipeline."""
    if not chunk_ids:
        return []
    placeholders = ",".join("?" for _ in chunk_ids)
    sql = (
        "SELECT dc.chunk_id, dc.document_id, dc.company_id, dc.page_number, dc.chunk_index, dc.text, "
        "       d.document_type, d.fiscal_year, d.quarter, d.source, d.published_at, d.retrieved_at "
        "FROM document_chunks dc "
        "JOIN documents d ON d.document_id = dc.document_id "
        f"WHERE dc.chunk_id IN ({placeholders})"
    )
    return conn.execute(sql, chunk_ids).fetchall()


def set_document_chunks_embedding_status(
    conn: sqlite3.Connection, chunk_ids: list[int], *, status: str, model: str | None, embedded_at: str | None
) -> None:
    """Record semantic-indexing status per chunk (retrieval/semantic_indexer.py)
    — what makes backfill idempotent (a chunk already 'indexed' under the
    current embedding_model is skipped on the next run) without querying the
    vector store just to find out. Never touches document_chunks_fts or any
    other column — a failed embedding attempt (status='failed') must not
    disturb the FTS5 index this same chunk already serves (section 10)."""
    if not chunk_ids:
        return
    placeholders = ",".join("?" for _ in chunk_ids)
    conn.execute(
        f"UPDATE document_chunks SET embedding_status = ?, embedding_model = ?, embedded_at = ? "
        f"WHERE chunk_id IN ({placeholders})",
        (status, model, embedded_at, *chunk_ids),
    )
    conn.commit()


def insert_retrieval_diagnostic(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    query_excerpt: str | None,
    company_id: str | None,
    as_of: str | None,
    keyword_candidate_count: int,
    semantic_candidate_count: int,
    returned_count: int,
    embedding_latency_ms: float | None,
    vector_store_latency_ms: float | None,
    keyword_latency_ms: float | None,
    degraded: bool,
    degradation_reason: str | None,
    passages_json: str,
) -> None:
    """One row per retrieval/hybrid_search.py call (section 13,
    observability) — same "structured row per call" role
    insert_llm_call_log plays for LLM calls, just without any cost/token
    accounting since retrieval never calls the LLM."""
    conn.execute(
        """
        INSERT INTO retrieval_diagnostics (
            created_at, query_excerpt, company_id, as_of, keyword_candidate_count,
            semantic_candidate_count, returned_count, embedding_latency_ms, vector_store_latency_ms,
            keyword_latency_ms, degraded, degradation_reason, passages_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at, query_excerpt, company_id, as_of, keyword_candidate_count,
            semantic_candidate_count, returned_count, embedding_latency_ms, vector_store_latency_ms,
            keyword_latency_ms, int(degraded), degradation_reason, passages_json,
        ),
    )
    conn.commit()


def list_retrieval_diagnostics(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Most recent retrieval diagnostic rows, newest first — admin/debug
    visibility (section 13), same shape of use as the Admin Usage page reads
    llm_call_log."""
    return conn.execute(
        "SELECT * FROM retrieval_diagnostics ORDER BY retrieval_id DESC LIMIT ?", (limit,)
    ).fetchall()


def list_knowledge_evidence_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_evidence WHERE claim_id = ? ORDER BY evidence_id", (claim_id,)
    ).fetchall()


def find_knowledge_claims_about_entity(conn: sqlite3.Connection, entity_type: str, entity_name: str) -> list[sqlite3.Row]:
    """Every claim (any company) whose extracted relationships touch this
    entity — the SQLite path for context/knowledge_graph.py's cross-entity
    query, a real join-based traversal, not a stub."""
    return conn.execute(
        """
        SELECT DISTINCT c.*
        FROM knowledge_entities e
        JOIN knowledge_relationships r ON r.source_entity_id = e.entity_id OR r.target_entity_id = e.entity_id
        JOIN knowledge_claims c ON c.claim_id = r.claim_id
        WHERE e.entity_type = ? AND e.name = ?
        ORDER BY c.fiscal_year, c.quarter, c.claim_id
        """,
        (entity_type, entity_name),
    ).fetchall()


def list_knowledge_relationships_for_claim(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, se.entity_type AS source_type, se.name AS source_name,
               te.entity_type AS target_type, te.name AS target_name
        FROM knowledge_relationships r
        JOIN knowledge_entities se ON se.entity_id = r.source_entity_id
        JOIN knowledge_entities te ON te.entity_id = r.target_entity_id
        WHERE r.claim_id = ?
        """,
        (claim_id,),
    ).fetchall()


# ------------------------------------------------------------------
# Entity resolution (context/entity_resolution.py) — merging a duplicate
# Company-type knowledge_entities row into the canonical one, and the
# multi-hop BFS primitives context/knowledge_graph.py::find_multi_hop_claims()
# needs. See the implementation plan's Phase 1/Phase 2 for the full context.
# ------------------------------------------------------------------


def list_company_type_knowledge_entities(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    """Every Company-type knowledge_entities row scoped to this company_id —
    normally exactly one (the canonical row get_or_create_knowledge_entity
    creates, named after the company_id itself), but a document naming the
    company by its own extracted legal/display name before entity
    resolution existed (or before it was applied to a company's earlier
    documents) can leave a second, duplicate row here. main.py
    entity-resolution-backfill is the one-off reader/writer of this."""
    return conn.execute(
        "SELECT * FROM knowledge_entities WHERE entity_type = 'Company' AND company_id = ? ORDER BY entity_id",
        (company_id,),
    ).fetchall()


def merge_knowledge_entities(conn: sqlite3.Connection, *, from_entity_id: int, into_entity_id: int) -> None:
    """Repoints every knowledge_relationships row referencing the duplicate
    entity (from_entity_id) to the canonical one (into_entity_id), then
    deletes the duplicate row — one transaction, so a merge is never left
    half-done (a relationship pointing at an entity_id that no longer
    exists). Only ever called by main.py entity-resolution-backfill's
    --apply path, and only after context/entity_resolution.py's
    is_same_company_identity() has already confirmed this is a genuine
    exact-match duplicate, never a fuzzy/similarity guess."""
    conn.execute(
        "UPDATE knowledge_relationships SET source_entity_id = ? WHERE source_entity_id = ?",
        (into_entity_id, from_entity_id),
    )
    conn.execute(
        "UPDATE knowledge_relationships SET target_entity_id = ? WHERE target_entity_id = ?",
        (into_entity_id, from_entity_id),
    )
    conn.execute("DELETE FROM knowledge_entities WHERE entity_id = ?", (from_entity_id,))
    conn.commit()


def list_knowledge_entity_ids_by_type_and_name(conn: sqlite3.Connection, entity_type: str, name: str) -> list[int]:
    """Resolve a (entity_type, name) pair to every matching entity_id — the
    starting frontier for context/knowledge_graph.py::find_multi_hop_claims()'s
    BFS. Not scoped to one company_id (same as find_knowledge_claims_about_entity),
    since a generic entity name (e.g. a Risk) can legitimately be extracted
    once per company that mentions it, each its own entity row."""
    rows = conn.execute(
        "SELECT entity_id FROM knowledge_entities WHERE entity_type = ? AND name = ?", (entity_type, name)
    ).fetchall()
    return [row["entity_id"] for row in rows]


def list_entity_neighbors(conn: sqlite3.Connection, entity_ids: list[int]) -> list[sqlite3.Row]:
    """Every relationship edge touching any of the given entities, either
    direction, joined to both endpoint entities — the batched-per-hop
    primitive find_multi_hop_claims()'s BFS needs (one query per hop across
    the whole frontier, not one query per node, which would be a real
    N+1 cost on a highly-connected entity)."""
    if not entity_ids:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    return conn.execute(
        f"""
        SELECT r.relationship_id, r.claim_id, r.source_entity_id, r.relationship_type, r.target_entity_id,
               se.entity_type AS source_type, se.name AS source_name,
               te.entity_type AS target_type, te.name AS target_name
        FROM knowledge_relationships r
        JOIN knowledge_entities se ON se.entity_id = r.source_entity_id
        JOIN knowledge_entities te ON te.entity_id = r.target_entity_id
        WHERE r.source_entity_id IN ({placeholders}) OR r.target_entity_id IN ({placeholders})
        """,
        [*entity_ids, *entity_ids],
    ).fetchall()


def find_knowledge_claims_for_entity_ids(conn: sqlite3.Connection, entity_ids: list[int]) -> list[sqlite3.Row]:
    """Same shape as find_knowledge_claims_about_entity, but keyed by a batch
    of entity_ids directly (find_multi_hop_claims()'s BFS already resolved
    the frontier to entity_ids and has no name to look up by). Each result
    row also carries `matched_entity_id` — which entity in the requested
    batch this claim was reached through — since a caller doing a BFS needs
    that to attribute the right hop_distance/path to the claim; a plain
    DISTINCT c.* the way find_knowledge_claims_about_entity returns would
    lose exactly that information."""
    if not entity_ids:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    return conn.execute(
        f"""
        SELECT DISTINCT c.*, x.entity_id AS matched_entity_id
        FROM (
            SELECT claim_id, source_entity_id AS entity_id FROM knowledge_relationships
            WHERE source_entity_id IN ({placeholders})
            UNION
            SELECT claim_id, target_entity_id AS entity_id FROM knowledge_relationships
            WHERE target_entity_id IN ({placeholders})
        ) x
        JOIN knowledge_claims c ON c.claim_id = x.claim_id
        ORDER BY c.fiscal_year, c.quarter, c.claim_id
        """,
        [*entity_ids, *entity_ids],
    ).fetchall()


def _row_to_generated_report(row: sqlite3.Row) -> dict:
    return {
        "thread_id": row["thread_id"],
        "question": row["question"],
        "company_ids": json.loads(row["company_ids"]),
        "statement_type": row["statement_type"],
        "report_markdown": row["report_markdown"],
        "generated_at": row["generated_at"],
        "question_embedding": json.loads(row["question_embedding"]) if row["question_embedding"] else None,
        "question_embedding_model": row["question_embedding_model"],
    }


def save_generated_report(
    conn: sqlite3.Connection,
    thread_id: str,
    question: str,
    company_ids: list[str],
    statement_type: str,
    report_markdown: str,
    *,
    question_embedding: list[float] | None = None,
    question_embedding_model: str | None = None,
) -> None:
    """Persist a full Signals report (research/signals_report.py, via
    /research/thread/generate) so it survives a server restart — unlike the
    short /research/ask answers, which stay ephemeral by design.

    question_embedding/question_embedding_model are optional (this module
    never imports retrieval/research code — architecture guardrail #3 — so
    the caller computes the embedding and hands it in already-made, same
    "storage stays a passive persistence layer" discipline
    retrieval/semantic_indexer.py's VectorRecord handoff already follows).
    Left NULL when the caller couldn't get one (embedding provider down) —
    context/reuse.py falls back to word-overlap-only for that report."""
    conn.execute(
        "INSERT INTO generated_reports "
        "(thread_id, question, company_ids, statement_type, report_markdown, generated_at, "
        " question_embedding, question_embedding_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thread_id, question, json.dumps(company_ids), statement_type, report_markdown, utcnow_iso(),
            json.dumps(question_embedding) if question_embedding is not None else None,
            question_embedding_model,
        ),
    )
    conn.commit()


def get_generated_report(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM generated_reports WHERE thread_id = ?", (thread_id,)).fetchone()
    return _row_to_generated_report(row) if row is not None else None


def list_generated_reports(conn: sqlite3.Connection) -> list[dict]:
    """Every generated report, newest first."""
    rows = conn.execute("SELECT * FROM generated_reports ORDER BY generated_at DESC").fetchall()
    return [_row_to_generated_report(row) for row in rows]


def save_report_evidence(conn: sqlite3.Connection, thread_id: str, evidence: list[dict]) -> None:
    """Persist the deterministic Evidence (research/evidence.py) that grounded a
    generated_reports row, in retrieval order, for the Investigations evidence
    rail. Each dict has kind/company_id/label/value/citation, matching
    Evidence's fields — callers pass plain dicts rather than Evidence objects
    so this storage-layer module doesn't need to import from research/."""
    conn.executemany(
        "INSERT INTO research_thread_evidence "
        "(thread_id, sort_order, kind, company_id, label, value, citation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (thread_id, i, ev["kind"], ev["company_id"], ev["label"], ev["value"], ev["citation"])
            for i, ev in enumerate(evidence)
        ],
    )
    conn.commit()


def list_report_evidence(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    """A generated report's grounding evidence, in the order it was retrieved in."""
    rows = conn.execute(
        "SELECT * FROM research_thread_evidence WHERE thread_id = ? ORDER BY sort_order", (thread_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def save_report_followups(conn: sqlite3.Connection, thread_id: str, followups: list[str]) -> None:
    """Persist the follow-up question suggestions a Signals report ended with
    (research/signals_report.py's ===FOLLOWUP_QUESTIONS=== parsing), so the
    Follow-up research rail's buttons are real and re-clickable."""
    conn.executemany(
        "INSERT INTO research_thread_followups (thread_id, sort_order, followup_text) VALUES (?, ?, ?)",
        [(thread_id, i, text) for i, text in enumerate(followups)],
    )
    conn.commit()


def list_report_followups(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT followup_text FROM research_thread_followups WHERE thread_id = ? ORDER BY sort_order", (thread_id,)
    ).fetchall()
    return [row["followup_text"] for row in rows]


def insert_llm_call_log(
    conn: sqlite3.Connection,
    *,
    task_name: str,
    company_ids: str,
    question: str | None,
    thread_id: str | None,
    complexity_tier: str,
    complexity_level: int,
    complexity_reason: str,
    model_used: str,
    provider_used: str,
    fallback_used: bool,
    attempts_json: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    stop_reason: str,
    context_tokens_before: int | None = None,
    context_tokens_after: int | None = None,
    context_items_dropped: int | None = None,
    reuse_hit: bool = False,
    reused_thread_id: str | None = None,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    graph_hit: bool = False,
    graph_hit_thread_id: str | None = None,
    graph_hit_score: float | None = None,
    investigation_id: str | None = None,
) -> None:
    """Persist one llm/router.py route() outcome, or one context/reuse.py
    reuse hit (llm/observability.py) — the Context Optimization + Model
    Routing + Fallback layer's audit trail."""
    conn.execute(
        "INSERT INTO llm_call_log "
        "(created_at, task_name, company_ids, question, thread_id, complexity_tier, complexity_level, "
        "complexity_reason, model_used, provider_used, fallback_used, attempts_json, input_tokens, "
        "output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before, "
        "context_tokens_after, context_items_dropped, reuse_hit, reused_thread_id, "
        "cache_creation_input_tokens, cache_read_input_tokens, graph_hit, graph_hit_thread_id, "
        "graph_hit_score, investigation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utcnow_iso(), task_name, company_ids, question, thread_id, complexity_tier, complexity_level,
            complexity_reason, model_used, provider_used, int(fallback_used), attempts_json, input_tokens,
            output_tokens, estimated_cost_usd, latency_ms, stop_reason, context_tokens_before,
            context_tokens_after, context_items_dropped, int(reuse_hit), reused_thread_id,
            cache_creation_input_tokens, cache_read_input_tokens, int(graph_hit), graph_hit_thread_id,
            graph_hit_score, investigation_id,
        ),
    )
    conn.commit()


def list_llm_call_log(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Most recent LLM calls, newest first — for a future cost/observability view."""
    rows = conn.execute(
        "SELECT * FROM llm_call_log ORDER BY call_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_llm_usage_summary(conn: sqlite3.Connection) -> dict:
    """All-time totals plus a by-task and by-model breakdown of llm_call_log
    — backs the /admin/usage page (web/app.py). A reuse hit (context/reuse.py)
    costs 0 tokens/0 dollars by construction (llm/observability.py's
    record_reuse), so it's counted separately (reused_calls) rather than
    diluting the per-model cost breakdown with free rows."""
    totals = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(reuse_hit), 0) AS reused_calls "
        "FROM llm_call_log"
    ).fetchone()

    by_task = conn.execute(
        "SELECT task_name, COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
        "FROM llm_call_log GROUP BY task_name ORDER BY cost_usd DESC"
    ).fetchall()

    by_model = conn.execute(
        "SELECT model_used, COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
        "FROM llm_call_log WHERE reuse_hit = 0 GROUP BY model_used ORDER BY cost_usd DESC"
    ).fetchall()

    return {
        "calls": totals["calls"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cost_usd": totals["cost_usd"],
        "reused_calls": totals["reused_calls"],
        "by_task": [dict(row) for row in by_task],
        "by_model": [dict(row) for row in by_model],
    }


def get_investigation_cost_summary(conn: sqlite3.Connection, investigation_id: str) -> dict:
    """Total cost/tokens/calls for one research/investigation.py run — every
    llm_call_log row tagged with this investigation_id (hypothesis
    generation, per-hypothesis evaluation, research synthesis, and any
    macro-retrieval-plan call made while gathering evidence for it), so the
    Investigations tab can show a single investigation's own spend instead
    of only the site-wide total on /admin/usage."""
    row = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd "
        "FROM llm_call_log WHERE investigation_id = ?",
        (investigation_id,),
    ).fetchone()
    return dict(row)


def get_latest_data_timestamp(conn: sqlite3.Connection, company_ids: list[str]) -> str | None:
    """Most recent financial_observations.created_at / documents.retrieved_at
    across these companies — the freshness signal context/reuse.py checks a
    cached generated_reports row against before reusing it. A generated
    report older than this timestamp was built from data that has since
    changed, so it must not be silently reused (README §17: "an old cached
    result must not silently masquerade as current data")."""
    placeholders = ",".join("?" * len(company_ids))
    row = conn.execute(
        f"""
        SELECT MAX(ts) AS latest FROM (
            SELECT MAX(created_at) AS ts FROM financial_observations WHERE company_id IN ({placeholders})
            UNION ALL
            SELECT MAX(retrieved_at) AS ts FROM documents WHERE company_id IN ({placeholders})
        )
        """,
        (*company_ids, *company_ids),
    ).fetchone()
    return row["latest"] if row is not None else None


def delete_generated_report(conn: sqlite3.Connection, thread_id: str) -> bool:
    """Remove a generated report and its evidence/followups. Child rows have to
    go first — research_thread_evidence/research_thread_followups reference
    generated_reports(thread_id) with FK enforcement on (storage/database.py),
    so deleting the parent first would raise IntegrityError."""
    conn.execute("DELETE FROM research_thread_evidence WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM research_thread_followups WHERE thread_id = ?", (thread_id,))
    cursor = conn.execute("DELETE FROM generated_reports WHERE thread_id = ?", (thread_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_company_index_tags(conn: sqlite3.Connection, company_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT index_name FROM company_index_membership WHERE company_id = ? ORDER BY index_name",
        (company_id,),
    ).fetchall()
    return [row["index_name"] for row in rows]


def get_all_company_index_tags(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every company_id's index tags, in one query — for a page that needs
    every company's tags at once (e.g. to filter/search a large company list
    before pagination), where calling get_company_index_tags() per row would
    mean one query per company instead of one query total."""
    rows = conn.execute(
        "SELECT company_id, index_name FROM company_index_membership ORDER BY company_id, index_name"
    ).fetchall()
    tags_by_company: dict[str, list[str]] = {}
    for row in rows:
        tags_by_company.setdefault(row["company_id"], []).append(row["index_name"])
    return tags_by_company


def set_company_index_tags(conn: sqlite3.Connection, company_id: str, index_names: list[str]) -> None:
    """Replace this company's whole tag set — simpler and safe at this scale
    (a handful of tags per company) than diffing adds/removes."""
    known = {row["name"] for row in conn.execute("SELECT name FROM index_definitions")}
    unknown = set(index_names) - known
    if unknown:
        raise ValueError(f"Unknown index name(s): {sorted(unknown)}; must be one of {sorted(known)}")
    conn.execute("DELETE FROM company_index_membership WHERE company_id = ?", (company_id,))
    conn.executemany(
        "INSERT INTO company_index_membership (company_id, index_name) VALUES (?, ?)",
        [(company_id, name) for name in index_names],
    )
    conn.commit()


# ============================================================
# Sector / Industry / Index-tag vocabularies (Admin tab: "Sectors,
# Industries & Tags") — editable lookup tables, not pure freeform text or a
# hardcoded Python list. See schemas/sqlite_schema.sql's comment on these
# three tables for why they're TEXT-primary-keyed (a rename is one UPDATE
# on the name itself, not an id lookup).
# ============================================================


def list_sectors(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM sectors ORDER BY name")]


def count_companies_by_sector(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT sector, COUNT(*) AS n FROM companies WHERE sector IS NOT NULL GROUP BY sector"
    ).fetchall()
    return {row["sector"]: row["n"] for row in rows}


def add_sector(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sectors (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_sector(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    """Renames the lookup row and every company currently using it, in one
    transaction — a rename must never leave some companies pointing at a
    name that no longer exists in the lookup table."""
    conn.execute("UPDATE sectors SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute("UPDATE companies SET sector = ? WHERE sector = ?", (new_name, old_name))
    conn.commit()


def delete_sector(conn: sqlite3.Connection, name: str) -> None:
    """Clears `sector` to NULL on every company using it, then removes the
    lookup row — deleting a sector means "this no longer applies to
    anyone," not a silent reassignment to some other guessed value."""
    conn.execute("UPDATE companies SET sector = NULL WHERE sector = ?", (name,))
    conn.execute("DELETE FROM sectors WHERE name = ?", (name,))
    conn.commit()


def list_industries(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM industries ORDER BY name")]


def count_companies_by_industry(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT industry, COUNT(*) AS n FROM companies WHERE industry IS NOT NULL GROUP BY industry"
    ).fetchall()
    return {row["industry"]: row["n"] for row in rows}


def add_industry(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO industries (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_industry(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    conn.execute("UPDATE industries SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute("UPDATE companies SET industry = ? WHERE industry = ?", (new_name, old_name))
    conn.commit()


def delete_industry(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("UPDATE companies SET industry = NULL WHERE industry = ?", (name,))
    conn.execute("DELETE FROM industries WHERE name = ?", (name,))
    conn.commit()


def list_index_definitions(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM index_definitions ORDER BY name")]


def count_companies_by_index_tag(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT index_name, COUNT(*) AS n FROM company_index_membership GROUP BY index_name"
    ).fetchall()
    return {row["index_name"]: row["n"] for row in rows}


def add_index_definition(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO index_definitions (name, created_at) VALUES (?, ?)", (name, utcnow_iso()))
    conn.commit()


def rename_index_definition(conn: sqlite3.Connection, old_name: str, new_name: str) -> None:
    conn.execute("UPDATE index_definitions SET name = ? WHERE name = ?", (new_name, old_name))
    conn.execute(
        "UPDATE company_index_membership SET index_name = ? WHERE index_name = ?", (new_name, old_name)
    )
    conn.commit()


def delete_index_definition(conn: sqlite3.Connection, name: str) -> None:
    """Removes every company's membership in this tag, then the tag itself
    — same "delete means it no longer applies to anyone" rule as
    delete_sector/delete_industry above."""
    conn.execute("DELETE FROM company_index_membership WHERE index_name = ?", (name,))
    conn.execute("DELETE FROM index_definitions WHERE name = ?", (name,))
    conn.commit()


# Optional columns on the Companies list (Admin tab: "Company List Columns").
# Fixed vocabulary, same pattern as INDEX_NAMES/ARCHIVE_REASONS — "Company"
# itself is always shown and isn't part of this set.
COMPANY_LIST_COLUMNS = [
    {"key": "sector", "label": "Sector"},
    {"key": "industry", "label": "Industry"},
    {"key": "price", "label": "Price"},
    {"key": "market_cap", "label": "Mkt Cap"},
    {"key": "week52", "label": "52W Range"},
    {"key": "all_time", "label": "All-Time Range"},
    {"key": "status", "label": "Status"},
    {"key": "tags", "label": "Index tags & IDs"},
]
_COMPANY_LIST_COLUMN_KEYS = {c["key"] for c in COMPANY_LIST_COLUMNS}


def get_company_list_column_settings(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which optional columns Admin has made available on the Companies
    list. A column with no row yet defaults to enabled — new columns show
    up available-by-default rather than silently hidden until an admin
    visits the settings page."""
    rows = conn.execute("SELECT column_key, enabled FROM company_list_column_settings").fetchall()
    overrides = {row["column_key"]: bool(row["enabled"]) for row in rows}
    return {key: overrides.get(key, True) for key in _COMPANY_LIST_COLUMN_KEYS}


def set_company_list_column_settings(conn: sqlite3.Connection, enabled_keys: Iterable[str]) -> None:
    """Replace the whole set — same replace-all pattern as set_company_index_tags."""
    enabled_keys = set(enabled_keys)
    unknown = enabled_keys - _COMPANY_LIST_COLUMN_KEYS
    if unknown:
        raise ValueError(f"Unknown column key(s): {sorted(unknown)}; must be one of {sorted(_COMPANY_LIST_COLUMN_KEYS)}")
    conn.executemany(
        "INSERT INTO company_list_column_settings (column_key, enabled) VALUES (?, ?) "
        "ON CONFLICT(column_key) DO UPDATE SET enabled = excluded.enabled",
        [(key, 1 if key in enabled_keys else 0) for key in _COMPANY_LIST_COLUMN_KEYS],
    )
    conn.commit()


# The Overview tab's ratio grid (web/templates/company.html, "About" ->
# renders into web/static/js/valuation_dashboard.js's renderOverview()).
# Every key here has a matching compute case in that file's RATIO_CATALOG
# object — adding a genuinely new ratio is exactly two edits (one entry
# here for the admin-facing label, one compute case there), never a schema
# or settings-table change. `default_enabled=False` entries are ratios this
# app can already compute from ingested data but aren't shown out of the
# box — an admin can turn them on with no code change at all.
OVERVIEW_RATIO_CATALOG = [
    {"key": "marketCap", "label": "Market Cap", "default_enabled": True},
    {"key": "price", "label": "Current Price", "default_enabled": True},
    {"key": "stockPE", "label": "Stock P/E", "default_enabled": True},
    {"key": "bookValue", "label": "Book Value", "default_enabled": True},
    {"key": "dividendYield", "label": "Dividend Yield", "default_enabled": True},
    {"key": "roe", "label": "ROE", "default_enabled": True},
    {"key": "eps", "label": "EPS", "default_enabled": True},
    {"key": "priceToBook", "label": "Price to Book Value", "default_enabled": True},
    {"key": "debtToEquity", "label": "Debt to Equity", "default_enabled": True},
    {"key": "payout", "label": "Dividend Payout", "default_enabled": True},
    {"key": "shares", "label": "No. Equity Shares", "default_enabled": True},
    {"key": "netProfit", "label": "Net Profit (latest FY)", "default_enabled": True},
    {"key": "revenue", "label": "Revenue (latest FY)", "default_enabled": True},
    {"key": "salesCagr", "label": "Sales Growth (full recorded range)", "default_enabled": True},
    {"key": "profitCagr", "label": "Profit Growth (full recorded range)", "default_enabled": True},
    {"key": "netMargin", "label": "Net Profit Margin", "default_enabled": False},
    {"key": "taxRate", "label": "Tax Rate", "default_enabled": False},
    {"key": "retention", "label": "Retention Ratio", "default_enabled": False},
    {"key": "roa", "label": "Return on Assets (bank/NBFC)", "default_enabled": False},
    {"key": "cdRatio", "label": "Credit-Deposit Ratio (bank)", "default_enabled": False},
    {"key": "intCoverage", "label": "Interest Coverage", "default_enabled": False},
    {"key": "networth", "label": "Net Worth", "default_enabled": False},
    {"key": "totalAssets", "label": "Total Assets", "default_enabled": False},
    {"key": "salesPerShare", "label": "Sales per Share", "default_enabled": False},
]
_OVERVIEW_RATIO_KEYS = {r["key"] for r in OVERVIEW_RATIO_CATALOG}
_OVERVIEW_RATIO_DEFAULT_ENABLED = {r["key"] for r in OVERVIEW_RATIO_CATALOG if r["default_enabled"]}


def get_overview_ratio_settings(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which of the ratio catalog's entries actually render on a company's
    Overview tab. A ratio with no row yet defaults per its own
    default_enabled — same reasoning get_company_list_column_settings
    already gives for new columns, except the default can be off (most of
    the always-on-by-default set is the original 15; extras start hidden
    until an admin opts in)."""
    rows = conn.execute("SELECT ratio_key, enabled FROM overview_ratio_settings").fetchall()
    overrides = {row["ratio_key"]: bool(row["enabled"]) for row in rows}
    return {key: overrides.get(key, key in _OVERVIEW_RATIO_DEFAULT_ENABLED) for key in _OVERVIEW_RATIO_KEYS}


def set_overview_ratio_settings(conn: sqlite3.Connection, enabled_keys: Iterable[str]) -> None:
    """Replace the whole set — same replace-all pattern as set_company_list_column_settings."""
    enabled_keys = set(enabled_keys)
    unknown = enabled_keys - _OVERVIEW_RATIO_KEYS
    if unknown:
        raise ValueError(f"Unknown ratio key(s): {sorted(unknown)}; must be one of {sorted(_OVERVIEW_RATIO_KEYS)}")
    conn.executemany(
        "INSERT INTO overview_ratio_settings (ratio_key, enabled) VALUES (?, ?) "
        "ON CONFLICT(ratio_key) DO UPDATE SET enabled = excluded.enabled",
        [(key, 1 if key in enabled_keys else 0) for key in _OVERVIEW_RATIO_KEYS],
    )
    conn.commit()


def insert_macro_observations(
    conn: sqlite3.Connection, observations: Iterable[MacroNormalizedObservation]
) -> list[int]:
    """Insert each macro observation as a new row — append-only, same as
    insert_financial_observations (README: raw observations are never
    overwritten). No reconciliation step yet: with one source per series
    today there's nothing to reconcile, the same trivial pass-through
    financial_observations started with before NSE/BSE (step 6)."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO macro_observations (
                series_key, region, period_type, period, value, unit,
                source, source_file, source_url, retrieved_at, parser_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.series_key, obs.region, obs.period_type, obs.period, obs.value, obs.unit,
                obs.source, _normalize_source_file(obs.source_file), obs.source_url, obs.retrieved_at or now, obs.parser_version, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def insert_bank_infrastructure_observations(
    conn: sqlite3.Connection, observations: Iterable[BankInfrastructureObservation]
) -> list[int]:
    """Insert each bank-infrastructure observation as a new row —
    append-only, same as insert_macro_observations."""
    now = utcnow_iso()
    ids: list[int] = []
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT INTO bank_infrastructure_observations (
                bank_name, metric, period_type, period, value, unit,
                source, source_file, parser_version, retrieved_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.bank_name, obs.metric, obs.period_type, obs.period, obs.value, obs.unit,
                obs.source, _normalize_source_file(obs.source_file), obs.parser_version, now, now,
            ),
        )
        ids.append(cursor.lastrowid)
    conn.commit()
    return ids


def get_bank_infrastructure_series(
    conn: sqlite3.Connection, bank_name: str, metric: str
) -> list[sqlite3.Row]:
    """One bank's one metric across every period ingested, oldest first."""
    return conn.execute(
        "SELECT * FROM bank_infrastructure_observations WHERE bank_name = ? AND metric = ? ORDER BY period",
        (bank_name, metric),
    ).fetchall()


def get_macro_series(
    conn: sqlite3.Connection, series_key: str, region: str | None = None
) -> list[sqlite3.Row]:
    """Every observation for one macro series, oldest to newest. region=None
    means all-India/national (matches how the adapter stores an omitted
    region column) rather than "any region" — pass a specific region string
    to fetch a regional series instead."""
    return conn.execute(
        "SELECT * FROM macro_observations WHERE series_key = ? AND region IS ? ORDER BY period ASC",
        (series_key, region),
    ).fetchall()


def list_macro_series_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every distinct (series_key, source, earliest, latest) at the national
    level (region IS NULL) — research/macro_evidence.py's own catalog
    discovery used to run this as raw SQL directly."""
    return conn.execute(
        "SELECT series_key, source, MIN(period) AS earliest, MAX(period) AS latest "
        "FROM macro_observations WHERE region IS NULL GROUP BY series_key, source"
    ).fetchall()


# ============================================================
# Users (README: sign-up is email-based, no verification; the one seeded
# admin account logs in by username instead — see schemas/sqlite_schema.sql)
# ============================================================

VALID_THEMES = {"light", "white", "green", "dark", "schwab", "signals", "signals-light"}
DEFAULT_THEME = "signals"


def create_user(conn: sqlite3.Connection, email: str, password_hash: str) -> int:
    """Register a new sign-up user. Raises sqlite3.IntegrityError if the
    email is already taken — callers check that first via get_user_by_email
    for a friendly error message, this is the last-word uniqueness guard.

    theme is passed explicitly (not left to the column's own DEFAULT) so the
    actual default a new signup gets doesn't silently depend on which
    version of the schema this particular database was first created
    under — a long-lived database's stored column default doesn't retroactively
    follow a later change to schemas/sqlite_schema.sql the way a fresh
    install's would."""
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, is_admin, theme, created_at) VALUES (?, ?, 0, ?, ?)",
        (email, password_hash, DEFAULT_THEME, utcnow_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_login(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    """Login accepts either a sign-up user's email or the admin account's
    username — one field, matched against whichever column is populated."""
    return conn.execute(
        "SELECT * FROM users WHERE email = ? OR username = ?", (identifier, identifier)
    ).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def update_user_theme(conn: sqlite3.Connection, user_id: int, theme: str) -> None:
    if theme not in VALID_THEMES:
        raise ValueError(f"theme must be one of {sorted(VALID_THEMES)}, got {theme!r}")
    conn.execute("UPDATE users SET theme = ? WHERE user_id = ?", (theme, user_id))
    conn.commit()


# ============================================================
# Shareholding pattern (SEBI LODR Reg 31) -- sources/nse_shareholding.py.
# Single-source (NSE only) today, so upserted directly by natural key
# rather than routed through the metric_aliases/reconciliation machinery
# the financials tables use.
# ============================================================

def insert_shareholding_observations(conn: sqlite3.Connection, company_id: str, summaries: Iterable) -> int:
    """Upsert one row per (company, fiscal_year, quarter) -- a re-fetch of
    an already-seen quarter overwrites its percentages/provenance in place
    (a later submission for the same quarter is a correction, not a second
    observation to keep both of), same "latest wins" reasoning as
    sources/nse_fetch.py's own seq_Id dedup for revised filings."""
    now = utcnow_iso()
    count = 0
    for s in summaries:
        conn.execute(
            """
            INSERT INTO shareholding_observations
                (company_id, fiscal_year, quarter, promoter_holding_percent,
                 public_holding_percent, employee_trust_percent, source,
                 source_url, submission_date, retrieved_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'nse', ?, ?, ?, ?)
            ON CONFLICT(company_id, fiscal_year, quarter) DO UPDATE SET
                promoter_holding_percent = excluded.promoter_holding_percent,
                public_holding_percent = excluded.public_holding_percent,
                employee_trust_percent = excluded.employee_trust_percent,
                source_url = excluded.source_url,
                submission_date = excluded.submission_date,
                retrieved_at = excluded.retrieved_at
            """,
            (
                company_id, s.fiscal_year, s.quarter, s.promoter_percent,
                s.public_percent, s.employee_trust_percent, s.source_url,
                s.submission_date, now, now,
            ),
        )
        count += 1
    conn.commit()
    return count


def update_shareholding_category_breakdown(
    conn: sqlite3.Connection, company_id: str, fiscal_year: str, quarter: str, breakdown
) -> None:
    """Backfill the FII/DII/Government/Public(non-institutional) columns
    onto an already-upserted shareholding_observations row -- a separate
    call from insert_shareholding_observations() because this data comes
    from a per-quarter XBRL parse (one extra HTTP call per quarter,
    sources/nse_shareholding.py's fetch_shareholding_detail()), not the
    master listing every summary row is upserted from. A no-op if that row
    doesn't exist yet (shouldn't happen in the normal fetch-then-detail
    call order, but silently doing nothing is safer than erroring the
    whole ingest run over one quarter's ordering)."""
    conn.execute(
        """
        UPDATE shareholding_observations
        SET fii_percent = ?, dii_percent = ?, government_percent = ?,
            public_non_institutional_percent = ?, num_shareholders = ?
        WHERE company_id = ? AND fiscal_year = ? AND quarter = ?
        """,
        (
            breakdown.fii_percent, breakdown.dii_percent, breakdown.government_percent,
            breakdown.public_non_institutional_percent, breakdown.num_shareholders,
            company_id, fiscal_year, quarter,
        ),
    )
    conn.commit()


def mark_shareholding_detail_fetched(conn: sqlite3.Connection, company_id: str, fiscal_year: str, quarter: str) -> None:
    """Records *when* the per-quarter detail step (sources/
    nse_shareholding.py's fetch_shareholding_detail(), one extra HTTP call
    per quarter) last ran for this quarter -- separate from whether it
    found a named-holder/FII-DII breakdown to parse, so scripts/
    batch_fetch_nse.py's shareholding job can tell "we tried, this quarter
    genuinely has none" apart from "we haven't tried yet" (see
    detail_fetched_at's own migration comment in storage/database.py).
    Call this once per quarter right after a *successful* detail fetch,
    whether or not the returned breakdown was None -- never after an
    NSEFetchError, so a transient failure still gets retried next run."""
    conn.execute(
        "UPDATE shareholding_observations SET detail_fetched_at = ? WHERE company_id = ? AND fiscal_year = ? AND quarter = ?",
        (utcnow_iso(), company_id, fiscal_year, quarter),
    )
    conn.commit()


def get_shareholding_detail_fetched_periods(conn: sqlite3.Connection, company_id: str) -> set[tuple[str, str]]:
    """{(fiscal_year, quarter), ...} already carrying a detail_fetched_at
    timestamp for this company -- scripts/batch_fetch_nse.py's
    shareholding job skips the expensive per-quarter detail HTTP call for
    any of these on a repeat "Run now" (Settings > Data Operations >
    Schedule), instead of re-fetching every quarter NSE's master listing
    returns on every single click."""
    rows = conn.execute(
        "SELECT fiscal_year, quarter FROM shareholding_observations "
        "WHERE company_id = ? AND detail_fetched_at IS NOT NULL",
        (company_id,),
    ).fetchall()
    return {(row["fiscal_year"], row["quarter"]) for row in rows}


def insert_shareholding_holders(
    conn: sqlite3.Connection,
    company_id: str,
    fiscal_year: str,
    quarter: str,
    holdings: Iterable,
    *,
    source_url: str | None,
    submission_date: str | None,
) -> int:
    """Upsert one row per (company, fiscal_year, quarter, side, holder_name)
    -- same latest-wins reasoning as insert_shareholding_observations()."""
    now = utcnow_iso()
    count = 0
    for h in holdings:
        conn.execute(
            """
            INSERT INTO shareholding_holders
                (company_id, fiscal_year, quarter, side, category, holder_name,
                 num_shares, percent_of_shares, source, source_url,
                 submission_date, retrieved_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nse', ?, ?, ?, ?)
            ON CONFLICT(company_id, fiscal_year, quarter, side, holder_name) DO UPDATE SET
                category = excluded.category,
                num_shares = excluded.num_shares,
                percent_of_shares = excluded.percent_of_shares,
                source_url = excluded.source_url,
                submission_date = excluded.submission_date,
                retrieved_at = excluded.retrieved_at
            """,
            (
                company_id, fiscal_year, quarter, h.side, h.category, h.holder_name,
                h.num_shares, h.percent_of_shares, source_url, submission_date, now, now,
            ),
        )
        count += 1
    conn.commit()
    return count


# 40 quarters = 10 years -- matches the Financials tab's own 10-year framing
# (valuation_dashboard_interactive.js's growth-projection table). Was 12
# (3 years) originally; too tight once the Major Holders table grew an
# Annual view (Q4-of-each-year), which needs several years of Q4s to be
# useful and would otherwise silently truncate a company's older years --
# found via ICICIBANK (20 quarters on file, oldest 8 got cut off).
_SHAREHOLDING_HISTORY_QUARTERS = 40


def list_shareholding_history(
    conn: sqlite3.Connection, company_id: str, limit: int = _SHAREHOLDING_HISTORY_QUARTERS
) -> list[dict]:
    """Up to the last `limit` quarters' shareholding summaries, oldest
    first (left-to-right column order for a Screener-style time-series
    table) -- for the company page's Shareholding Pattern tab."""
    rows = conn.execute(
        """
        SELECT * FROM shareholding_observations
        WHERE company_id = ?
        ORDER BY fiscal_year DESC, quarter DESC
        LIMIT ?
        """,
        (company_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def list_shareholding_holders_all(conn: sqlite3.Connection, company_id: str) -> list[dict]:
    """Every individually-named holder row on file for this company, across
    every quarter -- the raw material web/shareholding_feed.py groups into
    per-quarter FII/DII/Public-other buckets (sources.nse_shareholding.
    classify_public_category) and per-holder trend sparklines. One query
    covering the whole company rather than one per quarter: this table is
    small (a few dozen named holders per quarter, and named-holder
    extraction only covers the last several quarters -- see
    sources/nse_shareholding.py's module docstring), so the feed builder
    just filters/groups the full set in Python against
    list_shareholding_history()'s own quarter list."""
    rows = conn.execute(
        """
        SELECT fiscal_year, quarter, side, category, holder_name, num_shares, percent_of_shares
        FROM shareholding_holders
        WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Batch job audit log -- ingestion/batch_log.py is the only caller of these;
# everything else reads via list_batch_job_runs/list_batch_job_items (Admin
# UI, main.py's batch-log CLI command).
# ============================================================


def start_batch_job_run(conn: sqlite3.Connection, job_name: str, scope_label: str | None = None) -> int:
    now = utcnow_iso()
    cursor = conn.execute(
        "INSERT INTO batch_job_runs (job_name, scope_label, started_at, status) VALUES (?, ?, ?, 'running')",
        (job_name, scope_label, now),
    )
    conn.commit()
    return cursor.lastrowid


def finish_batch_job_run(conn: sqlite3.Connection, run_id: int, *, status: str, notes: str | None = None) -> None:
    counts = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS succeeded, "
        "  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed "
        "FROM batch_job_items WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.execute(
        "UPDATE batch_job_runs SET finished_at = ?, status = ?, notes = ?, "
        "  items_total = ?, items_succeeded = ?, items_failed = ? "
        "WHERE run_id = ?",
        (
            utcnow_iso(), status, notes,
            counts["total"] or 0, counts["succeeded"] or 0, counts["failed"] or 0,
            run_id,
        ),
    )
    conn.commit()


def start_batch_job_item(conn: sqlite3.Connection, run_id: int, company_id: str | None) -> int:
    cursor = conn.execute(
        "INSERT INTO batch_job_items (run_id, company_id, started_at, status) VALUES (?, ?, ?, 'running')",
        (run_id, company_id, utcnow_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def finish_batch_job_item(conn: sqlite3.Connection, item_id: int, *, status: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE batch_job_items SET finished_at = ?, status = ?, detail = ? WHERE item_id = ?",
        (utcnow_iso(), status, detail, item_id),
    )
    conn.commit()


def list_batch_job_runs(conn: sqlite3.Connection, job_name: str | None = None, limit: int = 20) -> list[dict]:
    """Most recent runs first -- Admin UI / CLI history view. job_name is
    optional and keyword-compatible with every existing caller (main.py's
    batch-log CLI command calls this positionally-conn/keyword-limit only) --
    added so the Settings > Data Operations > Schedule panel's "last run"
    display (get_latest_batch_job_run below) and the Audit Log > Job Runs
    tab can each narrow to one job without a second, near-duplicate query."""
    if job_name is not None:
        rows = conn.execute(
            "SELECT * FROM batch_job_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
            (job_name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM batch_job_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_batch_job_run(conn: sqlite3.Connection, job_name: str) -> dict | None:
    """Most recent run for one job, or None if it's never been triggered --
    the Schedule panel's "last run" column. Thin wrapper over
    list_batch_job_runs(job_name=..., limit=1) rather than a separate query,
    so the two stay consistent by construction."""
    rows = list_batch_job_runs(conn, job_name=job_name, limit=1)
    return rows[0] if rows else None


def list_batch_job_items(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Every item in one run, in the order they started -- for drilling into
    which companies failed and why."""
    rows = conn.execute(
        "SELECT * FROM batch_job_items WHERE run_id = ? ORDER BY item_id ASC", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch_job_run_live_progress(conn: sqlite3.Connection, run_id: int) -> dict:
    """How far a run has gotten *right now*, computed live from
    batch_job_items -- unlike batch_job_runs.items_total/succeeded/failed
    (only written once, at the very end, by finish_batch_job_run()), so
    those columns stay NULL for a run's entire duration and give the
    Settings > Data Operations > Schedule panel / Audit Log > Job Runs tab
    nothing to show for a `status='running'` row today, even mid-run --
    e.g. someone navigating away from a long NSE batch fetch and back
    sees only "running" with a start timestamp, no sense of whether it's
    5% or 95% done. Safe to call on an already-finished run too (numbers
    will just match the stored summary), but callers only need this for a
    still-running one -- a finished run already has its own summary."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS items_started,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS items_succeeded,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS items_failed
        FROM batch_job_items WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return {
        "items_started": row["items_started"] or 0,
        "items_succeeded": row["items_succeeded"] or 0,
        "items_failed": row["items_failed"] or 0,
    }


# ============================================================
# Dataset-centric ingestion events (ingestion/events.py, ingestion/event_bus.py)
# ============================================================

def insert_dataset_event(conn: sqlite3.Connection, event: DatasetIngestedEvent) -> None:
    """Append one DATASET_INGESTED event to the Event Store. event_id/
    ingested_at are expected to already be filled (event_bus.publish() does
    that before calling this) -- this function only ever appends, never
    updates, matching the Event Store's immutable-history contract."""
    conn.execute(
        """
        INSERT INTO dataset_events (
            event_id, event_type, dataset_id, dataset_type, source, scope_json,
            period, storage_reference_json, ingestion_id, ingested_at, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id, event.event_type, event.dataset_id, event.dataset_type, event.source,
            json.dumps(event.scope), event.period, json.dumps(event.storage_reference),
            event.ingestion_id, event.ingested_at, json.dumps(event.metadata), utcnow_iso(),
        ),
    )
    conn.commit()


def get_dataset_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dataset_events WHERE event_id = ?", (event_id,)).fetchone()


def list_dataset_events(
    conn: sqlite3.Connection,
    *,
    event_id: str | None = None,
    dataset_type: str | None = None,
    source: str | None = None,
    ingestion_id: str | None = None,
    since: str | None = None,
) -> list[sqlite3.Row]:
    """Query the Event Store -- ingestion/event_bus.py's replay() uses this
    to find events to re-dispatch (README: manual replay / worker failure
    recovery / historical processing for a newly-added worker). Filters
    combine with AND; every filter is optional so callers only constrain
    what they care about."""
    clauses, params = [], []
    if event_id is not None:
        clauses.append("event_id = ?")
        params.append(event_id)
    if dataset_type is not None:
        clauses.append("dataset_type = ?")
        params.append(dataset_type)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if ingestion_id is not None:
        clauses.append("ingestion_id = ?")
        params.append(ingestion_id)
    if since is not None:
        clauses.append("ingested_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM dataset_events {where} ORDER BY ingested_at ASC", params
    ).fetchall()


def start_worker_log(
    conn: sqlite3.Connection, *, event_id: str, ingestion_id: str, worker_name: str, worker_version: str
) -> int:
    """A worker is about to run against one event -- same start/finish
    shape as start_batch_job_item()/finish_batch_job_item(). A replay that
    re-runs the same (event_id, worker_name, worker_version) increments
    retry_count on the existing row instead of violating the table's
    UNIQUE constraint, so history for that exact worker version stays one
    row (the log's own idempotency key), not a growing pile of duplicates."""
    now = utcnow_iso()
    existing = get_worker_log(conn, event_id, worker_name, worker_version)
    if existing is not None:
        conn.execute(
            "UPDATE worker_processing_log SET status = 'running', started_at = ?, completed_at = NULL, "
            "  retry_count = retry_count + 1 WHERE log_id = ?",
            (now, existing["log_id"]),
        )
        conn.commit()
        return existing["log_id"]
    cursor = conn.execute(
        """
        INSERT INTO worker_processing_log (event_id, ingestion_id, worker_name, worker_version, status, started_at)
        VALUES (?, ?, ?, ?, 'running', ?)
        """,
        (event_id, ingestion_id, worker_name, worker_version, now),
    )
    conn.commit()
    return cursor.lastrowid


def finish_worker_log(
    conn: sqlite3.Connection,
    log_id: int,
    *,
    status: str,
    output_reference: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "UPDATE worker_processing_log SET status = ?, completed_at = ?, output_reference = ?, error_message = ? "
        "WHERE log_id = ?",
        (status, utcnow_iso(), output_reference, error_message, log_id),
    )
    conn.commit()


def get_worker_log(
    conn: sqlite3.Connection, event_id: str, worker_name: str, worker_version: str
) -> sqlite3.Row | None:
    """The idempotency check ingestion/event_bus.py's replay() relies on:
    a non-forced replay skips a worker for an event once this returns a row
    with status 'ok' or 'skipped' for that exact worker_version."""
    return conn.execute(
        "SELECT * FROM worker_processing_log WHERE event_id = ? AND worker_name = ? AND worker_version = ?",
        (event_id, worker_name, worker_version),
    ).fetchone()


def list_worker_processing_log(
    conn: sqlite3.Connection,
    *,
    event_id: str | None = None,
    worker_name: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """"What did each worker do with this ingestion event" -- and the
    reverse, "what has this worker done across every event" -- same query,
    filtered differently."""
    clauses, params = [], []
    if event_id is not None:
        clauses.append("event_id = ?")
        params.append(event_id)
    if worker_name is not None:
        clauses.append("worker_name = ?")
        params.append(worker_name)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM worker_processing_log {where} ORDER BY log_id ASC", params
    ).fetchall()
