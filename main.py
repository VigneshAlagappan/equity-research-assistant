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
from ingestion.pipeline import (
    ingest_bank_infrastructure_file,
    ingest_file,
    ingest_macro_file,
    ingest_yfinance_company,
)
from normalization.financials import ensure_metric_vocabulary
from research.assistant import answer_question
from storage.database import init_db, list_tables
from storage.repositories import add_watchlist_item, list_watchlist_items, remove_watchlist_item
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
    """Register the POC seed companies (HDFCBANK, ICICIBANK)."""
    setup_logging()
    conn = init_db()
    company_ids = seed_companies(conn)
    conn.close()
    logger.info("Seeded companies: %s", ", ".join(company_ids))


def cmd_add_company(args: argparse.Namespace) -> None:
    """Register (or update) a single company."""
    setup_logging()
    conn = init_db()
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
    required if you actually use this command, not for the rest of the CLI."""
    setup_logging()
    ensure_data_dirs()
    conn = init_db()
    ensure_metric_vocabulary(conn)
    conn.close()

    from web.app import create_app

    create_app().run(host=args.host, port=args.port, debug=args.debug)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Indian Equity AI Research Assistant (POC) CLI",
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
        "seed-companies", help="Register the POC seed companies (HDFCBANK, ICICIBANK)"
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
