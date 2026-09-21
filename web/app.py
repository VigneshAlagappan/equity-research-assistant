"""Local web viewer — renders the same analyze report in-browser.

Mostly read-only: everything except the Admin tab only ever reads
canonical_financials via the existing financials/report.py. The Admin tab
edits a company's own metadata (name, sector, industry, active/archived) the
same as before, and now also accepts a raw-file upload per company (Import
Data panel) that runs it through the same ingest_file() pipeline the CLI
uses — the only other thing besides Admin metadata edits allowed to write
ingested financial data from the web UI.
"""

from __future__ import annotations

# Must run before any other import in this file (or any module this file
# transitively imports) touches storage.repositories/company_repository/
# fact_store/indicator_repository/investigation_repository -- see
# storage/backend_bootstrap.py's own docstring for why. web/app.py is
# gunicorn's entry point (web.app:create_app()), so this is the earliest
# possible point in the whole process for it to run.
import storage.backend_bootstrap

storage.backend_bootstrap.install()

# Sentry -- initialized here, at import time, before Flask itself is
# imported below, so its Flask integration auto-instruments every route
# registered by create_app() (uncaught exceptions, request context) with
# no per-route wiring. Gated on config.settings.SENTRY_DSN being set --
# unset (every local dev/test run that doesn't export it) means this is a
# no-op, same contract every other optional integration in this app
# follows.
from config.settings import SENTRY_DSN

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # Request headers/IP on error events -- acceptable here since this
        # is a small, internally-used app, not a consumer product with a
        # broad user base to consider privacy policy implications for.
        send_default_pii=True,
        enable_logs=True,
    )

import hashlib
import json
import logging
import os
import re
from storage.db_types import DBConnection, Row
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import anthropic
from flask import Blueprint, Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from jinja2 import ChoiceLoader, FileSystemLoader

from reports.schema import from_investigation_data
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from charts.financial_charts import build_comparison_charts, build_company_charts, figure_to_base64_png
from companies.lifecycle import (
    ARCHIVE_REASONS,
    CompanyNotActiveError,
    CompanyNotFoundError,
    InvalidArchiveReasonError,
    archive_company,
    restore_company,
)
from companies.registry import get_company, list_companies, register_company, search_companies
from storage.raw_object_repository import list_raw_objects
from ingestion.onboarding import onboard_new_company
from sources.yfinance_company_lookup import CompanyLookupError, search_companies as search_yfinance_companies
from companies.stock_actions import (
    ACTION_TYPES,
    InvalidStockActionError,
    StockActionNotFoundError,
    add_stock_action,
    delete_stock_action,
    list_stock_actions,
)
from config import settings as app_settings
from config.settings import ANTHROPIC_API_KEY_SET, DATABASE_BACKEND, SECRET_KEY, from_repo_relative, to_repo_relative
from ingestion.coordinator import (
    archive_documents,
    archive_financial_items,
    discover_pending_documents,
    discover_pending_financial_items,
    process_all_pending_documents,
    process_all_pending_financial_items,
    process_documents,
    process_financial_items,
    retry_failed_documents,
    retry_failed_financial_items,
    unarchive_documents,
    unarchive_financial_items,
)
from analytics.patterns import detect_yoy_spikes
from indicators.evaluation import evaluate_company_indicators, group_by_classification
from indicators.framework import (
    CLASSIFICATIONS,
    CLASSIFICATION_LABELS,
    InvalidIndicatorConfigError,
    get_rule,
    list_families,
)
from indicators.settings import (
    build_rules_settings,
    parse_override_form,
    reset_rule_override,
    save_rule_override,
)
from ingestion.detector import ADAPTER_CLASSES
from ingestion.pipeline import ingest_file
from research.abstracts import generate_abstract
from research.assistant import answer_question, sentry_span
from research.company_resolver import resolve_companies
from llm.hardness import Tier, classify as classify_hardness
from research.insights import NoDataToSummarizeError, generate_key_insights
from research.aggregate_query import compute_group_aggregate, extract_aggregate_intent, format_aggregate_answer
from research.investigation import InvestigationError, run_investigation
from retrieval.tag_resolver import resolve_tags_in_text
from research.signals_report import extract_report_meta, generate_signals_report
from research.system_insights import SystemInsightGenerationError, generate_system_insights
from scheduling.jobs import CATEGORY_ORDER, ScheduledJob, SCHEDULED_JOBS, get_job, open_db as scheduling_open_db
from scripts.batch_fetch_nse import run_nse_batch
from scripts.batch_fetch_sec_edgar import run_sec_edgar_batch
from storage.company_repository import select_company_ids_by_index
from storage.database import init_db, init_postgres_db
from storage.document_store import DocumentStoreError, default_document_store
from storage.investigation_repository import (
    count_investigation_hypotheses,
    select_investigations_for_company,
)
from storage.price_repository import (
    get_price_history,
    list_52_week_range,
    list_all_time_range,
    list_latest_close,
    list_latest_daily_change,
)
from storage.repositories import (
    COMPANY_LIST_COLUMNS,
    OVERVIEW_RATIO_CATALOG,
    DEFAULT_THEME,
    NEWS_RETENTION_DAYS,
    VALID_THEMES,
    add_index_definition,
    add_industry,
    add_sector,
    add_watchlist_item,
    company_has_canonical_financials,
    count_companies_by_index_tag,
    count_companies_by_industry,
    count_companies_by_sector,
    create_user,
    delete_company_note,
    delete_generated_report,
    hide_generated_report,
    hide_investigation,
    soft_delete_generated_report,
    soft_delete_investigation,
    unhide_generated_report,
    unhide_investigation,
    delete_index_definition,
    delete_industry,
    delete_note_attachment,
    delete_sector,
    finish_batch_job_run,
    get_company_document,
    get_note_attachment,
    get_all_company_index_tags,
    get_company_index_tags,
    get_company_insights,
    get_company_list_column_settings,
    get_overview_ratio_settings,
    get_all_company_index_tags,
    get_generated_report,
    get_investigation,
    get_investigation_cost_summary,
    get_strongest_verdict_by_investigation,
    get_batch_job_run_live_progress,
    get_latest_batch_job_run,
    get_latest_batch_item_for_company,
    get_llm_usage_summary,
    get_macro_series,
    get_user_by_email,
    get_user_by_id,
    get_user_by_login,
    is_watchlisted,
    list_batch_job_items,
    list_batch_job_runs,
    list_company_insights,
    list_company_news,
    list_company_notes,
    list_documents_by_status,
    list_generated_reports,
    list_index_definitions,
    list_industries,
    list_ingestion_queue_items,
    list_investigation_hypotheses,
    list_investigation_hypothesis_evidence,
    list_investigations,
    list_latest_shares_outstanding,
    list_llm_call_log,
    list_macro_series_summary,
    list_distinct_batch_job_names,
    list_reconciliation_log_by_company,
    list_running_batch_job_runs,
    list_sec_edgar_migration_status,
    list_xbrl_migration_status,
    list_note_attachments_for_company,
    list_report_evidence,
    list_report_followups,
    list_sectors,
    list_system_insights,
    list_watchlist_items,
    reconcile_company,
    remove_watchlist_item,
    rename_index_definition,
    rename_industry,
    rename_sector,
    save_company_document,
    save_company_insights,
    save_company_news,
    save_company_note,
    save_note_attachment,
    update_company_note,
    save_generated_report,
    save_report_evidence,
    save_report_followups,
    update_generated_report_s3_metadata,
    set_company_index_tags,
    set_company_list_column_settings,
    set_overview_ratio_settings,
    update_system_insight_status,
    update_user_theme,
    get_research_case,
    list_research_cases_for_feed,
    list_research_cases_for_audit,
    list_stale_in_progress_cases,
    fail_research_case,
    request_case_cancellation,
    update_case_activity,
)
from research.case_runner import run_case_in_background, start_case
from web.docs_feed import KEY_TO_DOCUMENT_TYPE, build_docs_feed
from web.corporate_actions_feed import build_corporate_actions_feed
from web.shareholding_feed import build_shareholding_feed
from web.fixtures import EXAMPLES, THREADS
from web.fx_rate import get_usd_inr_rate
from web.live_quote import get_live_quote, peek_cached_quote
from web.news import fetch_company_news, google_news_last_24h_url
from web.rich_text import sanitize_note_html
from web.charts_feed import build_charts_feed
from web.valuation_feed import build_valuation_feed

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _redirect_to_return_or(default_endpoint: str, **default_kwargs):
    """A POST handler reachable from more than one page (admin_update_company
    from both Settings > Admin > Companies and the public /companies list;
    case_hide/case_delete from the Cases list) redirects back to wherever
    the form was actually submitted from, via an explicit return_to hidden
    field, instead of always landing on one hardcoded page. Restricted to a
    same-origin relative path (must start with "/", never "//" -- a
    protocol-relative URL still points off-site) so this can't be turned
    into an open redirect via a crafted form post; falls back to
    `default_endpoint` when return_to is absent or fails that check."""
    return_to = request.form.get("return_to", "")
    if return_to.startswith("/") and not return_to.startswith("//"):
        return redirect(return_to)
    return redirect(url_for(default_endpoint, **default_kwargs))

# For now, every tab is browsable without signing in — login only gates
# Admin (needs is_admin on a real user). Settings used to be gated too (a
# per-user preference, meaningless anonymously) but now degrades instead:
# signed-in preferences persist to users.theme, signed-out ones live in the
# session (see settings() and g.theme below) — same page either way.
# Revisit if/when the whole app should go back to being login-only.
_LOGIN_REQUIRED_ENDPOINTS: set[str] = set()
_LOGIN_REQUIRED_PREFIXES = ("admin",)
# admin_schedule_run_async is the one "admin"-prefixed route deliberately
# exempt from the session-login gate below -- a cron trigger (EventBridge
# Scheduler etc.) has no browser session to log in with. It authenticates
# via its own X-Cron-Secret header check instead (see its docstring) —
# excluding it here doesn't skip auth, it just moves auth into the route.
_LOGIN_EXEMPT_ENDPOINTS = {"admin_schedule_run_async"}

_TAG_RE = re.compile(r"\[(FACT|CALCULATION|INFERENCE)\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _is_blank_note_html(html: str) -> bool:
    """A contenteditable div with no real content still serializes to
    something like '<div><br></div>', not an empty string — strip tags
    before deciding whether there's anything worth saving."""
    return not _HTML_TAG_RE.sub("", html).strip()


def _chart_points(series_a: list[float], series_b: list[float], width: int = 460, height: int = 150) -> dict[str, str]:
    """Map two same-length series to SVG polyline `points` strings on a shared scale.

    Both series share one min/max so they're comparable on one axis — this is
    an indexed-comparison chart, not two independently-scaled ones. Port of
    the wireframe's own chartPoints(), kept numerically identical so the mock
    threads render the same shape they do in the design tool.
    """
    pad = 14
    plot_w, plot_h = width - pad * 2, height - pad * 2
    all_values = series_a + series_b
    lo, hi = min(all_values), max(all_values)
    span = (hi - lo) or 1

    def to_points(series: list[float]) -> str:
        n = len(series) - 1 or 1
        pts = []
        for i, v in enumerate(series):
            x = pad + (i / n) * plot_w
            y = pad + plot_h - ((v - lo) / span) * plot_h
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    return {"a": to_points(series_a), "b": to_points(series_b)}


def _highlight_tags(text: str) -> Markup:
    """Escape the whole text, then re-wrap only the known [FACT]/[CALCULATION]/
    [INFERENCE] tokens in a span — never trusts report or LLM content as HTML
    beyond those three known tokens. Shared by the report page and the chat
    assistant's answers, since both come from the same evidence-labeling scheme."""
    escaped = str(escape(text))
    highlighted = _TAG_RE.sub(
        lambda m: f'<span class="tag tag-{m.group(1).lower()}">[{m.group(1)}]</span>', escaped
    )
    return Markup(highlighted)


def _render_markdown_with_tags(text: str) -> Markup:
    """Escape the whole text, then re-build only a small whitelist of Markdown
    constructs (#/##/### headers, **bold**, "- " list items, paragraphs) plus the
    same [FACT]/[CALCULATION]/[INFERENCE] tag spans as _highlight_tags — never
    trusts LLM-generated text as HTML beyond those known tokens.

    Used for both the full Signals report and the live Ask AI / research
    answer: research/assistant.py's SYSTEM_PROMPT doesn't constrain the model
    to plain text, so it routinely comes back with headers/bold/lists (see
    the "Short answer first" / "What the data covers" style structure a
    multi-part question tends to produce) — _highlight_tags alone would
    render that as one unbroken, literally-escaped "## "/"**" wall of text
    instead of actual structure."""
    escaped = str(escape(text))
    escaped = _TAG_RE.sub(
        lambda m: f'<span class="tag tag-{m.group(1).lower()}">[{m.group(1)}]</span>', escaped
    )

    def inline(line: str) -> str:
        return _BOLD_RE.sub(r"<strong>\1</strong>", line)

    html_parts: list[str] = []
    in_list = False
    for raw_line in escaped.split("\n"):
        line = raw_line.strip()
        if line.startswith("### "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h4>{inline(line[4:])}</h4>")
        elif line.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h3>{inline(line[3:])}</h3>")
        elif line.startswith("# "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h2>{inline(line[2:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{inline(line[2:])}</li>")
        elif line == "":
            if in_list:
                html_parts.append("</ul>")
                in_list = False
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<p>{inline(line)}</p>")
    if in_list:
        html_parts.append("</ul>")

    return Markup("\n".join(html_parts))


def _embed_question_for_reuse(question: str) -> tuple[list[float] | None, str | None]:
    """Best-effort (embedding, model_id) for a freshly-generated report's
    question, so context/reuse.py's semantic reuse-matching layer has
    something to compare future questions against — (None, None) when the
    embedding provider is unavailable, same graceful-degradation shape
    retrieval/hybrid_search.py uses; save_generated_report() already accepts
    NULLs here, and find_reusable_report() already falls back to
    word-overlap-only for a report with none. Never blocks saving the report
    itself on this."""
    from retrieval.embedding_provider import EmbeddingProviderUnavailable, default_embedding_provider

    try:
        provider = default_embedding_provider()
        return provider.embed_text(question), provider.model_id
    except EmbeddingProviderUnavailable:
        return None, None


def _persist_generated_report_s3(
    db, thread_id: str, question: str, company_ids: list[str], statement_type: str,
    report_markdown: str, evidence: list[dict] | None = None, followups: list[str] | None = None,
    *, owner_id: str | None = None,
) -> None:
    """ADR-021: alongside save_generated_report()/save_report_evidence()/
    save_report_followups() (unchanged, still called exactly as before by
    every caller below — report_markdown stays the live NOT-NULL column,
    see storage/database.py::_migrate_generated_reports_s3_columns for why
    it's kept populated rather than moved out), also write the full
    report (+ evidence + followups) as one JSON artifact to S3 and record
    its key + an LLM-generated abstract + the current user (if any) on the
    generated_reports row. Called from all four /research/... routes that
    save a report, so every one gets identical treatment — no route-
    specific variation to keep in sync.

    `owner_id` is resolved from g.user by the caller rather than read here
    -- this can run inside a background thread (the -async ask routes'
    _compute_answer_question path), and g is bound to the request context,
    which a background thread doesn't have."""
    artifact = {
        "thread_id": thread_id, "question": question, "company_ids": company_ids,
        "statement_type": statement_type, "report_markdown": report_markdown,
        "evidence": evidence or [], "followups": followups or [],
    }
    s3_key = f"threads/{thread_id}/v1.json"
    default_document_store().store(s3_key, json.dumps(artifact, indent=2).encode("utf-8"))
    abstract = generate_abstract(db, report_markdown)
    update_generated_report_s3_metadata(db, thread_id, s3_key=s3_key, abstract=abstract, version=1, owner_id=owner_id)


def _split_index_tags(tags: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split a company's INDEX_NAMES tags (storage/repositories.py) into the
    NSE ones (Nifty-prefixed), the BSE ones (BSE-prefixed, plus Sensex), and
    everything else (US indices — S&P 500, Nasdaq 100, Dow Jones — the only
    other kind INDEX_NAMES has today) for the Overview tab's About section."""
    nse_tags = [t for t in tags if t.lower().startswith("nifty")]
    bse_tags = [t for t in tags if t.lower().startswith("bse") or t == "Sensex"]
    other_tags = [t for t in tags if t not in nse_tags and t not in bse_tags]
    return nse_tags, bse_tags, other_tags


_DOCS_QUARTER_PERIOD_RE = re.compile(r"^q([1-4])fy(\d{4})$")


def _parse_docs_period_id(period_id: str, type_key: str) -> tuple[str, str | None]:
    """"q1fy2026"/"year:FY2026" (docs_timeline.js's period ids) -> (fiscal_year,
    quarter). An annual-report add needs a "year:" id; every other type needs
    a real quarter id — the client is expected to have already resolved a
    "full year" period selection down to that year's Q4 for non-annual types
    (see docs_timeline.js's submitAdd), so a mismatch here is a bad request,
    not something to silently default."""
    if type_key == "annual":
        if not period_id.startswith("year:"):
            raise ValueError("An annual report needs a fiscal year, not a quarter.")
        return period_id[5:], None
    match = _DOCS_QUARTER_PERIOD_RE.match(period_id)
    if not match:
        raise ValueError(f"Not a recognizable quarter: {period_id!r}")
    return f"FY{match.group(2)}", f"Q{match.group(1)}"


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    # Signal Report Design System (reports/) -- registers its component/
    # template directories onto Jinja's search path and its theme CSS as a
    # small static blueprint, without moving those files under web/. See
    # reports/__init__.py for the Investigation Engine -> InvestigationReport
    # -> Signal Report Design System -> Web/Print data flow this supports.
    _repo_root = Path(__file__).resolve().parent.parent
    _reports_templates_dir = _repo_root / "reports" / "templates"
    _reports_components_dir = _repo_root / "reports" / "components"
    _reports_theme_dir = _repo_root / "reports" / "theme"
    app.jinja_loader = ChoiceLoader(
        [
            app.jinja_loader,
            FileSystemLoader(str(_reports_templates_dir)),
            FileSystemLoader(str(_reports_components_dir)),
        ]
    )
    reports_theme_bp = Blueprint(
        "reports_theme", __name__, static_folder=str(_reports_theme_dir), static_url_path="/reports-theme"
    )
    app.register_blueprint(reports_theme_bp)

    @app.template_global("reports_theme_url")
    def reports_theme_url(filename: str) -> str:
        """Same cache-busting idea as static_url() above, scoped to the
        Signal report theme CSS served from reports/theme/ via the
        reports_theme blueprint registered just above."""
        url = url_for("reports_theme.static", filename=filename)
        try:
            mtime = int((_reports_theme_dir / filename).stat().st_mtime)
        except OSError:
            return url
        return f"{url}?v={mtime}"

    @app.route("/health")
    def health():
        # Deliberately no DB/Qdrant/Neo4j dependency -- a container
        # orchestrator's liveness check should reflect "is this process up
        # and serving," not cascade-fail every instance because one
        # external managed service had a blip. Deeper dependency checks
        # belong in a separate readiness probe if one is ever needed.
        return {"status": "ok"}, 200

    @app.template_global("static_url")
    def static_url(filename: str) -> str:
        """url_for('static', filename=...) plus a ?v=<mtime> cache-buster.

        Without this, a browser that already loaded a page this session
        keeps its cached copy of styles.css/*.js indefinitely — editing the
        file on disk (common during active dev) doesn't force a refetch on
        its own, so a real code change can look like it "didn't take" even
        though the server is serving the new bytes correctly (a plain
        reload can still reuse the cached response; only a change to the
        URL itself, or a hard refresh, guarantees a refetch). Falls back to
        a plain url_for if the file can't be stat'd (e.g. a bad filename)
        rather than raising — a missing cache-buster is harmless, a 500
        here wouldn't be.
        """
        url = url_for("static", filename=filename)
        try:
            mtime = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            return url
        return f"{url}?v={mtime}"

    @app.context_processor
    def _inject_assistant_availability():
        """The Ask AI drawer ships on every page via base.html, so whether the
        assistant can run at all has to be known without each route
        remembering to pass it. Routes that render their own key banner
        (chat/research) still pass `api_key_set` explicitly — same value,
        different name, so this can't shadow theirs."""
        return {"assistant_enabled": ANTHROPIC_API_KEY_SET}

    @app.teardown_appcontext
    def _close_db(_exception: BaseException | None) -> None:
        conn: DBConnection | None = g.pop("db_conn", None)
        if conn is not None:
            conn.close()
        price_conn: DBConnection | None = g.pop("price_db_conn", None)
        if price_conn is not None:
            price_conn.close()
        logs_conn: DBConnection | None = g.pop("logs_db_conn", None)
        if logs_conn is not None:
            logs_conn.close()

    def get_db() -> DBConnection:
        """DATABASE_BACKEND=postgres routes this at Neon instead of SQLite
        -- every route already calls get_db() uniformly, so this one
        function is the entire "main data" half of the backend switch (the
        storage.backend_bootstrap swap above is the other half, making
        `from storage.repositories import X` resolve correctly against
        whichever backend this connection actually is)."""
        if "db_conn" not in g:
            g.db_conn = init_postgres_db() if DATABASE_BACKEND == "postgres" else init_db()
        return g.db_conn

    def get_logs_db() -> DBConnection:
        """Same connection as get_db() now -- batch_job_runs/items,
        dataset_events, worker_processing_log, retrieval_diagnostics,
        llm_call_log, and ingestion_queue_items were added to schemas/
        postgres_schema.sql on 2026-09-13 (previously excluded citing a
        Neon free-tier storage cap production had already grown past --
        see storage/backend_bootstrap.py's docstring and docs/ADR/021),
        so they live in the same database as everything else under either
        backend now. Kept as a distinct name (not simply replaced by
        get_db() at every call site) purely so those call sites keep
        reading as "this touches an audit/log table" -- not because the
        connection is actually different anymore."""
        return get_db()

    def get_price_db() -> DBConnection:
        if "price_db_conn" not in g:
            g.price_db_conn = storage.backend_bootstrap.open_price_db()
        return g.price_db_conn

    @app.before_request
    def _require_login():
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            g.user = get_user_by_id(get_db(), user_id)
            if g.user is None:
                # Session outlived the account it points at (e.g. a reseeded
                # dev database) — drop the stale cookie rather than 500ing.
                session.clear()
        # Theme preference: a signed-in user's own users.theme row; a
        # signed-out visitor's choice, stashed in their session by
        # settings() instead (see that route) — same DEFAULT_THEME fallback
        # either way until something's actually been chosen.
        g.theme = g.user["theme"] if g.user is not None else session.get("theme", DEFAULT_THEME)
        if request.endpoint is None:
            return None
        if request.endpoint in _LOGIN_EXEMPT_ENDPOINTS:
            return None
        needs_login = (
            request.endpoint in _LOGIN_REQUIRED_ENDPOINTS
            or request.endpoint.startswith(_LOGIN_REQUIRED_PREFIXES)
        )
        if needs_login and g.user is None:
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if request.endpoint.startswith("admin") and g.user is not None and not g.user["is_admin"]:
            abort(403, "Admin access required")
        return None

    def _valuation_model_data_path(valuation_model_file: str) -> Path:
        """A per-company valuation-model dataset lives under web/static/data/
        if one has been ported from a Claude Design project for that company
        — see README/HDFC Bank Equity Dashboard import. Which companies have
        one is a fact on the company row (`valuation_model_file`), not
        inferred from company_id — a test double or a differently-sourced
        company can share an id like "HDFCBANK" without acquiring this."""
        return Path(app.static_folder) / "data" / valuation_model_file

    def _latest_price(valuation_model_file: str) -> float | None:
        """Last recorded price from the ported valuation-model dataset, if
        one exists for this company — the only price data this app has
        anywhere (README: no live market-data pipeline). Not a live quote."""
        path = _valuation_model_data_path(valuation_model_file)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        price_metric = next(
            (m for m in data.get("METRICS", {}).get("valuation", []) if m.get("key") == "price"), None
        )
        if price_metric is None:
            return None
        for value in reversed(price_metric["values"]):
            if value is not None:
                return value
        return None

    def _is_shares_outstanding_current(fiscal_year: str | None) -> bool:
        """Whether a shares_outstanding row's fiscal year (e.g. "FY2014") is
        recent enough to drive a Market Cap figure someone might act on,
        rather than a confidently-wrong number computed from a share count
        that's years or a decade stale (as of 2026-08, most of the ~2,500
        companies here have no shares_outstanding data past FY2013/FY2014 —
        list_latest_shares_outstanding() still returns "the latest row on
        file" for them, which is not the same as "current"). Allows the
        latest fully-elapsed Indian fiscal year (ending March 31) or the one
        before it, to cover an annual report not yet filed for the FY that
        just ended."""
        if not fiscal_year or not fiscal_year.startswith("FY"):
            return False
        try:
            fy = int(fiscal_year[2:])
        except ValueError:
            return False
        today = datetime.now(timezone.utc)
        latest_reported_fy = today.year if today.month >= 4 else today.year - 1
        return fy >= latest_reported_fy - 1

    @app.route("/companies/search.json")
    def companies_search():
        # Static path, so Flask/Werkzeug matches it ahead of the
        # /companies/<company_id> dynamic route below regardless of
        # registration order — header_search.js's typeahead.
        query = request.args.get("q", "")
        index_name = request.args.get("index") or None
        db = get_db()
        results = [
            {
                "company_id": row["company_id"],
                "display_name": row["display_name"],
                "nse_symbol": row["nse_symbol"],
                "sector": row["sector"],
            }
            for row in search_companies(db, query, index_name=index_name)
        ]
        return jsonify(results=results)

    @app.route("/companies")
    def companies():
        db = get_db()
        shares_outstanding_by_company = list_latest_shares_outstanding(db)
        # price_history.db (scripts/backfill_price_history.py) now covers
        # essentially every Nifty 500 company with real daily closes, far
        # more complete than the old ported-dashboard/live-quote-cache
        # fallback chain below — bulk-fetched once here (3 queries total,
        # not one per row) rather than a get_latest_close() call per company.
        price_db = get_price_db()
        latest_close_by_company = list_latest_close(price_db)
        daily_change_by_company = list_latest_daily_change(price_db)
        week52_by_company = list_52_week_range(price_db)
        all_time_by_company = list_all_time_range(price_db)
        # Same one-query-total reasoning as the price/shares lookups above —
        # a get_company_index_tags() call per row was fine on local SQLite
        # (no network round trip) but turned a ~2,600-row page load into
        # minutes once DATABASE_BACKEND=postgres put each of those queries
        # over the network to Neon.
        index_tags_by_company = get_all_company_index_tags(db)

        def _range_pct(price, low, high):
            """Where `price` sits between `low` and `high`, as 0-100 — for
            positioning the 52-week/all-time range markers. Clamped: a stale
            valuation-model price can fall slightly outside the price-history
            range it's plotted against."""
            if price is None or low is None or high is None or high <= low:
                return None
            return max(0.0, min(100.0, (price - low) / (high - low) * 100))

        def _format_market_cap(value, currency):
            """`value` (== row["market_cap_cr"] below) is price times
            list_latest_shares_outstanding()'s canonical_value -- and that
            value is NOT always "in Cr" despite its docstring/the field's own
            name: it's crore for an Indian company (NSE/screener-sourced
            shares_outstanding), but millions for a US one (sources/
            yfinance_financials.py divides by _UNIT_DIVISOR = 1_000_000, not
            10_000_000, when normalizing yfinance's raw share count) -- both
            conventions exist because each matches how that source already
            expresses every other aggregate line item (reserves, revenue,
            ...) for the same company, not a bug in the ingestion itself.
            Multiplying price x shares therefore lands in the right currency
            either way, just at a different real-world scale -- so a USD
            company unconditionally labeled "Cr" here was wrong (verified:
            Apple showed "$4,727,000 Cr", a rupee-crore unit slapped on a
            number that's actually already in USD millions). INR keeps the
            flat Cr convention this app uses everywhere else (no further
            Lakh/Cr-of-Cr scaling); USD gets a normal T/B/M scale instead,
            since "$4,727,000M" reads far worse than "$4.73T" for a mega-cap."""
            if value is None:
                return None
            if currency != "USD":
                return f"₹{value:,.0f} Cr"
            if value >= 1_000_000:  # >= $1T, value is in millions
                return f"${value / 1_000_000:,.2f}T"
            if value >= 1_000:  # >= $1B
                return f"${value / 1_000:,.1f}B"
            return f"${value:,.0f}M"

        rows = []
        # include_archived=True -- the Status filter below (client-side,
        # defaulting to "Active" on load) needs archived companies actually
        # present in the DOM to filter INTO view when switched to "Archived"
        # or "All"; excluding them server-side (this route's old default)
        # would leave nothing for that filter option to ever show.
        for c in list_companies(db, include_archived=True):
            row = dict(c)
            row["latest_price"] = _latest_price(row["valuation_model_file"]) if row["valuation_model_file"] else None
            row["price_change_pct"] = None
            if row["latest_price"] is None:
                row["latest_price"] = latest_close_by_company.get(row["company_id"])
                if row["latest_price"] is not None:
                    # Only meaningful when the shown price actually is this
                    # table's latest close — a ported valuation-model price
                    # above (often months stale) has no daily-change figure
                    # to pair it with.
                    row["price_change_pct"] = daily_change_by_company.get(row["company_id"])
            if row["latest_price"] is None:
                # No ported dashboard and no backfilled price history (e.g.
                # not a Nifty 500 company) — fall back to whatever price is
                # already cached from someone having visited this company's
                # own page (get_live_quote there). Never fetches here: with
                # ~2,500 rows a live call per row isn't viable on a list page.
                ticker = row["nse_symbol"] or (row["company_id"] if row["country"] != "IN" else None)
                cached_quote = peek_cached_quote(ticker, row["country"])
                if cached_quote is not None:
                    row["latest_price"] = cached_quote["price"]
                    row["price_change_pct"] = cached_quote["change_pct"]
            # Market cap (Cr) = price/share x shares outstanding (Cr) — shares
            # outstanding only exists for companies with real financial data
            # ingested (~60 of ~2,500 today), so this stays None for most
            # rows. Also None when the shares figure on file is too stale to
            # trust (_is_shares_outstanding_current) — a decade-old share
            # count times today's price is a confidently-wrong number, worse
            # than showing nothing.
            shares_outstanding_entry = shares_outstanding_by_company.get(row["company_id"])
            if (
                row["latest_price"] is not None
                and shares_outstanding_entry is not None
                and _is_shares_outstanding_current(shares_outstanding_entry[1])
            ):
                row["market_cap_cr"] = row["latest_price"] * shares_outstanding_entry[0]
            else:
                row["market_cap_cr"] = None
            row["market_cap_display"] = _format_market_cap(row["market_cap_cr"], row["currency"])
            week52 = week52_by_company.get(row["company_id"])
            row["week52_low"], row["week52_high"] = week52 if week52 else (None, None)
            row["week52_pct"] = _range_pct(row["latest_price"], row["week52_low"], row["week52_high"])
            all_time = all_time_by_company.get(row["company_id"])
            row["all_time_low"], row["all_time_high"] = all_time if all_time else (None, None)
            row["all_time_pct"] = _range_pct(row["latest_price"], row["all_time_low"], row["all_time_high"])
            row["index_tags"] = index_tags_by_company.get(row["company_id"], [])
            rows.append(row)
        sectors = sorted({row["sector"] for row in rows if row["sector"]})
        industries = sorted({row["industry"] for row in rows if row["industry"]})
        index_tag_options = sorted({tag for row in rows for tag in row["index_tags"]})
        countries = sorted({row["country"] for row in rows})
        column_settings = get_company_list_column_settings(db)
        columns = [c for c in COMPANY_LIST_COLUMNS if column_settings[c["key"]]]
        return render_template(
            "index.html",
            companies=rows,
            countries=countries,
            sectors=sectors,
            industries=industries,
            index_tag_options=index_tag_options,
            columns=columns,
        )

    _NEW_OPTION_VALUE = "__new__"

    def _resolve_dropdown_or_custom(field_name: str) -> str | None:
        """Sector/Industry are dropdowns of existing values (README: Admin
        tab, avoids typos) with a "+ Add new…" escape hatch — the custom
        text field only matters when that option was picked."""
        value = request.form.get(field_name, "")
        if value == _NEW_OPTION_VALUE:
            value = request.form.get(f"{field_name}_other", "")
        value = value.strip()
        return value or None

    ADMIN_COMPANIES_PAGE_SIZE = 50
    ADMIN_INGEST_PAGE_SIZE = 50

    def _paginate(rows: list, *, query: str, haystack_fn, page_arg: str, page_size: int) -> dict:
        """Shared search+pagination for one Ingest sub-table — same
        filter-then-slice approach the Companies panel above already uses
        (rows here are at most a few hundred, so Python-side filtering is
        fine; no need for SQL-side search)."""
        filtered = rows
        if query:
            tokens = query.lower().split()
            filtered = [r for r in filtered if all(t in haystack_fn(r) for t in tokens)]
        total = len(filtered)
        total_pages = max(1, -(-total // page_size))
        page = max(1, min(request.args.get(page_arg, 1, type=int) or 1, total_pages))
        start = (page - 1) * page_size
        return {
            "rows": filtered[start:start + page_size],
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "page_size": page_size,
        }

    def _filter_and_paginate(rows: list, *, filters: dict[str, str], page_arg: str, page_size: int) -> dict:
        """Per-column equality filtering (each filters[col] value, if set,
        must exactly match that row's column) + pagination — the
        per-column-dropdown counterpart to _paginate()'s free-text search,
        for the Ingest sub-tables that filter by a specific field (company,
        kind, type) instead of searching across all of them at once."""
        filtered = rows
        for column, value in filters.items():
            if value:
                filtered = [r for r in filtered if (r[column] or "") == value]
        total = len(filtered)
        total_pages = max(1, -(-total // page_size))
        page = max(1, min(request.args.get(page_arg, 1, type=int) or 1, total_pages))
        start = (page - 1) * page_size
        return {
            "rows": filtered[start:start + page_size],
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "page_size": page_size,
        }

    def _distinct_values(rows: list, column: str) -> list[str]:
        return sorted({r[column] for r in rows if r[column]})

    # Job registry moved to scheduling/jobs.py (ScheduledJob, SCHEDULED_JOBS,
    # get_job) -- see that module's docstring for why.

    def _resume_interrupted_batch_jobs() -> None:
        """Called once, at process startup (see the WERKZEUG_RUN_MAIN-guarded
        call near the bottom of create_app()) -- admin_schedule_run's "Run
        now" is synchronous and blocking with no background-job
        infrastructure, so a batch_job_runs row can only be at
        status='running' while its own request is still in flight in a
        live process. On a *fresh* process start there is no such request,
        so every row list_running_batch_job_runs() finds here is left over
        from a previous process that died (crashed, or was restarted)
        mid-run -- never a run to leave alone.

        Marked 'failed' (the schema's status CHECK has no 'interrupted'
        value, and this run genuinely didn't complete) with a note
        explaining why, then replayed via the exact same runner used for a
        manual "Run now" click -- safe to just replay the whole scope
        rather than reconstructing "which companies are left" from
        batch_job_items, since every runner's own per-company work is
        already idempotent (NSE's dest_path-exists check, the
        shareholding detail_fetched_at flag, SEC EDGAR's reconciliation
        step): a replay cheaply re-confirms whatever the dead run already
        finished and only does real work for what's left. Run in a
        background thread, each on its own db connection (this isn't a
        request, so there's no request-scoped `g` connection to reuse, and
        a sqlite3 connection can't cross threads) -- startup itself must
        not block on what could be a several-minute crawl.
        """
        conn = storage.backend_bootstrap.open_db()
        try:
            stale_runs = list_running_batch_job_runs(conn)
            for run in stale_runs:
                finish_batch_job_run(
                    conn, run["run_id"], status="failed",
                    notes="Interrupted by a server restart — auto re-queued.",
                )
        finally:
            conn.close()

        if not stale_runs:
            return

        jobs_by_name = {job.job_name: job for job in SCHEDULED_JOBS if job.job_name}
        to_resume = []
        for run in stale_runs:
            job = jobs_by_name.get(run["job_name"])
            if job is None or job.runner is None:
                logger.warning(
                    "Interrupted batch run_id=%s (job_name=%r) has no registered runner to resume",
                    run["run_id"], run["job_name"],
                )
                continue
            to_resume.append((job, run))

        if to_resume:
            # One worker thread processing the list in sequence, not one
            # thread per job -- these are exactly the several-minute,
            # several-hundred-company NSE/SEC crawls admin_schedule_run's
            # own docstring describes; several of them hammering NSE's WAF
            # at once (a real, previously-observed failure mode in this app
            # -- see sources/nse_fetch.py's _bootstrap()) would be worse
            # than the interruption this is meant to recover from, not
            # better.
            threading.Thread(target=_run_resumed_jobs_sequentially, args=(to_resume,), daemon=True).start()

    def _run_resumed_jobs_sequentially(to_resume: list) -> None:
        for job, run in to_resume:
            logger.info(
                "Auto-resuming interrupted batch job %r (was run_id=%s, scope=%r)",
                job.job_id, run["run_id"], run["scope_label"],
            )
            conn = storage.backend_bootstrap.open_db()
            try:
                job.runner(conn)
            except Exception:  # noqa: BLE001 -- one job's resume failing shouldn't block the rest of the queue
                logger.exception("Auto-resume of interrupted job %r failed", job.job_id)
            finally:
                conn.close()

    def _resume_interrupted_cases() -> None:
        """The research_cases equivalent of _resume_interrupted_batch_jobs()
        above, called from the same startup guard -- a case still at
        status='in_progress' when a fresh process starts was left running
        by a process that died (crashed, or was restarted) mid-run, same
        reasoning as that function's own docstring.

        Unlike a batch job's per-company work (NSE fetch, SEC EDGAR pull,
        ...), there's no safe way to replay "the rest of" one case: it may
        have died mid-LLM-call, and research/assistant.py's evidence
        gathering + LLM call together aren't naturally resumable from an
        arbitrary point the way a per-company loop is. So these are marked
        failed with a clear, honest note instead of silently replayed --
        this is a genuine "the server restarted while this was running"
        technical failure (not insufficient_data, not cancelled), and the
        user can just ask again. This is a DIFFERENT guarantee than "Continue
        in Background" / browser-disconnect resilience: those never touch
        the server process at all, so the background thread this function
        is cleaning up after was never interrupted by them in the first
        place -- only a real process restart reaches this code path."""
        conn = storage.backend_bootstrap.open_db()
        try:
            stale_cases = list_stale_in_progress_cases(conn)
            for case in stale_cases:
                fail_research_case(conn, case["case_id"], "Interrupted by a server restart — please ask again.")
            if stale_cases:
                logger.info("Marked %d interrupted research case(s) as failed on startup", len(stale_cases))
        finally:
            conn.close()

    def _schedule_panel_context(db) -> dict:
        """Only computed when the Schedule panel is actually being viewed,
        same reasoning _ingest_panel_context()/_audit_panel_context() give
        for their own panels -- get_latest_batch_job_run() is one query per
        registered job, wasted work on every /settings load otherwise.

        Each registry entry is turned into a plain dict (not the
        ScheduledJob dataclass itself) merged with its `last_run` and a
        `runner_available` flag, since the template needs to branch on
        "does this job have a working Run now button" without importing
        Callable-ness checks into Jinja. A still-`running` last_run also
        gets `live_progress` attached (get_batch_job_run_live_progress()) --
        otherwise a run in progress shows nothing but a start timestamp
        until it finishes, since batch_job_runs' own items_total/succeeded/
        failed columns are only written once, at the very end.

        Grouped into `schedule_categories` (one entry per CATEGORY_ORDER
        value, in that order, each holding its own `jobs` list) rather than
        one flat list -- the template renders one collapsible <details> per
        category instead of one long table, same real-estate-efficient
        disclosure pattern the Charts tab already established. `has_running`
        on a category lets the template auto-open only the section a run is
        actually in progress in, closed by default otherwise -- with 6
        price-history-backfill rows alone (soon more per category), leaving
        everything expanded would be the same wall-of-rows problem the
        category grouping exists to fix."""
        jobs_by_category: dict[str, list[dict]] = {name: [] for name in CATEGORY_ORDER}
        for job in SCHEDULED_JOBS:
            last_run = get_latest_batch_job_run(db, job.job_name) if job.job_name else None
            if last_run is not None and last_run["status"] == "running":
                last_run = {**last_run, "live_progress": get_batch_job_run_live_progress(db, last_run["run_id"])}
            jobs_by_category.setdefault(job.category, []).append({
                "job_id": job.job_id,
                "label": job.label,
                "cadence": job.cadence,
                "reason": job.reason,
                "runner_available": job.runner is not None,
                "last_run": last_run,
            })
        schedule_categories = [
            {
                "name": name,
                "jobs": jobs,
                "has_running": any(j["last_run"] and j["last_run"]["status"] == "running" for j in jobs),
            }
            for name, jobs in jobs_by_category.items() if jobs
        ]
        return {"schedule_categories": schedule_categories}

    def _ingest_panel_context(db, logs_db) -> dict:
        """Only computed when the Ingest panel is actually being viewed —
        discover_pending_financial_items() walks the whole data/raw/ tree,
        which is wasted work on every /admin load otherwise (same reasoning
        the Companies panel's own pagination comment already gives for not
        materializing everything unconditionally).

        One unified, filterable+paginated table per underlying data source
        (ingestion_queue_items, documents) rather than one sub-table per
        status — a Status dropdown replaces what used to be 4 separately
        rendered financial-queue sections (Pending/Needs Review/Failed/
        Processed) and 2 document sections (Pending/Failed). Company/Kind
        (Type) dropdown options come from *every* row regardless of the
        current Status filter, so switching Status doesn't make options
        disappear out from under the user."""
        discover_pending_financial_items(logs_db)

        active_ingest_tab = "documents" if request.args.get("ingest_tab") == "documents" else "financial"

        fq_all = list_ingestion_queue_items(logs_db)
        fq_status_filter = request.args.get("fq_status") or ""
        fq_company_filter = request.args.get("fq_company") or ""
        fq_kind_filter = request.args.get("fq_kind") or ""
        fq = _filter_and_paginate(
            list_ingestion_queue_items(logs_db, status=fq_status_filter or None),
            filters={"company_id": fq_company_filter, "item_kind": fq_kind_filter},
            page_arg="fq_page", page_size=ADMIN_INGEST_PAGE_SIZE,
        )

        dq_all = list_documents_by_status(db)
        dq_status_filter = request.args.get("dq_status") or ""
        dq_company_filter = request.args.get("dq_company") or ""
        dq_type_filter = request.args.get("dq_type") or ""
        dq = _filter_and_paginate(
            list_documents_by_status(db, dq_status_filter or None),
            filters={"company_id": dq_company_filter, "document_type": dq_type_filter},
            page_arg="dq_page", page_size=ADMIN_INGEST_PAGE_SIZE,
        )

        return {
            "active_ingest_tab": active_ingest_tab,
            "ingest_fq_rows": fq["rows"],
            "ingest_fq_total": fq["total"],
            "ingest_fq_page": fq["page"],
            "ingest_fq_total_pages": fq["total_pages"],
            "ingest_fq_page_size": fq["page_size"],
            "ingest_fq_status_filter": fq_status_filter,
            "ingest_fq_company_filter": fq_company_filter,
            "ingest_fq_kind_filter": fq_kind_filter,
            "ingest_fq_companies": _distinct_values(fq_all, "company_id"),
            "ingest_fq_kinds": _distinct_values(fq_all, "item_kind"),
            "ingest_dq_rows": dq["rows"],
            "ingest_dq_total": dq["total"],
            "ingest_dq_page": dq["page"],
            "ingest_dq_total_pages": dq["total_pages"],
            "ingest_dq_page_size": dq["page_size"],
            "ingest_dq_status_filter": dq_status_filter,
            "ingest_dq_company_filter": dq_company_filter,
            "ingest_dq_type_filter": dq_type_filter,
            "ingest_dq_companies": _distinct_values(dq_all, "company_id"),
            "ingest_dq_types": _distinct_values(dq_all, "document_type"),
        }

    def _audit_panel_context(db, logs_db) -> dict:
        """Only computed when the Audit Log panel is actually being viewed,
        same reasoning _ingest_panel_context() gives for the Ingest panel.

        One table, one row per NSE-listed active company — not two separate
        tables (migration status + a standalone reconciliation-log table),
        because those two datasets have different grains and the second
        can't stand alone anyway: a company with zero XBRL activity (the
        vast majority today) has zero reconciliation_log rows, so a flat
        event log can never show it — only a company-per-row structure can
        answer "what's pending" for a company that's never been touched.
        Each row instead carries its own recent decision trail as nested
        detail (audit_migration_rows[i]['recent_log']), collapsed by
        default and expanded client-side (admin.html's toggle script) —
        same real-estate-efficient disclosure shape already used elsewhere
        in this app, just hand-rolled with plain <tr>s here since <details>
        can't wrap table rows directly.

        A free-text search (company id/name/NSE symbol) is layered on top
        of the status filter for the same reason Import Data's company
        field got a search box instead of a dropdown — 2,600 rows is too
        many to page through by eye.

        Also returns the "Job Runs" tab's data (audit_job_runs) -- every
        Schedule-panel trigger (and any other BatchRun-wrapped job, present
        or future) shows up here, same audit-trail idea as the
        reconciliation table above but at the batch-run grain instead of
        the per-metric grain. Items are eager-loaded per run rather than
        lazily on expand-click (which would need its own endpoint) — 50
        runs at a handful of items each is small, the same "just eager-load
        it, it's cheap" call this function already makes for recent_log
        above."""
        _AUDIT_TABS = ("reconciliation", "usa_reconciliation", "job_runs", "cases", "raw_documents")
        active_tab = request.args.get("al_tab") if request.args.get("al_tab") in _AUDIT_TABS else "reconciliation"

        # Schwab "Transfer Activity"-style filter bar: a job picker (their
        # account picker) + a time-period picker, instead of always
        # dumping the last 50 runs across every job unfiltered -- with 13+
        # jobs now sharing this one table (NSE x4 tiers x2 kinds, SEC
        # EDGAR, FRED, price history x2, DB shard), an unfiltered view
        # buries any one job's history under whichever jobs happen to run
        # most often. "All jobs" + "All time" (both empty string) reproduces
        # the old unfiltered behavior exactly, so this is additive, not a
        # behavior change for anyone who ignores the new controls.
        job_filter = request.args.get("al_job") or ""
        period_filter = request.args.get("al_period") or ""
        # Hour-granularity options (1h/4h/8h/24h) alongside the original
        # day-granularity ones -- a stuck/failing job needs "what happened
        # in the last hour" far more than "the last 5 days" while actively
        # debugging it, so a suffix-keyed dict (rather than the old
        # bare-day-count one) is what lets both units share one filter.
        _PERIOD_DELTAS = {
            "1h": timedelta(hours=1), "4h": timedelta(hours=4),
            "8h": timedelta(hours=8), "24h": timedelta(hours=24),
            "5d": timedelta(days=5), "30d": timedelta(days=30),
            "90d": timedelta(days=90), "365d": timedelta(days=365),
        }
        since_iso = None
        if period_filter in _PERIOD_DELTAS:
            since_iso = (datetime.now(timezone.utc) - _PERIOD_DELTAS[period_filter]).isoformat()

        job_labels = {job.job_name: job.label for job in SCHEDULED_JOBS if job.job_name}
        job_filter_options = [
            {"job_name": name, "label": job_labels.get(name, name)}
            for name in list_distinct_batch_job_names(logs_db)
        ]

        job_runs = list_batch_job_runs(logs_db, job_name=job_filter or None, since_iso=since_iso, limit=200)
        for run in job_runs:
            run["items"] = list_batch_job_items(logs_db, run["run_id"])
            # Derived from the already-eager-loaded items above, not
            # run['items_total']/etc -- those columns are only written once,
            # at the very end, by finish_batch_job_run(), so they're still
            # NULL for the entire duration of a run that's still in
            # progress (the row would otherwise show a misleading "0/0"
            # instead of real live progress). For an already-finished run
            # these come out identical to the stored summary, since
            # batch_job_items reflects the final state exactly by then --
            # so the template can just always use these instead of the
            # stored columns, one code path either way.
            run["items_started_live"] = len(run["items"])
            run["items_succeeded_live"] = sum(1 for i in run["items"] if i["status"] == "ok")
            run["items_failed_live"] = sum(1 for i in run["items"] if i["status"] == "failed")

        # Cases tab -- every Quick Answer/Deep Dive run, not just the ones
        # a user currently sees on /cases (list_research_cases_for_feed
        # deliberately hides completed/answered cases there since those
        # already have their own generated_reports/investigations row;
        # here the point is a complete operator-facing record, so nothing
        # is excluded). duration_seconds is None for a still-running case
        # -- only a terminal case has a real, frozen duration to show,
        # same reasoning as _case_status_payload()'s own elapsed_seconds fix.
        case_status_filter = request.args.get("al_case_status") or ""
        case_kind_filter = request.args.get("al_case_kind") or ""
        case_rows = [
            dict(c) for c in list_research_cases_for_audit(
                db, status=case_status_filter or None, kind=case_kind_filter or None,
                since_iso=since_iso, limit=200,
            )
        ]
        for row in case_rows:
            started = datetime.fromisoformat(row["started_at"])
            if row["completed_at"]:
                completed = datetime.fromisoformat(row["completed_at"])
                row["duration_seconds"] = round((completed - started).total_seconds(), 1)
            else:
                row["duration_seconds"] = None

        status_filter = request.args.get("al_status") or ""
        query = (request.args.get("al_q") or "").strip().lower()
        index_filter = request.args.get("al_index") or ""
        migration_rows = list_xbrl_migration_status(db, logs_db)

        filtered_rows = migration_rows
        if query:
            filtered_rows = [
                r for r in filtered_rows
                if query in (r["company_id"] or "").lower()
                or query in (r["display_name"] or "").lower()
                or query in (r["nse_symbol"] or "").lower()
            ]
        if index_filter:
            index_company_ids = {r["company_id"] for r in select_company_ids_by_index(db, index_filter)}
            filtered_rows = [r for r in filtered_rows if r["company_id"] in index_company_ids]

        page_size = ADMIN_INGEST_PAGE_SIZE
        page = _filter_and_paginate(
            filtered_rows, filters={"migration_status": status_filter},
            page_arg="al_page", page_size=page_size,
        )

        recent_log_by_company = list_reconciliation_log_by_company(
            logs_db, [r["company_id"] for r in page["rows"]], limit_per_company=20,
        )
        for row in page["rows"]:
            row["recent_log"] = recent_log_by_company.get(row["company_id"], [])

        # USA Reconciliation -- list_sec_edgar_migration_status()'s own
        # docstring explains why this is a sibling table to the NSE one
        # above, not a country branch of it. No pagination here (unlike
        # the India table's 2,600 rows): the whole US universe is ~25
        # companies today, small enough to render in one page without the
        # complexity earning its keep yet -- revisit if that ever changes.
        usa_status_filter = request.args.get("al_usa_status") or ""
        usa_query = (request.args.get("al_usa_q") or "").strip().lower()
        usa_migration_rows_all = list_sec_edgar_migration_status(db, logs_db)
        usa_migration_rows = usa_migration_rows_all
        if usa_status_filter:
            usa_migration_rows = [r for r in usa_migration_rows if r["migration_status"] == usa_status_filter]
        if usa_query:
            usa_migration_rows = [
                r for r in usa_migration_rows
                if usa_query in (r["company_id"] or "").lower() or usa_query in (r["display_name"] or "").lower()
            ]
        usa_recent_log_by_company = list_reconciliation_log_by_company(
            logs_db, [r["company_id"] for r in usa_migration_rows], limit_per_company=20,
        )
        for row in usa_migration_rows:
            row["recent_log"] = usa_recent_log_by_company.get(row["company_id"], [])

        # Raw Documents tab -- per-company coverage of every "pull and
        # store, don't process yet" backfill, across every source (NSE's
        # quarterly filings/presentations/transcripts/annual reports, SEC
        # EDGAR's 10-Ks, and whatever else lands in this same raw_objects
        # catalog later) -- ADR-022:
        # docs/ADR/022-s3-raw-processed-object-store-with-lineage-catalog.md.
        # Aggregated here in Python, not a new SQL GROUP BY per backend --
        # matches list_all_raw_objects()'s own "cheap at this app's scale"
        # reasoning (hundreds of companies × a handful of doc types each).
        # `processed` counts any state past 'fetched'/'stored' -- always 0
        # today (every backfill built so far deliberately stops at
        # 'stored'), but the column is real and will start moving the
        # moment a future processing step advances any row's state, with
        # no template change needed then.
        _RAW_DOC_TYPE_ORDER = (
            "quarterly_result_filing", "investor_presentation", "concall_transcript",
            "annual_report", "annual_report_10k",
        )
        raw_query = (request.args.get("al_raw_q") or "").strip().lower()
        # Filtered by object_type, not source -- raw_objects is the shared
        # ADR-022 catalog every external-fetch job in this app uses for its
        # own audit trail (corporate actions, shareholding snapshots,
        # yfinance prices, ...), not something exclusive to the document
        # backfills this tab exists to surface. object_type is the
        # source-agnostic signal that actually distinguishes "one of our
        # pull-and-store document backfills" (whichever source produced
        # it) from every other job's own unrelated raw_objects rows -- a
        # future object type in this same family just needs adding to
        # _RAW_DOC_TYPE_ORDER above, no further filtering logic here.
        raw_objects_all = [
            obj for obj in list_raw_objects(db, limit=50000) if obj["object_type"] in _RAW_DOC_TYPE_ORDER
        ]
        raw_by_company: dict[str, dict] = {}
        for obj in raw_objects_all:
            company_id = obj["entity"] or "—"
            bucket = raw_by_company.setdefault(company_id, {
                "company_id": company_id, "by_type": {}, "total": 0,
                "processed": 0, "latest_fetched_at": None, "sources": set(),
            })
            bucket["by_type"][obj["object_type"]] = bucket["by_type"].get(obj["object_type"], 0) + 1
            bucket["total"] += 1
            bucket["sources"].add(obj["source"])
            if obj["state"] not in ("fetched", "stored"):
                bucket["processed"] += 1
            if bucket["latest_fetched_at"] is None or obj["fetched_at"] > bucket["latest_fetched_at"]:
                bucket["latest_fetched_at"] = obj["fetched_at"]

        raw_company_names = {c["company_id"]: c["display_name"] for c in list_companies(db)}
        raw_rows = list(raw_by_company.values())
        for row in raw_rows:
            row["display_name"] = raw_company_names.get(row["company_id"], row["company_id"])
            row["source_label"] = " + ".join(sorted(row["sources"])) if row["sources"] else "—"
        if raw_query:
            raw_rows = [
                r for r in raw_rows
                if raw_query in r["company_id"].lower() or raw_query in (r["display_name"] or "").lower()
            ]
        raw_rows.sort(key=lambda r: r["display_name"] or r["company_id"])
        raw_doc_types_present = [t for t in _RAW_DOC_TYPE_ORDER if any(t in r["by_type"] for r in raw_rows)]
        # Explicit labels rather than a generic .replace('_', ' ')|title in
        # the template -- that mangles "annual_report_10k" into "Annual
        # Report 10k" (Jinja's title filter doesn't know "10k" should stay
        # "10-K"), the same way it would mangle any future acronym-bearing
        # object_type.
        _RAW_DOC_TYPE_LABELS = {
            "quarterly_result_filing": "Quarterly Result Filing",
            "investor_presentation": "Investor Presentation",
            "concall_transcript": "Concall Transcript",
            "annual_report": "Annual Report (NSE)",
            "annual_report_10k": "10-K (SEC)",
        }
        raw_doc_type_labels = {t: _RAW_DOC_TYPE_LABELS.get(t, t.replace("_", " ").title()) for t in raw_doc_types_present}

        return {
            "audit_rows": page["rows"],
            "audit_total": page["total"],
            "audit_page": page["page"],
            "audit_total_pages": page["total_pages"],
            "audit_page_size": page["page_size"],
            "audit_status_filter": status_filter,
            "audit_query": request.args.get("al_q", ""),
            "audit_index_filter": index_filter,
            "audit_index_options": list_index_definitions(db),
            "audit_pending_count": sum(1 for r in migration_rows if r["migration_status"] == "pending"),
            "audit_not_started_count": sum(1 for r in migration_rows if r["migration_status"] == "not_started"),
            "audit_active_tab": active_tab,
            "audit_usa_rows": usa_migration_rows,
            "audit_usa_status_filter": usa_status_filter,
            "audit_usa_query": request.args.get("al_usa_q", ""),
            "audit_usa_pending_count": sum(1 for r in usa_migration_rows_all if r["migration_status"] == "pending"),
            "audit_usa_not_started_count": sum(1 for r in usa_migration_rows_all if r["migration_status"] == "not_started"),
            "audit_job_runs": job_runs,
            "audit_job_filter": job_filter,
            "audit_job_filter_options": job_filter_options,
            "audit_period_filter": period_filter,
            "audit_case_rows": case_rows,
            "audit_case_status_filter": case_status_filter,
            "audit_case_kind_filter": case_kind_filter,
            "audit_raw_rows": raw_rows,
            "audit_raw_query": request.args.get("al_raw_q", ""),
            "audit_raw_doc_types": raw_doc_types_present,
            "audit_raw_doc_type_labels": raw_doc_type_labels,
            "audit_raw_total_objects": sum(r["total"] for r in raw_rows),
        }

    @app.route("/admin")
    def admin():
        """Retired as a standalone page — its 8 panels now live under
        Settings' Administration group (web/templates/settings.html), built
        from the same context this endpoint used to render admin.html with
        (see _build_admin_settings_context). Kept as a redirect, not removed
        outright, so old links/bookmarks to /admin?panel=... still land
        somewhere correct instead of 404ing."""
        args = request.args.to_dict(flat=True)
        panel = args.pop("panel", "companies")
        return redirect(url_for("settings", panel=f"admin-{panel}", **args))

    def _build_admin_settings_context(admin_sub: str) -> dict:
        """Everything the migrated Administration panels in settings.html
        need to render — this is the body of the old admin() view, just
        returning a dict instead of calling render_template, and taking the
        active admin sub-panel ("companies", "ingest", ...) as a parameter
        rather than re-reading request.args["panel"] itself, since by the
        time this runs that query param holds "admin-<sub>" (settings()'s
        own active_panel), not the bare sub-panel name."""
        db = get_db()
        # ~2,600 companies today — the edit panel renders one <form> per row
        # (not just a display row), so materializing and index-tag-querying
        # all of them on every load was both an N+1 query storm (one SELECT
        # per company for get_company_index_tags) and a multi-thousand-form
        # page that made the browser hang. Only the current page (after
        # search/filtering below) ever gets rendered; tags for every company
        # come from one batched query (get_all_company_index_tags), not one
        # query per row, so filtering by tag doesn't reintroduce the N+1.
        all_companies = [dict(c) for c in list_companies(db, include_archived=True)]
        tags_by_company = get_all_company_index_tags(db)
        for row in all_companies:
            row["index_tags"] = tags_by_company.get(row["company_id"], [])
        # From the lookup tables (Admin > Sectors, Industries & Tags), not
        # derived from company usage — so a sector/industry an admin has
        # added but not yet assigned to any company still shows up as a
        # dropdown option here.
        sectors = list_sectors(db)
        industries = list_industries(db)

        # Search/filters mirror the Companies list page (/companies,
        # web/templates/index.html) — but applied server-side, before
        # pagination, since a client-side filter over only the current
        # page's ~50 rows would miss a company that happens to be on a
        # different page (the whole reason to search a 2,600-row list in
        # the first place).
        query = (request.args.get("q") or "").strip().lower()
        sector_filter = request.args.get("sector") or ""
        industry_filter = request.args.get("industry") or ""
        tag_filter = request.args.get("tag") or ""
        country_filter = request.args.get("country") or ""
        # Defaults to "active" on a fresh page load (no `status` query param
        # at all) rather than "all statuses" -- with ~2,580 active companies
        # and a much smaller archived set, landing on a mixed list by
        # default buried the common case (browsing active companies) under
        # rows nobody's usually looking for. request.args still lets
        # "status=" (present but empty, e.g. from the Clear link below)
        # explicitly ask for all statuses -- only a fully absent param
        # falls back to "active".
        status_filter = request.args.get("status", "active")

        filtered_companies = all_companies
        if query:
            tokens = query.split()
            def _haystack(row: dict) -> str:
                parts = [
                    row.get("company_id"), row.get("display_name"), row.get("legal_name"),
                    row.get("nse_symbol"), row.get("bse_code"), row.get("isin"),
                    row.get("sector"), row.get("industry"), row.get("status"),
                ] + row["index_tags"]
                return " ".join(p for p in parts if p).lower()
            filtered_companies = [c for c in filtered_companies if all(t in _haystack(c) for t in tokens)]
        if sector_filter:
            filtered_companies = [c for c in filtered_companies if c["sector"] == sector_filter]
        if industry_filter:
            filtered_companies = [c for c in filtered_companies if c["industry"] == industry_filter]
        if tag_filter:
            filtered_companies = [c for c in filtered_companies if tag_filter in c["index_tags"]]
        if country_filter:
            filtered_companies = [c for c in filtered_companies if c["country"] == country_filter]
        if status_filter:
            filtered_companies = [c for c in filtered_companies if c["status"] == status_filter]

        total_companies = len(filtered_companies)
        total_pages = max(1, -(-total_companies // ADMIN_COMPANIES_PAGE_SIZE))
        page = max(1, min(request.args.get("page", 1, type=int) or 1, total_pages))
        start = (page - 1) * ADMIN_COMPANIES_PAGE_SIZE
        page_companies = filtered_companies[start:start + ADMIN_COMPANIES_PAGE_SIZE]

        column_settings = get_company_list_column_settings(db)
        ratio_settings = get_overview_ratio_settings(db)
        index_tag_names = list_index_definitions(db)
        # Sectors, Industries & Tags panel: each vocabulary's full list plus
        # how many companies currently use each entry — an admin needs the
        # count to judge whether a rename/delete is safe before doing it.
        taxonomy = {
            "sector": {"items": sectors, "counts": count_companies_by_sector(db)},
            "industry": {"items": industries, "counts": count_companies_by_industry(db)},
            "index-tag": {"items": index_tag_names, "counts": count_companies_by_index_tag(db)},
        }
        import_selected_company = request.args.get("company_id", "")
        import_selected_company_label = ""
        if import_selected_company:
            match = next((c for c in all_companies if c["company_id"] == import_selected_company), None)
            if match:
                import_selected_company_label = f"{match['display_name']} ({match['company_id']})"

        return {
            "companies": page_companies,
            "companies_page": page,
            "companies_total_pages": total_pages,
            "companies_total": total_companies,
            "companies_page_size": ADMIN_COMPANIES_PAGE_SIZE,
            "companies_query": query,
            "companies_sector_filter": sector_filter,
            "companies_industry_filter": industry_filter,
            "companies_tag_filter": tag_filter,
            "companies_country_filter": country_filter,
            "companies_status_filter": status_filter,
            # status_filter alone no longer implies an active filter --
            # "active" is now the unset default (see status_filter's own
            # comment above), so the Clear link would otherwise show on
            # every fresh, untouched page load with nothing to clear.
            "companies_filters_active": bool(
                query or sector_filter or industry_filter or tag_filter or country_filter
                or (status_filter and status_filter != "active")
            ),
            "active_companies": [c for c in all_companies if c["status"] == "active"],
            "archive_reasons": sorted(ARCHIVE_REASONS),
            "sectors": sectors,
            "industries": industries,
            "countries": sorted({c["country"] for c in all_companies if c.get("country")}),
            "index_names": index_tag_names,
            "taxonomy": taxonomy,
            "list_columns": COMPANY_LIST_COLUMNS,
            "column_settings": column_settings,
            "ratio_catalog": OVERVIEW_RATIO_CATALOG,
            "ratio_settings": ratio_settings,
            "import_sources": sorted(ADAPTER_CLASSES),
            "active_admin_panel": admin_sub,
            "import_selected_company": import_selected_company,
            "import_selected_company_label": import_selected_company_label,
            "stock_action_types": sorted(ACTION_TYPES),
            "stock_action_selected_company": request.args.get("sa_company_id", ""),
            "stock_actions": (
                list_stock_actions(db, request.args["sa_company_id"])
                if request.args.get("sa_company_id") else []
            ),
            # logs_db (always SQLite, regardless of DATABASE_BACKEND) is
            # passed alongside db (backend-dependent) since batch_job_runs/
            # items, ingestion_queue_items, and reconciliation_log stay
            # SQLite-only forever -- see get_logs_db()'s own docstring.
            **(_ingest_panel_context(db, get_logs_db()) if admin_sub == "ingest" else {}),
            **(_audit_panel_context(db, get_logs_db()) if admin_sub == "audit" else {}),
            **(_schedule_panel_context(get_logs_db()) if admin_sub == "schedule" else {}),
        }

    @app.route("/admin/usage")
    def admin_usage():
        """LLM token/cost observability — every research/assistant.py,
        research/insights.py, research/signals_report.py, and
        research/macro_evidence.py call logs one llm_call_log row
        (llm/observability.py); this page is just that table, summarized.
        Admin-only (endpoint name starts with "admin" — see _require_login
        above) since spend data is an operator concern, not a general
        end-user one."""
        db = get_logs_db()
        return render_template(
            "usage.html",
            summary=get_llm_usage_summary(db),
            recent_calls=list_llm_call_log(db, limit=100),
        )

    @app.route("/admin/columns", methods=["POST"])
    def admin_update_columns():
        db = get_db()
        set_company_list_column_settings(db, request.form.getlist("columns"))
        return redirect(url_for("settings", panel="admin-companies"))

    @app.route("/admin/overview-ratios", methods=["POST"])
    def admin_update_overview_ratios():
        db = get_db()
        set_overview_ratio_settings(db, request.form.getlist("ratios"))
        return redirect(url_for("settings", panel="admin-overview_ratios"))

    # One route family for all three vocabularies (Sector/Industry/Index tag)
    # — structurally identical (a name-keyed lookup table an admin can add/
    # rename/delete from, with a company-usage count) rather than tripling
    # the same three routes. "kind" in the URL, not the table name directly,
    # so an unknown value 404s instead of silently no-op-ing.
    _VOCAB_HANDLERS = {
        "sector": (add_sector, rename_sector, delete_sector),
        "industry": (add_industry, rename_industry, delete_industry),
        "index-tag": (add_index_definition, rename_index_definition, delete_index_definition),
    }

    @app.route("/admin/vocabulary/<kind>/add", methods=["POST"])
    def admin_vocabulary_add(kind: str):
        if kind not in _VOCAB_HANDLERS:
            abort(404, f"Unknown vocabulary: {kind!r}")
        add_fn, _rename_fn, _delete_fn = _VOCAB_HANDLERS[kind]
        name = (request.form.get("name") or "").strip()
        if name:
            add_fn(get_db(), name)
        return redirect(url_for("settings", panel="admin-taxonomy"))

    @app.route("/admin/vocabulary/<kind>/rename", methods=["POST"])
    def admin_vocabulary_rename(kind: str):
        if kind not in _VOCAB_HANDLERS:
            abort(404, f"Unknown vocabulary: {kind!r}")
        _add_fn, rename_fn, _delete_fn = _VOCAB_HANDLERS[kind]
        old_name = (request.form.get("old_name") or "").strip()
        new_name = (request.form.get("new_name") or "").strip()
        if old_name and new_name and old_name != new_name:
            rename_fn(get_db(), old_name, new_name)
        return redirect(url_for("settings", panel="admin-taxonomy"))

    @app.route("/admin/vocabulary/<kind>/delete", methods=["POST"])
    def admin_vocabulary_delete(kind: str):
        if kind not in _VOCAB_HANDLERS:
            abort(404, f"Unknown vocabulary: {kind!r}")
        _add_fn, _rename_fn, delete_fn = _VOCAB_HANDLERS[kind]
        name = (request.form.get("name") or "").strip()
        if name:
            delete_fn(get_db(), name)
        return redirect(url_for("settings", panel="admin-taxonomy"))

    @app.route("/admin/import", methods=["POST"])
    def admin_import_raw_file():
        """Upload a raw file for one company and run it through the same
        ingest_file() pipeline `python main.py ingest` uses — parse ->
        validate -> store -> reconcile. The only Admin action that writes
        ingested financial data rather than company metadata."""
        db = get_db()
        company_id = request.form.get("company_id", "").strip()
        source_id = request.form.get("source_id", "").strip()
        statement_type = request.form.get("statement_type", "consolidated")
        upload = request.files.get("file")

        if not company_id or get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        if source_id not in ADAPTER_CLASSES:
            abort(400, f"source must be one of {sorted(ADAPTER_CLASSES)}, got {source_id!r}")
        if statement_type not in ("consolidated", "standalone"):
            abort(400, "statement_type must be 'consolidated' or 'standalone'")
        if upload is None or not upload.filename:
            flash("Choose a file to import.", "error")
            return redirect(url_for("settings", panel="admin-import"))
        filename = secure_filename(upload.filename)
        if not filename:
            flash("That filename isn't valid.", "error")
            return redirect(url_for("settings", panel="admin-import"))

        # data/raw/<COMPANY>/<source>/<file> — the same convention the CLI's
        # own path-based detection expects, so a file uploaded here is
        # indistinguishable from one dropped in by hand (README: Ingestion
        # Approach by Source). Timestamp-prefixed so re-uploading the same
        # filename never silently overwrites a previous raw file — every
        # upload is kept, matching the "raw observations are never
        # overwritten" rule the rest of ingestion already follows.
        dest_dir = app_settings.RAW_DIR / company_id / source_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_path = dest_dir / f"{stamp}__{filename}"
        upload.save(dest_path)

        try:
            result = ingest_file(db, dest_path, company_id=company_id, source_id=source_id, statement_type=statement_type)
        except CompanyNotActiveError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            # Adapter parse failures (wrong file format, missing expected
            # sheet/columns, ...) are user-input errors here, not bugs — this
            # is an upload boundary, so surface them instead of 500ing.
            flash(f"Import failed: {exc}", "error")
        else:
            flash(
                f"Imported {filename} for {company_id} ({source_id}, {statement_type}): "
                f"parsed {result.parsed_count}, inserted {result.inserted_count}, "
                f"skipped {result.skipped_count}, reconciled {result.reconciled_count}.",
                "success",
            )
            for reason in result.skip_reasons[:20]:
                flash(reason, "warning")

        return redirect(url_for("settings", panel="admin-import"))

    @app.route("/companies/<company_id>/reconcile", methods=["POST"])
    def admin_reconcile_company(company_id: str):
        """Re-derive canonical_financials for this company from whatever's
        already been ingested — no new upload, just re-runs the same
        trust_rank-based pick every ingest already does automatically, on
        demand (e.g. after an alias/trust_rank change). Ratios need no
        equivalent action — financials/ratios.py reads canonical_financials
        live on every page view, nothing is cached."""
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        count = reconcile_company(db, company_id)
        flash(f"Reconciled {count} metric/period combinations for {company_id}.", "success")
        return redirect(url_for("company_report", company_id=company_id))

    # (job_label, job_name) pairs run_now_status()/admin_company_run_now()
    # both key off of -- job_name literals match _JOB_NAMES in
    # scripts/batch_fetch_nse.py / _JOB_NAME in scripts/batch_fetch_sec_edgar.py
    # (same literals the Schedule panel's own ScheduledJob entries already
    # use below), so this reads back the very same audit trail those bulk
    # jobs write to, not a parallel one.
    _RUN_NOW_JOBS_IN = [("Financials", "nse_xbrl_fetch"), ("Shareholding", "nse_shareholding_fetch")]
    _RUN_NOW_JOBS_US = [("Financials", "sec_edgar_financials_fetch")]

    def _run_now_jobs_for(company: Row) -> list[tuple[str, str]]:
        return _RUN_NOW_JOBS_US if company["country"] == "US" else _RUN_NOW_JOBS_IN

    def run_now_status(company: Row) -> list[dict]:
        """This company's own latest attempt at each applicable job
        (Financials, and Shareholding for an NSE-listed company) -- the
        status badges Company Report shows next to "Run now"
        (running/complete/failed, from whatever batch_job_items row this
        company most recently appeared in for that job_name, via
        get_latest_batch_item_for_company()). A company that's never been
        run gets a "never run" placeholder rather than being left off the
        list entirely, so the badge for a brand-new company doesn't just
        silently not appear."""
        db = get_logs_db()
        statuses = []
        for label, job_name in _run_now_jobs_for(company):
            item = get_latest_batch_item_for_company(db, job_name, company["company_id"])
            statuses.append({
                "label": label,
                "status": item["status"] if item else "never_run",
                "detail": item["detail"] if item else None,
                "finished_at": item["finished_at"] if item else None,
            })
        return statuses

    @app.route("/companies/<company_id>/run-now", methods=["POST"])
    def admin_company_run_now(company_id: str):
        """Company Report's "Run now" — pulls this one company's latest
        financials (SEC EDGAR for a US company, NSE filings for an Indian
        one) plus, for an NSE-listed company, its shareholding pattern —
        through the exact same audited batch runners
        (scripts/batch_fetch_nse.py::run_nse_batch / scripts/
        batch_fetch_sec_edgar.py::run_sec_edgar_batch) the Schedule panel's
        bulk jobs use, just given a single-company list instead of an
        index/country-wide one. That's why this replaces the old
        admin_refresh_company route rather than sitting alongside it: same
        NSE-financials-refresh logic (refresh_company_filings + ingest_file),
        now also recorded to batch_job_runs/batch_job_items (Audit Log ->
        Job Runs gets a real, queryable entry) instead of a plain flash
        message and nothing else. force=True on the US path bypasses
        run_sec_edgar_batch's own skip-if-recently-succeeded check — an
        explicit "Run now" click means fetch now, not "only if it's been
        24h". Synchronous, same no-background-job tradeoff as every other
        admin action in this file."""
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        if company["country"] == "US":
            run_sec_edgar_batch(db, [company_id], scope_label=f"{company_id} (manual run)", force=True)
        else:
            if not company["nse_symbol"]:
                flash(f"{company_id} has no nse_symbol on file — nothing to fetch.", "error")
                return redirect(url_for("company_report", company_id=company_id))
            run_nse_batch(db, "financials", [company_id], scope_label=f"{company_id} (manual run)")
            run_nse_batch(db, "shareholding", [company_id], scope_label=f"{company_id} (manual run)")

        for status in run_now_status(company):
            if status["status"] == "ok":
                flash(f"{status['label']}: {status['detail'] or 'done'}", "success")
            else:
                flash(f"{status['label']} failed: {status['detail'] or 'see Audit Log for details'}", "error")

        return redirect(url_for("company_report", company_id=company_id))

    @app.route("/admin/schedule/run/<job_id>", methods=["POST"])
    def admin_schedule_run(job_id: str):
        """Settings > Data Operations > Schedule's "Run now" button —
        deliberately kept synchronous and blocking (see admin_refresh_
        company's own docstring above for the same tradeoff at
        single-company scale): an admin clicking "Run now" is expected to
        wait for the response. Real unattended scheduling goes through
        admin_schedule_run_async below instead, not this route — see its
        docstring for why a cron trigger can't just POST here directly.

        Every registered job's runner already writes its own BatchRun
        audit trail (this route doesn't do that bookkeeping itself), so
        all this does is look the job up, call its runner with the
        current request-scoped db connection, and turn the result into a
        flash message pointing at where the details actually live."""
        job = get_job(job_id)
        if job is None or job.runner is None:
            abort(404)
        db = get_db()
        try:
            run_id = job.runner(db)
            flash(f"{job.label}: run #{run_id} finished — see Audit Log → Job Runs for details.", "success")
        except Exception as exc:  # noqa: BLE001 -- surface any failure as a flash, not a 500
            flash(f"{job.label} failed: {exc}", "error")
        return redirect(url_for("settings", panel="admin-schedule"))

    @app.route("/admin/schedule/run-async/<job_id>", methods=["POST"])
    def admin_schedule_run_async(job_id: str):
        """The cron-trigger counterpart to admin_schedule_run above — for
        an external scheduler (e.g. AWS EventBridge Scheduler hitting this
        over HTTPS) rather than a human waiting on a button. Can't just
        point a scheduler at admin_schedule_run directly: that route is
        synchronous and blocks for however long the job takes (several
        minutes for a several-hundred-company NSE crawl), which would
        exceed gunicorn's worker timeout and get the worker killed
        mid-batch, leaving a batch_job_runs row stuck at status='running'
        until _resume_interrupted_batch_jobs cleans it up on next restart.

        Starts the same job.runner(conn) — no different logic, no
        duplicate audit trail — in a background thread and returns
        immediately, same "fire off a background thread, own db
        connection since a connection can't cross threads" shape
        _resume_interrupted_batch_jobs already uses at startup. The
        caller never sees success/failure; that's what Audit Log -> Job
        Runs is for, same as every other trigger of these jobs.

        Authenticated by a shared secret header (not session/cookie auth —
        a scheduler has no browser session), read from the
        CRON_TRIGGER_SECRET env var. Refuses every request if that env var
        isn't set, rather than silently accepting unauthenticated triggers
        — this endpoint doesn't exist in practice until an operator
        deliberately configures a secret."""
        secret = app_settings.CRON_TRIGGER_SECRET
        if not secret or request.headers.get("X-Cron-Secret") != secret:
            abort(403)
        job = get_job(job_id)
        if job is None or job.runner is None:
            abort(404)

        def _run_in_background(runner, label: str) -> None:
            conn = scheduling_open_db()
            try:
                runner(conn)
            except Exception:
                logger.exception("Cron-triggered job %r failed", label)
            finally:
                conn.close()

        threading.Thread(target=_run_in_background, args=(job.runner, job.label), daemon=True).start()
        return {"status": "started", "job_id": job_id}, 202

    @app.route("/admin/ingest/refresh", methods=["POST"])
    def admin_ingest_refresh():
        """Refresh Pending Files — re-scan data/raw/ now, rather than
        waiting for the next /admin?panel=ingest load (which already
        refreshes on its own, but an explicit action makes "did my newly
        dropped file show up" not depend on remembering that)."""
        db = get_logs_db()
        touched = discover_pending_financial_items(db)
        flash(f"Rescanned data/raw/ — {touched} item(s) added or updated.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/process", methods=["POST"])
    def admin_ingest_process():
        """Ingest Selected — the specific ingestion_queue_items rows checked
        in the UI, through the existing financial/macro pipeline."""
        db = get_db()
        item_ids = [int(v) for v in request.form.getlist("item_id")]
        summary = process_financial_items(db, item_ids)
        flash(f"Processed {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/process-all", methods=["POST"])
    def admin_ingest_process_all():
        """Ingest All Pending."""
        db = get_db()
        summary = process_all_pending_financial_items(db)
        flash(f"Processed {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/retry-failed", methods=["POST"])
    def admin_ingest_retry_failed():
        """Retry Failed — every FAILED row, as-is."""
        db = get_db()
        summary = retry_failed_financial_items(db)
        flash(f"Retried {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} still failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/archive", methods=["POST"])
    def admin_ingest_archive():
        """Archive Selected — "no need for review/processing right now"
        (ingestion/coordinator.py::archive_financial_items). Parks the
        checked rows out of Pending/Failed/Needs Review without deleting
        them; reversible via Unarchive Selected."""
        db = get_db()
        item_ids = [int(v) for v in request.form.getlist("item_id")]
        count = archive_financial_items(db, item_ids)
        flash(f"Archived {count} item(s).", "success")
        return redirect(url_for("settings", panel="admin-ingest", ingest_tab="financial"))

    @app.route("/admin/ingest/unarchive", methods=["POST"])
    def admin_ingest_unarchive():
        """Unarchive Selected — back to PENDING."""
        db = get_db()
        item_ids = [int(v) for v in request.form.getlist("item_id")]
        count = unarchive_financial_items(db, item_ids)
        flash(f"Unarchived {count} item(s).", "success")
        return redirect(url_for("settings", panel="admin-ingest", ingest_tab="financial"))

    @app.route("/admin/ingest/documents/process", methods=["POST"])
    def admin_ingest_process_documents():
        """Process the specific pending documents checked in the UI —
        registers/hashes each one and runs Step 2A's Knowledge Builder
        extraction against it (ingestion/coordinator.py::process_documents)."""
        db = get_db()
        document_ids = [int(v) for v in request.form.getlist("document_id")]
        summary = process_documents(db, document_ids)
        flash(f"Registered {summary.succeeded} document(s), {summary.failed} failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/documents/process-all", methods=["POST"])
    def admin_ingest_process_all_documents():
        db = get_db()
        summary = process_all_pending_documents(db)
        flash(f"Registered {summary.succeeded} document(s), {summary.failed} failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/documents/retry-failed", methods=["POST"])
    def admin_ingest_retry_failed_documents():
        db = get_db()
        summary = retry_failed_documents(db)
        flash(f"Retried {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} still failed.", "success")
        return redirect(url_for("settings", panel="admin-ingest"))

    @app.route("/admin/ingest/documents/archive", methods=["POST"])
    def admin_ingest_archive_documents():
        """Archive Selected — same parking-lot semantics as
        admin_ingest_archive(), for documents (ingestion/coordinator.py::
        archive_documents)."""
        db = get_db()
        document_ids = [int(v) for v in request.form.getlist("document_id")]
        count = archive_documents(db, document_ids)
        flash(f"Archived {count} document(s).", "success")
        return redirect(url_for("settings", panel="admin-ingest", ingest_tab="documents"))

    @app.route("/admin/ingest/documents/unarchive", methods=["POST"])
    def admin_ingest_unarchive_documents():
        """Unarchive Selected — back to pending."""
        db = get_db()
        document_ids = [int(v) for v in request.form.getlist("document_id")]
        count = unarchive_documents(db, document_ids)
        flash(f"Unarchived {count} document(s).", "success")
        return redirect(url_for("settings", panel="admin-ingest", ingest_tab="documents"))

    @app.route("/admin/<company_id>/stock-actions", methods=["POST"])
    def admin_add_stock_action(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        subscription_price = request.form.get("subscription_price", "").strip()
        try:
            add_stock_action(
                db,
                company_id,
                request.form.get("action_type", ""),
                request.form.get("action_date", ""),
                float(request.form.get("ratio_from", "")),
                float(request.form.get("ratio_to", "")),
                subscription_price=float(subscription_price) if subscription_price else None,
                source=request.form.get("source") or None,
                source_url=request.form.get("source_url") or None,
                notes=request.form.get("notes") or None,
            )
        except (InvalidStockActionError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Recorded {request.form.get('action_type')} for {company_id}.", "success")
        return redirect(url_for("settings", panel="admin-stock_actions", sa_company_id=company_id))

    @app.route("/admin/<company_id>/stock-actions/<int:action_id>/delete", methods=["POST"])
    def admin_delete_stock_action(company_id: str, action_id: int):
        db = get_db()
        try:
            delete_stock_action(db, company_id, action_id)
        except StockActionNotFoundError as exc:
            abort(404, str(exc))
        return redirect(url_for("settings", panel="admin-stock_actions", sa_company_id=company_id))

    @app.route("/admin/companies/search")
    def admin_companies_search():
        """Add Company's type-ahead -- plausible company names as the admin
        types a ticker/name, via sources.yfinance_company_lookup.
        search_companies() (Yahoo Finance's own search, filtered to the
        requested country's home exchange). Read-only, no DB write; just a
        JSON list for the front-end <datalist> to render."""
        query = request.args.get("q", "")
        country = (request.args.get("country", "IN").strip() or "IN").upper()
        return jsonify(results=search_yfinance_companies(query, country))

    @app.route("/admin/companies/add", methods=["POST"])
    def admin_add_company():
        """Admin Companies panel's "Add Company" form -- just a ticker +
        country. Everything else (legal/display name, currency, sector,
        industry, website) is looked up from Yahoo Finance, then the
        company is registered and its financials (SEC EDGAR + Yahoo
        Finance for a US company, NSE filings for an Indian one) and a 10y
        price history are fetched, all via ingestion/onboarding.py::
        onboard_new_company(). Synchronous and blocking, same
        no-background-job tradeoff as admin_refresh_company/
        admin_schedule_run above -- a first backfill is the heaviest single
        admin action in this file, so this can take a while to return."""
        db = get_db()
        company_id = request.form.get("company_id", "").strip()
        if not company_id:
            abort(400, "company_id is required")
        country = (request.form.get("country", "IN").strip() or "IN").upper()

        try:
            result = onboard_new_company(db, get_price_db(), app_settings.RAW_DIR, company_id=company_id, country=country)
        except CompanyLookupError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings", panel="admin-companies"))
        except Exception as exc:  # noqa: BLE001 -- surface as a flash, not a 500, same as admin_schedule_run
            flash(f"Add company failed: {exc}", "error")
            return redirect(url_for("settings", panel="admin-companies"))

        for step in result.steps:
            flash(f"{step.label}: {step.detail}", "success" if step.ok else "error")
        return redirect(url_for("company_report", company_id=result.company_id))

    @app.route("/admin/<company_id>", methods=["POST"])
    def admin_update_company(company_id: str):
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        action = request.form.get("action", "save")

        if action == "archive":
            reason = request.form.get("archive_reason", "")
            try:
                archive_company(db, company_id, reason)
            except InvalidArchiveReasonError as exc:
                abort(400, str(exc))
            except CompanyNotFoundError as exc:
                abort(404, str(exc))
        elif action == "restore":
            try:
                restore_company(db, company_id)
            except CompanyNotFoundError as exc:
                abort(404, str(exc))
        elif action == "save":
            display_name = request.form.get("display_name", "").strip()
            legal_name = request.form.get("legal_name", "").strip()
            if not display_name or not legal_name:
                abort(400, "display_name and legal_name are required")
            sector = _resolve_dropdown_or_custom("sector")
            industry = _resolve_dropdown_or_custom("industry")
            # A custom-typed sector/industry ("+ Add new...") needs to land
            # in the lookup table too, not just this company's own row — the
            # dropdown options and the Sectors/Industries admin panel both
            # read from sectors/industries now, not from company usage.
            # INSERT OR IGNORE (add_sector/add_industry) makes this a no-op
            # when the value already exists (picked from the dropdown).
            if sector:
                add_sector(db, sector)
            if industry:
                add_industry(db, industry)
            # register_company() overwrites every mutable field it's given —
            # pass through the identifiers this form doesn't edit (NSE/BSE/
            # ISIN/country/currency/fiscal_year_end_month/website/listed_date,
            # and macro_economic_sector/basic_industry — the outer two levels
            # of NSE's 4-level classification, curated via `add-company`/
            # `import-nse-companies` rather than this grid, which doesn't
            # scale to a 2,500+ row dropdown) unchanged, or they'd be wiped
            # to NULL/reset to India/INR/March-close.
            register_company(
                db,
                company_id,
                legal_name,
                display_name,
                nse_symbol=company["nse_symbol"],
                bse_code=company["bse_code"],
                isin=company["isin"],
                country=company["country"],
                currency=company["currency"],
                fiscal_year_end_month=company["fiscal_year_end_month"],
                website=company["website"],
                macro_economic_sector=company["macro_economic_sector"],
                sector=sector,
                industry=industry,
                basic_industry=company["basic_industry"],
                listed_date=company["listed_date"],
            )
            try:
                set_company_index_tags(db, company_id, request.form.getlist("index_tags"))
            except ValueError as exc:
                abort(400, str(exc))
        else:
            abort(400, f"Unknown action: {action!r}")

        # Archive/restore is also reachable from the public /companies list
        # (a plain status toggle there, not the fuller edit form) -- see
        # _redirect_to_return_or's own docstring.
        return _redirect_to_return_or("settings", panel="admin-companies")

    @app.route("/companies/<company_id>")
    def company_report(company_id: str):
        statement_type = request.args.get("statement_type", "consolidated")
        if statement_type not in ("consolidated", "standalone"):
            abort(400, "statement_type must be 'consolidated' or 'standalone'")
        tab = request.args.get("tab", "overview")
        valid_tabs = (
            "overview", "key_insights", "indicators", "charts", "financials", "valuation_model",
            "shareholding", "commentary", "news", "notes", "docs", "threads",
        )
        if tab not in valid_tabs:
            abort(400, f"tab must be one of {', '.join(valid_tabs)}")

        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        valuation_model_file = company["valuation_model_file"]
        has_ported_dataset = bool(valuation_model_file) and _valuation_model_data_path(valuation_model_file).exists()
        # canonical_financials is this app's one source of truth for
        # financial facts (see the migration writeup off web/static/data/
        # *.json's stale, hand-ported workbook copies — those independently
        # forked the exact same data errors canonical_financials itself has
        # since fixed, and lack years canonical_financials already has).
        # A "ported dataset" company only keeps reading the static file for
        # financials_data_url when canonical_financials has genuinely
        # nothing for it yet (verified per-company against real Neon: 1 of
        # 21 ported companies, SRG Housing Finance, was never ingested at
        # all) — switching an empty-live company would blank the whole
        # Financials tab, a real regression, not a data-quality improvement.
        # valuation_data_url (the Valuation Model tab's Growth Projection
        # calculator) is untouched by this: still the static file for every
        # ported company regardless, since that section is assumption-
        # driven config this migration deliberately didn't touch — see
        # web/valuation_feed.py's module docstring.
        has_live_financials = has_ported_dataset and company_has_canonical_financials(db, company_id)

        # valuation_data_url backs only the Valuation Model tab's Growth
        # Projection / Intrinsic Value calculator (assumptions-driven,
        # inherently annual — see web/valuation_feed.py's module docstring).
        # financials_data_url backs the Financials tab and the Overview
        # tab's snapshot — both just facts, so both get the annual/quarterly
        # toggle via web/charts_feed.py's already period-aware feed (the
        # same one the Charts tab uses) instead.
        if has_ported_dataset:
            # A richer, manually-ported dataset (see the "HDFC Bank Equity
            # Dashboard" Claude Design import) — not statement_type-aware
            # and annual-only (no period_type concept at all), so the
            # Valuation Model tab keeps reading the static file here
            # regardless of has_live_financials above.
            valuation_data_url = url_for("static", filename=f"data/{valuation_model_file}")
            financials_data_url = (
                valuation_data_url
                if not has_live_financials
                else url_for("company_charts_feed", company_id=company_id, statement_type=statement_type)
            )
        else:
            # Same dashboard template, every company — built live from
            # whatever this company's canonical_financials actually has.
            # Genuine gaps (advances, EPS, price, ...) render as "—", not a
            # different page layout. See web/valuation_feed.py.
            valuation_data_url = url_for(
                "company_valuation_feed", company_id=company_id, statement_type=statement_type
            )
            # No period_type baked in, same convention as the Charts tab's
            # own data-compare-url-template below — the client appends
            # "&period_type=annual|quarterly" itself once the toggle exists.
            financials_data_url = url_for(
                "company_charts_feed", company_id=company_id, statement_type=statement_type
            )

        website_display = None
        if company["website"]:
            website_display = (urlparse(company["website"]).netloc or company["website"]).removeprefix("www.")
        nse_url = (
            f"https://www.nseindia.com/get-quotes/equity?symbol={company['nse_symbol']}"
            if company["nse_symbol"]
            else None
        )
        bse_url = f"https://m.bseindia.com/StockReach.aspx?scripcd={company['bse_code']}" if company["bse_code"] else None
        nse_index_tags, bse_index_tags, other_index_tags = _split_index_tags(get_company_index_tags(db, company_id))

        # Generated Signals reports that named this company (research/signals_report.py,
        # via /research/thread/generate) — a comparison question naming several companies
        # shows up under every one of their Threads tabs, not just the first, since the
        # investigation is genuinely about all of them (matches the Investigations tab,
        # just filtered to one company).
        # The page is now one continuous scroll (all tabs render as stacked
        # sections — see company.html), so every section's data is fetched
        # unconditionally; `tab` only picks which section is active/scrolled-to
        # on load (for old ?tab=... bookmarks and the tab bar's initial state).
        company_threads = []
        for generated in list_generated_reports(db):
            if company_id not in generated["company_ids"]:
                continue
            other_companies = [c for c in generated["company_ids"] if c != company_id]
            meta = extract_report_meta(generated["report_markdown"])
            company_threads.append(
                {
                    "thread_id": generated["thread_id"],
                    "kicker": "Generated · also " + ", ".join(other_companies) if other_companies else "Generated",
                    "title": meta["title"] or generated["question"],
                    "question": generated["question"],
                    "confidence": meta["confidence"] or "Unknown",
                    "generated_at": generated["generated_at"],
                }
            )

        # Structured 2E-2H investigations (research/investigation.py) that
        # cover this company — looked up through the investigation_companies
        # join table (storage/investigation_repository.py), so a cross-company
        # investigation ("HDFC Bank vs ICICI Bank") appears under EVERY company
        # it names, from one shared record rather than a copy per company.
        company_investigations = []
        for inv in select_investigations_for_company(db, company_id):
            other_companies = [c for c in json.loads(inv["company_ids"] or "[]") if c != company_id]
            company_investigations.append(
                {
                    "investigation_id": inv["investigation_id"],
                    "question": inv["question"],
                    "kicker": "Deep Dive · also " + ", ".join(other_companies) if other_companies else "Deep Dive",
                    "hypothesis_count": count_investigation_hypotheses(db, inv["investigation_id"]),
                    "as_of": inv["as_of"] if "as_of" in inv.keys() else None,
                    "generated_at": inv["generated_at"],
                }
            )

        insights = None
        insights_preview = None
        insights_history = []
        all_insights = list_company_insights(db, company_id)
        if all_insights:
            latest_row = all_insights[0]
            insights = {
                "html": str(_highlight_tags(latest_row["insight_text"])),
                "generated_at": latest_row["generated_at"],
                "statement_type": latest_row["statement_type"],
            }
            # Plain-text (not tag-highlighted) excerpt for the Overview
            # tab's "Key Points" sidebar box — the full HTML version isn't
            # safe to truncate mid-tag, so this is built from the raw text
            # instead, before _highlight_tags ever runs on it.
            raw_text = latest_row["insight_text"].strip()
            insights_preview = raw_text if len(raw_text) <= 220 else raw_text[:220].rsplit(" ", 1)[0] + "…"
            insights_history = [
                {
                    "html": str(_highlight_tags(row["insight_text"])),
                    "generated_at": row["generated_at"],
                    "statement_type": row["statement_type"],
                }
                for row in all_insights[1:]
            ]

        note_attachments_by_note = list_note_attachments_for_company(db, company_id)
        notes = [
            {
                "note_id": row["note_id"],
                "html": row["note_text"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "attachments": [
                    {
                        "attachment_id": a["attachment_id"],
                        "filename": a["filename"],
                        "size_bytes": a["size_bytes"],
                        "uploaded_at": a["uploaded_at"],
                        "url": f"/companies/{company_id}/notes/{row['note_id']}/attachments/{a['attachment_id']}/file",
                    }
                    for a in note_attachments_by_note.get(row["note_id"], [])
                ],
            }
            for row in list_company_notes(db, company_id)
        ]

        latest_price = _latest_price(valuation_model_file) if valuation_model_file else None
        live_quote = get_live_quote(
            # nse_symbol only exists for Indian companies; a non-Indian
            # company (no NSE/BSE identifiers at all) uses its own
            # company_id as the yfinance ticker instead — see
            # web/live_quote.py and cmd_ingest_yfinance's own convention.
            company["nse_symbol"] or (company_id if company["country"] != "IN" else None),
            company["country"],
        )
        # Same resolved price the page header already shows (live_quote,
        # falling back to the ported dataset's own last price) — passed to
        # the Overview ratio grid (valuation_dashboard.js) as a data
        # attribute, since the live-computed feed (web/valuation_feed.py)
        # deliberately never populates price itself (no market-data
        # pipeline; see that module's docstring).
        overview_price = live_quote["price"] if live_quote else latest_price
        shares_outstanding_entry = list_latest_shares_outstanding(db).get(company_id)
        shares_outstanding = shares_outstanding_entry[0] if shares_outstanding_entry else None
        # Bare year (e.g. 2014, not "FY2014") — matches the plain-int
        # fiscal-year convention web/valuation_feed.py's own YEARS array
        # already uses, so valuation_dashboard.js's RATIO_CATALOG can show
        # "No. Equity Shares, FY2014" / "Market Cap, FY2014" the same way it
        # already suffixes Net Profit/Revenue with their own fiscal year —
        # visible, not just a hover tooltip, so a stale share count reads as
        # stale everywhere its number appears, not just on the Companies list.
        shares_outstanding_fy = int(shares_outstanding_entry[1][2:]) if shares_outstanding_entry else None
        # Which ratio-grid rows an admin has enabled (Admin -> Overview
        # Ratios) — the catalog itself lives in storage/repositories.py,
        # each key's compute logic in valuation_dashboard.js's RATIO_CATALOG.
        ratio_settings = get_overview_ratio_settings(db)
        enabled_ratio_keys = [r["key"] for r in OVERVIEW_RATIO_CATALOG if ratio_settings[r["key"]]]

        # Indicators (indicators/*.py) — deterministic, rule-based factual
        # patterns, recomputed on every view from already-normalized facts
        # (no LLM call on this path at all) under this user's own rule
        # configuration. Persists an audit row per newly-triggered/changed
        # indicator; see indicators/evaluation.py's persistence policy.
        indicators_by_class = group_by_classification(
            evaluate_company_indicators(
                db, company_id, user_id=g.user["user_id"] if g.user is not None else None
            )
        )
        indicator_columns = [
            {
                "key": key,
                "label": CLASSIFICATION_LABELS[key],
                "items": indicators_by_class.get(key, []),
                # Real-estate convention (collapse into <details>, auto-open
                # only on active state): only Warnings force themselves open,
                # and only when something actually fired.
                "open": key == "warning" and bool(indicators_by_class.get(key)),
            }
            for key in CLASSIFICATIONS
        ]
        indicator_total = sum(len(c["items"]) for c in indicator_columns)

        return render_template(
            "company.html",
            company=company,
            company_id=company_id,
            tab=tab,
            statement_type=statement_type,
            website_display=website_display,
            nse_url=nse_url,
            bse_url=bse_url,
            nse_index_tags=nse_index_tags,
            bse_index_tags=bse_index_tags,
            other_index_tags=other_index_tags,
            latest_price=latest_price,
            live_quote=live_quote,
            overview_price=overview_price,
            shares_outstanding=shares_outstanding,
            shares_outstanding_fy=shares_outstanding_fy,
            enabled_ratio_keys=enabled_ratio_keys,
            is_watchlisted=is_watchlisted(db, "company", company_id),
            has_ported_dataset=has_ported_dataset,
            has_live_financials=has_live_financials,
            valuation_data_url=valuation_data_url,
            financials_data_url=financials_data_url,
            docs_data_url=url_for("company_docs_feed", company_id=company_id),
            shareholding_data_url=url_for("company_shareholding_feed", company_id=company_id),
            corporate_actions_data_url=url_for("company_corporate_actions_feed", company_id=company_id),
            insights=insights,
            insights_preview=insights_preview,
            insights_history=insights_history,
            notes=notes,
            company_threads=company_threads,
            company_investigations=company_investigations,
            indicator_columns=indicator_columns,
            indicator_total=indicator_total,
            api_key_set=ANTHROPIC_API_KEY_SET,
            # Only worth computing for the admin who'll actually see the
            # "Run now" button next to it (company.html gates both on
            # g.user.is_admin) -- two more small, indexed lookups on every
            # anonymous/non-admin page view otherwise.
            run_now_status=run_now_status(company) if g.user and g.user["is_admin"] else None,
        )

    @app.route("/companies/<company_id>/insights/generate", methods=["POST"])
    def company_generate_insights(company_id: str):
        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        statement_type = request.get_json(silent=True, force=True) or {}
        statement_type = statement_type.get("statement_type", "consolidated")
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        try:
            insight_text = generate_key_insights(db, company_id, statement_type=statement_type)
        except NoDataToSummarizeError as exc:
            return jsonify(error=str(exc)), 400
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        save_company_insights(db, company_id, insight_text, statement_type)
        row = get_company_insights(db, company_id)
        return jsonify(
            insight_html=str(_highlight_tags(row["insight_text"])),
            generated_at=row["generated_at"],
            statement_type=row["statement_type"],
        )

    @app.route("/companies/<company_id>/notes/add", methods=["POST"])
    def company_add_note(company_id: str):
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        body = request.get_json(silent=True, force=True) or {}
        note_html = sanitize_note_html(body.get("html") or "")
        if _is_blank_note_html(note_html):
            return jsonify(error="Note can't be empty."), 400

        row = save_company_note(db, company_id, note_html)
        return jsonify(note_id=row["note_id"], html=row["note_text"], created_at=row["created_at"], attachments=[])

    @app.route("/companies/<company_id>/notes/<int:note_id>/edit", methods=["POST"])
    def company_edit_note(company_id: str, note_id: int):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        body = request.get_json(silent=True, force=True) or {}
        note_html = sanitize_note_html(body.get("html") or "")
        if _is_blank_note_html(note_html):
            return jsonify(error="Note can't be empty."), 400

        row = update_company_note(db, company_id, note_id, note_html)
        if row is None:
            abort(404, f"No note {note_id} for company_id={company_id!r}")
        return jsonify(note_id=row["note_id"], html=row["note_text"], created_at=row["created_at"], updated_at=row["updated_at"])

    @app.route("/companies/<company_id>/notes/<int:note_id>/delete", methods=["POST"])
    def company_delete_note(company_id: str, note_id: int):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        if not delete_company_note(db, company_id, note_id):
            abort(404, f"No note {note_id} for company_id={company_id!r}")
        return jsonify(ok=True)

    _NOTE_ATTACHMENTS_DIR_NAME = "note_attachments"

    @app.route("/companies/<company_id>/notes/<int:note_id>/attachments/add", methods=["POST"])
    def company_add_note_attachment(company_id: str, note_id: int):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify(error="Choose a file to attach."), 400
        filename = secure_filename(upload.filename)
        if not filename:
            return jsonify(error="That filename isn't valid."), 400

        # data/documents/<COMPANY>/note_attachments/<timestamp>__<file> —
        # same never-overwrite convention as company_add_document. Routed
        # through the active DocumentStore (storage/document_store.py)
        # rather than upload.save() directly, so this works unchanged
        # whether DOCUMENT_STORE_BACKEND is "local" (default, identical
        # on-disk behaviour) or "s3".
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_dir = app_settings.DOCUMENTS_DIR / company_id / _NOTE_ATTACHMENTS_DIR_NAME
        key = to_repo_relative(dest_dir / f"{stamp}__{filename}")
        content = upload.read()
        storage_key = default_document_store().store(key, content)
        size_bytes = len(content)

        row = save_note_attachment(db, note_id, filename, storage_key, size_bytes)
        return jsonify(
            attachment_id=row["attachment_id"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            uploaded_at=row["uploaded_at"],
            url=url_for(
                "company_note_attachment_file", company_id=company_id, note_id=note_id, attachment_id=row["attachment_id"]
            ),
        )

    @app.route("/companies/<company_id>/notes/<int:note_id>/attachments/<int:attachment_id>/file")
    def company_note_attachment_file(company_id: str, note_id: int, attachment_id: int):
        db = get_db()
        row = get_note_attachment(db, note_id, attachment_id)
        if row is None:
            abort(404)
        # Mixed-mode during migration: a presigned URL when the active
        # backend can produce one (S3), falling back to today's send_file
        # for a document still only on local disk (LocalDocumentStore's
        # presigned_url() always returns None, same as before this routed
        # through DocumentStore).
        store = default_document_store()
        url = store.presigned_url(row["raw_file_path"])
        if url:
            return redirect(url)
        return send_file(from_repo_relative(row["raw_file_path"]), download_name=row["filename"])

    @app.route("/companies/<company_id>/notes/<int:note_id>/attachments/<int:attachment_id>/delete", methods=["POST"])
    def company_delete_note_attachment(company_id: str, note_id: int, attachment_id: int):
        db = get_db()
        row = delete_note_attachment(db, note_id, attachment_id)
        if row is None:
            abort(404)
        default_document_store().delete(row["raw_file_path"])
        return jsonify(ok=True)

    @app.route("/companies/<company_id>/valuation-feed.json")
    def company_valuation_feed(company_id: str):
        statement_type = request.args.get("statement_type", "consolidated")
        if statement_type not in ("consolidated", "standalone"):
            abort(400, "statement_type must be 'consolidated' or 'standalone'")
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(build_valuation_feed(db, company_id, statement_type=statement_type))

    @app.route("/companies/<company_id>/charts-feed.json")
    def company_charts_feed(company_id: str):
        statement_type = request.args.get("statement_type", "consolidated")
        if statement_type not in ("consolidated", "standalone"):
            abort(400, "statement_type must be 'consolidated' or 'standalone'")
        period_type = request.args.get("period_type", "annual")
        if period_type not in ("annual", "quarterly"):
            abort(400, "period_type must be 'annual' or 'quarterly'")
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(
            build_charts_feed(
                db, company_id, statement_type=statement_type, period_type=period_type, price_conn=get_price_db()
            )
        )

    @app.route("/companies/<company_id>/price-feed.json")
    def company_price_feed(company_id: str):
        period = request.args.get("period", "1y")
        period_days = {"1y": 366, "5y": 1827, "10y": 3653}
        if period not in period_days and period != "max":
            abort(400, "period must be one of '1y', '5y', '10y', 'max'")
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        start_date = "1990-01-01" if period == "max" else (
            datetime.now(timezone.utc) - timedelta(days=period_days[period])
        ).strftime("%Y-%m-%d")
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        price_db = get_price_db()
        rows = get_price_history(price_db, company_id, start_date, end_date)
        return jsonify(
            {
                "dates": [r["trade_date"] for r in rows],
                "open": [r["open"] for r in rows],
                "high": [r["high"] for r in rows],
                "low": [r["low"] for r in rows],
                "close": [r["close"] for r in rows],
                "volume": [r["volume"] for r in rows],
            }
        )

    @app.route("/companies/<company_id>/docs-feed.json")
    def company_docs_feed(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(build_docs_feed(db, company_id))

    @app.route("/companies/<company_id>/shareholding-feed.json")
    def company_shareholding_feed(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(build_shareholding_feed(db, company_id))

    @app.route("/companies/<company_id>/corporate-actions-feed.json")
    def company_corporate_actions_feed(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(build_corporate_actions_feed(db, company_id))

    @app.route("/companies/<company_id>/compare-meta.json")
    def company_compare_meta(company_id: str):
        """Everything the Compare page (web/static/js/compare.js) needs
        about one company besides its financial-statement time series
        (already covered by company_charts_feed below) -- the same live
        price / shares-outstanding resolution company_report() does for its
        own Overview tab (right down to the same nse_symbol-or-company_id
        ticker convention and the same shares-outstanding staleness gate
        the Companies list uses, _is_shares_outstanding_current, since a
        confidently-wrong decade-old market cap is worse in a side-by-side
        comparison than in a single company's own page), just exposed here
        so a company *other* than the one currently on screen can be
        added/swapped into a comparison without a full page load."""
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        latest_price = _latest_price(company["valuation_model_file"]) if company["valuation_model_file"] else None
        live_quote = get_live_quote(
            company["nse_symbol"] or (company_id if company["country"] != "IN" else None),
            company["country"],
        )
        price = live_quote["price"] if live_quote else latest_price

        shares_outstanding_entry = list_latest_shares_outstanding(db).get(company_id)
        shares_outstanding = shares_outstanding_fy = None
        if shares_outstanding_entry is not None and _is_shares_outstanding_current(shares_outstanding_entry[1]):
            shares_outstanding = shares_outstanding_entry[0]
            shares_outstanding_fy = int(shares_outstanding_entry[1][2:])

        return jsonify(
            company_id=company_id,
            display_name=company["display_name"],
            nse_symbol=company["nse_symbol"],
            country=company["country"],
            currency=company["currency"],
            price=price,
            shares_outstanding=shares_outstanding,
            shares_outstanding_fy=shares_outstanding_fy,
            financials_url=url_for("company_charts_feed", company_id=company_id, statement_type="consolidated"),
        )

    @app.route("/fx/usdinr.json")
    def fx_usdinr():
        """USD/INR spot rate for the Compare page's cross-currency
        conversion footnote (web/fx_rate.py) -- a plain JSON endpoint
        rather than baking a rate into every page load, since most visits
        never compare a US company against an Indian one and shouldn't pay
        for a yfinance call they'll never use."""
        rate = get_usd_inr_rate()
        if rate is None:
            return jsonify(error="USD/INR rate unavailable right now"), 502
        return jsonify(rate)

    @app.route("/compare")
    def compare():
        """Car/product-comparison-style spec sheet -- pick up to
        COMPARE_MAX_COMPANIES companies (web/templates/compare.html), see
        them side by side across the same Overview Ratios catalog
        (OVERVIEW_RATIO_CATALOG) a company's own Overview tab already
        uses, one metric catalog reused a second time rather than a
        parallel one maintained just for this page. All the actual company
        data is fetched and rendered client-side (web/static/js/
        compare.js) via company_compare_meta()/company_charts_feed() per
        selected company -- this route only renders the page shell plus
        which ratio rows are currently enabled (the same admin setting,
        Settings > Data & Classification > Overview Ratios, the Overview
        tab itself respects, so the two never show a different metric set
        for the same company)."""
        db = get_db()
        ratio_settings = get_overview_ratio_settings(db)
        return render_template(
            "compare.html",
            ratio_catalog=[r for r in OVERVIEW_RATIO_CATALOG if ratio_settings[r["key"]]],
            search_url=url_for("companies_search"),
            # web/static/js/compare_detailed.js substitutes __ID__ itself --
            # same server-builds-the-template, client-fills-the-id
            # convention company.html's own data-compare-url-template
            # already uses for charts_overlay.js, so the route path/query
            # params live in one place (this url_for call), not hardcoded
            # a second time in JS.
            charts_url_template=url_for("company_charts_feed", company_id="__ID__", statement_type="consolidated"),
            # Same __ID__-substitution convention, for Compare's Valuation
            # Model tab (web/static/js/compare_valuation.js) -- one company
            # at a time, picked from whichever companies are currently
            # selected in Quick Comparison, reusing company_valuation_feed
            # (the same live feed the company page's own Valuation Model
            # tab calls) rather than a second valuation computation.
            valuation_url_template=url_for("company_valuation_feed", company_id="__ID__"),
        )

    @app.route("/companies/<company_id>/docs/add", methods=["POST"])
    def company_add_document(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        is_multipart = (request.content_type or "").startswith("multipart/form-data")
        data = request.form if is_multipart else (request.get_json(silent=True, force=True) or {})

        period_id = (data.get("period") or "").strip()
        type_key = (data.get("type") or "").strip()
        source = (data.get("source") or "").strip()

        document_type = KEY_TO_DOCUMENT_TYPE.get(type_key)
        if document_type is None:
            return jsonify(error=f"type must be one of {sorted(KEY_TO_DOCUMENT_TYPE)}"), 400
        try:
            fiscal_year, quarter = _parse_docs_period_id(period_id, type_key)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        raw_file_path = None
        storage_object_key = None
        content_hash = None
        source_url = None
        if source == "upload":
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                return jsonify(error="Choose a file to upload."), 400
            filename = secure_filename(upload.filename)
            if not filename:
                return jsonify(error="That filename isn't valid."), 400
            # data/documents/<COMPANY>/<timestamp>__<file> — same
            # never-overwrite, company-scoped convention admin_import_raw_file
            # uses for data/raw/, just under DOCUMENTS_DIR since these are
            # narrative documents, not financial-statement source files.
            # Routed through the active DocumentStore (storage/document_store.py)
            # rather than upload.save() directly, so this works unchanged
            # whether DOCUMENT_STORE_BACKEND is "local" (default, identical
            # on-disk behaviour) or "s3".
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest_dir = app_settings.DOCUMENTS_DIR / company_id
            key = to_repo_relative(dest_dir / f"{stamp}__{filename}")
            content = upload.read()
            storage_object_key = default_document_store().store(key, content)
            content_hash = hashlib.sha256(content).hexdigest()
            raw_file_path = storage_object_key
        elif source == "link":
            source_url = (data.get("ref") or "").strip()
            if not source_url:
                return jsonify(error="Enter a URL."), 400
        else:
            return jsonify(error="source must be 'upload' or 'link'"), 400

        added_by_user = (g.user["email"] or g.user["username"]) if g.user else "you"
        row = save_company_document(
            db,
            company_id,
            document_type=document_type,
            fiscal_year=fiscal_year,
            quarter=quarter,
            added_by_user=added_by_user,
            raw_file_path=raw_file_path,
            source_url=source_url,
            storage_object_key=storage_object_key,
            content_hash=content_hash,
        )

        # Ingest immediately in the background, rather than leaving this
        # document at processing_status='pending' until someone clicks
        # Admin -> Ingest queue's "Process All Pending" or the quarterly
        # doc_analysis scheduled job (scheduling/jobs.py) happens to run --
        # a real gap: Federal Bank's 4 manually-uploaded documents sat
        # un-ingested (no chunks, no Qdrant vectors) for as long as neither
        # of those had run, so Ask AI had nothing but the one
        # officially-sourced document to answer from. Reuses
        # process_documents() unchanged (same knowledge_builder +
        # chunk_indexer workers, same batch_job_runs/batch_job_items audit
        # trail) -- just triggered per-upload instead of only from those
        # two existing entry points. Backgrounded (own DB connection, same
        # "a connection can't cross threads" shape as admin_schedule_run_
        # async and the -async ask/investigate routes) so a slow LLM
        # extraction call never makes the upload response itself wait or
        # risk the platform's own gateway timeout.
        document_id = row["document_id"]

        def _ingest_in_background(document_id: int = document_id) -> None:
            conn = scheduling_open_db()
            try:
                process_documents(conn, [document_id])
            except Exception:
                logger.exception("Background ingestion failed for document %s", document_id)
            finally:
                conn.close()

        threading.Thread(target=_ingest_in_background, daemon=True).start()

        return jsonify(
            document_id=row["document_id"],
            fiscal_year=row["fiscal_year"],
            quarter=row["quarter"],
            added_by_user=row["added_by_user"],
            source_url=row["source_url"],
            file_url=f"/companies/{company_id}/docs/{row['document_id']}/file" if row["raw_file_path"] else None,
            period_id=period_id,
            type_key=type_key,
        )

    @app.route("/companies/<company_id>/docs/<int:document_id>/file")
    def company_document_file(company_id: str, document_id: int):
        db = get_db()
        row = get_company_document(db, company_id, document_id)
        if row is None or not (row["raw_file_path"] or row["storage_object_key"]):
            abort(404)
        # Mixed-mode during migration: a presigned URL when the active
        # backend can produce one (S3), falling back to today's send_file
        # for a document still only on local disk (LocalDocumentStore's
        # presigned_url() always returns None, same as before this routed
        # through DocumentStore). A document with only storage_object_key
        # (no raw_file_path at all -- e.g. one uploaded straight to S3,
        # never staged on local disk) has no local-disk fallback to send,
        # so it depends on the active backend producing a presigned URL.
        key = row["storage_object_key"] or row["raw_file_path"]
        store = default_document_store()
        url = store.presigned_url(key)
        if url:
            return redirect(url)
        if not row["raw_file_path"]:
            abort(404)
        return send_file(from_repo_relative(row["raw_file_path"]))

    def _safe_login_next() -> str:
        """Same defense-in-depth as watchlist's _safe_next() — `next` is our own
        query/form param, but only a same-site path is ever honored."""
        next_url = request.values.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return url_for("home")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if g.user is not None:
            return redirect(url_for("home"))
        if request.method == "GET":
            return render_template("signup.html", error=None, email="")

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        error = None
        if not _EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords don't match."
        elif get_user_by_email(get_db(), email) is not None:
            error = "An account with that email already exists."

        if error:
            return render_template("signup.html", error=error, email=email), 400

        user_id = create_user(get_db(), email, generate_password_hash(password))
        session.clear()
        session["user_id"] = user_id
        return redirect(url_for("home"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user is not None:
            return redirect(_safe_login_next())
        if request.method == "GET":
            return render_template("login.html", error=None, identifier="", next=request.args.get("next", ""))

        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_login(get_db(), identifier.lower() if "@" in identifier else identifier)

        if user is None or not check_password_hash(user["password_hash"], password):
            return render_template(
                "login.html", error="Incorrect email/username or password.", identifier=identifier,
                next=request.form.get("next", ""),
            ), 401

        session.clear()
        session["user_id"] = user["user_id"]
        return redirect(_safe_login_next())

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    _THEME_LABELS = [("signals", "Signals"), ("signals-light", "Signals Light"), ("schwab", "Schwab"), ("white", "White"), ("light", "Light"), ("green", "Green"), ("dark", "Dark")]

    # One entry per leaf page in the Settings left nav (web/templates/settings.html),
    # grouped there under Profile & Preferences / Research Configuration / Indicators & Data.
    _SETTINGS_PANELS = (
        "profile", "appearance", "notifications", "personal-defaults",
        "methodology", "research-defaults", "indicators",
    )
    # The Administration group's items are "admin-<this>" — kept as a
    # separate, admin-gated namespace rather than folded into
    # _SETTINGS_PANELS above, since these also need the is_admin check
    # _require_login() used to do for them via the old /admin route's
    # endpoint-name prefix (see settings() below).
    _ADMIN_SETTINGS_PANELS = (
        "companies", "taxonomy", "columns", "overview_ratios",
        "import", "stock_actions", "ingest", "schedule", "audit",
    )

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            theme = request.form.get("theme", "")
            if theme not in VALID_THEMES:
                abort(400, f"theme must be one of {sorted(VALID_THEMES)}")
            if g.user is not None:
                update_user_theme(get_db(), g.user["user_id"], theme)
            else:
                # No account to persist to — session-only, so it survives
                # this browser session but doesn't follow the person to
                # another device or outlive clearing cookies, unlike the
                # signed-in path above.
                session["theme"] = theme
            return redirect(url_for("settings", panel="appearance"))

        active_panel = request.args.get("panel", "profile")
        admin_sub = active_panel[len("admin-"):] if active_panel.startswith("admin-") else None
        if admin_sub is not None and admin_sub not in _ADMIN_SETTINGS_PANELS:
            admin_sub, active_panel = None, "profile"
        elif admin_sub is None and active_panel not in _SETTINGS_PANELS:
            active_panel = "profile"

        context = {"themes": _THEME_LABELS, **_indicator_settings_context()}
        if admin_sub is not None:
            # Mirrors the is_admin gate _require_login() applies to every
            # endpoint whose name starts with "admin" — these panels used to
            # be admin()'s own view (endpoint "admin"), but now render
            # through "settings", so that name-prefix check no longer
            # reaches them and this has to stand in for it.
            if g.user is None:
                return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
            if not g.user["is_admin"]:
                abort(403, "Admin access required")
            context.update(_build_admin_settings_context(admin_sub))

        return render_template("settings.html", active_panel=active_panel, **context)

    def _indicator_settings_context() -> dict:
        """Indicator-rule configuration is per-user by construction (user_id
        is part of every override's key), so a signed-out visitor sees the
        rule catalog with system defaults and no editing controls rather
        than someone else's configuration."""
        db = get_db()
        if g.user is None:
            return {
                "indicator_rules": [],
                "indicator_signed_out": True,
                "indicator_classifications": [],
                "indicator_families": list_families(),
                "indicator_sectors": [],
            }
        return {
            "indicator_rules": build_rules_settings(db, g.user["user_id"]),
            "indicator_signed_out": False,
            "indicator_classifications": [(c, CLASSIFICATION_LABELS[c]) for c in CLASSIFICATIONS],
            "indicator_families": list_families(),
            # Sectors only in the datalist: the sector vocabulary is a short,
            # closed list, while the company universe is ~2,500 rows and would
            # bloat every Settings render for a field that's typed, not browsed.
            "indicator_sectors": list_sectors(db),
        }

    def _require_signed_in_user():
        if g.user is None:
            abort(403, "Sign in to configure indicator rules")
        return g.user

    @app.route("/settings/indicators", methods=["POST"])
    def settings_save_indicator_rule():
        """Save one (rule, scope) override. The system rule itself is never
        touched — only this user's indicator_rule_config row."""
        user = _require_signed_in_user()
        rule = get_rule(request.form.get("rule_id", ""))
        if rule is None:
            abort(404, f"No indicator rule with rule_id={request.form.get('rule_id')!r}")
        try:
            save_rule_override(get_db(), user_id=user["user_id"], **parse_override_form(rule, request.form))
        except InvalidIndicatorConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings", panel="indicators") + "#indicator-rules")
        flash(f"Saved configuration for “{rule.name}”.", "success")
        return redirect(url_for("settings", panel="indicators") + "#indicator-rules")

    @app.route("/settings/indicators/reset", methods=["POST"])
    def settings_reset_indicator_rule():
        """Reset an override back to whatever it inherits — a DELETE of the
        override row, not a write of today's defaults."""
        user = _require_signed_in_user()
        rule = get_rule(request.form.get("rule_id", ""))
        if rule is None:
            abort(404, f"No indicator rule with rule_id={request.form.get('rule_id')!r}")
        try:
            removed = reset_rule_override(
                get_db(),
                user_id=user["user_id"],
                rule_id=rule.rule_id,
                scope_type=(request.form.get("scope_type") or "global").strip(),
                scope_value=(request.form.get("scope_value") or "").strip(),
            )
        except InvalidIndicatorConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings", panel="indicators") + "#indicator-rules")
        flash(
            f"Reset “{rule.name}” to its inherited configuration." if removed
            else f"“{rule.name}” had no override at that scope.",
            "success" if removed else "error",
        )
        return redirect(url_for("settings", panel="indicators") + "#indicator-rules")

    _DOCS_SECTIONS = ("sources", "xbrl", "research", "point_in_time", "model_routing", "macro_data", "release_notes")

    @app.route("/docs")
    def docs():
        active_section = request.args.get("section", "sources")
        if active_section not in _DOCS_SECTIONS:
            active_section = "sources"
        return render_template("docs.html", active_section=active_section)

    @app.route("/")
    def home():
        # Real, live counts rather than copy that goes stale as the dataset
        # grows — cheap single-column aggregates, fine to run on every
        # landing-page load (same reasoning as the company-count queries
        # elsewhere on this page).
        db = get_db()
        # Split by country now that non-Indian companies are tracked too —
        # a bare count(*) would silently blend the two into a number neither
        # "NSE companies" nor "US companies" honestly describes.
        # Uses a cursor + aliased column (not db.execute(...)[0]) so this
        # works against both sqlite3.Row (index or key access) and Postgres's
        # RealDictCursor rows (key access only, no integer indexing).
        # execute()/fetchone() are split (not chained) because psycopg2's
        # cursor.execute() returns None, unlike sqlite3's which returns self.
        cur = db.cursor()

        def _count(sql: str) -> int:
            cur.execute(sql)
            return cur.fetchone()["n"]

        stat_companies = _count("SELECT count(*) AS n FROM companies WHERE country = 'IN'")
        stat_us_companies = _count("SELECT count(*) AS n FROM companies WHERE country = 'US'")
        stat_sectors = _count("SELECT count(DISTINCT sector) AS n FROM companies WHERE sector IS NOT NULL")
        stat_documents = _count("SELECT count(*) AS n FROM documents WHERE processing_status = 'processed'")
        stat_claims = _count("SELECT count(*) AS n FROM knowledge_claims")
        return render_template(
            "landing.html",
            stat_companies=stat_companies, stat_us_companies=stat_us_companies, stat_sectors=stat_sectors,
            stat_documents=stat_documents, stat_claims=stat_claims,
        )

    @app.route("/about")
    def about():
        return render_template("about.html")

    @app.route("/research")
    def research():
        active_companies = [dict(c) for c in list_companies(get_db(), include_archived=False)]
        return render_template(
            "research.html", examples=EXAMPLES, companies=active_companies, api_key_set=ANTHROPIC_API_KEY_SET
        )

    class _AskRequestError(Exception):
        """Carries the same (message, http_status) the old synchronous
        _answer_question_response() used to return directly via
        jsonify(error=...), status -- raised by _compute_answer_question()
        instead now that it has two callers (a synchronous route, and a
        background thread with no response object to return early from)."""

        def __init__(self, message: str, status: int) -> None:
            super().__init__(message)
            self.status = status

    def _compute_answer_question(
        db, question: str, company_ids: list[str], *, statement_type: str, thread_id: str, thread_url: str,
        owner_id: str | None, case_id: str | None = None,
    ) -> dict:
        """The actual "ask the LLM research assistant" work, extracted out
        of the old _answer_question_response() so it can run either
        synchronously (the original /chat, /research/ask, /companies/<id>/
        ask routes, kept for compatibility) or in a background thread
        (the -async routes below, added after a real production incident:
        a broad/slow question -- or, as happened live, a company with a
        slow-to-parse uploaded PDF -- can take long enough to exceed
        gunicorn's own worker timeout *or* the platform's fronting load
        balancer's timeout, either of which kills the request out from
        under a synchronous caller and hands the browser an infrastructure
        error page instead of JSON ("Unexpected token '<' ... is not valid
        JSON") -- same root cause /investigate/generate-async was built to
        fix earlier, just a different route hitting it).

        `thread_id`/`thread_url` are generated by the caller, not here --
        url_for() needs an active request context, which a background
        thread doesn't have, so it must be resolved before the thread
        starts (see /investigate/generate-async's own comment for the
        established reasoning). `owner_id` gets the same treatment -- it's
        g.user["user_id"], and g is likewise unavailable inside a
        background thread."""
        if not ANTHROPIC_API_KEY_SET:
            raise _AskRequestError("ANTHROPIC_API_KEY is not set on the server — the assistant can't run.", 503)
        if not question:
            raise _AskRequestError("Ask a question first.", 400)
        # company_ids may be empty: a question can be grounded in Macro
        # evidence alone (research/macro_evidence.py) rather than any one
        # company's Financials/Docs — answer_question() handles that case,
        # including the "found nothing at all" message.
        if statement_type not in ("consolidated", "standalone"):
            raise _AskRequestError("statement_type must be 'consolidated' or 'standalone'", 400)

        for company_id in company_ids:
            if get_company(db, company_id) is None:
                raise _AskRequestError(f"No company registered with company_id={company_id!r}", 404)

        # Neither the client's own company detection (research.html's
        # detectCompaniesInText(), an exact-word-match regex) nor tag
        # resolution (_parse_ask_request, above this function in the call
        # chain) found anything -- last resort before treating this as a
        # company-less/macro-only question: research/company_resolver.py's
        # LLM-based resolution, which catches a natural shorthand neither
        # of those does ("IDFC Bank" for the registered "IDFC First Bank",
        # "Federal Bank" for "The Federal Bank" -- a real, observed failure
        # that silently sent a real comparison question through empty and
        # produced "no evidence found" for two companies whose financials
        # were fully ingested). Deliberately NOT done in _parse_ask_request
        # -- that runs synchronously before a case even exists, and an
        # -async route's whole point is returning a case_id immediately;
        # doing this here instead means it only ever costs time inside the
        # background thread, tracked as its own case activity, never the
        # request/response round trip.
        if not company_ids and question:
            if case_id is not None:
                update_case_activity(db, case_id, "Understanding question")
            with sentry_span("llm.anthropic", "Company resolution"):
                company_ids = resolve_companies(db, question).company_ids

        # A group question ("Nifty 50 net profit CAGR") is a sum-then-CAGR
        # arithmetic problem, not something an LLM should reason about
        # company-by-company -- a real, observed failure otherwise: handing
        # 50 companies' worth of evidence to answer_question() below
        # produced an illegible comparison chart and a non-answer, not a
        # number. research/aggregate_query.py's own docstring covers why
        # the intent-extraction call is STANDARD tier, not QUICK. Only
        # attempted for >1 company -- a single company's own metric history
        # already has a real per-share/ratio answer path, no aggregation
        # needed. Falls through to the normal answer_question() flow
        # unchanged whenever is_aggregate comes back False (most questions).
        if len(company_ids) > 1:
            aggregate_intent = extract_aggregate_intent(db, question)
            if aggregate_intent.is_aggregate:
                aggregate_result = compute_group_aggregate(db, company_ids, aggregate_intent)
                answer = format_aggregate_answer(aggregate_result, len(company_ids))
                question_embedding, question_embedding_model = _embed_question_for_reuse(question)
                save_generated_report(
                    db, thread_id, question, company_ids, statement_type, answer,
                    question_embedding=question_embedding, question_embedding_model=question_embedding_model,
                )
                _persist_generated_report_s3(db, thread_id, question, company_ids, statement_type, answer, owner_id=owner_id)
                return dict(
                    question=question,
                    company_ids=company_ids,
                    answer_html=str(_render_markdown_with_tags(answer)),
                    charts={},
                    comparison_charts={},
                    comparison_chart_company_count=0,
                    total_company_count=len(company_ids),
                    thread_id=thread_id,
                    thread_url=thread_url,
                )

        try:
            answer = answer_question(db, question, company_ids, statement_type=statement_type, case_id=case_id)
        except anthropic.APIError as exc:
            raise _AskRequestError(f"The assistant request failed: {exc}", 502) from exc
        # InsufficientEvidenceError/CaseCancelledError (only ever raised when
        # case_id is set) deliberately propagate past this try/except --
        # research.case_runner.run_case_in_background()'s own except
        # clauses are what translate those into the right research_cases
        # outcome, not this function.

        # A peer-comparison question (>1 company) gets combined, indexed-to-100
        # comparison charts instead of separate same-company charts on each
        # company's own scale — the whole point of a comparison chart is
        # putting both companies on one axis over the same overlapping period,
        # not six small charts a reader has to eyeball against each other.
        #
        # Capped independently of company_ids itself (which still goes into
        # answer_question() above at full size, e.g. all 50 Nifty 50 members
        # a tag-resolved question produces — retrieval/tag_resolver.py) --
        # a real, observed bug otherwise, not hypothetical: 50 companies on
        # one indexed-line chart is an illegible wall of overlapping colors
        # and a 50-row legend that runs off the page. compare.js's own
        # Quick/Detailed Comparison tabs already cap interactive multi-
        # company work at MAX_COMPARISONS=4 for the same legibility reason;
        # this is the equivalent cap for the one-shot chart a text answer
        # embeds, a bit more generous since there's no legend-hover
        # interactivity here to lean on.
        if case_id is not None:
            update_case_activity(db, case_id, "Building charts")

        MAX_COMPARISON_CHART_COMPANIES = 8
        comparison_charts = {}
        charts_by_company = {}
        with sentry_span("db.postgres", "Chart data (canonical_financials)"):
            if len(company_ids) > 1:
                chart_company_ids = company_ids[:MAX_COMPARISON_CHART_COMPANIES]
                comparison_charts = {
                    chart_key: figure_to_base64_png(figure)
                    for chart_key, figure in build_comparison_charts(db, chart_company_ids, statement_type=statement_type).items()
                }
            else:
                charts_by_company = {
                    company_id: {
                        chart_key: figure_to_base64_png(figure)
                        for chart_key, figure in build_company_charts(db, company_id, statement_type=statement_type).items()
                    }
                    for company_id in company_ids
                }

        if case_id is not None:
            update_case_activity(db, case_id, "Persisting result")

        question_embedding, question_embedding_model = _embed_question_for_reuse(question)
        with sentry_span("db.postgres", "save_generated_report"):
            save_generated_report(
                db, thread_id, question, company_ids, statement_type, answer,
                question_embedding=question_embedding, question_embedding_model=question_embedding_model,
            )
        with sentry_span("s3", "_persist_generated_report_s3"):
            _persist_generated_report_s3(db, thread_id, question, company_ids, statement_type, answer, owner_id=owner_id)

        return dict(
            question=question,
            company_ids=company_ids,
            answer_html=str(_render_markdown_with_tags(answer)),
            charts=charts_by_company,
            comparison_charts=comparison_charts,
            # So the UI can caption "showing 8 of 50 companies" instead of
            # silently rendering a chart with fewer lines than company_ids
            # would imply -- see MAX_COMPARISON_CHART_COMPANIES above.
            comparison_chart_company_count=len(company_ids[:MAX_COMPARISON_CHART_COMPANIES]) if comparison_charts else 0,
            total_company_count=len(company_ids),
            thread_id=thread_id,
            thread_url=thread_url,
        )

    def _parse_ask_request(company_ids: list[str] | None):
        """Common request-parsing for both the synchronous and -async ask
        routes -- returns (question, company_ids, statement_type).

        Deliberately only cheap, deterministic resolution here (tag
        matching) -- this runs synchronously, before a case even exists,
        so it must stay fast (an -async route's whole point is returning a
        case_id immediately; an LLM call here would silently undo that).
        The LLM-based fallback (research/company_resolver.py, for a
        natural shorthand tag/company_ids resolution above both missed --
        "IDFC Bank" for the registered "IDFC First Bank", "Federal Bank"
        for "The Federal Bank") runs INSIDE _compute_answer_question
        instead, as its own case activity, only when needed."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        if company_ids is None:
            company_ids = payload.get("company_ids") or []
            # Tag resolution ("Nifty 50", "Technology companies") -- same
            # mechanism /investigate/generate uses (retrieval/tag_resolver.py),
            # applied here too so "Ask"/"/chat" don't behave differently
            # from "Run structured investigation" for the identical
            # question text. Only reached via this branch, never when a
            # company_id was passed in explicitly by the caller (the
            # per-company Ask AI drawer's URL-scoped call) -- see
            # _compute_answer_question's docstring on why that path must
            # never widen past the company it was opened on.
            if not company_ids and question:
                company_ids = resolve_tags_in_text(get_db(), question)
        statement_type = payload.get("statement_type", "consolidated")
        return question, company_ids, statement_type

    def _answer_question_response(company_ids: list[str] | None = None):
        """Shared by /chat, /research/ask and /companies/<id>/ask — all three are
        "ask the LLM research assistant about these companies", just reached from
        different places (the standalone company-lookup flow, the Research tab's
        own composer, and the per-company Ask AI drawer). Same validation, same
        evidence-grounded answer, same response shape.

        Kept as the synchronous path for compatibility -- the -async routes
        below are what the actual UI calls now (see _compute_answer_question's
        docstring for why). Every call here persists into generated_reports
        (same table /research/thread/generate writes to) — so it shows up,
        timestamped, on the Investigations list and (when scoped to one
        company) that company's Threads tab, instead of vanishing once
        answered. This is also what feeds research.assistant.answer_
        question()'s own reuse-before-recompute check: the second time the
        same/near-same question comes in, it's served from this saved row
        instead of a fresh LLM call."""
        question, company_ids, statement_type = _parse_ask_request(company_ids)
        thread_id = uuid.uuid4().hex[:12]
        thread_url = url_for("research_thread", thread_id=thread_id)
        owner_id = g.user["user_id"] if g.user else None
        try:
            result = _compute_answer_question(
                get_db(), question, company_ids,
                statement_type=statement_type, thread_id=thread_id, thread_url=thread_url,
                owner_id=owner_id,
            )
        except _AskRequestError as exc:
            return jsonify(error=str(exc)), exc.status
        return jsonify(result)

    def _answer_question_async_response(company_ids: list[str] | None = None):
        """Async counterpart of _answer_question_response() -- validates
        input synchronously (fails fast on bad input, same checks as the
        sync path), creates a durable research_cases row (research/
        case_runner.py), then hands the actual _compute_answer_question()
        call to a background thread and returns immediately with a
        case_id. See _compute_answer_question's own docstring for why this
        exists, and research_cases' own schema comment (schemas/*.sql) for
        why this is a DB row and not a JSON file: it has to survive a
        browser refresh/close, a network blip, AND this process restarting
        -- "Cases is the source of truth."""
        question, company_ids, statement_type = _parse_ask_request(company_ids)

        # Same validation _compute_answer_question() would raise on, done
        # here first so a bad request fails immediately rather than after
        # a background thread (and a research_cases row) has already
        # started (mirrors /investigate/generate-async's identical
        # reasoning).
        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        if not question:
            return jsonify(error="Ask a question first."), 400
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        db = get_db()
        for company_id in company_ids:
            if get_company(db, company_id) is None:
                return jsonify(error=f"No company registered with company_id={company_id!r}"), 404

        thread_id = uuid.uuid4().hex[:12]
        thread_url = url_for("research_thread", thread_id=thread_id)
        # Resolved here, before the thread starts -- g is bound to this
        # request context and isn't available inside the background
        # thread below (same reasoning as thread_id/thread_url above).
        owner_id = g.user["user_id"] if g.user else None

        case_id = uuid.uuid4().hex[:12]
        start_case(
            db, case_id=case_id, kind="ask", question=question, company_ids=company_ids,
            statement_type=statement_type, owner_id=owner_id,
        )

        def compute(conn) -> dict:
            try:
                return _compute_answer_question(
                    conn, question, company_ids,
                    statement_type=statement_type, thread_id=thread_id, thread_url=thread_url,
                    owner_id=owner_id, case_id=case_id,
                )
            except _AskRequestError as exc:
                # Reached only via a race (e.g. a company archived between
                # the pre-check above and the background thread running) --
                # translated to a plain exception so run_case_in_background's
                # generic handler marks the case failed with a readable
                # message, same "technical failure" bucket, not
                # insufficient_data (that's reserved for InsufficientEvidenceError).
                raise RuntimeError(str(exc)) from exc

        run_case_in_background(scheduling_open_db, case_id, "ask", compute)
        # job_id kept for the already-deployed frontend's own polling code
        # (ctx.askAsyncUrl callers do `pollAskStatus(data.job_id)`) --
        # case_id is the same value, just the name new/Cases-aware callers
        # should use going forward.
        return jsonify(job_id=case_id, case_id=case_id), 202

    def _case_status_payload(case) -> dict:
        """Translates one research_cases row into the JSON shape /ask/
        status/<job_id> returns -- {status: running|done|error, ...} is
        the contract _ask_ai.html/chat.html/research.html's pollAskStatus()
        already polls against (unchanged, so the already-deployed frontend
        keeps working); current_activity/elapsed_seconds/case_id are new,
        additive fields for the Cases list/detail UI to use without
        breaking any existing caller that ignores them."""
        started = datetime.fromisoformat(case["started_at"])
        # A terminal case's elapsed time is frozen at how long it actually
        # took (completed_at - started_at), not "how long ago it finished"
        # -- using `now` unconditionally here was a real bug: polling a
        # case minutes after it already completed reported an ever-growing
        # "elapsed_seconds" that had nothing to do with its real processing
        # time.
        if case["status"] == "in_progress":
            reference = datetime.now(started.tzinfo) if started.tzinfo else datetime.utcnow()
        else:
            reference = datetime.fromisoformat(case["completed_at"]) if case["completed_at"] else started
        elapsed_seconds = max(0.0, (reference - started).total_seconds())
        payload = {
            "case_id": case["case_id"],
            "current_activity": case["current_activity"],
            "elapsed_seconds": round(elapsed_seconds, 1),
        }
        if case["status"] == "in_progress":
            payload["status"] = "running"
            return payload
        if case["status"] == "failed":
            payload["status"] = "error"
            payload["error"] = case["error_message"] or "Something went wrong."
            return payload
        if case["status"] == "cancelled":
            payload["status"] = "error"
            payload["error"] = "This request was cancelled."
            return payload

        # status == "completed"
        company_ids = json.loads(case["company_ids"] or "[]")
        if case["outcome"] == "insufficient_data":
            raw = json.loads(case["result_json"]) if case["result_json"] else {}
            message = raw.get("message", "Not enough data was found to answer this question.")
            result = {
                "question": case["question"],
                "company_ids": company_ids,
                "answer_html": str(_render_markdown_with_tags(message)),
                "charts": {},
                "comparison_charts": {},
                "comparison_chart_company_count": 0,
                "total_company_count": len(company_ids),
                "thread_id": None,
                "thread_url": None,
            }
        else:
            result = json.loads(case["result_json"]) if case["result_json"] else {}
        payload["status"] = "done"
        payload["outcome"] = case["outcome"]
        payload["result"] = result
        return payload

    @app.route("/ask/status/<job_id>")
    def ask_status(job_id: str):
        """Polled by _ask_ai.html/chat.html/research.html after their own
        -async route returns a job_id (== case_id -- see
        _answer_question_async_response's own comment). A 404 (unknown
        job_id) is deliberately distinct from a real "error" status --
        same conventions as /investigate/status."""
        case = get_research_case(get_db(), job_id)
        if case is None:
            abort(404)
        return jsonify(_case_status_payload(case))

    @app.route("/cases/<case_id>/cancel", methods=["POST"])
    def case_cancel(case_id: str):
        """Cooperative cancellation -- see schemas/*.sql's research_cases
        docstring and research/assistant.py's CaseCancelledError. Returns
        404 for an unknown case_id, 200 either way otherwise (whether or
        not it was actually in_progress at the moment this ran -- a case
        that finished a beat earlier isn't an error, just a no-op)."""
        db = get_db()
        if get_research_case(db, case_id) is None:
            abort(404)
        request_case_cancellation(db, case_id)
        return jsonify(ok=True)

    @app.route("/cases/<case_id>")
    def case_detail(case_id: str):
        """Case detail/reconnect page -- opening this for an in_progress
        case shows its current activity and keeps polling (same
        pollAskStatus() mechanism the Ask AI drawer already uses, just
        pointed at whichever status endpoint/payload shape matches this
        case's kind -- see _case_status_payload/_investigation_case_status_
        payload); a completed case redirects straight to its real result
        (the generated_reports thread for kind='ask', the /investigate/<id>
        page for kind='investigation' -- or, for an outcome='insufficient_
        data' case, which never got either, rendered inline here instead)."""
        db = get_db()
        case = get_research_case(db, case_id)
        if case is None:
            abort(404)
        if case["status"] == "completed" and case["outcome"] == "answered":
            if case["kind"] == "investigation" and case["investigation_id"]:
                return redirect(url_for("investigate_view", investigation_id=case["investigation_id"]))
            if case["kind"] != "investigation" and case["thread_id"]:
                return redirect(url_for("research_thread", thread_id=case["thread_id"]))
        if case["kind"] == "investigation":
            status_url = url_for("investigate_status", investigation_id=case_id)
            initial_status = _investigation_case_status_payload(case)
        else:
            status_url = url_for("ask_status", job_id=case_id)
            initial_status = _case_status_payload(case)
        return render_template(
            "case_detail.html", case_id=case_id, question=case["question"], kind=case["kind"],
            company_ids=json.loads(case["company_ids"] or "[]"),
            status_url=status_url, initial_status=initial_status,
        )

    @app.route("/research/understand", methods=["POST"])
    def research_understand():
        """Called by research.html as soon as the user pauses typing (or
        before Ask/Investigate, whichever fires first) -- resolves which
        companies the question is actually about (tags first, e.g. "Nifty
        50", cheap and deterministic; research/company_resolver.py's
        LLM-based resolution as the fallback for an individual company
        named informally -- "IDFC Bank" for the registered "IDFC First
        Bank" -- neither the client's own old regex matching nor tag
        resolution catches that), and suggests Quick Answer vs Deep Dive
        via llm/hardness.py's classify() (unchanged, existing heuristic --
        DEEP for a peer comparison or "why"/"compare"/"versus"-shaped
        question, otherwise Quick).

        Returns a SUGGESTION, never a decision the caller is forced into
        -- research.html's own toggle starts on whichever this names, but
        the user can click the other option before submitting; nothing
        here creates a case or spends more than this one resolution call
        (no chart building, no full answer/investigation)."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        if not question:
            return jsonify(error="Ask a question first."), 400

        db = get_db()
        company_ids = resolve_tags_in_text(db, question)
        if not company_ids:
            company_ids = resolve_companies(db, question).company_ids

        companies = [get_company(db, company_id) for company_id in company_ids]
        company_labels = [c["display_name"] for c in companies if c is not None]

        hardness = classify_hardness(question, company_ids, evidence_count=0)
        suggested_case_type = "deep" if hardness.tier == Tier.DEEP else "quick"

        return jsonify(
            company_ids=company_ids,
            company_labels=company_labels,
            suggested_case_type=suggested_case_type,
            suggestion_reason=hardness.reason,
        )

    @app.route("/research/ask", methods=["POST"])
    def research_ask():
        return _answer_question_response()

    @app.route("/research/ask-async", methods=["POST"])
    def research_ask_async():
        return _answer_question_async_response()

    @app.route("/companies/<company_id>/ask", methods=["POST"])
    def company_ask(company_id: str):
        """Backs the Ask AI drawer (web/templates/_ask_ai.html), which every
        company page and company-list row can open. Scope comes from the path
        rather than a picker, so the drawer needs no company selection step at
        all — it already knows which company it was opened on. Every answer
        here is auto-saved as a thread (save_thread=True) so it lands on the
        company's Threads tab, timestamped and deletable — unlike /research/ask
        and /chat, which stay ephemeral. Kept as the synchronous path for
        compatibility -- the drawer's own JS calls company_ask_async below now."""
        return _answer_question_response(company_ids=[company_id])

    @app.route("/companies/<company_id>/ask-async", methods=["POST"])
    def company_ask_async(company_id: str):
        return _answer_question_async_response(company_ids=[company_id])

    @app.route("/research/thread/generate", methods=["POST"])
    def research_thread_generate():
        """Generate a full Signals-format report (research/signals_report.py) for a
        question and stash it under a new thread_id, so it gets a shareable
        /research/thread/<id> URL the same as the example investigations — just
        generated on demand from live evidence instead of hand-written."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        company_ids = payload.get("company_ids") or []
        # Tag resolution -- same mechanism as /research/ask and
        # /investigate/generate (retrieval/tag_resolver.py), so "Generate
        # full report" doesn't reject a tag-only question ("Nifty 50
        # companies...") with "Select at least one company" the way the
        # other two flows used to before this was wired in everywhere.
        if not company_ids and question:
            company_ids = resolve_tags_in_text(get_db(), question)
        statement_type = payload.get("statement_type", "consolidated")

        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        if not question:
            return jsonify(error="Ask a question first."), 400
        if not company_ids:
            return jsonify(error="Select at least one company."), 400
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        db = get_db()
        for company_id in company_ids:
            if get_company(db, company_id) is None:
                return jsonify(error=f"No company registered with company_id={company_id!r}"), 404

        # Same deterministic short-circuit as /research/ask -- see that
        # route's own comment (web/app.py's _answer_question_response) for
        # why a group aggregate question never reaches generate_signals_report.
        if len(company_ids) > 1:
            aggregate_intent = extract_aggregate_intent(db, question)
            if aggregate_intent.is_aggregate:
                aggregate_result = compute_group_aggregate(db, company_ids, aggregate_intent)
                answer = format_aggregate_answer(aggregate_result, len(company_ids))
                thread_id = uuid.uuid4().hex[:12]
                question_embedding, question_embedding_model = _embed_question_for_reuse(question)
                save_generated_report(
                    db, thread_id, question, company_ids, statement_type, answer,
                    question_embedding=question_embedding, question_embedding_model=question_embedding_model,
                )
                _persist_generated_report_s3(
                    db, thread_id, question, company_ids, statement_type, answer,
                    owner_id=g.user["user_id"] if g.user else None,
                )
                return jsonify(thread_id=thread_id, url=url_for("research_thread", thread_id=thread_id))

        try:
            result = generate_signals_report(db, question, company_ids, statement_type=statement_type)
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        thread_id = uuid.uuid4().hex[:12]
        question_embedding, question_embedding_model = _embed_question_for_reuse(question)
        save_generated_report(
            db, thread_id, question, company_ids, statement_type, result.report_markdown,
            question_embedding=question_embedding, question_embedding_model=question_embedding_model,
        )
        evidence_dicts = [
            {"kind": e.kind, "company_id": e.company_id, "label": e.label, "value": e.value, "citation": e.citation}
            for e in result.evidence
        ]
        if result.evidence:
            save_report_evidence(db, thread_id, evidence_dicts)
        if result.followups:
            save_report_followups(db, thread_id, result.followups)
        _persist_generated_report_s3(
            db, thread_id, question, company_ids, statement_type, result.report_markdown,
            evidence=evidence_dicts, followups=result.followups,
            owner_id=g.user["user_id"] if g.user else None,
        )
        return jsonify(thread_id=thread_id, url=url_for("research_thread", thread_id=thread_id))

    @app.route("/research/thread/<thread_id>")
    def research_thread(thread_id: str):
        db = get_db()
        generated = get_generated_report(db, thread_id)
        if generated is not None:
            # ADR-021: prefer the S3 artifact when present (every report
            # saved since the persistence split) -- report_markdown/
            # research_thread_evidence/research_thread_followups stay
            # populated too (see storage/database.py's
            # _migrate_generated_reports_s3_columns for why), so this is a
            # belt-and-suspenders read, not a required fallback the way
            # investigate_view()'s is -- but reading from S3 here keeps
            # the two entities' render logic consistent, and proves the
            # artifact is genuinely the thing served, not just written and
            # never read.
            #
            # A real gap found live (2026-09-17): a handful of rows carry
            # an s3_key whose object no longer exists in the bucket -- this
            # used to be an unhandled DocumentStoreError -> unstyled 500,
            # even though the Postgres columns below are the exact same
            # "belt-and-suspenders" copy this comment already described.
            # Fall back to them instead of crashing.
            report_markdown = report_evidence = report_followups = None
            if generated["s3_key"]:
                try:
                    artifact = json.loads(default_document_store().retrieve(generated["s3_key"]))
                    report_markdown = artifact["report_markdown"]
                    report_evidence = artifact["evidence"]
                    report_followups = artifact["followups"]
                except DocumentStoreError:
                    logger.warning(
                        "Thread %s: s3_key=%r unreadable, falling back to Postgres copy",
                        thread_id, generated["s3_key"],
                    )
            if report_markdown is None:
                report_markdown = generated["report_markdown"]
                report_evidence = list_report_evidence(db, thread_id)
                report_followups = list_report_followups(db, thread_id)
            return render_template(
                "research_thread.html",
                thread_id=thread_id,
                generated=generated,
                report_html=_render_markdown_with_tags(report_markdown),
                report_evidence=report_evidence,
                report_followups=report_followups,
                is_watchlisted=is_watchlisted(db, "thread", thread_id),
            )

        thread = THREADS.get(thread_id)
        if thread is None:
            abort(404, f"No example investigation with id={thread_id!r}")
        chart_data_a, chart_data_b = thread["chart_data"]
        return render_template(
            "research_thread.html",
            thread_id=thread_id,
            thread=thread,
            confidence_label=f"{thread['confidence']} confidence",
            chart=_chart_points(chart_data_a, chart_data_b),
            is_watchlisted=is_watchlisted(get_db(), "thread", thread_id),
        )

    @app.route("/research/thread/<thread_id>/delete", methods=["POST"])
    def research_thread_delete(thread_id: str):
        """Delete a generated report (Ask AI auto-saved threads and
        /research/thread/generate reports alike). The 3 hand-written example
        investigations in THREADS aren't DB rows, so they simply 404 here —
        there's nothing to delete."""
        db = get_db()
        if not delete_generated_report(db, thread_id):
            abort(404, f"No generated thread with id={thread_id!r}")
        remove_watchlist_item(db, "thread", thread_id)
        return jsonify(ok=True)

    @app.route("/cases/<case_type>/<case_id>/hide", methods=["POST"])
    def case_hide(case_type: str, case_id: str):
        """Toggles hidden_at on/off for one Cases-list entry (either table --
        case_type is "generated" or "structured", matching the same values
        the entry dicts in investigations() already use). Reversible by
        design: the button that posts here relabels itself Hide/Unhide
        based on current state, so this reads the row first rather than
        taking a fixed direction as a form field."""
        db = get_db()
        if case_type == "generated":
            report = get_generated_report(db, case_id)
            if report is None:
                abort(404, f"No case with id={case_id!r}")
            (unhide_generated_report if report["hidden_at"] else hide_generated_report)(db, case_id)
        elif case_type == "structured":
            inv = get_investigation(db, case_id)
            if inv is None:
                abort(404, f"No case with id={case_id!r}")
            (unhide_investigation if inv["hidden_at"] else hide_investigation)(db, case_id)
        else:
            abort(400, f"Unknown case_type: {case_type!r}")
        return _redirect_to_return_or("investigations")

    @app.route("/cases/<case_type>/<case_id>/delete", methods=["POST"])
    def case_delete(case_type: str, case_id: str):
        """"Archived forever" -- see soft_delete_generated_report/
        soft_delete_investigation's own docstrings: deleted_at is set, the
        row is never actually removed, and no route/button anywhere clears
        it back. Deliberately not the same as research_thread_delete's real
        DELETE above -- that pre-existing hard-delete path (the individual
        thread page's own Delete action) is untouched."""
        db = get_db()
        if case_type == "generated":
            ok = soft_delete_generated_report(db, case_id)
        elif case_type == "structured":
            ok = soft_delete_investigation(db, case_id)
        else:
            abort(400, f"Unknown case_type: {case_type!r}")
        if not ok:
            abort(404, f"No case with id={case_id!r}")
        return _redirect_to_return_or("investigations")

    @app.route("/investigate/generate", methods=["POST"])
    def investigate_generate():
        """Run the Steps 2E-2H hypothesis-driven investigation
        (research/investigation.py::run_investigation) for a question:
        generate competing hypotheses, gather evidence for each, evaluate
        each independently, then rank/synthesize. Distinct from
        /research/thread/generate's single-narrative Signals report — this
        persists several individually-evaluated hypotheses
        (investigations/investigation_hypotheses/investigation_hypothesis_evidence),
        not one markdown blob, with its own /investigate/<id> view."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        company_ids = payload.get("company_ids") or []
        # Tag resolution ("Nifty 50", "Technology companies") -- only when
        # nothing was already explicitly picked (see retrieval/
        # tag_resolver.py's own docstring for why a tag mention alongside
        # an explicit selection is left alone rather than widening it).
        if not company_ids and question:
            company_ids = resolve_tags_in_text(get_db(), question)
        statement_type = payload.get("statement_type", "consolidated")
        # Optional point-in-time cutoff (research/temporal.py) — a "could this
        # have been detected at the time?" question runs with every evidence
        # capability restricted to what was on file on that date.
        as_of = (payload.get("as_of") or "").strip() or None

        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        if not question:
            return jsonify(error="Ask a question first."), 400
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        db = get_db()
        for company_id in company_ids:
            if get_company(db, company_id) is None:
                return jsonify(error=f"No company registered with company_id={company_id!r}"), 404

        try:
            investigation = run_investigation(
                db, question, company_ids, statement_type=statement_type, as_of=as_of
            )
        except InvestigationError as exc:
            return jsonify(error=f"The investigation couldn't complete: {exc}"), 502
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        return jsonify(
            investigation_id=investigation.investigation_id,
            url=url_for("investigate_view", investigation_id=investigation.investigation_id),
        )

    @app.route("/investigate/generate-async", methods=["POST"])
    def investigate_generate_async():
        """Async counterpart of investigate_generate above, for the same
        reason admin_schedule_run_async exists alongside admin_schedule_run:
        a broad, multi-hypothesis investigation (several evidence-gathering
        + evaluation passes, each its own LLM round trip) can easily run
        past gunicorn's --timeout 120 (Dockerfile) or the platform's own
        fronting load balancer timeout — when that happens mid-request, the
        browser gets back an infrastructure error page instead of JSON,
        which research.html's generateInvestigation() surfaced as
        "Network error: Unexpected token '<' ... is not valid JSON" (the
        request never actually failed on this app's own terms — it was cut
        off from outside).

        Does the same synchronous validation investigate_generate does
        (bad input should fail fast, not after a background thread has
        already started), then hands the actual run_investigation() call to
        a background thread — same "own db connection, since a connection
        can't cross threads" shape admin_schedule_run_async uses — and
        returns the investigation_id immediately so the page can poll
        investigate_status below instead of blocking one HTTP request on
        however long the whole thing takes."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        company_ids = payload.get("company_ids") or []
        if not company_ids and question:
            company_ids = resolve_tags_in_text(get_db(), question)
        statement_type = payload.get("statement_type", "consolidated")
        as_of = (payload.get("as_of") or "").strip() or None

        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        if not question:
            return jsonify(error="Ask a question first."), 400
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        db = get_db()
        for company_id in company_ids:
            if get_company(db, company_id) is None:
                return jsonify(error=f"No company registered with company_id={company_id!r}"), 404

        # investigation_id doubles as case_id -- one id, not two, since
        # nothing about this pipeline needs them to differ (unlike the Ask
        # AI case/thread_id split, where a case can also complete WITHOUT a
        # thread -- outcome='insufficient_data'). Same "own db connection,
        # since a connection can't cross threads" shape every other -async
        # route uses; research_cases (schemas/*.sql) is the durable, DB-
        # backed record now -- see that table's own docstring for why this
        # replaced the earlier per-job JSON file (research/investigation_
        # jobs.py, now unused for this route) -- it has to survive a
        # browser refresh/close, a network blip, AND this process
        # restarting, none of which a disposable file on one gunicorn
        # worker's local disk could promise.
        investigation_id = uuid.uuid4().hex[:12]
        # Built here, inside this real request's context, not inside the
        # background thread below -- url_for needs an active request (or
        # app) context to resolve SERVER_NAME/APPLICATION_ROOT, which a bare
        # background thread doesn't have. investigation_id is already fixed
        # at this point, so the URL it produces is valid regardless of how
        # the run underneath it turns out.
        result_url = url_for("investigate_view", investigation_id=investigation_id)
        owner_id = g.user["user_id"] if g.user else None

        start_case(
            db, case_id=investigation_id, kind="investigation", question=question, company_ids=company_ids,
            statement_type=statement_type, owner_id=owner_id,
        )

        def compute(conn) -> dict:
            try:
                run_investigation(
                    conn, question, company_ids, statement_type=statement_type, as_of=as_of,
                    investigation_id=investigation_id, case_id=investigation_id,
                )
            except InvestigationError as exc:
                raise RuntimeError(f"The investigation couldn't complete: {exc}") from exc
            except anthropic.APIError as exc:
                raise RuntimeError(f"The assistant request failed: {exc}") from exc
            return {"investigation_id": investigation_id, "url": result_url}

        run_case_in_background(scheduling_open_db, investigation_id, "investigation", compute)
        return jsonify(investigation_id=investigation_id, case_id=investigation_id), 202

    def _investigation_case_status_payload(case) -> dict:
        """Same {status, url}/{status, error} contract research.html's
        pollInvestigationStatus() already polls against (unchanged, so the
        already-deployed frontend keeps working) -- current_activity/
        elapsed_seconds are new, additive fields for the Cases detail page
        to use. Mirrors web/app.py's own _case_status_payload() (the Ask AI
        equivalent) but returns `url` instead of `result` on completion,
        since an investigation's real result is its own /investigate/<id>
        page, not something to render inline."""
        started = datetime.fromisoformat(case["started_at"])
        # A terminal case's elapsed time is frozen at how long it actually
        # took (completed_at - started_at), not "how long ago it finished"
        # -- see _case_status_payload()'s identical fix for why.
        if case["status"] == "in_progress":
            reference = datetime.now(started.tzinfo) if started.tzinfo else datetime.utcnow()
        else:
            reference = datetime.fromisoformat(case["completed_at"]) if case["completed_at"] else started
        elapsed_seconds = max(0.0, (reference - started).total_seconds())
        payload = {
            "case_id": case["case_id"],
            "current_activity": case["current_activity"],
            "elapsed_seconds": round(elapsed_seconds, 1),
        }
        if case["status"] == "in_progress":
            payload["status"] = "running"
            return payload
        if case["status"] == "failed":
            payload["status"] = "error"
            payload["error"] = case["error_message"] or "Something went wrong."
            return payload
        if case["status"] == "cancelled":
            payload["status"] = "error"
            payload["error"] = "This request was cancelled."
            return payload

        # status == "completed"
        if case["outcome"] == "insufficient_data":
            raw = json.loads(case["result_json"]) if case["result_json"] else {}
            payload["status"] = "error"
            payload["error"] = raw.get("message", "Not enough data was found to answer this question.")
            payload["outcome"] = "insufficient_data"
            return payload
        result = json.loads(case["result_json"]) if case["result_json"] else {}
        payload["status"] = "done"
        payload["outcome"] = case["outcome"]
        payload["url"] = result.get("url")
        return payload

    @app.route("/investigate/status/<investigation_id>")
    def investigate_status(investigation_id: str):
        """Polled by research.html's generateInvestigation() every few
        seconds after investigate_generate_async returns. {status:
        "running"} keeps the page waiting; {status: "done", url: ...}
        triggers the same redirect investigate_generate's synchronous
        response used to drive directly; {status: "error", error: ...}
        surfaces the same message the synchronous route would have
        returned inline. A 404 (unknown investigation_id — never tracked,
        e.g. a stale/mistyped link) is deliberately distinct from a real
        "error" status."""
        case = get_research_case(get_db(), investigation_id)
        if case is None:
            abort(404)
        return jsonify(_investigation_case_status_payload(case))

    @app.route("/investigate/<investigation_id>")
    def investigate_view(investigation_id: str):
        db = get_db()
        investigation_row = get_investigation(db, investigation_id)
        if investigation_row is None:
            abort(404, f"No investigation with id={investigation_id!r}")

        # Persistence architecture: full content (hypotheses + evidence)
        # now lives in S3 (research/investigation.py::_persist()); the
        # investigations row only carries s3_key/abstract/metadata. NULL
        # s3_key means this investigation predates the migration -- fall
        # back to the original table-based read so old data keeps
        # rendering without needing a backfill (see storage/database.py's
        # _migrate_investigation_s3_columns docstring).
        if investigation_row["s3_key"]:
            artifact = json.loads(default_document_store().retrieve(investigation_row["s3_key"]))
            investigation = {
                "investigation_id": artifact["investigation_id"], "question": artifact["question"],
                "company_ids": artifact["company_ids"], "statement_type": artifact["statement_type"],
                "strongest_explanation": artifact["strongest_explanation"],
                "unanswered_questions": artifact["unanswered_questions"],
                "additional_evidence_needed": artifact["additional_evidence_needed"],
                "generated_at": investigation_row["generated_at"], "as_of": artifact["as_of"],
                "hidden_at": investigation_row["hidden_at"], "deleted_at": investigation_row["deleted_at"],
            }
            hypotheses = artifact["hypotheses"]
        else:
            hypotheses = []
            for h in list_investigation_hypotheses(db, investigation_id):
                evidence = [dict(e) for e in list_investigation_hypothesis_evidence(db, h["hypothesis_id"])]
                hypotheses.append(
                    {
                        **dict(h),
                        "unknowns": json.loads(h["unknowns"] or "[]"),
                        # chain_steps/confidence_score predate a hypothesis generated/
                        # evaluated before those columns existed — "[]" and NULL are
                        # the correct fallback (see storage/database.py's migration),
                        # not an error.
                        "chain_steps": json.loads(h["chain_steps"] or "[]") if "chain_steps" in h.keys() else [],
                        "supporting_evidence": [e for e in evidence if e["stance"] == "supporting"],
                        "contradicting_evidence": [e for e in evidence if e["stance"] == "contradicting"],
                        "missing_evidence": [e for e in evidence if e["stance"] == "missing"],
                    }
                )
            investigation = {
                **dict(investigation_row),
                "company_ids": json.loads(investigation_row["company_ids"] or "[]"),
                "unanswered_questions": json.loads(investigation_row["unanswered_questions"] or "[]"),
                "additional_evidence_needed": json.loads(investigation_row["additional_evidence_needed"] or "[]"),
            }

        # llm_call_log stays SQLite-only regardless of DATABASE_BACKEND --
        # get_logs_db(), not db (which may be a Postgres connection here,
        # used for the investigation/hypotheses queries above).
        cost = get_investigation_cost_summary(get_logs_db(), investigation_id)

        # Presentation layer: reshape the same investigation/hypotheses data
        # (whichever branch above produced it -- S3 artifact or legacy
        # table read, both dict shapes) into the normalized
        # reports.schema.InvestigationReport via a pure read-side adapter,
        # then render it through the Signal Report Design System
        # (reports/templates/deep_dive/report.html). No research logic is
        # touched or duplicated -- see reports/schema/investigation_report.py.
        report = from_investigation_data(investigation, hypotheses, cost)

        return render_template("deep_dive/report.html", report=report)

    INVESTIGATIONS_PAGE_SIZE = 20
    WATCHLIST_PAGE_SIZE = 25

    @app.route("/investigations")
    def investigations():
        # Generated reports (research/signals_report.py) and structured
        # investigations (research/investigation.py) are two different tables
        # under the hood, but from a user's perspective both are just "an
        # investigation I ran" — merged into one entries list (one search box,
        # one Type filter, one growing/paginated feed) instead of the two
        # separately-searched, separately-paginated sections this page used
        # to render side by side. The 3 hand-written EXAMPLES/THREADS fixtures
        # (web/fixtures.py — illustrative wireframe content, not real data)
        # deliberately don't appear here at all: mixing fabricated numbers
        # into a feed of real investigations risked a user mistaking one for
        # the other. They still have a real home — the Research page's own
        # "try an example" showcase (research.html) links into the same
        # /research/thread/<id> fixture-rendering branch.
        # Two distinct status vocabularies sharing one filter dropdown, per
        # instruction (kept separate, not forced into one shared label set):
        # a Quick Answer's parsed confidence (research/signals_report.py's
        # own _CONFIDENCE_RE: High|Moderate|Low, or "Unknown" when the
        # model didn't follow the template) vs. a Deep Dive's strongest
        # hypothesis verdict (Step 2G's investigation_hypotheses.verdict).
        _VERDICT_STATUS = {
            "SUPPORTED": ("supported", "Supported"),
            "PARTIALLY_SUPPORTED": ("partially_supported", "Partially Supported"),
            "REFUTED": ("refuted", "Refuted"),
            "INSUFFICIENT_EVIDENCE": ("insufficient_evidence", "Insufficient Evidence"),
        }
        _STATUS_FILTER_OPTIONS = [
            ("high", "High confidence"), ("moderate", "Moderate confidence"),
            ("low", "Low confidence"), ("unknown", "Unknown confidence"),
            ("supported", "Supported"), ("partially_supported", "Partially Supported"),
            ("refuted", "Refuted"), ("insufficient_evidence", "Insufficient Evidence"),
            ("no_verdict", "No verdict yet"),
            # A third, distinct status vocabulary for research_cases entries
            # below (case_ prefix so these can never collide with the two
            # existing ones) -- "case_insufficient_data" for a *completed*
            # case is intentionally its own key, not reused from
            # "insufficient_evidence" above: that one is a per-hypothesis
            # Deep Dive verdict (Step 2G), this one is a whole case's
            # outcome (research/assistant.py's InsufficientEvidenceError) --
            # genuinely different concepts that happen to share a word.
            ("case_in_progress", "In progress"), ("case_failed", "Failed"),
            ("case_cancelled", "Cancelled"), ("case_insufficient_data", "Insufficient data"),
        ]

        entries = []
        for generated in list_generated_reports(get_db()):
            meta = extract_report_meta(generated["report_markdown"])
            confidence = meta["confidence"] or "Unknown"
            entries.append(
                {
                    "type": "generated",
                    "id": generated["thread_id"],
                    "type_label": "Quick Answer",
                    "href": url_for("research_thread", thread_id=generated["thread_id"]),
                    "title": meta["title"] or generated["question"],
                    # Only shown when it adds information beyond the title.
                    "subtitle": generated["question"] if meta["title"] else "",
                    # company_ids can be empty for a macro-only question
                    # (research/macro_evidence.py) — no company to list.
                    "companies_label": ", ".join(generated["company_ids"]) or "Macro/regulatory",
                    "right_tag": confidence + " confidence",
                    "status_key": confidence.lower(),
                    "generated_at": generated["generated_at"] or "",
                    "hidden": bool(generated["hidden_at"]),
                }
            )
        all_investigations = list_investigations(get_db())
        # Batched, not one list_investigation_hypotheses() call per
        # investigation -- see get_strongest_verdict_by_investigation's own
        # docstring. Verdict of the strongest (synthesis-ranked) hypothesis
        # is the Deep Dive equivalent of a Quick Answer's parsed confidence
        # -- the two are genuinely different concepts (one's an LLM's
        # stated confidence in its own single-pass answer, the other's
        # Step 2G's evidence-based verdict on a specific competing
        # explanation), kept as distinct labels rather than forced into one
        # shared vocabulary, per instruction.
        # strongest_verdict is now computed once at persist time and
        # stored directly on the row (storage/database.py's
        # _migrate_investigation_s3_columns) -- the batched live-JOIN
        # fallback below only ever runs for investigations that predate
        # that column, never for new ones.
        legacy_ids = [inv["investigation_id"] for inv in all_investigations if inv["strongest_verdict"] is None]
        verdict_by_investigation = get_strongest_verdict_by_investigation(get_db(), legacy_ids) if legacy_ids else {}
        for inv in all_investigations:
            company_ids = json.loads(inv["company_ids"] or "[]")
            verdict = inv["strongest_verdict"] or verdict_by_investigation.get(inv["investigation_id"])
            status_key, status_label = _VERDICT_STATUS.get(verdict, ("no_verdict", "No verdict yet"))
            entries.append(
                {
                    "type": "structured",
                    "id": inv["investigation_id"],
                    "type_label": "Deep Dive",
                    "href": url_for("investigate_view", investigation_id=inv["investigation_id"]),
                    "title": inv["question"],
                    "subtitle": "",
                    "companies_label": ", ".join(company_ids) or "Macro/regulatory",
                    "right_tag": status_label,
                    "status_key": status_key,
                    "generated_at": inv["generated_at"] or "",
                    "hidden": bool(inv["hidden_at"]),
                }
            )
        # research_cases -- only the ones with no other representation in
        # this feed (list_research_cases_for_feed already excludes
        # status='completed' outcome='answered', which shows up as its own
        # generated_reports row above instead -- see that function's own
        # docstring). This is what makes "Cases is the source of truth"
        # real for a user Browse-ing this page: an in_progress case is
        # here immediately on submit, not just once it finishes.
        _CASE_STATUS_LABEL = {
            "in_progress": ("case_in_progress", "In progress"),
            "failed": ("case_failed", "Failed"),
            "cancelled": ("case_cancelled", "Cancelled"),
        }
        owner_id = g.user["user_id"] if g.user else None
        for case in list_research_cases_for_feed(get_db(), owner_id=owner_id):
            company_ids = json.loads(case["company_ids"] or "[]")
            if case["status"] == "completed":  # only outcome='insufficient_data' reaches here
                status_key, status_label = "case_insufficient_data", "Insufficient data"
            else:
                status_key, status_label = _CASE_STATUS_LABEL[case["status"]]
            # Same label a terminal (completed/answered) row of the same
            # kind already uses elsewhere in this feed ("Quick Answer" /
            # "Deep Dive") -- so the label doesn't change the moment a case
            # flips from in_progress to done, just the right_tag does.
            kind_label = "Deep Dive" if case["kind"] == "investigation" else "Quick Answer"
            entries.append(
                {
                    "type": "case",
                    "id": case["case_id"],
                    "type_label": kind_label,
                    "href": url_for("case_detail", case_id=case["case_id"]),
                    "title": case["question"],
                    "subtitle": "",
                    "companies_label": ", ".join(company_ids) or "Macro/regulatory",
                    "right_tag": status_label,
                    "status_key": status_key,
                    "generated_at": case["started_at"] or "",
                    "hidden": False,
                }
            )
        entries.sort(key=lambda r: r["generated_at"], reverse=True)

        iv_type_filter = request.args.get("iv_type") or ""
        if iv_type_filter:
            entries = [r for r in entries if r["type"] == iv_type_filter]
        iv_status_filter = request.args.get("iv_status") or ""
        if iv_status_filter:
            entries = [r for r in entries if r["status_key"] == iv_status_filter]
        # Hidden entries tucked away by default -- "Show hidden" flips this
        # into a dedicated review mode (only hidden entries, so Unhide is
        # findable) rather than interleaving hidden/visible together, which
        # would make "is this hidden or not" a per-card guessing game.
        iv_show_hidden = request.args.get("iv_hidden") == "1"
        entries = [r for r in entries if r["hidden"] == iv_show_hidden]
        iv_query = (request.args.get("iv_q") or "").strip()
        iv = _paginate(
            entries, query=iv_query,
            haystack_fn=lambda r: " ".join(filter(None, [r["title"], r["subtitle"], r["companies_label"]])).lower(),
            page_arg="iv_page", page_size=INVESTIGATIONS_PAGE_SIZE,
        )

        return render_template(
            "investigations.html",
            entries=iv["rows"], entries_total=iv["total"],
            entries_page=iv["page"], entries_total_pages=iv["total_pages"],
            entries_query=iv_query, entries_type_filter=iv_type_filter,
            entries_status_filter=iv_status_filter, status_options=_STATUS_FILTER_OPTIONS,
            entries_show_hidden=iv_show_hidden,
        )

    def _tools_macro_context(db) -> dict:
        """Only the catalog (cheap — one GROUP BY query) — the actual series
        points are fetched client-side from /tools/macro/series.json once a
        series is picked, same lazy-until-needed reasoning
        _ingest_panel_context already documents for its own panel."""
        catalog = [dict(row) for row in list_macro_series_summary(db)]
        catalog.sort(key=lambda r: (r["source"], r["series_key"]))
        return {"tools_macro_catalog": catalog}

    def _tools_analytics_context(db) -> dict:
        patterns = detect_yoy_spikes(db)
        return {
            "tools_analytics_patterns": [
                {
                    "company_id": p.company_id, "metric_label": p.metric_label,
                    "fiscal_year": p.fiscal_year, "yoy_percent": p.yoy_percent,
                }
                for p in patterns
            ]
        }

    def _tools_insights_context(db) -> dict:
        insights = list_system_insights(db)
        return {"tools_insights": insights}

    @app.route("/tools")
    def tools():
        db = get_db()
        active_panel = request.args.get("panel", "macro")
        if active_panel not in ("macro", "analytics", "insights"):
            active_panel = "macro"
        context: dict = {}
        # Only the active panel's (potentially real) work runs — same
        # "don't materialize what isn't being viewed" reasoning
        # _ingest_panel_context already follows for the Admin Ingest tab.
        if active_panel == "macro":
            context.update(_tools_macro_context(db))
        elif active_panel == "analytics":
            context.update(_tools_analytics_context(db))
        elif active_panel == "insights":
            context.update(_tools_insights_context(db))
        return render_template(
            "tools.html", active_panel=active_panel, api_key_set=ANTHROPIC_API_KEY_SET, **context
        )

    @app.route("/tools/macro/series.json")
    def tools_macro_series():
        db = get_db()
        series_key = request.args.get("series_key")
        if not series_key:
            return jsonify(error="series_key is required"), 400
        region = request.args.get("region") or None
        rows = get_macro_series(db, series_key, region)
        if not rows:
            return jsonify(error=f"No data for series_key={series_key!r}"), 404
        return jsonify(
            series_key=series_key,
            unit=rows[0]["unit"],
            source=rows[0]["source"],
            points=[{"period": r["period"], "value": r["value"]} for r in rows],
        )

    @app.route("/tools/insights/generate", methods=["POST"])
    def tools_insights_generate():
        db = get_db()
        if not ANTHROPIC_API_KEY_SET:
            flash("ANTHROPIC_API_KEY is not set on the server — insight generation can't run.", "error")
            return redirect(url_for("tools", panel="insights"))
        try:
            insights = generate_system_insights(db)
        except SystemInsightGenerationError as exc:
            flash(f"Insight generation failed: {exc}", "error")
            return redirect(url_for("tools", panel="insights"))
        flash(
            f"Generated {len(insights)} insight(s)." if insights else "No new insights — not enough grounded claims yet.",
            "success",
        )
        return redirect(url_for("tools", panel="insights"))

    @app.route("/tools/insights/<insight_id>/retain", methods=["POST"])
    def tools_insights_retain(insight_id: str):
        update_system_insight_status(get_db(), insight_id, "retained")
        return redirect(url_for("tools", panel="insights"))

    @app.route("/tools/insights/<insight_id>/archive", methods=["POST"])
    def tools_insights_archive(insight_id: str):
        update_system_insight_status(get_db(), insight_id, "archived")
        return redirect(url_for("tools", panel="insights"))

    @app.route("/watchlist")
    def watchlist():
        db = get_db()
        entries = []
        for item in list_watchlist_items(db):
            if item["item_type"] == "company":
                company = get_company(db, item["item_ref"])
                if company is None:
                    continue  # pinned company was later archived/removed from the registry
                entries.append(
                    {
                        "item_type": "company",
                        "item_ref": item["item_ref"],
                        "pinned_at": item["pinned_at"],
                        "title": company["display_name"],
                        "subtitle": f"{company['sector'] or 'n/a'} · {company['company_id']}",
                        "href": url_for("company_report", company_id=item["item_ref"]),
                    }
                )
            elif item["item_type"] == "thread":
                thread = THREADS.get(item["item_ref"])
                if thread is not None:
                    entries.append(
                        {
                            "item_type": "thread",
                            "item_ref": item["item_ref"],
                            "pinned_at": item["pinned_at"],
                            "title": thread["title"],
                            "subtitle": f"{thread['confidence']} confidence",
                            "href": url_for("research_thread", thread_id=item["item_ref"]),
                        }
                    )
                    continue
                generated = get_generated_report(db, item["item_ref"])
                if generated is None:
                    continue  # watchlisted thread no longer exists (fixture removed, or the
                    # generated report it pointed to was deleted)
                meta = extract_report_meta(generated["report_markdown"])
                entries.append(
                    {
                        "item_type": "thread",
                        "item_ref": item["item_ref"],
                        "pinned_at": item["pinned_at"],
                        "title": meta["title"] or generated["question"],
                        "subtitle": f"{meta['confidence'] or 'Unknown'} confidence",
                        "href": url_for("research_thread", thread_id=item["item_ref"]),
                    }
                )

        wl_query = (request.args.get("wl_q") or "").strip()
        wl_type_filter = request.args.get("wl_type") or ""
        wl = _paginate(
            [e for e in entries if not wl_type_filter or e["item_type"] == wl_type_filter],
            query=wl_query,
            haystack_fn=lambda r: " ".join(filter(None, [r["title"], r["subtitle"]])).lower(),
            page_arg="wl_page", page_size=WATCHLIST_PAGE_SIZE,
        )
        return render_template(
            "watchlist.html",
            entries=wl["rows"], entries_total=wl["total"],
            entries_page=wl["page"], entries_total_pages=wl["total_pages"],
            entries_query=wl_query, entries_type_filter=wl_type_filter,
        )

    def _safe_next() -> str:
        """The `next` field is same-origin form data we render ourselves, but validate
        anyway (defense in depth) — only a same-site path is honored, never an
        absolute or protocol-relative URL, so a redirect can't be pointed off-site."""
        next_url = request.form.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return url_for("watchlist")

    @app.route("/watchlist/add", methods=["POST"])
    def watchlist_add():
        item_type = request.form.get("item_type")
        item_ref = request.form.get("item_ref")
        if item_type not in ("company", "thread") or not item_ref:
            abort(400, "item_type must be 'company' or 'thread', and item_ref is required")
        if item_type == "company" and get_company(get_db(), item_ref) is None:
            abort(404, f"No company registered with company_id={item_ref!r}")
        if item_type == "thread" and item_ref not in THREADS and get_generated_report(get_db(), item_ref) is None:
            abort(404, f"No thread with id={item_ref!r}")
        add_watchlist_item(get_db(), item_type, item_ref)
        return redirect(_safe_next())

    @app.route("/watchlist/remove", methods=["POST"])
    def watchlist_remove():
        item_type = request.form.get("item_type")
        item_ref = request.form.get("item_ref")
        if item_type not in ("company", "thread") or not item_ref:
            abort(400, "item_type must be 'company' or 'thread', and item_ref is required")
        remove_watchlist_item(get_db(), item_type, item_ref)
        return redirect(_safe_next())

    @app.route("/watchlist/news/<company_id>")
    def watchlist_news(company_id: str):
        """Lazily-fetched, on the collapsible's first expand — not loaded for every
        watchlist row up front, so a long watchlist never fires a burst of outbound
        requests just from opening the page. Shared by the Watchlist row's 24h
        teaser (default) and the Overview tab's news section (?days=2). Write-through:
        whatever this call fetches also lands in company_news (storage/repositories.py)
        so the News page's merged feed builds up real history over time instead of
        starting from zero — this endpoint's own response is unaffected."""
        window_days = request.args.get("days", 1, type=int)
        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        items = fetch_company_news(company["display_name"], window_days=window_days)
        if items:
            save_company_news(db, company_id, items)
        return jsonify(
            ok=items is not None,
            items=items or [],
            news_url=google_news_last_24h_url(company["display_name"], window_days=window_days),
        )

    @app.route("/news")
    def news():
        """Standalone News tab (sidebar) — a merged, newest-first feed across
        every company on the Watchlist by default, or one company via the
        filter box. Reads/writes the same company_news cache watchlist_news()
        above does (see storage/repositories.py) rather than a live RSS fetch
        per company on every page view — infeasible outright across this
        app's full company registry (thousands of companies)."""
        db = get_db()
        watchlisted_companies = []
        for item in list_watchlist_items(db):
            if item["item_type"] != "company":
                continue
            company = get_company(db, item["item_ref"])
            if company is None:
                continue
            watchlisted_companies.append({"company_id": company["company_id"], "display_name": company["display_name"]})
        return render_template(
            "news.html",
            watchlisted_companies=watchlisted_companies,
            search_url=url_for("companies_search"),
            feed_url=url_for("news_feed"),
            company_url_template=url_for("company_report", company_id="__ID__"),
        )

    @app.route("/news/feed.json")
    def news_feed():
        """`company_id` given: live-fetch + store that one company (even if
        it's not on the Watchlist — the filter box can name anyone), then
        read its up-to-7-week stored history back. No `company_id`: refresh
        every Watchlisted company (bounded, unlike the full registry) and
        read the merged, deduped result across just those."""
        db = get_db()
        company_id = request.args.get("company_id")
        if company_id:
            company = get_company(db, company_id)
            if company is None:
                abort(404, f"No company registered with company_id={company_id!r}")
            items = fetch_company_news(company["display_name"], window_days=NEWS_RETENTION_DAYS)
            if items:
                save_company_news(db, company_id, items)
            rows = list_company_news(db, company_ids=[company_id])
        else:
            watch_company_ids = [
                item["item_ref"] for item in list_watchlist_items(db) if item["item_type"] == "company"
            ]
            for watch_company_id in watch_company_ids:
                watch_company = get_company(db, watch_company_id)
                if watch_company is None:
                    continue
                fresh = fetch_company_news(watch_company["display_name"], window_days=7)
                if fresh:
                    save_company_news(db, watch_company_id, fresh)
            rows = list_company_news(db, company_ids=watch_company_ids) if watch_company_ids else []
        return jsonify(
            items=[
                {
                    "title": r["title"], "link": r["link"], "source": r["source"],
                    "published_at": r["published_at"], "company_id": r["company_id"],
                    "company_name": r["display_name"],
                }
                for r in rows
            ]
        )

    @app.route("/chat")
    def chat():
        companies = [dict(c) for c in list_companies(get_db(), include_archived=False)]
        return render_template(
            "chat.html", companies=companies, api_key_set=ANTHROPIC_API_KEY_SET
        )

    @app.route("/chat", methods=["POST"])
    def chat_ask():
        return _answer_question_response()

    @app.route("/chat-async", methods=["POST"])
    def chat_ask_async():
        return _answer_question_async_response()

    # Guarded so this runs exactly once in the process that actually serves
    # requests -- with the debug reloader on, create_app() executes once in
    # Werkzeug's monitor process (which never serves anything, just watches
    # files and re-execs a child) and again in the reloader's child, which
    # sets WERKZEUG_RUN_MAIN=true; without this guard the monitor process
    # would also mark runs failed / spawn resume threads it then immediately
    # abandons. Without the reloader (debug=False, or no --debug) that env
    # var is never set, so the `not app.debug` half covers that case.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _resume_interrupted_batch_jobs()
        _resume_interrupted_cases()

    return app
