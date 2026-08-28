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

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import anthropic
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
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
from companies.stock_actions import (
    ACTION_TYPES,
    InvalidStockActionError,
    StockActionNotFoundError,
    add_stock_action,
    delete_stock_action,
    list_stock_actions,
)
from config.settings import ANTHROPIC_API_KEY_SET, DOCUMENTS_DIR, RAW_DIR, SECRET_KEY, from_repo_relative, to_repo_relative
from ingestion.coordinator import (
    discover_pending_documents,
    discover_pending_financial_items,
    process_all_pending_documents,
    process_all_pending_financial_items,
    process_documents,
    process_financial_items,
    retry_failed_documents,
    retry_failed_financial_items,
)
from analytics.patterns import detect_yoy_spikes
from ingestion.detector import ADAPTER_CLASSES
from ingestion.pipeline import ingest_file
from research.assistant import answer_question
from research.insights import NoDataToSummarizeError, generate_key_insights
from research.investigation import InvestigationError, run_investigation
from research.signals_report import extract_report_meta, generate_signals_report
from research.system_insights import SystemInsightGenerationError, generate_system_insights
from storage.database import init_db
from storage.repositories import (
    COMPANY_LIST_COLUMNS,
    OVERVIEW_RATIO_CATALOG,
    DEFAULT_THEME,
    VALID_THEMES,
    add_index_definition,
    add_industry,
    add_sector,
    add_watchlist_item,
    count_companies_by_index_tag,
    count_companies_by_industry,
    count_companies_by_sector,
    create_user,
    delete_company_note,
    delete_generated_report,
    delete_index_definition,
    delete_industry,
    delete_note_attachment,
    delete_sector,
    get_company_document,
    get_note_attachment,
    get_company_index_tags,
    get_company_insights,
    get_company_list_column_settings,
    get_overview_ratio_settings,
    get_all_company_index_tags,
    get_generated_report,
    get_investigation,
    get_llm_usage_summary,
    get_macro_series,
    get_user_by_email,
    get_user_by_id,
    get_user_by_login,
    is_watchlisted,
    list_company_insights,
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
    save_company_note,
    save_note_attachment,
    update_company_note,
    save_generated_report,
    save_report_evidence,
    save_report_followups,
    set_company_index_tags,
    set_company_list_column_settings,
    set_overview_ratio_settings,
    update_system_insight_status,
    update_user_theme,
)
from web.docs_feed import KEY_TO_DOCUMENT_TYPE, build_docs_feed
from web.fixtures import EXAMPLES, THREADS
from web.live_quote import get_live_quote, peek_cached_quote
from web.news import fetch_company_news, google_news_last_24h_url
from web.rich_text import sanitize_note_html
from web.charts_feed import build_charts_feed
from web.valuation_feed import build_valuation_feed

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# For now, every tab is browsable without signing in — login only gates
# Admin (needs is_admin on a real user). Settings used to be gated too (a
# per-user preference, meaningless anonymously) but now degrades instead:
# signed-in preferences persist to users.theme, signed-out ones live in the
# session (see settings() and g.theme below) — same page either way.
# Revisit if/when the whole app should go back to being login-only.
_LOGIN_REQUIRED_ENDPOINTS: set[str] = set()
_LOGIN_REQUIRED_PREFIXES = ("admin",)

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
        conn: sqlite3.Connection | None = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    def get_db() -> sqlite3.Connection:
        if "db_conn" not in g:
            g.db_conn = init_db()
        return g.db_conn

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

    @app.route("/companies/search.json")
    def companies_search():
        # Static path, so Flask/Werkzeug matches it ahead of the
        # /companies/<company_id> dynamic route below regardless of
        # registration order — header_search.js's typeahead.
        query = request.args.get("q", "")
        db = get_db()
        results = [
            {
                "company_id": row["company_id"],
                "display_name": row["display_name"],
                "nse_symbol": row["nse_symbol"],
                "sector": row["sector"],
            }
            for row in search_companies(db, query)
        ]
        return jsonify(results=results)

    @app.route("/companies")
    def companies():
        db = get_db()
        shares_outstanding_by_company = list_latest_shares_outstanding(db)
        rows = []
        for c in list_companies(db):
            row = dict(c)
            row["latest_price"] = _latest_price(row["valuation_model_file"]) if row["valuation_model_file"] else None
            if row["latest_price"] is None:
                # No ported dashboard — fall back to whatever price is
                # already cached from someone having visited this company's
                # own page (get_live_quote there). Never fetches here: with
                # ~2,500 rows a live call per row isn't viable on a list page.
                ticker = row["nse_symbol"] or (row["company_id"] if row["country"] != "IN" else None)
                cached_quote = peek_cached_quote(ticker, row["country"])
                if cached_quote is not None:
                    row["latest_price"] = cached_quote["price"]
            # Market cap (Cr) = price/share x shares outstanding (Cr) — shares
            # outstanding only exists for companies with real financial data
            # ingested (~100 of ~2,500 today), so this stays None for most rows.
            shares_outstanding = shares_outstanding_by_company.get(row["company_id"])
            if row["latest_price"] is not None and shares_outstanding is not None:
                row["market_cap_cr"] = row["latest_price"] * shares_outstanding
            else:
                row["market_cap_cr"] = None
            row["index_tags"] = get_company_index_tags(db, row["company_id"])
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

    def _ingest_panel_context(db) -> dict:
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
        discover_pending_financial_items(db)

        fq_all = list_ingestion_queue_items(db)
        fq_status_filter = request.args.get("fq_status") or ""
        fq_company_filter = request.args.get("fq_company") or ""
        fq_kind_filter = request.args.get("fq_kind") or ""
        fq = _filter_and_paginate(
            list_ingestion_queue_items(db, status=fq_status_filter or None),
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

    @app.route("/admin")
    def admin():
        db = get_db()
        # ~2,600 companies today — the edit panel renders one <form> per row
        # (not just a display row), so materializing and index-tag-querying
        # all of them on every /admin load was both an N+1 query storm (one
        # SELECT per company for get_company_index_tags) and a multi-thousand-
        # form page that made the browser hang. Only the current page (after
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
        status_filter = request.args.get("status") or ""

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
        return render_template(
            "admin.html",
            companies=page_companies,
            companies_page=page,
            companies_total_pages=total_pages,
            companies_total=total_companies,
            companies_page_size=ADMIN_COMPANIES_PAGE_SIZE,
            companies_query=query,
            companies_sector_filter=sector_filter,
            companies_industry_filter=industry_filter,
            companies_tag_filter=tag_filter,
            companies_status_filter=status_filter,
            companies_filters_active=bool(query or sector_filter or industry_filter or tag_filter or status_filter),
            active_companies=[c for c in all_companies if c["status"] == "active"],
            archive_reasons=sorted(ARCHIVE_REASONS),
            sectors=sectors,
            industries=industries,
            index_names=index_tag_names,
            taxonomy=taxonomy,
            list_columns=COMPANY_LIST_COLUMNS,
            column_settings=column_settings,
            ratio_catalog=OVERVIEW_RATIO_CATALOG,
            ratio_settings=ratio_settings,
            import_sources=sorted(ADAPTER_CLASSES),
            active_panel=request.args.get("panel", "companies"),
            import_selected_company=request.args.get("company_id", ""),
            stock_action_types=sorted(ACTION_TYPES),
            stock_action_selected_company=request.args.get("sa_company_id", ""),
            stock_actions=(
                list_stock_actions(db, request.args["sa_company_id"])
                if request.args.get("sa_company_id") else []
            ),
            **(_ingest_panel_context(db) if request.args.get("panel") == "ingest" else {}),
        )

    @app.route("/admin/usage")
    def admin_usage():
        """LLM token/cost observability — every research/assistant.py,
        research/insights.py, research/signals_report.py, and
        research/macro_evidence.py call logs one llm_call_log row
        (llm/observability.py); this page is just that table, summarized.
        Admin-only (endpoint name starts with "admin" — see _require_login
        above) since spend data is an operator concern, not a general
        end-user one."""
        db = get_db()
        return render_template(
            "usage.html",
            summary=get_llm_usage_summary(db),
            recent_calls=list_llm_call_log(db, limit=100),
        )

    @app.route("/admin/columns", methods=["POST"])
    def admin_update_columns():
        db = get_db()
        set_company_list_column_settings(db, request.form.getlist("columns"))
        return redirect(url_for("admin"))

    @app.route("/admin/overview-ratios", methods=["POST"])
    def admin_update_overview_ratios():
        db = get_db()
        set_overview_ratio_settings(db, request.form.getlist("ratios"))
        return redirect(url_for("admin", panel="overview_ratios"))

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
        return redirect(url_for("admin", panel="taxonomy"))

    @app.route("/admin/vocabulary/<kind>/rename", methods=["POST"])
    def admin_vocabulary_rename(kind: str):
        if kind not in _VOCAB_HANDLERS:
            abort(404, f"Unknown vocabulary: {kind!r}")
        _add_fn, rename_fn, _delete_fn = _VOCAB_HANDLERS[kind]
        old_name = (request.form.get("old_name") or "").strip()
        new_name = (request.form.get("new_name") or "").strip()
        if old_name and new_name and old_name != new_name:
            rename_fn(get_db(), old_name, new_name)
        return redirect(url_for("admin", panel="taxonomy"))

    @app.route("/admin/vocabulary/<kind>/delete", methods=["POST"])
    def admin_vocabulary_delete(kind: str):
        if kind not in _VOCAB_HANDLERS:
            abort(404, f"Unknown vocabulary: {kind!r}")
        _add_fn, _rename_fn, delete_fn = _VOCAB_HANDLERS[kind]
        name = (request.form.get("name") or "").strip()
        if name:
            delete_fn(get_db(), name)
        return redirect(url_for("admin", panel="taxonomy"))

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
            return redirect(url_for("admin", panel="import"))
        filename = secure_filename(upload.filename)
        if not filename:
            flash("That filename isn't valid.", "error")
            return redirect(url_for("admin", panel="import"))

        # data/raw/<COMPANY>/<source>/<file> — the same convention the CLI's
        # own path-based detection expects, so a file uploaded here is
        # indistinguishable from one dropped in by hand (README: Ingestion
        # Approach by Source). Timestamp-prefixed so re-uploading the same
        # filename never silently overwrites a previous raw file — every
        # upload is kept, matching the "raw observations are never
        # overwritten" rule the rest of ingestion already follows.
        dest_dir = RAW_DIR / company_id / source_id
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

        return redirect(url_for("admin", panel="import"))

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

    @app.route("/admin/ingest/refresh", methods=["POST"])
    def admin_ingest_refresh():
        """Refresh Pending Files — re-scan data/raw/ now, rather than
        waiting for the next /admin?panel=ingest load (which already
        refreshes on its own, but an explicit action makes "did my newly
        dropped file show up" not depend on remembering that)."""
        db = get_db()
        touched = discover_pending_financial_items(db)
        flash(f"Rescanned data/raw/ — {touched} item(s) added or updated.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/process", methods=["POST"])
    def admin_ingest_process():
        """Ingest Selected — the specific ingestion_queue_items rows checked
        in the UI, through the existing financial/macro pipeline."""
        db = get_db()
        item_ids = [int(v) for v in request.form.getlist("item_id")]
        summary = process_financial_items(db, item_ids)
        flash(f"Processed {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/process-all", methods=["POST"])
    def admin_ingest_process_all():
        """Ingest All Pending."""
        db = get_db()
        summary = process_all_pending_financial_items(db)
        flash(f"Processed {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/retry-failed", methods=["POST"])
    def admin_ingest_retry_failed():
        """Retry Failed — every FAILED row, as-is."""
        db = get_db()
        summary = retry_failed_financial_items(db)
        flash(f"Retried {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} still failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/documents/process", methods=["POST"])
    def admin_ingest_process_documents():
        """Process the specific pending documents checked in the UI —
        registers/hashes each one and runs Step 2A's Knowledge Builder
        extraction against it (ingestion/coordinator.py::process_documents)."""
        db = get_db()
        document_ids = [int(v) for v in request.form.getlist("document_id")]
        summary = process_documents(db, document_ids)
        flash(f"Registered {summary.succeeded} document(s), {summary.failed} failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/documents/process-all", methods=["POST"])
    def admin_ingest_process_all_documents():
        db = get_db()
        summary = process_all_pending_documents(db)
        flash(f"Registered {summary.succeeded} document(s), {summary.failed} failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

    @app.route("/admin/ingest/documents/retry-failed", methods=["POST"])
    def admin_ingest_retry_failed_documents():
        db = get_db()
        summary = retry_failed_documents(db)
        flash(f"Retried {summary.attempted}: {summary.succeeded} succeeded, {summary.failed} still failed.", "success")
        return redirect(url_for("admin", panel="ingest"))

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
        return redirect(url_for("admin", panel="stock_actions", sa_company_id=company_id))

    @app.route("/admin/<company_id>/stock-actions/<int:action_id>/delete", methods=["POST"])
    def admin_delete_stock_action(company_id: str, action_id: int):
        db = get_db()
        try:
            delete_stock_action(db, company_id, action_id)
        except StockActionNotFoundError as exc:
            abort(404, str(exc))
        return redirect(url_for("admin", panel="stock_actions", sa_company_id=company_id))

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

        return redirect(url_for("admin"))

    @app.route("/companies/<company_id>")
    def company_report(company_id: str):
        statement_type = request.args.get("statement_type", "consolidated")
        if statement_type not in ("consolidated", "standalone"):
            abort(400, "statement_type must be 'consolidated' or 'standalone'")
        tab = request.args.get("tab", "overview")
        valid_tabs = (
            "overview", "key_insights", "charts", "financials", "valuation_model",
            "commentary", "news", "notes", "docs", "threads",
        )
        if tab not in valid_tabs:
            abort(400, f"tab must be one of {', '.join(valid_tabs)}")

        db = get_db()
        company = get_company(db, company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")

        valuation_model_file = company["valuation_model_file"]
        has_ported_dataset = bool(valuation_model_file) and _valuation_model_data_path(valuation_model_file).exists()

        # Same feed backs both the facts-only Financials tab and the
        # assumptions-driven Valuation Model tab — the two tabs render it
        # differently (Valuation Model adds the editable assumptions panel,
        # Growth Projection, and Intrinsic Value/Margin of Safety on top),
        # not two different data sources.
        if has_ported_dataset:
            # A richer, manually-ported dataset (see the "HDFC Bank Equity
            # Dashboard" Claude Design import) — not statement_type-aware,
            # the ported file isn't split consolidated/standalone.
            valuation_data_url = url_for("static", filename=f"data/{valuation_model_file}")
        else:
            # Same dashboard template, every company — built live from
            # whatever this company's canonical_financials actually has.
            # Genuine gaps (advances, EPS, price, ...) render as "—", not a
            # different page layout. See web/valuation_feed.py.
            valuation_data_url = url_for(
                "company_valuation_feed", company_id=company_id, statement_type=statement_type
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
        shares_outstanding = list_latest_shares_outstanding(db).get(company_id)
        # Which ratio-grid rows an admin has enabled (Admin -> Overview
        # Ratios) — the catalog itself lives in storage/repositories.py,
        # each key's compute logic in valuation_dashboard.js's RATIO_CATALOG.
        ratio_settings = get_overview_ratio_settings(db)
        enabled_ratio_keys = [r["key"] for r in OVERVIEW_RATIO_CATALOG if ratio_settings[r["key"]]]

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
            enabled_ratio_keys=enabled_ratio_keys,
            is_watchlisted=is_watchlisted(db, "company", company_id),
            has_ported_dataset=has_ported_dataset,
            valuation_data_url=valuation_data_url,
            docs_data_url=url_for("company_docs_feed", company_id=company_id),
            insights=insights,
            insights_preview=insights_preview,
            insights_history=insights_history,
            notes=notes,
            company_threads=company_threads,
            api_key_set=ANTHROPIC_API_KEY_SET,
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
        # same never-overwrite convention as company_add_document.
        dest_dir = DOCUMENTS_DIR / company_id / _NOTE_ATTACHMENTS_DIR_NAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_path = dest_dir / f"{stamp}__{filename}"
        upload.save(dest_path)
        size_bytes = dest_path.stat().st_size

        row = save_note_attachment(db, note_id, filename, to_repo_relative(dest_path), size_bytes)
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
        return send_file(from_repo_relative(row["raw_file_path"]), download_name=row["filename"])

    @app.route("/companies/<company_id>/notes/<int:note_id>/attachments/<int:attachment_id>/delete", methods=["POST"])
    def company_delete_note_attachment(company_id: str, note_id: int, attachment_id: int):
        db = get_db()
        row = delete_note_attachment(db, note_id, attachment_id)
        if row is None:
            abort(404)
        from_repo_relative(row["raw_file_path"]).unlink(missing_ok=True)
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
        return jsonify(build_charts_feed(db, company_id, statement_type=statement_type, period_type=period_type))

    @app.route("/companies/<company_id>/docs-feed.json")
    def company_docs_feed(company_id: str):
        db = get_db()
        if get_company(db, company_id) is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        return jsonify(build_docs_feed(db, company_id))

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
            dest_dir = DOCUMENTS_DIR / company_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest_path = dest_dir / f"{stamp}__{filename}"
            upload.save(dest_path)
            raw_file_path = to_repo_relative(dest_path)
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
        )
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
        if row is None or not row["raw_file_path"]:
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

    _THEME_LABELS = [("schwab", "Schwab"), ("white", "White"), ("light", "Light"), ("green", "Green"), ("dark", "Dark")]

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
            return redirect(url_for("settings"))
        return render_template("settings.html", themes=_THEME_LABELS)

    @app.route("/")
    def home():
        return render_template("landing.html")

    @app.route("/about")
    def about():
        return render_template("about.html")

    @app.route("/research")
    def research():
        active_companies = [dict(c) for c in list_companies(get_db(), include_archived=False)]
        return render_template(
            "research.html", examples=EXAMPLES, companies=active_companies, api_key_set=ANTHROPIC_API_KEY_SET
        )

    def _answer_question_response(company_ids: list[str] | None = None):
        """Shared by /chat, /research/ask and /companies/<id>/ask — all three are
        "ask the LLM research assistant about these companies", just reached from
        different places (the standalone company-lookup flow, the Research tab's
        own composer, and the per-company Ask AI drawer). Same validation, same
        evidence-grounded answer, same response shape.

        `company_ids` is passed in only by the per-company route, where the scope
        comes from the URL path — the body's own company_ids is ignored there, so
        a request can't widen its scope past the company it was opened on.

        Every call here persists into generated_reports (same table
        /research/thread/generate writes to) — so it shows up, timestamped, on
        the Investigations list and (when scoped to one company) that
        company's Threads tab, instead of vanishing once answered. This is
        also what feeds research.assistant.answer_question()'s own
        reuse-before-recompute check: the second time the same/near-same
        question comes in, it's served from this saved row instead of a
        fresh LLM call."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        if company_ids is None:
            company_ids = payload.get("company_ids") or []
        statement_type = payload.get("statement_type", "consolidated")

        if not ANTHROPIC_API_KEY_SET:
            return jsonify(error="ANTHROPIC_API_KEY is not set on the server — the assistant can't run."), 503
        if not question:
            return jsonify(error="Ask a question first."), 400
        # company_ids may be empty: a question can be grounded in Macro
        # evidence alone (research/macro_evidence.py) rather than any one
        # company's Financials/Docs — answer_question() handles that case,
        # including the "found nothing at all" message.
        if statement_type not in ("consolidated", "standalone"):
            return jsonify(error="statement_type must be 'consolidated' or 'standalone'"), 400

        db = get_db()
        for company_id in company_ids:
            if get_company(db, company_id) is None:
                return jsonify(error=f"No company registered with company_id={company_id!r}"), 404

        try:
            answer = answer_question(db, question, company_ids, statement_type=statement_type)
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        # A peer-comparison question (>1 company) gets combined, indexed-to-100
        # comparison charts instead of separate same-company charts on each
        # company's own scale — the whole point of a comparison chart is
        # putting both companies on one axis over the same overlapping period,
        # not six small charts a reader has to eyeball against each other.
        comparison_charts = {}
        charts_by_company = {}
        if len(company_ids) > 1:
            comparison_charts = {
                chart_key: figure_to_base64_png(figure)
                for chart_key, figure in build_comparison_charts(db, company_ids, statement_type=statement_type).items()
            }
        else:
            charts_by_company = {
                company_id: {
                    chart_key: figure_to_base64_png(figure)
                    for chart_key, figure in build_company_charts(db, company_id, statement_type=statement_type).items()
                }
                for company_id in company_ids
            }

        thread_id = uuid.uuid4().hex[:12]
        save_generated_report(db, thread_id, question, company_ids, statement_type, answer)
        thread_url = url_for("research_thread", thread_id=thread_id)

        return jsonify(
            question=question,
            company_ids=company_ids,
            answer_html=str(_render_markdown_with_tags(answer)),
            charts=charts_by_company,
            comparison_charts=comparison_charts,
            thread_id=thread_id,
            thread_url=thread_url,
        )

    @app.route("/research/ask", methods=["POST"])
    def research_ask():
        return _answer_question_response()

    @app.route("/companies/<company_id>/ask", methods=["POST"])
    def company_ask(company_id: str):
        """Backs the Ask AI drawer (web/templates/_ask_ai.html), which every
        company page and company-list row can open. Scope comes from the path
        rather than a picker, so the drawer needs no company selection step at
        all — it already knows which company it was opened on. Every answer
        here is auto-saved as a thread (save_thread=True) so it lands on the
        company's Threads tab, timestamped and deletable — unlike /research/ask
        and /chat, which stay ephemeral."""
        return _answer_question_response(company_ids=[company_id])

    @app.route("/research/thread/generate", methods=["POST"])
    def research_thread_generate():
        """Generate a full Signals-format report (research/signals_report.py) for a
        question and stash it under a new thread_id, so it gets a shareable
        /research/thread/<id> URL the same as the example investigations — just
        generated on demand from live evidence instead of hand-written."""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        company_ids = payload.get("company_ids") or []
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

        try:
            result = generate_signals_report(db, question, company_ids, statement_type=statement_type)
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        thread_id = uuid.uuid4().hex[:12]
        save_generated_report(db, thread_id, question, company_ids, statement_type, result.report_markdown)
        if result.evidence:
            save_report_evidence(
                db,
                thread_id,
                [
                    {"kind": e.kind, "company_id": e.company_id, "label": e.label, "value": e.value, "citation": e.citation}
                    for e in result.evidence
                ],
            )
        if result.followups:
            save_report_followups(db, thread_id, result.followups)
        return jsonify(thread_id=thread_id, url=url_for("research_thread", thread_id=thread_id))

    @app.route("/research/thread/<thread_id>")
    def research_thread(thread_id: str):
        db = get_db()
        generated = get_generated_report(db, thread_id)
        if generated is not None:
            return render_template(
                "research_thread.html",
                thread_id=thread_id,
                generated=generated,
                report_html=_render_markdown_with_tags(generated["report_markdown"]),
                report_evidence=list_report_evidence(db, thread_id),
                report_followups=list_report_followups(db, thread_id),
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
        statement_type = payload.get("statement_type", "consolidated")

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
            investigation = run_investigation(db, question, company_ids, statement_type=statement_type)
        except InvestigationError as exc:
            return jsonify(error=f"The investigation couldn't complete: {exc}"), 502
        except anthropic.APIError as exc:
            return jsonify(error=f"The assistant request failed: {exc}"), 502

        return jsonify(
            investigation_id=investigation.investigation_id,
            url=url_for("investigate_view", investigation_id=investigation.investigation_id),
        )

    @app.route("/investigate/<investigation_id>")
    def investigate_view(investigation_id: str):
        db = get_db()
        investigation = get_investigation(db, investigation_id)
        if investigation is None:
            abort(404, f"No investigation with id={investigation_id!r}")

        hypotheses = []
        for h in list_investigation_hypotheses(db, investigation_id):
            evidence = [dict(e) for e in list_investigation_hypothesis_evidence(db, h["hypothesis_id"])]
            hypotheses.append(
                {
                    **dict(h),
                    "unknowns": json.loads(h["unknowns"] or "[]"),
                    "supporting_evidence": [e for e in evidence if e["stance"] == "supporting"],
                    "contradicting_evidence": [e for e in evidence if e["stance"] == "contradicting"],
                    "missing_evidence": [e for e in evidence if e["stance"] == "missing"],
                }
            )

        return render_template(
            "investigation.html",
            investigation={
                **dict(investigation),
                "company_ids": json.loads(investigation["company_ids"] or "[]"),
                "unanswered_questions": json.loads(investigation["unanswered_questions"] or "[]"),
                "additional_evidence_needed": json.loads(investigation["additional_evidence_needed"] or "[]"),
            },
            hypotheses=hypotheses,
        )

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
        entries = []
        for generated in list_generated_reports(get_db()):
            meta = extract_report_meta(generated["report_markdown"])
            entries.append(
                {
                    "type": "generated",
                    "type_label": "Quick Answer",
                    "href": url_for("research_thread", thread_id=generated["thread_id"]),
                    "title": meta["title"] or generated["question"],
                    # Only shown when it adds information beyond the title.
                    "subtitle": generated["question"] if meta["title"] else "",
                    # company_ids can be empty for a macro-only question
                    # (research/macro_evidence.py) — no company to list.
                    "companies_label": ", ".join(generated["company_ids"]) or "Macro/regulatory",
                    "right_tag": (meta["confidence"] or "Unknown") + " confidence",
                    "generated_at": generated["generated_at"] or "",
                }
            )
        for inv in list_investigations(get_db()):
            company_ids = json.loads(inv["company_ids"] or "[]")
            entries.append(
                {
                    "type": "structured",
                    "type_label": "Deep Dive",
                    "href": url_for("investigate_view", investigation_id=inv["investigation_id"]),
                    "title": inv["question"],
                    "subtitle": "",
                    "companies_label": ", ".join(company_ids) or "Macro/regulatory",
                    "right_tag": inv["generated_at"],
                    "generated_at": inv["generated_at"] or "",
                }
            )
        entries.sort(key=lambda r: r["generated_at"], reverse=True)

        iv_type_filter = request.args.get("iv_type") or ""
        if iv_type_filter:
            entries = [r for r in entries if r["type"] == iv_type_filter]
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
        teaser (default) and the Overview tab's news section (?days=2)."""
        window_days = request.args.get("days", 1, type=int)
        company = get_company(get_db(), company_id)
        if company is None:
            abort(404, f"No company registered with company_id={company_id!r}")
        items = fetch_company_news(company["display_name"], window_days=window_days)
        return jsonify(
            ok=items is not None,
            items=items or [],
            news_url=google_news_last_24h_url(company["display_name"], window_days=window_days),
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

    return app
