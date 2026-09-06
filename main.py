"""CLI entrypoint.

Phase 1: scaffold + schema init + config + logging.
Phase 2: CompanyRegistry, ScreenerAdapter, ingestion pipeline.
Phase 3: calculation layer + `analyze` text report.
Phase 4: charts, wired into the same `analyze` command.
Phase 5: LLM research assistant (`ask`), grounded in retrieved evidence
(README: Implementation Sequence, steps 1-5).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env before any project import — config/settings.py reads
# ANTHROPIC_API_KEY at import time to set ANTHROPIC_API_KEY_SET, so this has
# to run first or that flag would be stale for the whole process (affects
# `ask` and `serve`/the web chat). Never overrides a variable already set in
# the real environment (python-dotenv's default) — an exported
# ANTHROPIC_API_KEY still wins over .env.
load_dotenv(Path(__file__).resolve().parent / ".env")

from charts.financial_charts import build_company_charts, save_charts
from companies.lifecycle import archive_company, restore_company
from companies.nse_import import import_nse_companies
from companies.registry import get_company, list_companies, register_company, seed_companies
from companies.stock_actions import ACTION_TYPES, add_stock_action, list_stock_actions
from config.settings import (
    CHARTS_DIR,
    DB_PATH,
    ANTHROPIC_API_KEY_SET,
    ANTHROPIC_MODEL,
    ensure_data_dirs,
    setup_logging,
)
from financials.report import build_analysis_report
from ingestion.detector import is_macro_path
from ingestion.event_bus import replay
from ingestion.pipeline import (
    ingest_bank_infrastructure_file,
    ingest_file,
    ingest_fred_series,
    ingest_macro_file,
    ingest_sec_edgar_company,
    ingest_yfinance_company,
)
from normalization.financials import ensure_metric_vocabulary
from sources.sec_edgar import get_cik_for_ticker
from research.assistant import answer_question
from storage.database import init_db, list_tables
from storage.repositories import (
    add_watchlist_item,
    list_batch_job_items,
    list_batch_job_runs,
    list_watchlist_items,
    remove_watchlist_item,
)
from web.fixtures import THREADS

logger = logging.getLogger(__name__)


def cmd_init(_args: argparse.Namespace) -> None:
    """Create data/log directories, initialize the SQLite schema, and seed reference data."""
    setup_logging()
    ensure_data_dirs()
    conn = init_db()
    ensure_metric_vocabulary(conn)
    tables = list_tables(conn)
    conn.close()
    logger.info("Initialized database at %s", DB_PATH)
    logger.info("Tables: %s", ", ".join(tables))


def cmd_status(_args: argparse.Namespace) -> None:
    """Report whether the database exists and which tables it has."""
    setup_logging()
    if not DB_PATH.exists():
        logger.info("No database at %s yet. Run: python main.py init", DB_PATH)
        return
    conn = init_db()  # idempotent: creates nothing new if already initialized
    tables = list_tables(conn)
    conn.close()
    logger.info("Database: %s", DB_PATH)
    logger.info("Tables (%d): %s", len(tables), ", ".join(tables))
    logger.info(
        "LLM: model=%s, ANTHROPIC_API_KEY set=%s",
        ANTHROPIC_MODEL or "auto (per-question routing)",
        ANTHROPIC_API_KEY_SET,
    )


def cmd_seed_companies(_args: argparse.Namespace) -> None:
    """Register the seed companies (HDFCBANK, ICICIBANK)."""
    setup_logging()
    conn = init_db()
    company_ids = seed_companies(conn)
    conn.close()
    logger.info("Seeded companies: %s", ", ".join(company_ids))


def cmd_add_company(args: argparse.Namespace) -> None:
    """Register (or update) a single company."""
    setup_logging()
    conn = init_db()
    # --fiscal-year-end-month left unset means "use the country's usual
    # default" rather than a single global one: 3 (March close) for India,
    # 12 (calendar year) for the US — an explicit flag always wins.
    fiscal_year_end_month = args.fiscal_year_end_month
    if fiscal_year_end_month is None:
        fiscal_year_end_month = 12 if args.country == "US" else 3
    company_id = register_company(
        conn,
        args.company_id,
        args.legal_name,
        args.display_name,
        nse_symbol=args.nse_symbol,
        bse_code=args.bse_code,
        isin=args.isin,
        country=args.country,
        currency=args.currency,
        fiscal_year_end_month=fiscal_year_end_month,
        website=args.website,
        macro_economic_sector=args.macro_economic_sector,
        sector=args.sector,
        industry=args.industry,
        basic_industry=args.basic_industry,
        listed_date=args.listed_date,
    )
    conn.close()
    logger.info("Registered company %s", company_id)


def cmd_import_nse_companies(args: argparse.Namespace) -> None:
    """Bulk-register companies + NIFTY index tags from an NSE company master
    export (see companies/nse_import.py). Idempotent — safe to re-run
    against a refreshed file."""
    setup_logging()
    conn = init_db()
    result = import_nse_companies(conn, Path(args.file))
    conn.close()
    logger.info(
        "%s: %d rows, %d registered, %d updated, %d skipped",
        args.file, result.total_rows, result.registered, result.updated, len(result.skipped),
    )
    for reason in result.skipped:
        logger.warning("Skipped: %s", reason)


def cmd_ingest_yfinance(args: argparse.Namespace) -> None:
    """Ingest a company's annual financials live from Yahoo Finance (see
    sources/yfinance_financials.py) — the pilot path for non-Indian
    companies, which screener.in has no coverage for."""
    setup_logging()
    conn = init_db()
    ensure_metric_vocabulary(conn)

    if not get_company(conn, args.company_id):
        conn.close()
        raise SystemExit(
            f"No company registered with company_id={args.company_id!r}. "
            f"Run: python main.py add-company {args.company_id} --currency {args.currency} ..."
        )

    result = ingest_yfinance_company(
        conn, args.company_id, args.ticker, currency=args.currency, statement_type=args.statement_type
    )
    conn.close()
    logger.info(
        "%s (yfinance): parsed=%d inserted=%d skipped=%d reconciled=%d",
        args.ticker, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    for reason in result.skip_reasons:
        logger.warning("Skipped: %s", reason)


def cmd_ingest_sec_edgar(args: argparse.Namespace) -> None:
    """Ingest a US company's quarterly + annual financials live from SEC
    EDGAR's own XBRL data (see sources/sec_edgar.py) — the structured,
    regulator-published source that actually closes the "Financials — USA"
    gap sources/yfinance_financials.py's annual-only pilot left open."""
    setup_logging()
    conn = init_db()
    ensure_metric_vocabulary(conn)

    if not get_company(conn, args.company_id):
        conn.close()
        raise SystemExit(
            f"No company registered with company_id={args.company_id!r}. "
            f"Run: python main.py add-company {args.company_id} --currency {args.currency} ..."
        )

    cik = args.cik
    if cik is None:
        cik = get_cik_for_ticker(args.company_id)
        if cik is None:
            conn.close()
            raise SystemExit(
                f"Could not resolve a SEC CIK for ticker {args.company_id!r} from SEC's own "
                f"ticker directory — pass --cik explicitly if you already know it."
            )

    result = ingest_sec_edgar_company(conn, args.company_id, cik, currency=args.currency)
    conn.close()
    logger.info(
        "%s (sec_edgar, CIK%010d): parsed=%d inserted=%d skipped=%d reconciled=%d",
        args.company_id, cik, result.parsed_count, result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    for reason in result.skip_reasons:
        logger.warning("Skipped: %s", reason)


def cmd_add_stock_action(args: argparse.Namespace) -> None:
    """Record a discrete stock action (split/bonus/rights) for a company."""
    setup_logging()
    conn = init_db()
    if get_company(conn, args.company_id) is None:
        conn.close()
        raise SystemExit(
            f"No company registered with company_id={args.company_id!r}. "
            f"Run: python main.py add-company {args.company_id} ... (or seed-companies)"
        )
    action = add_stock_action(
        conn, args.company_id, args.action_type, args.action_date, args.ratio_from, args.ratio_to,
        subscription_price=args.subscription_price, source=args.source, source_url=args.source_url,
        notes=args.notes,
    )
    conn.close()
    logger.info(
        "Recorded %s for %s on %s (%s -> %s)",
        action["action_type"], action["company_id"], action["action_date"],
        action["ratio_from"], action["ratio_to"],
    )


def cmd_list_stock_actions(args: argparse.Namespace) -> None:
    """List every recorded stock action for a company."""
    setup_logging()
    conn = init_db()
    actions = list_stock_actions(conn, args.company_id)
    conn.close()
    if not actions:
        logger.info("No stock actions recorded yet for %s.", args.company_id)
        return
    for action in actions:
        logger.info(
            "%-10s %-6s %s -> %s%s",
            action["action_date"], action["action_type"], action["ratio_from"], action["ratio_to"],
            f" @ {action['subscription_price']}" if action["subscription_price"] is not None else "",
        )


def cmd_ingest_fred(args: argparse.Namespace) -> None:
    """Ingest one US macro series live from FRED (see sources/fred.py) — the
    US counterpart to `ingest data/raw/_macro/rbi/...` for India."""
    setup_logging()
    conn = init_db()
    result = ingest_fred_series(
        conn, args.series_id, unit=args.unit, series_key=args.series_key, region=args.region
    )
    conn.close()
    logger.info(
        "%s (macro/fred): parsed=%d inserted=%d skipped=%d",
        args.series_id, result.parsed_count, result.inserted_count, result.skipped_count,
    )
    for reason in result.skip_reasons:
        logger.warning("Skipped: %s", reason)


def cmd_list_companies(args: argparse.Namespace) -> None:
    """List registered companies."""
    setup_logging()
    conn = init_db()
    companies = list_companies(conn, include_archived=args.include_archived)
    conn.close()
    if not companies:
        logger.info("No companies registered yet. Run: python main.py seed-companies")
        return
    for company in companies:
        logger.info(
            "%-12s %-28s status=%s sector=%s",
            company["company_id"], company["display_name"], company["status"], company["sector"],
        )


def cmd_list_batch_runs(args: argparse.Namespace) -> None:
    """Recent batch job runs (scripts/batch_fetch_nse.py and similar) — start/end, status, per-run outcome counts."""
    setup_logging()
    conn = init_db()
    runs = list_batch_job_runs(conn, limit=args.limit)
    conn.close()
    if not runs:
        logger.info("No batch job runs on file yet.")
        return
    for r in runs:
        logger.info(
            "run_id=%-4s %-24s %-10s status=%-9s ok=%s failed=%s started=%s finished=%s %s",
            r["run_id"], r["job_name"], r["scope_label"] or "", r["status"],
            r["items_succeeded"], r["items_failed"], r["started_at"], r["finished_at"] or "-",
            f"note={r['notes']}" if r["notes"] else "",
        )


def cmd_show_batch_run(args: argparse.Namespace) -> None:
    """Every item (company) in one batch run — status and detail/error per item."""
    setup_logging()
    conn = init_db()
    items = list_batch_job_items(conn, args.run_id)
    conn.close()
    if not items:
        logger.info("No items found for run_id=%s.", args.run_id)
        return
    for it in items:
        logger.info(
            "%-14s status=%-8s %s",
            it["company_id"] or "-", it["status"], it["detail"] or "",
        )


def cmd_replay_events(args: argparse.Namespace) -> None:
    """Re-dispatch already-stored DATASET_INGESTED events to registered
    workers (ingestion/event_bus.py::replay()) -- worker failure recovery,
    backfilling a newly-added worker over history, or reprocessing after a
    worker's logic changed. Never re-fetches/re-ingests source data; a
    worker re-derives purely from what its event's storage_reference
    points at. Idempotent unless --force: a worker already logged ok/skipped
    for an event is left alone."""
    setup_logging()
    conn = init_db()
    outcomes = replay(
        conn,
        event_id=args.event_id,
        dataset_type=args.dataset_type,
        worker_name=args.worker,
        since=args.since,
        force=args.force,
    )
    conn.close()
    if not outcomes:
        logger.info("Nothing to replay (no matching events, or every worker already ok/skipped -- use --force to override).")
        return
    for outcome in outcomes:
        logger.info(
            "%-22s v%-3s %-8s %s",
            outcome.worker_name, outcome.worker_version, outcome.result.status,
            outcome.result.output_reference or outcome.result.error or "",
        )


def cmd_vector_backfill(args: argparse.Namespace) -> None:
    """One-time, idempotent backfill (section 11): generate embeddings for
    every already-processed document's existing chunks and upsert them into
    the VectorStore. Reuses retrieval/semantic_indexer.py's
    embed_and_index_document_chunks() -- the same function
    ingestion/workers/embedding_indexer_worker.py calls on every future
    ingestion, so there is exactly one embedding-generation implementation
    (section 12), not a second backfill-only path.

    Idempotent by construction: a chunk already embedding_status='indexed'
    under the CURRENT embedding model is skipped without calling the
    embedding provider or the vector store, so re-running this command costs
    nothing extra and never duplicates vectors. --force re-embeds everything
    regardless (e.g. after deliberately changing EMBEDDING_MODEL_LOCAL/
    EMBEDDING_PROVIDER). --company-id/--limit are the cost guardrail this
    feature's spec calls for -- point this at a small/synthetic dataset (see
    tests/test_vector_backfill.py) or a small handful of real documents
    before ever running it unbounded against the real document archive.
    --document-type narrows to one document_type (e.g. 'transcript' for
    concall transcripts only), combinable with --company-id/--limit.

    Each document's outcome is recorded to batch_job_runs/batch_job_items
    (ingestion/batch_log.py) — the same durable, queryable audit trail
    scripts/batch_fetch_nse.py uses — not just this process's own stdout/
    logs/app.log. `main.py list-batch-runs`/`show-batch-run` read it back."""
    setup_logging()
    conn = init_db()

    from ingestion.batch_log import BatchRun
    from retrieval.embedding_provider import EmbeddingProviderUnavailable, default_embedding_provider
    from retrieval.semantic_indexer import embed_and_index_document_chunks
    from retrieval.vector_store import VectorStoreUnavailable, default_vector_store
    from storage.repositories import list_documents_by_status

    store = default_vector_store()
    if store is None:
        conn.close()
        raise SystemExit("VECTOR_STORE_BACKEND=none — the vector layer is disabled, nothing to backfill.")
    if not store.health_check():
        conn.close()
        raise SystemExit(
            "Vector store unreachable (config.settings.QDRANT_URL) — start it (see config/settings.py's "
            "VECTOR_STORE_BACKEND comment for the docker run command) and retry. FTS5/BM25 keyword search "
            "is unaffected in the meantime (section 10)."
        )
    try:
        provider = default_embedding_provider()
    except EmbeddingProviderUnavailable as exc:
        conn.close()
        raise SystemExit(f"Embedding provider unavailable: {exc}")

    documents = list_documents_by_status(conn, "processed")
    if args.company_id:
        documents = [d for d in documents if d["company_id"] == args.company_id]
    if args.document_type:
        documents = [d for d in documents if d["document_type"] == args.document_type]
    if args.limit is not None:
        documents = documents[: args.limit]

    logger.info("vector-backfill: %d eligible document(s), embedding_model=%s", len(documents), provider.model_id)

    scope_label = (
        f"company_id={args.company_id or 'all'} document_type={args.document_type or 'all'} "
        f"limit={args.limit if args.limit is not None else 'none'} force={args.force}"
    )
    documents_embedded = 0
    chunks_embedded = 0
    chunks_already_indexed = 0
    failed = 0
    with BatchRun(conn, "vector_backfill", scope_label) as run:
        for doc in documents:
            doc_failed = False
            with run.item(doc["company_id"]) as item:
                try:
                    result = embed_and_index_document_chunks(
                        conn, doc, embedding_provider=provider, vector_store=store, force=args.force
                    )
                except (VectorStoreUnavailable, EmbeddingProviderUnavailable) as exc:
                    logger.warning("document %s: %s", doc["document_id"], exc)
                    doc_failed = True
                    raise  # re-raise so run.item() records this item as failed too
                chunks_already_indexed += result.chunks_already_indexed
                if result.chunks_embedded:
                    documents_embedded += 1
                    chunks_embedded += result.chunks_embedded
                item.detail = (
                    f"document_id={doc['document_id']} chunks_total={result.chunks_total} "
                    f"embedded={result.chunks_embedded} already_indexed={result.chunks_already_indexed}"
                )
                logger.info(
                    "document %-6s chunks_total=%-4d embedded=%-4d already_indexed=%-4d",
                    doc["document_id"], result.chunks_total, result.chunks_embedded, result.chunks_already_indexed,
                )
            if doc_failed:
                failed += 1

    conn.close()
    logger.info(
        "vector-backfill done: documents_considered=%d documents_with_new_embeddings=%d "
        "chunks_embedded=%d chunks_already_indexed=%d failed=%d",
        len(documents), documents_embedded, chunks_embedded, chunks_already_indexed, failed,
    )


def cmd_graph_backfill(args: argparse.Namespace) -> None:
    """One-time, explicit full (re)sync of SQLite's facts into Neo4j
    (context/graph_neo4j.py) — company/sector/investigation nodes
    (sync_graph), knowledge-graph entities/claims/relationships/evidence
    (sync_knowledge_graph), and optionally canonical_financials observations
    (sync_financials). SQLite stays the source of truth throughout; every
    sync here is idempotent (MERGE, never duplicates) and is otherwise done
    lazily/automatically on the first graph read once GRAPH_BACKEND=neo4j —
    this command exists to prime the graph explicitly (e.g. right after
    switching backends, or after a large ingestion run) instead of paying
    for that sync on whichever request happens to trigger it first.

    sync_financials is TRIAL and deliberately NOT part of the automatic
    resync path (context/graph_neo4j.py's module comment: 1000+ rows per
    company makes "sync everything" a real scale/cost decision) — so it's
    opt-in here too: pass --company-id (repeatable) to scope it, or
    --all-financials to sync every registered company. Neither flag touches
    sync_graph/sync_knowledge_graph, which always run in full — those are
    already the automatic-resync default and cheap at this app's scale.

    Each phase is recorded as one item to batch_job_runs/batch_job_items
    (ingestion/batch_log.py) — the same durable, queryable audit trail
    scripts/batch_fetch_nse.py uses — not just this process's own stdout/
    logs/app.log. `main.py list-batch-runs`/`show-batch-run` read it back."""
    setup_logging()

    from config.settings import GRAPH_BACKEND

    if GRAPH_BACKEND != "neo4j":
        raise SystemExit(
            "GRAPH_BACKEND=sqlite — Neo4j is disabled, nothing to backfill. Set GRAPH_BACKEND=neo4j "
            "(config/settings.py) and start Neo4j, then retry."
        )

    from context import graph_neo4j

    try:
        driver = graph_neo4j.get_driver()
        driver.verify_connectivity()
    except Exception as exc:
        raise SystemExit(
            f"Neo4j unreachable ({exc}) — start it (see context/graph_neo4j.py's module docstring for the "
            "docker run command) and retry."
        ) from exc

    conn = init_db()
    from ingestion.batch_log import BatchRun
    from storage.fact_store import default_fact_store

    fs = default_fact_store()

    if args.company_id:
        financial_company_ids = args.company_id
    elif args.all_financials:
        financial_company_ids = [c["company_id"] for c in list_companies(conn)]
    else:
        financial_company_ids = []

    scope_label = f"financials={','.join(financial_company_ids) if financial_company_ids else 'none'}"

    with BatchRun(conn, "graph_backfill", scope_label) as run:
        logger.info("graph-backfill: syncing companies/sectors/investigations...")
        with run.item(None) as item:
            graph_neo4j.sync_graph(conn, driver, fact_store=fs)
            item.detail = "companies/sectors/investigations synced"

        logger.info("graph-backfill: syncing knowledge entities/claims/relationships...")
        with run.item(None) as item:
            graph_neo4j.sync_knowledge_graph(conn, driver, fact_store=fs)
            item.detail = "knowledge entities/claims/relationships/evidence synced"

        if financial_company_ids:
            logger.info(
                "graph-backfill: syncing canonical_financials for %d company(ies)...", len(financial_company_ids)
            )
            with run.item(None) as item:
                synced = graph_neo4j.sync_financials(conn, driver, fact_store=fs, company_ids=financial_company_ids)
                item.detail = f"{synced} financial observation(s) synced for {len(financial_company_ids)} company(ies)"
                logger.info("graph-backfill: %d financial observation(s) synced", synced)
        else:
            logger.info(
                "graph-backfill: skipping canonical_financials sync (pass --company-id or --all-financials to "
                "include it — see context/graph_neo4j.py's TRIAL comment on sync_financials for why this isn't "
                "automatic)"
            )

    conn.close()
    logger.info("graph-backfill done.")


def cmd_entity_resolution_backfill(args: argparse.Namespace) -> None:
    """One-off backfill (context/entity_resolution.py, Step 2B follow-up):
    for each company, find its duplicate `Company`-type knowledge_entities
    rows (a free-form extracted name alongside the canonical row named
    after the company_id itself) and merge any that are an EXACT match,
    after normalization, against this company's own known identifiers
    (is_same_company_identity()) — never a fuzzy/similarity guess. A
    read-only query against the real database (this feature's implementation
    plan) found 127 companies with duplicate Company-type rows, but most
    duplicates are genuinely distinct subsidiaries/auditors/extraction noise
    sharing the company's company_id scope, not spelling variants of the
    same company — leaving those alone (not merging) is the intended,
    correct outcome, not a shortfall of this command.

    Defaults to a dry run (report-only, no writes) — pass --apply to
    actually repoint knowledge_relationships and delete the duplicate rows
    (storage/repositories.py::merge_knowledge_entities()). --company-id
    (repeatable) scopes to specific companies; omitted, every registered
    company (including archived ones — a duplicate doesn't stop existing
    just because the company was archived) is considered.

    Each company is recorded as one batch_job_runs/batch_job_items item
    (ingestion/batch_log.py) — the same durable, queryable audit trail
    scripts/batch_fetch_nse.py and main.py graph-backfill/vector-backfill
    already use — with item.detail spelling out exactly what was merged and
    what was deliberately left alone (e.g. "merged=1 (AMBUJA CEMENTS
    LIMITED); left_alone=1 (ACC)"), queryable forever via
    `main.py show-batch-run <id>` — the human-reviewable report this gap
    calls for, no separate report file needed.

    KNOWN GAP: if GRAPH_BACKEND=neo4j, this only merges the SQLite side —
    context/graph_neo4j.py::sync_knowledge_graph() is pure-MERGE and never
    prunes, so a merged-away duplicate's Neo4j node is left orphaned until a
    fresh `main.py graph-backfill` (or a manual Cypher DETACH DELETE) cleans
    it up. Building a real prune step is a bigger, separate change."""
    setup_logging()
    conn = init_db()

    from context.entity_resolution import is_same_company_identity
    from ingestion.batch_log import BatchRun
    from storage.repositories import list_company_type_knowledge_entities, merge_knowledge_entities

    if args.company_id:
        company_ids = args.company_id
    else:
        company_ids = [c["company_id"] for c in list_companies(conn, include_archived=True)]

    scope_label = f"company_id={','.join(args.company_id) if args.company_id else 'all'} apply={args.apply}"
    companies_with_merges = 0
    companies_left_alone_only = 0

    with BatchRun(conn, "entity_resolution_backfill", scope_label) as run:
        for company_id in company_ids:
            with run.item(company_id) as item:
                company_row = get_company(conn, company_id)
                if company_row is None:
                    item.detail = "no such company, skipped"
                    continue

                entities = list_company_type_knowledge_entities(conn, company_id)
                canonical = next((e for e in entities if e["name"] == company_id), None)
                if canonical is None:
                    item.detail = "no canonical Company-type entity on file, nothing to merge"
                    continue

                merged_names: list[str] = []
                left_alone_names: list[str] = []
                for entity in entities:
                    if entity["entity_id"] == canonical["entity_id"]:
                        continue
                    if is_same_company_identity(entity["name"], company_row):
                        merged_names.append(entity["name"])
                        if args.apply:
                            merge_knowledge_entities(
                                conn, from_entity_id=entity["entity_id"], into_entity_id=canonical["entity_id"]
                            )
                    else:
                        left_alone_names.append(entity["name"])

                if not merged_names and not left_alone_names:
                    item.detail = "no duplicate Company-type entities found"
                    continue
                item.detail = (
                    f"merged={len(merged_names)} ({', '.join(merged_names)}); "
                    f"left_alone={len(left_alone_names)} ({', '.join(left_alone_names)})"
                )
                if merged_names:
                    companies_with_merges += 1
                elif left_alone_names:
                    companies_left_alone_only += 1

    conn.close()
    logger.info(
        "entity-resolution-backfill done (dry_run=%s): %d compan(y/ies) considered, "
        "%d with a merge candidate, %d with left-alone-only duplicates. "
        "Run `main.py show-batch-run <run_id>` for the per-company detail%s.",
        not args.apply, len(company_ids), companies_with_merges, companies_left_alone_only,
        "" if args.apply else " (nothing was written — pass --apply to actually merge)",
    )


def cmd_archive_company(args: argparse.Namespace) -> None:
    """Archive a company (metadata flip only — observations/documents untouched)."""
    setup_logging()
    conn = init_db()
    archive_company(conn, args.company_id, args.reason)
    conn.close()
    logger.info("Archived %s (reason=%s)", args.company_id, args.reason)


def cmd_restore_company(args: argparse.Namespace) -> None:
    """Restore an archived company to active."""
    setup_logging()
    conn = init_db()
    restore_company(conn, args.company_id)
    conn.close()
    logger.info("Restored %s to active", args.company_id)


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest one raw file. Routes on path: data/raw/_macro/<source>/<file> goes
    through the company-less macro pipeline; everything else through the
    normal detect -> parse -> validate -> store -> reconcile flow."""
    setup_logging()
    conn = init_db()
    ensure_metric_vocabulary(conn)
    file_path = Path(args.file)

    if is_macro_path(file_path):
        # RBI's monthly bank-level ATM/NEFT/RTGS bulletins are bank x metric
        # x period, not a flat series like every other macro file — routed
        # to their own table/pipeline by filename (they live alongside the
        # regular RBI DBIE table exports under the same _macro/rbi/ path).
        if file_path.name.upper().startswith(("ATM", "NEFTRTGS")):
            bank_result = ingest_bank_infrastructure_file(conn, file_path, source_id=args.source)
            conn.close()
            logger.info(
                "%s (bank_infrastructure/%s): parsed=%d inserted=%d",
                bank_result.file_path, bank_result.source_id, bank_result.parsed_count, bank_result.inserted_count,
            )
            return

        macro_result = ingest_macro_file(conn, file_path, source_id=args.source, series_key=args.series_key)
        conn.close()
        logger.info(
            "%s (macro/%s): parsed=%d inserted=%d skipped=%d",
            macro_result.file_path, macro_result.source_id, macro_result.parsed_count,
            macro_result.inserted_count, macro_result.skipped_count,
        )
        for reason in macro_result.skip_reasons:
            logger.warning("Skipped: %s", reason)
        return

    if args.company_id and not get_company(conn, args.company_id):
        conn.close()
        raise SystemExit(
            f"No company registered with company_id={args.company_id!r}. "
            f"Run: python main.py add-company {args.company_id} ... (or seed-companies)"
        )

    result = ingest_file(
        conn,
        file_path,
        company_id=args.company_id,
        source_id=args.source,
        statement_type=args.statement_type,
    )
    conn.close()

    logger.info(
        "%s (%s): parsed=%d inserted=%d skipped=%d reconciled=%d",
        result.file_path, result.source_id, result.parsed_count,
        result.inserted_count, result.skipped_count, result.reconciled_count,
    )
    for reason in result.skip_reasons:
        logger.warning("Skipped: %s", reason)


def cmd_analyze(args: argparse.Namespace) -> None:
    """Print a text report for one company: trends, growth, ROA/ROE, vendor-reported ratios.
    With --charts, also save PNG trend charts to data/charts/<company_id>/."""
    setup_logging()
    conn = init_db()

    if get_company(conn, args.company_id) is None:
        conn.close()
        raise SystemExit(
            f"No company registered with company_id={args.company_id!r}. "
            f"Run: python main.py add-company {args.company_id} ... (or seed-companies)"
        )

    report = build_analysis_report(conn, args.company_id, statement_type=args.statement_type)

    if args.charts:
        figures = build_company_charts(conn, args.company_id, statement_type=args.statement_type)
        conn.close()
        if figures:
            output_dir = CHARTS_DIR / args.company_id
            paths = save_charts(figures, output_dir)
            logger.info("Saved %d chart(s) to %s", len(paths), output_dir)
        else:
            logger.info("No data available yet to chart for %s", args.company_id)
    else:
        conn.close()

    print(report)


def cmd_ask(args: argparse.Namespace) -> None:
    """Ask the LLM research assistant a question, grounded in retrieved evidence.
    Pass --company more than once to ask a peer-comparison question."""
    setup_logging()
    conn = init_db()

    for company_id in args.company:
        if get_company(conn, company_id) is None:
            conn.close()
            raise SystemExit(
                f"No company registered with company_id={company_id!r}. "
                f"Run: python main.py add-company {company_id} ... (or seed-companies)"
            )

    if not ANTHROPIC_API_KEY_SET:
        conn.close()
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Export it (e.g. via a .env file you source) before running `ask`."
        )

    answer = answer_question(conn, args.question, args.company, statement_type=args.statement_type)
    conn.close()
    print(answer)


def cmd_watchlist_add(args: argparse.Namespace) -> None:
    """Pin a company or example research thread to the (single, shared) watchlist."""
    setup_logging()
    conn = init_db()
    if args.item_type == "company" and get_company(conn, args.item_ref) is None:
        conn.close()
        raise SystemExit(f"No company registered with company_id={args.item_ref!r}.")
    if args.item_type == "thread" and args.item_ref not in THREADS:
        conn.close()
        raise SystemExit(f"No example thread with id={args.item_ref!r}. Known: {', '.join(THREADS)}")
    add_watchlist_item(conn, args.item_type, args.item_ref)
    conn.close()
    logger.info("Pinned %s:%s", args.item_type, args.item_ref)


def cmd_watchlist_remove(args: argparse.Namespace) -> None:
    """Unpin a company or thread from the watchlist."""
    setup_logging()
    conn = init_db()
    remove_watchlist_item(conn, args.item_type, args.item_ref)
    conn.close()
    logger.info("Unpinned %s:%s", args.item_type, args.item_ref)


def cmd_list_watchlist(_args: argparse.Namespace) -> None:
    """List everything currently pinned, most recent first."""
    setup_logging()
    conn = init_db()
    items = list_watchlist_items(conn)
    conn.close()
    if not items:
        logger.info("Nothing pinned yet. Run: python main.py watchlist-add company HDFCBANK")
        return
    for item in items:
        logger.info("%-8s %-16s pinned_at=%s", item["item_type"], item["item_ref"], item["pinned_at"])


def cmd_serve(args: argparse.Namespace) -> None:
    """Run the local Flask viewer. Import is deferred so `flask` is only
    required if you actually use this command, not for the rest of the CLI.

    The vector store is a hard dependency of `serve` (unlike every other
    command, where it's optional/degrades gracefully per section 10) — checked
    and failed fast, before any other startup work, rather than left to fail
    later/silently on the first hybrid_search_documents() call."""
    setup_logging()

    from retrieval.vector_store import default_vector_store

    store = default_vector_store()
    if store is None:
        raise SystemExit(
            "VECTOR_STORE_BACKEND=none — the vector layer is disabled, but `serve` requires it. "
            "Set VECTOR_STORE_BACKEND=qdrant (config/settings.py) and start Qdrant, then retry."
        )
    if not store.health_check():
        raise SystemExit(
            "Vector store unreachable (config.settings.QDRANT_URL) — start it (see config/settings.py's "
            "VECTOR_STORE_BACKEND comment for the docker run command) and retry. `serve` will not start "
            "without it."
        )

    ensure_data_dirs()
    conn = init_db()
    ensure_metric_vocabulary(conn)
    conn.close()

    from web.app import create_app

    create_app().run(host=args.host, port=args.port, debug=args.debug)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Global Equity Research Assistant CLI — US + India focus",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create data/log directories and initialize the SQLite schema"
    )
    init_parser.set_defaults(func=cmd_init)

    status_parser = subparsers.add_parser(
        "status", help="Show database location, tables, and LLM config"
    )
    status_parser.set_defaults(func=cmd_status)

    seed_companies_parser = subparsers.add_parser(
        "seed-companies", help="Register the seed companies (HDFCBANK, ICICIBANK)"
    )
    seed_companies_parser.set_defaults(func=cmd_seed_companies)

    add_company_parser = subparsers.add_parser("add-company", help="Register a company")
    add_company_parser.add_argument("company_id")
    add_company_parser.add_argument("--legal-name", required=True)
    add_company_parser.add_argument("--display-name", required=True)
    add_company_parser.add_argument("--nse-symbol")
    add_company_parser.add_argument("--bse-code")
    add_company_parser.add_argument("--isin")
    add_company_parser.add_argument("--country", default="IN", help="ISO 3166-1 alpha-2, e.g. IN, US (default: IN)")
    add_company_parser.add_argument("--currency", default="INR", help="ISO 4217, e.g. INR, USD (default: INR)")
    add_company_parser.add_argument(
        "--fiscal-year-end-month", type=int, default=None,
        help="1-12, the month the fiscal year closes in (default: 3 for --country IN, 12 for --country US)",
    )
    add_company_parser.add_argument("--website")
    add_company_parser.add_argument("--macro-economic-sector", help="NSE classification, broadest level")
    add_company_parser.add_argument("--sector", help="NSE classification")
    add_company_parser.add_argument("--industry", help="NSE classification")
    add_company_parser.add_argument("--basic-industry", help="NSE classification, most granular level")
    add_company_parser.add_argument("--listed-date")
    add_company_parser.set_defaults(func=cmd_add_company)

    import_nse_companies_parser = subparsers.add_parser(
        "import-nse-companies",
        help="Bulk-register companies + NIFTY index tags from an NSE company master .xlsx export",
    )
    import_nse_companies_parser.add_argument("file", help="Path to the NSE company master .xlsx")
    import_nse_companies_parser.set_defaults(func=cmd_import_nse_companies)

    list_companies_parser = subparsers.add_parser("list-companies", help="List registered companies")
    list_companies_parser.add_argument(
        "--include-archived", action="store_true", help="Include archived companies"
    )
    list_companies_parser.set_defaults(func=cmd_list_companies)

    archive_parser = subparsers.add_parser("archive-company", help="Archive a company")
    archive_parser.add_argument("company_id")
    archive_parser.add_argument(
        "--reason", required=True,
        choices=["delisted", "acquired", "merged", "renamed", "duplicate", "manual"],
    )
    archive_parser.set_defaults(func=cmd_archive_company)

    restore_parser = subparsers.add_parser("restore-company", help="Restore an archived company")
    restore_parser.add_argument("company_id")
    restore_parser.set_defaults(func=cmd_restore_company)

    ingest_parser = subparsers.add_parser(
        "ingest",
        help=(
            "Ingest a raw file — a company file (e.g. data/raw/HDFCBANK/screener/HDFCBANK.xlsx) "
            "or a macro file (e.g. data/raw/_macro/rbi/repo_rate.csv), detected from the path"
        ),
    )
    ingest_parser.add_argument("file", help="Path to the raw file")
    ingest_parser.add_argument(
        "--company-id", help="Override the company_id inferred from the file path (company files only)"
    )
    ingest_parser.add_argument(
        "--source", help="Override the source_id inferred from the file path (e.g. screener, rbi, imd)"
    )
    ingest_parser.add_argument(
        "--statement-type", default="consolidated", choices=["consolidated", "standalone"],
    )
    ingest_parser.add_argument(
        "--series-key", help="Override the series_key inferred from the filename (macro files only)"
    )
    ingest_parser.set_defaults(func=cmd_ingest)

    ingest_yfinance_parser = subparsers.add_parser(
        "ingest-yfinance",
        help="Ingest a company's annual financials live from Yahoo Finance (non-Indian companies — screener.in has no coverage for them)",
    )
    ingest_yfinance_parser.add_argument("company_id")
    ingest_yfinance_parser.add_argument("ticker", help="Yahoo Finance ticker, e.g. AAPL")
    ingest_yfinance_parser.add_argument("--currency", default="USD", help="ISO 4217 (default: USD)")
    ingest_yfinance_parser.add_argument(
        "--statement-type", default="consolidated", choices=["consolidated", "standalone"],
    )
    ingest_yfinance_parser.set_defaults(func=cmd_ingest_yfinance)

    ingest_sec_edgar_parser = subparsers.add_parser(
        "ingest-sec-edgar",
        help="Ingest a US company's quarterly + annual financials live from SEC EDGAR's own XBRL data",
    )
    ingest_sec_edgar_parser.add_argument("company_id")
    ingest_sec_edgar_parser.add_argument(
        "--cik", type=int, default=None,
        help="SEC CIK (integer, no leading zeros needed) — auto-resolved from company_id as a ticker if omitted",
    )
    ingest_sec_edgar_parser.add_argument("--currency", default="USD", help="ISO 4217 (default: USD)")
    ingest_sec_edgar_parser.set_defaults(func=cmd_ingest_sec_edgar)

    ingest_fred_parser = subparsers.add_parser(
        "ingest-fred",
        help="Ingest one US macro series live from FRED (e.g. FEDFUNDS, DGS10, CPIAUCSL, UNRATE)",
    )
    ingest_fred_parser.add_argument("series_id", help="FRED series id, e.g. FEDFUNDS")
    ingest_fred_parser.add_argument("--unit", required=True, help="e.g. PERCENT, INDEX, USD_BILLION")
    ingest_fred_parser.add_argument(
        "--series-key", help="Override the series_key (default: series_id lowercased)"
    )
    ingest_fred_parser.add_argument("--region", help="Default: national-level (no region)")
    ingest_fred_parser.set_defaults(func=cmd_ingest_fred)

    add_stock_action_parser = subparsers.add_parser(
        "add-stock-action", help="Record a discrete stock action (split, bonus, or rights issue)"
    )
    add_stock_action_parser.add_argument("company_id")
    add_stock_action_parser.add_argument("action_type", choices=sorted(ACTION_TYPES))
    add_stock_action_parser.add_argument("action_date", help="ISO date, e.g. 2024-06-15 (the ex-date)")
    add_stock_action_parser.add_argument("ratio_from", type=float, help="Shares held before, e.g. 1")
    add_stock_action_parser.add_argument("ratio_to", type=float, help="Shares held after, e.g. 2 (a 1:2 split)")
    add_stock_action_parser.add_argument(
        "--subscription-price", type=float, help="Rights issues only — the per-share subscription price"
    )
    add_stock_action_parser.add_argument("--source")
    add_stock_action_parser.add_argument("--source-url")
    add_stock_action_parser.add_argument("--notes")
    add_stock_action_parser.set_defaults(func=cmd_add_stock_action)

    list_stock_actions_parser = subparsers.add_parser(
        "list-stock-actions", help="List every recorded stock action for a company"
    )
    list_stock_actions_parser.add_argument("company_id")
    list_stock_actions_parser.set_defaults(func=cmd_list_stock_actions)

    analyze_parser = subparsers.add_parser(
        "analyze", help="Print a text report: trends, growth, ROA/ROE, vendor-reported ratios"
    )
    analyze_parser.add_argument("company_id")
    analyze_parser.add_argument(
        "--statement-type", default="consolidated", choices=["consolidated", "standalone"],
    )
    analyze_parser.add_argument(
        "--charts", action="store_true", help="Also save PNG trend charts to data/charts/<company_id>/"
    )
    analyze_parser.set_defaults(func=cmd_analyze)

    ask_parser = subparsers.add_parser(
        "ask", help="Ask the LLM research assistant a question, grounded in retrieved evidence"
    )
    ask_parser.add_argument("question")
    ask_parser.add_argument(
        "--company", action="append", required=True, dest="company",
        help="Company ID to include as evidence; repeat --company for a peer-comparison question",
    )
    ask_parser.add_argument(
        "--statement-type", default="consolidated", choices=["consolidated", "standalone"],
    )
    ask_parser.set_defaults(func=cmd_ask)

    watchlist_add_parser = subparsers.add_parser("watchlist-add", help="Pin a company or thread to the watchlist")
    watchlist_add_parser.add_argument("item_type", choices=["company", "thread"])
    watchlist_add_parser.add_argument("item_ref", help="company_id, or an example thread_id")
    watchlist_add_parser.set_defaults(func=cmd_watchlist_add)

    watchlist_remove_parser = subparsers.add_parser("watchlist-remove", help="Unpin a company or thread")
    watchlist_remove_parser.add_argument("item_type", choices=["company", "thread"])
    watchlist_remove_parser.add_argument("item_ref")
    watchlist_remove_parser.set_defaults(func=cmd_watchlist_remove)

    list_watchlist_parser = subparsers.add_parser("list-watchlist", help="List everything pinned")
    list_watchlist_parser.set_defaults(func=cmd_list_watchlist)

    list_batch_runs_parser = subparsers.add_parser(
        "list-batch-runs", help="Recent batch job runs (scripts/batch_fetch_nse.py and similar) — audit log"
    )
    list_batch_runs_parser.add_argument("--limit", type=int, default=20)
    list_batch_runs_parser.set_defaults(func=cmd_list_batch_runs)

    show_batch_run_parser = subparsers.add_parser(
        "show-batch-run", help="Every item (company) in one batch run — status and detail/error per item"
    )
    show_batch_run_parser.add_argument("run_id", type=int)
    show_batch_run_parser.set_defaults(func=cmd_show_batch_run)

    replay_events_parser = subparsers.add_parser(
        "replay-events",
        help="Re-dispatch stored DATASET_INGESTED events to registered workers (worker recovery/backfill/audit)",
    )
    replay_events_parser.add_argument("--event-id", help="Replay one specific event")
    replay_events_parser.add_argument("--dataset-type", help='e.g. "company_financials", "document", "macro"')
    replay_events_parser.add_argument("--worker", help="Only replay this worker (by name), e.g. financial_derivation")
    replay_events_parser.add_argument("--since", help="Only events with ingested_at >= this ISO-8601 timestamp")
    replay_events_parser.add_argument(
        "--force", action="store_true",
        help="Re-run a worker even if it already logged ok/skipped for that event (default: skip)",
    )
    replay_events_parser.set_defaults(func=cmd_replay_events)

    vector_backfill_parser = subparsers.add_parser(
        "vector-backfill",
        help="One-time/idempotent backfill: embed every already-processed document's existing "
             "chunks and upsert them into the VectorStore for semantic search (section 11)",
    )
    vector_backfill_parser.add_argument("--company-id", help="Only backfill this company's documents")
    vector_backfill_parser.add_argument(
        "--document-type",
        help="Only backfill documents of this type (e.g. 'transcript' for concall transcripts — see "
             "research/documents.py's _DOCUMENT_TYPE_LABELS for the full set of stored values)",
    )
    vector_backfill_parser.add_argument(
        "--limit", type=int,
        help="Only process the first N eligible documents — the cost guardrail for a real-data demo run",
    )
    vector_backfill_parser.add_argument(
        "--force", action="store_true",
        help="Re-embed every chunk even if already indexed under the current embedding model (default: skip)",
    )
    vector_backfill_parser.set_defaults(func=cmd_vector_backfill)

    graph_backfill_parser = subparsers.add_parser(
        "graph-backfill",
        help="One-time, explicit full sync of SQLite facts into Neo4j — company/sector/investigation "
             "graph, knowledge-graph entities/relationships, and (opt-in) financial observations",
    )
    graph_backfill_parser.add_argument(
        "--company-id", action="append",
        help="Include this company's canonical_financials in the sync (repeatable). Only affects the "
             "TRIAL financials sync — the company/sector/knowledge graph always syncs in full",
    )
    graph_backfill_parser.add_argument(
        "--all-financials", action="store_true",
        help="Sync canonical_financials for every registered company, not just --company-id",
    )
    graph_backfill_parser.set_defaults(func=cmd_graph_backfill)

    entity_resolution_backfill_parser = subparsers.add_parser(
        "entity-resolution-backfill",
        help="One-off backfill: merge a company's duplicate Company-type knowledge_entities rows into the "
             "canonical one, but ONLY on an exact match (never fuzzy) against the company's own known identifiers",
    )
    entity_resolution_backfill_parser.add_argument(
        "--company-id", action="append",
        help="Only consider this company (repeatable). Omitted: every registered company, including archived ones",
    )
    entity_resolution_backfill_parser.add_argument(
        "--apply", action="store_true",
        help="Actually repoint knowledge_relationships and delete the duplicate rows (default: dry run, report only)",
    )
    entity_resolution_backfill_parser.set_defaults(func=cmd_entity_resolution_backfill)

    serve_parser = subparsers.add_parser(
        "serve", help="Run the local web viewer (renders the same analyze report in-browser)"
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)
    serve_parser.add_argument("--debug", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
