"""Web viewer tests via Flask's test client — no live server needed."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from companies.registry import register_company, seed_companies
from ingestion.pipeline import ingest_file, ingest_yfinance_company
from normalization.financials import ensure_metric_vocabulary
from storage.database import init_db
from tests.test_screener_adapter import _make_screener_workbook


class _FakeMessages:
    def __init__(self, text: str, captured: list) -> None:
        self._text = text
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn")


class _FakeClient:
    def __init__(self, text: str, captured: list) -> None:
        self.messages = _FakeMessages(text, captured)


def _install_fake_llm(monkeypatch, text: str = "The answer. [FACT] some fact. [INFERENCE] a guess."):
    captured: list = []
    monkeypatch.setattr("llm.providers.anthropic_provider.anthropic.Anthropic", lambda *a, **kw: _FakeClient(text, captured))
    return captured


class _SequencedFakeMessages:
    """Returns a different fixed response per successive call, in order --
    several routes now make more than one LLM call in sequence (Jev's
    complexity classification, llm/complexity.py, ahead of the real
    answer/company-resolution call), each expecting a different response
    shape, so a single fixed _install_fake_llm() response can't serve all
    of them the way it could before docs/ADR/023."""

    def __init__(self, texts: list[str], captured: list) -> None:
        self._texts = texts
        self._captured = captured

    def create(self, **kwargs):
        self._captured.append(kwargs)
        text = self._texts[min(len(self._captured) - 1, len(self._texts) - 1)]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _install_sequenced_fake_llm(monkeypatch, texts: list[str]):
    captured: list = []
    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: SimpleNamespace(messages=_SequencedFakeMessages(texts, captured)),
    )
    return captured


def _build_app(db_path: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("config.settings.DB_PATH", db_path)
    # web/app.py's docs/add and note-attachment upload routes (and the raw-
    # file admin importer) write real files under these dirs — without
    # isolating them too, a test that exercises an upload writes its fixture
    # bytes into the actual data/documents//data/raw checked-out on disk
    # (found via a stray `data/documents/HDFCBANK/<timestamp>__ar.pdf` full
    # of test fixture bytes accumulating on every full-suite run; DB_PATH
    # being isolated didn't help since the file write and the DB row are
    # two separate writes).
    monkeypatch.setattr("config.settings.DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr("config.settings.RAW_DIR", tmp_path / "raw")
    # get_price_db() (web/app.py) reads settings.PRICE_DB_PATH fresh per
    # request — without isolating it too, the Companies list route reads
    # the real data/price_history.db during a test (real closes for real
    # Nifty 500 companies), silently pre-empting whatever a test expected
    # from its peek_cached_quote fallback.
    monkeypatch.setattr("config.settings.PRICE_DB_PATH", tmp_path / "price_history.db")
    # Same bug class as DOCUMENTS_DIR/RAW_DIR/PRICE_DB_PATH above, one hop
    # removed: _persist_generated_report_s3 (web/app.py) writes each saved
    # research report to default_document_store(), which under the default
    # "local" backend (LocalDocumentStore) resolves its "threads/<id>/v1.json"
    # key via config.settings.from_repo_relative -> BASE_DIR, not any of the
    # settings already isolated above -- without this, every test that saves
    # a report (research/ask routes) wrote a real file into this repo's own
    # threads/ directory instead of tmp_path.
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    from web.app import create_app

    app = create_app()
    app.testing = True
    return app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "web_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        yield test_client


def test_companies_page_lists_companies(client) -> None:
    response = client.get("/companies")
    assert response.status_code == 200
    assert b"HDFCBANK" in response.data
    assert b"ICICIBANK" in response.data


def test_companies_page_shows_cached_price_without_ported_dashboard(client, monkeypatch) -> None:
    # HDFCBANK has no valuation_model_file in this fixture DB (seed_companies
    # doesn't set one) — the list price column should still pick up whatever
    # web.live_quote already has cached for it, without this route calling
    # yfinance itself (asserted by not monkeypatching get_live_quote at all).
    monkeypatch.setattr(
        "web.app.peek_cached_quote",
        lambda ticker, country: {"price": 1234.5, "prev_close": 1200.0, "change": 34.5, "change_pct": 2.9}
        if ticker == "HDFCBANK"
        else None,
    )
    response = client.get("/companies")
    assert response.status_code == 200
    assert b"1,234.50" in response.data


def test_companies_page_has_separate_sortable_sector_and_industry_columns(client) -> None:
    response = client.get("/companies")
    body = response.data.decode()
    assert 'data-sort-key="sector"' in body
    assert 'data-sort-key="industry"' in body
    assert "Sector / Industry" not in body
    # HDFCBANK's sector/industry render as independent cells, not combined
    # into one "Sector · Industry" string.
    assert 'data-col="sector">Financial Services</td>' in body
    assert 'data-col="industry">Banks</td>' in body


def test_docs_feed_has_quarters_for_quarterly_ingested_company(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "docs_quarterly.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/companies/HDFCBANK/docs-feed.json")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["years"]) > 0
        assert any(year["quarter_count"] > 0 for year in data["years"])


def test_docs_feed_has_years_for_annual_only_company(tmp_path: Path, monkeypatch) -> None:
    # Regression test: years used to be derived only from quarterly periods,
    # so a company with only annual-granularity ingestion (e.g. yfinance's
    # pilot adapter) got an entirely empty Docs tab even though it has real
    # financials on file.
    import pandas as pd

    db_path = tmp_path / "docs_annual_only.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    register_company(conn, "AAPL", "Apple Inc.", "Apple", country="US", currency="USD")

    class _FakeTicker:
        def __init__(self, *_a, **_kw):
            self.financials = pd.DataFrame({pd.Timestamp("2024-09-30"): [1000.0]}, index=["Total Revenue"])
            self.balance_sheet = pd.DataFrame()
            self.cashflow = pd.DataFrame()

    monkeypatch.setattr("sources.yfinance_financials.yf.Ticker", _FakeTicker)
    # ADR-022: ingest_yfinance_company() now also lands a raw/companies/
    # artifact via storage.raw_object_store (LocalDocumentStore by
    # default) -- isolate BASE_DIR here too (same reasoning _build_app's
    # own DOCUMENTS_DIR/RAW_DIR isolation gives below), since this call
    # happens before _build_app() runs its own monkeypatching.
    monkeypatch.setattr("config.settings.BASE_DIR", tmp_path)
    ingest_yfinance_company(conn, "AAPL", "AAPL", currency="USD")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/companies/AAPL/docs-feed.json")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["years"]) > 0
        assert all(year["quarter_count"] == 0 for year in data["years"])
        # The annual pill's period_id still lets a user add an annual report
        # even though there are no quarters to show under it.
        assert data["years"][0]["period_id"].startswith("year:")


def test_docs_feed_is_empty_for_company_with_no_financials_or_docs(client) -> None:
    # HDFCBANK is registered by the `client` fixture's seed_companies() but
    # never ingested in this fixture DB, and no document has been added —
    # same shape as a real company like Infosys before anyone's run `ingest`
    # or added anything. Nothing to show yet: a period only appears once it
    # has real content (financials or a document), see build_docs_feed.
    response = client.get("/companies/HDFCBANK/docs-feed.json")
    assert response.status_code == 200
    data = response.get_json()
    assert data["synthetic"] is True
    assert data["years"] == []
    # The Add-document modal's own period range is unaffected by there
    # being nothing to show in the archive yet.
    assert len(data["annual_period_options"]) > 5


def test_docs_feed_shows_only_the_year_a_document_was_added_to(client) -> None:
    # No financials ingested for HDFCBANK in this fixture DB, so the feed
    # starts fully empty — adding one document should surface exactly that
    # year, not a wide placeholder range around it.
    add_response = client.post(
        "/companies/HDFCBANK/docs/add",
        json={"period": "q2fy2011", "type": "ppt", "source": "link", "ref": "https://example.com/old-deck.pdf"},
    )
    assert add_response.status_code == 200

    response = client.get("/companies/HDFCBANK/docs-feed.json")
    data = response.get_json()
    assert data["synthetic"] is True
    assert [y["fy"] for y in data["years"]] == ["FY2011"]
    fy2011 = data["years"][0]
    q2 = next(q for q in fy2011["quarters"] if q["id"] == "q2fy2011")
    assert q2["docs"]["ppt"] is not None
    assert q2["docs"]["ppt"]["added_by_user"] == "you"


def test_uploaded_document_stores_repo_relative_path_and_serves_correctly(client) -> None:
    upload_response = client.post(
        "/companies/HDFCBANK/docs/add",
        data={"period": "year:FY2024", "type": "annual", "source": "upload", "file": (BytesIO(b"%PDF-fake"), "ar.pdf")},
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    document_id = upload_response.get_json()["document_id"]

    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    row = conn.execute("SELECT raw_file_path FROM documents WHERE document_id = ?", (document_id,)).fetchone()
    # config.settings.to_repo_relative()'s own contract: relative-to-BASE_DIR
    # when the path is under BASE_DIR, an absolute fallback string otherwise
    # (its docstring) — this test's isolated DOCUMENTS_DIR (see _build_app())
    # is deliberately OUTSIDE BASE_DIR, so the meaningful check here is that
    # the stored path resolves to somewhere under that isolated dir (proving
    # the upload never touched the real repo), not the specific relative/
    # absolute string shape, which depends on where DOCUMENTS_DIR happens to be.
    stored_path = Path(row["raw_file_path"])
    resolved_path = stored_path if stored_path.is_absolute() else settings.BASE_DIR / stored_path
    assert resolved_path.is_relative_to(settings.DOCUMENTS_DIR)

    file_response = client.get(f"/companies/HDFCBANK/docs/{document_id}/file")
    assert file_response.status_code == 200
    assert file_response.data == b"%PDF-fake"


def test_document_with_only_storage_object_key_is_servable(client, monkeypatch) -> None:
    # A document written straight to S3 (storage_object_key set,
    # raw_file_path NULL -- e.g. the NSE-filing-PDF backfill) used to 404
    # on both docs-feed.json's file_url and the file route itself, because
    # each only checked raw_file_path even though the file route's own
    # presigned-URL fallback already handled storage_object_key correctly
    # three lines later -- a real gap the guard clause didn't match.
    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    conn.execute(
        "INSERT INTO documents (company_id, source, document_type, fiscal_year, quarter, "
        "storage_object_key, source_url, retrieved_at, processing_status) "
        "VALUES ('HDFCBANK', 'nse', 'financial_result', 'FY2015', 'Q2', "
        "'data/documents/HDFCBANK/fake.pdf', 'https://example.com/fake.zip', '2026-01-01', 'pending')"
    )
    conn.commit()
    document_id = conn.execute("SELECT document_id FROM documents WHERE storage_object_key = 'data/documents/HDFCBANK/fake.pdf'").fetchone()["document_id"]

    feed = client.get("/companies/HDFCBANK/docs-feed.json").get_json()
    fy2015 = next(y for y in feed["years"] if y["fy"] == "FY2015")
    q2 = next(q for q in fy2015["quarters"] if q["id"] == "q2fy2015")
    assert q2["docs"]["result"]["file_url"] == f"/companies/HDFCBANK/docs/{document_id}/file"

    class _FakeStore:
        def presigned_url(self, key: str) -> str:
            return f"https://fake-s3.example.com/{key}"

    monkeypatch.setattr("web.app.default_document_store", lambda: _FakeStore())
    file_response = client.get(f"/companies/HDFCBANK/docs/{document_id}/file")
    assert file_response.status_code == 302
    assert file_response.headers["Location"] == "https://fake-s3.example.com/data/documents/HDFCBANK/fake.pdf"


def test_docs_feed_period_options_span_2005_onward(client) -> None:
    response = client.get("/companies/HDFCBANK/docs-feed.json")
    data = response.get_json()
    annual_values = [o["value"] for o in data["annual_period_options"]]
    assert "year:FY2005" in annual_values
    assert not any(v == "year:FY2004" for v in annual_values)


def test_docs_feed_quarter_calendar_is_country_aware(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "docs_calendar.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)  # HDFCBANK -> India, default country
    register_company(conn, "AAPL", "Apple Inc.", "Apple", country="US", currency="USD")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        india = test_client.get("/companies/HDFCBANK/docs-feed.json").get_json()
        us = test_client.get("/companies/AAPL/docs-feed.json").get_json()

    india_q1 = next(o for o in india["quarter_period_options"] if o["value"].startswith("q1fy"))
    us_q1 = next(o for o in us["quarter_period_options"] if o["value"].startswith("q1fy"))
    assert "June" in india_q1["label"]
    assert "March" in us_q1["label"]
    # India's fiscal year reads as a span (e.g. "FY 2026–27"); the US
    # calendar-year convention reads as a single year (e.g. "FY 2027").
    assert "–" in india["annual_period_options"][0]["label"]
    assert "–" not in us["annual_period_options"][0]["label"]


def test_companies_page_shows_empty_state_when_no_companies(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "empty.db"
    init_db(db_path=db_path).close()
    app = _build_app(db_path, tmp_path, monkeypatch)

    with app.test_client() as test_client:
        response = test_client.get("/companies")

    assert response.status_code == 200
    assert b"No companies registered yet" in response.data


def test_home_page_is_landing(client) -> None:
    """"/" is the public marketing landing page (web/templates/landing.html)
    now, not the research tool directly — it links out to /research rather
    than embedding the question box itself."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"Find clarity in a market built on noise." in response.data
    assert b'href="/research"' in response.data


def test_home_page_has_no_company_search_box_but_the_rest_of_the_app_does(client) -> None:
    """The landing page drops the header's company search; every other page keeps it."""
    assert b"site-search-input" not in client.get("/").data
    for path in ("/research", "/companies", "/cases", "/login"):
        assert b"site-search-input" in client.get(path, follow_redirects=True).data, path


def test_research_path_renders_the_research_page(client) -> None:
    """/research is a real page now, not a legacy redirect to "/"."""
    response = client.get("/research")
    assert response.status_code == 200
    assert b"Ask your own investment question" in response.data


def test_company_report_unknown_company_is_404(client) -> None:
    response = client.get("/companies/NOPE")
    assert response.status_code == 404


def test_company_report_with_no_ingested_data_still_renders(client) -> None:
    """No PNG-chart/ledger fallback any more — every company gets the same
    dashboard template (see web/valuation_feed.py), client-rendered from a
    JSON feed. With nothing ingested that feed just comes back with no years
    and every metric all-null; the page itself still renders fine."""
    response = client.get("/companies/HDFCBANK?tab=financials")
    assert response.status_code == 200
    assert b'id="valuation-dashboard"' in response.data

    feed = client.get("/companies/HDFCBANK/valuation-feed.json").get_json()
    assert feed["YEARS"] == []
    net_profit_row = next(r for r in feed["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    assert net_profit_row["values"] == []


def test_company_report_renders_ingested_data(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "with_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        page = test_client.get("/companies/HDFCBANK?tab=financials")
        feed = test_client.get("/companies/HDFCBANK/valuation-feed.json").get_json()

    assert page.status_code == 200
    assert b'id="valuation-dashboard"' in page.data
    # No server-rendered ledger/PNG charts any more — the dashboard fetches
    # its own data client-side. A bare substring check on "data:image/png;
    # base64," would false-positive on _ask_ai.html's own client-side JS
    # (included on every page), which builds that prefix at runtime from a
    # template placeholder rather than embedding real image bytes — so look
    # for an actual base64 payload following the prefix instead.
    assert re.search(rb"data:image/png;base64,[A-Za-z0-9+/]{50,}", page.data) is None

    assert feed["YEARS"] == [2023, 2024]
    net_profit_row = next(r for r in feed["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    assert net_profit_row["values"] == [17000.0, 20500.0]


def test_company_report_invalid_statement_type_is_400(client) -> None:
    response = client.get("/companies/HDFCBANK?statement_type=bogus")
    assert response.status_code == 400


def test_company_report_defaults_to_overview_tab(client) -> None:
    """Landing on a company should show About + the financial snapshot first,
    not jump straight into the full Financials table."""
    response = client.get("/companies/HDFCBANK")
    body = response.data.decode()
    assert response.status_code == 200
    assert 'class="active">Overview' in body
    assert 'id="valuation-overview"' in body


def test_company_report_invalid_tab_is_400(client) -> None:
    response = client.get("/companies/HDFCBANK?tab=bogus")
    assert response.status_code == 400


def test_ported_dataset_company_with_no_live_data_keeps_static_financials_url(tmp_path: Path, monkeypatch) -> None:
    """A company with a ported valuation_model_file (web/static/data/*.json,
    see scripts/import_equity_analysis_workbooks.py) but zero
    canonical_financials rows must keep reading the static file for the
    Financials tab -- switching it to the live feed would blank the whole
    tab, a real regression (verified against real Neon: this is exactly
    SRG Housing Finance's situation in production today, the one ported
    company out of 21 with nothing ingested at all)."""
    db_path = tmp_path / "ported_no_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    from storage.company_repository import update_company_valuation_model_file

    update_company_valuation_model_file(conn, "HDFCBANK", "hdfcbank-analysis.json")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/companies/HDFCBANK?tab=financials")

    body = response.data.decode()
    assert response.status_code == 200
    assert 'id="valuation-dashboard" class="vm-layout" data-url="/static/data/hdfcbank-analysis.json"' in body
    # Charts tab (unconditionally live, unaffected by has_ported_dataset)
    # still points at charts-feed.json, so a blanket substring check on
    # "/charts-feed.json" would false-positive -- assert on the
    # Financials-tab data-url specifically instead, above.
    assert "/companies/HDFCBANK/charts-feed.json" not in body
    # No Annual/Quarterly or Consolidated/Standalone toggle -- the static
    # file has no period_type/statement_type concept at all. (The
    # ".vm-period-toggle" CSS rule itself is always present in <style>, so
    # check for the data attribute init() actually looks for instead.)
    assert "data-vm-period-toggle" not in body


def test_ported_dataset_company_with_live_data_switches_financials_url(tmp_path: Path, monkeypatch) -> None:
    """The same ported company, but now with at least one canonical_
    financials row ingested -- the Financials tab (and only that tab; the
    Valuation Model tab's Growth Projection calculator stays on the static
    file, see web/app.py's company_report()) should switch to the live
    charts-feed.json route instead, with the Annual/Quarterly and
    Consolidated/Standalone toggles now showing (both are period_type/
    statement_type concepts the live feed has and the static file never
    did)."""
    db_path = tmp_path / "ported_with_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    from storage.company_repository import update_company_valuation_model_file

    update_company_valuation_model_file(conn, "HDFCBANK", "hdfcbank-analysis.json")
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        response = test_client.get("/companies/HDFCBANK?tab=financials")
        feed = test_client.get("/companies/HDFCBANK/charts-feed.json").get_json()

    body = response.data.decode()
    assert response.status_code == 200
    assert 'id="valuation-dashboard" class="vm-layout" data-url="/companies/HDFCBANK/charts-feed.json' in body
    # The static file is still used, but only for the Valuation Model tab.
    assert 'id="valuation-dashboard-interactive" class="vm-layout" data-url="/static/data/hdfcbank-analysis.json"' in body
    assert "data-vm-period-toggle" in body
    assert feed["PERIODS"] == ["FY2023", "FY2024"]


def test_statement_type_toggle_switches_data(tmp_path: Path, monkeypatch) -> None:
    """The toggle now drives the valuation-feed endpoint (the dashboard's
    data source), not server-rendered page content."""
    db_path = tmp_path / "toggle.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener", statement_type="consolidated")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as test_client:
        consolidated = test_client.get("/companies/HDFCBANK/valuation-feed.json?statement_type=consolidated").get_json()
        standalone = test_client.get("/companies/HDFCBANK/valuation-feed.json?statement_type=standalone").get_json()

    consolidated_net_profit = next(r for r in consolidated["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    standalone_net_profit = next(r for r in standalone["METRICS"]["incomeStatement"] if r["key"] == "netProfit")
    assert 20500.0 in consolidated_net_profit["values"]
    assert standalone["YEARS"] == []
    assert standalone_net_profit["values"] == []


def test_highlighted_answer_never_double_escapes_ordinary_text(tmp_path: Path, monkeypatch) -> None:
    """The [FACT]/[CALCULATION]/[INFERENCE] highlighter (used by /chat and
    /research/ask, now that the company Financials page no longer renders a
    text ledger) must not corrupt or unescape the surrounding answer text —
    only those literal tokens become HTML, everything else escaped exactly once."""
    db_path = tmp_path / "escape_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Margins < 20% & rising. [FACT] some fact.")

    with app.test_client() as test_client:
        response = test_client.post("/chat", json={"question": "How's margin?", "company_ids": ["HDFCBANK"]})

    data = response.get_json()
    assert "Margins &lt; 20% &amp; rising." in data["answer_html"]  # escaped exactly once, not double-escaped
    assert '<span class="tag tag-fact">[FACT]</span>' in data["answer_html"]


# ------------------------------------------------------------------
# /chat — GET (page) and POST (JSON API). LLM calls are mocked throughout.
# ------------------------------------------------------------------


def test_chat_page_lists_companies(client) -> None:
    response = client.get("/chat")
    assert response.status_code == 200
    assert b"HDFCBANK" in response.data
    assert b"ICICIBANK" in response.data


def test_chat_page_shows_no_key_banner_when_unset(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.get("/chat")
    assert b"ANTHROPIC_API_KEY is not set" in response.data


def test_chat_page_omits_banner_when_key_set(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.get("/chat")
    assert b"ANTHROPIC_API_KEY is not set" not in response.data


def test_chat_post_returns_answer_and_charts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "chat_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Two LLM calls now: Jev's complexity classification (docs/ADR/023),
    # then the real answer -- company_ids is given explicitly in the
    # request body, so no company-resolution call happens in between.
    captured = _install_sequenced_fake_llm(monkeypatch, [
        '{"complexity_level": 3, "confidence": 0.8, "reason": "test"}',
        "Net profit rose. [FACT] x. [INFERENCE] y.",
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/chat", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["question"] == "How did net profit change?"
    assert '<span class="tag tag-fact">[FACT]</span>' in data["answer_html"]
    assert '<span class="tag tag-inference">[INFERENCE]</span>' in data["answer_html"]
    assert "net_profit" in data["charts"]["HDFCBANK"]
    assert len(captured) == 2


def test_chat_post_without_api_key_is_503(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.post("/chat", json={"question": "test", "company_ids": ["HDFCBANK"]})
    assert response.status_code == 503


def test_chat_post_without_question_is_400(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post("/chat", json={"question": "", "company_ids": ["HDFCBANK"]})
    assert response.status_code == 400


def test_chat_post_without_company_is_no_longer_rejected(client, monkeypatch) -> None:
    """company_ids=[] used to 400 outright; now it's a valid request that can
    still be grounded in Macro evidence alone (research/macro_evidence.py).
    Nothing in this test's DB matches the generic word "test", so
    answer_question()'s own "nothing matched" message comes back as a normal
    200 answer, without ever calling the (unmocked) LLM for company
    resolution or the answer itself (Jev's own complexity classification,
    docs/ADR/023, does still make one real, harmlessly-failing/falling-back
    attempt -- see test_chat_post_never_calls_llm_when_validation_fails for
    that call counted against a fake client instead)."""
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post("/chat", json={"question": "test", "company_ids": []})
    assert response.status_code == 200
    assert "No matching evidence found" in response.get_json()["answer_html"]


def test_chat_post_unregistered_company_is_404(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch)
    response = client.post("/chat", json={"question": "test", "company_ids": ["NOPE"]})
    assert response.status_code == 404


def test_chat_post_invalid_statement_type_is_400(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post(
        "/chat", json={"question": "test", "company_ids": ["HDFCBANK"], "statement_type": "bogus"}
    )
    assert response.status_code == 400


def test_chat_post_never_calls_llm_when_validation_fails(client, monkeypatch) -> None:
    """Two of these three requests genuinely fail validation (empty
    question, unregistered company) and must never reach any LLM call --
    confirmed by checking `captured` right after each one, not just at the
    end. The middle request ("test", company_ids=[]) is NOT a validation
    failure: it's a valid request that happens to name no company and find
    no evidence, and since docs/ADR/023 that still costs one Jev
    classification call (research/company_resolver.py's own cheap
    candidate pre-filter still avoids an LLM call for company resolution
    itself, since "test" matches no candidate company at all)."""
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    captured = _install_fake_llm(monkeypatch, text='{"complexity_level": 3, "confidence": 0.5, "reason": "test"}')

    client.post("/chat", json={"question": "", "company_ids": ["HDFCBANK"]})
    assert captured == []

    client.post("/chat", json={"question": "test", "company_ids": []})
    assert len(captured) == 1  # Jev's classification call, no company-resolution/answer call

    client.post("/chat", json={"question": "test", "company_ids": ["NOPE"]})
    assert len(captured) == 1  # unregistered company still 404s before Jev ever runs


# ------------------------------------------------------------------
# /research and /research/ask — the Research tab's own live-question
# composer, sharing _answer_question_response() with /chat.
# ------------------------------------------------------------------


def test_research_page_has_no_company_scope_picker(client) -> None:
    """No manual "Scope to companies" chip UI, and (since docs/ADR/023) no
    manual Quick Answer/Deep Dive toggle either — company scoping AND
    pipeline routing are both always resolved server-side, fresh, via
    /research/understand at submit time (research.html no longer embeds a
    COMPANIES list or does its own client-side text-matching fallback)."""
    response = client.get("/research")
    body = response.data.decode()
    assert response.status_code == 200
    assert "chip-toggle" not in body
    assert "case-type-toggle" not in body
    assert "const COMPANIES" not in body


def test_research_page_shows_no_key_banner_when_unset(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.get("/research")
    assert b"ANTHROPIC_API_KEY is not set" in response.data


def test_research_page_omits_banner_when_key_set(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.get("/research")
    assert b"ANTHROPIC_API_KEY is not set" not in response.data


def test_research_ask_returns_answer_and_charts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "research_ask.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Two LLM calls now: Jev's complexity classification (docs/ADR/023),
    # then the real answer -- company_ids is given explicitly in the
    # request body, so no company-resolution call happens in between.
    captured = _install_sequenced_fake_llm(monkeypatch, [
        '{"complexity_level": 3, "confidence": 0.8, "reason": "test"}',
        "Net profit rose. [FACT] x. [INFERENCE] y.",
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["question"] == "How did net profit change?"
    assert '<span class="tag tag-fact">[FACT]</span>' in data["answer_html"]
    assert "net_profit" in data["charts"]["HDFCBANK"]
    assert len(captured) == 2


def test_research_ask_level1_question_answers_via_neon_only_with_no_answer_llm_call(tmp_path: Path, monkeypatch) -> None:
    """A Level-1-shaped question (single company, single stored metric +
    fiscal year) must actually be answered by research/routing_policy.py's
    deterministic path (ADR-023), not the always-LLM answer_question()
    pipeline -- see the "Jev classifies but nothing executes the fast path"
    gap this test locks in the fix for. Only Jev's own classification call
    should reach the LLM; the answer itself comes from canonical_financials
    alone, and a signals_routing_log row should now exist for it (previously
    always zero rows for any live /research/ask traffic)."""
    from storage.repositories import list_signals_routing_log

    db_path = tmp_path / "research_ask_level1.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Only one LLM response supplied (Jev's classification) -- if the
    # deterministic path is bypassed and answer_question() runs anyway, the
    # second (missing) call raises IndexError inside _SequencedFakeMessages,
    # failing the test loudly rather than silently answering with stale data.
    captured = _install_sequenced_fake_llm(monkeypatch, [
        '{"complexity_level": 1, "confidence": 0.9, "reason": "single stored fact"}',
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask", json={"question": "What was net profit in FY2024?", "company_ids": ["HDFCBANK"]}
        )

    assert response.status_code == 200
    data = response.get_json()
    assert "[FACT]" in data["answer_html"] or "20500" in data["answer_html"] or "20,500" in data["answer_html"]
    assert "net_profit" in data["charts"]["HDFCBANK"]  # unchanged: charts still build from canonical_financials
    assert len(captured) == 1  # only the Jev classifier call -- no answer-generation LLM call

    audit_conn = init_db(db_path=db_path)
    rows = list_signals_routing_log(audit_conn)
    matching = [row for row in rows if row["question"] == "What was net profit in FY2024?"]
    assert len(matching) == 1
    assert matching[0]["jev_level"] == 1
    assert matching[0]["execution_status"] == "answered"


def test_research_ask_level1_classification_with_unextractable_question_still_uses_llm(
    tmp_path: Path, monkeypatch,
) -> None:
    """Jev classifies Level 1, but the question has no extractable metric/
    fiscal year -- Level 1 (and then Level 2) must escalate rather than
    guess, so this must fall through to the existing answer_question() path
    unchanged: two LLM calls, and no signals_routing_log row (a partial row
    here would misrepresent what actually answered the question)."""
    from storage.repositories import list_signals_routing_log

    db_path = tmp_path / "research_ask_level1_escalates.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    captured = _install_sequenced_fake_llm(monkeypatch, [
        '{"complexity_level": 1, "confidence": 0.4, "reason": "vague"}',
        "General commentary. [FACT] x. **Confidence:** Low -- vague question.",
    ])

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask", json={"question": "Tell me about HDFC Bank's profitability", "company_ids": ["HDFCBANK"]}
        )

    assert response.status_code == 200
    assert len(captured) == 2  # Jev's call, then the answer-generation call

    audit_conn = init_db(db_path=db_path)
    rows = list_signals_routing_log(audit_conn)
    assert rows == []


def test_research_ask_renders_markdown_structure_in_answer(tmp_path: Path, monkeypatch) -> None:
    """The model routinely answers a multi-part question with headers/bold/
    lists (research/assistant.py's SYSTEM_PROMPT doesn't forbid it) — the
    answer must come back as real HTML structure, not the literal "## "/"**"
    text _highlight_tags alone would leave escaped in one unbroken blob."""
    db_path = tmp_path / "research_ask_markdown.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(
        monkeypatch,
        text="## Short answer\nNet profit rose. [FACT] x.\n\n## Detail\n- **Point one**\n- Point two",
    )

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )

    answer_html = response.get_json()["answer_html"]
    assert "<h3>Short answer</h3>" in answer_html
    assert "<h3>Detail</h3>" in answer_html
    assert "<li><strong>Point one</strong></li>" in answer_html
    assert "## " not in answer_html  # not left as literal escaped markdown


def test_research_ask_without_api_key_is_503(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.post("/research/ask", json={"question": "test", "company_ids": ["HDFCBANK"]})
    assert response.status_code == 503


def test_research_ask_without_company_is_no_longer_rejected(client, monkeypatch) -> None:
    """Same relaxation as /chat (see test_chat_post_without_company_is_no_longer_rejected)."""
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post("/research/ask", json={"question": "test", "company_ids": []})
    assert response.status_code == 200
    assert "No matching evidence found" in response.get_json()["answer_html"]


def test_research_ask_without_company_uses_macro_evidence(client, monkeypatch) -> None:
    """End-to-end: a rainfall question with no company named gets grounded in
    real ingested IITM data (research/macro_evidence.py), not rejected for
    lacking a company scope."""
    import config.settings as settings
    from sources.macro import MacroNormalizedObservation
    from storage.database import init_db as _init_db
    from storage.repositories import insert_macro_observations

    # Reach the same DB the app is using (config.settings.DB_PATH was
    # monkeypatched onto the test's tmp_path in the `client` fixture).
    conn = _init_db(db_path=settings.DB_PATH)
    insert_macro_observations(
        conn,
        [
            MacroNormalizedObservation(
                series_key="rainfall_regional_annual", period_type="annual", period="2020",
                value=1100.5, unit="MILLIMETRES", source="iitm",
                source_file="data/raw/_macro/iitm/8-all_ind.txt", parser_version="test-v1",
            )
        ],
    )
    conn.close()

    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Three distinct LLM calls happen now: Jev's complexity classification
    # first (docs/ADR/023, falls back gracefully -- its prompt doesn't match
    # either branch below so it just gets whichever text happens to come
    # back and fails to parse as JSON, harmlessly), then research/
    # macro_evidence.py's own planner call ("Catalog: ..." — which
    # series/years apply), then the main answer call ("Evidence: ...")
    # grounded in what it picked. Dispatched by prompt content below, not
    # call order, so the extra call doesn't need special-casing.
    captured: list = []

    def fake_generate(**kwargs):
        content = kwargs["messages"][0]["content"]
        text = "SERIES: rainfall_regional_annual\nYEARS: ALL" if content.startswith("Catalog:") else (
            "Rainfall was steady. [FACT] some fact."
        )
        captured.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")

    monkeypatch.setattr(
        "llm.providers.anthropic_provider.anthropic.Anthropic",
        lambda *a, **kw: SimpleNamespace(messages=SimpleNamespace(create=fake_generate)),
    )
    response = client.post("/research/ask", json={"question": "What was rainfall in India?", "company_ids": []})

    assert response.status_code == 200
    assert response.get_json()["answer_html"]
    # "Evidence (Docs/Macro):", not "Evidence:" — research/assistant.py splits
    # Financials (cacheable, its own content block) from Docs/Macro evidence
    # (question-dependent, stays in the plain user_message); no company_ids
    # here means no Financials evidence at all, so content stays a plain
    # string rather than becoming a content-block list.
    evidence_call = next(
        c for c in captured if c["messages"][0]["content"].startswith("Evidence (Docs/Macro):")
    )
    assert "Rainfall Regional 2020" in evidence_call["messages"][0]["content"]


def test_research_ask_unregistered_company_is_404(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch)
    response = client.post("/research/ask", json={"question": "test", "company_ids": ["NOPE"]})
    assert response.status_code == 404


def test_research_ask_supports_peer_comparison(tmp_path: Path, monkeypatch) -> None:
    """The chip picker allows multiple companies — the backend already supports
    peer comparison, this just exercises it through the new endpoint rather
    than the CLI's --company flag."""
    db_path = tmp_path / "research_ask_peer.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    for company_id in ("HDFCBANK", "ICICIBANK"):
        file_path = tmp_path / f"{company_id}.xlsx"
        _make_screener_workbook(file_path)
        ingest_file(conn, file_path, company_id=company_id, source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Comparison. [FACT] x.")

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask",
            json={"question": "Compare these banks", "company_ids": ["HDFCBANK", "ICICIBANK"]},
        )

    assert response.status_code == 200
    data = response.get_json()
    # A >1-company request gets combined comparison charts, not a separate
    # per-company chart set — that's the whole point of comparing them.
    assert data["charts"] == {}
    assert "net_profit" in data["comparison_charts"]


def test_company_ask_saves_answer_as_a_thread(tmp_path: Path, monkeypatch) -> None:
    """The Ask AI drawer's answers, unlike /research/ask, get auto-saved as a
    thread — timestamped, and listed on the company's own Threads tab."""
    db_path = tmp_path / "company_ask.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")

    with app.test_client() as test_client:
        response = test_client.post(
            "/companies/HDFCBANK/ask", json={"question": "How did net profit change?"}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["thread_id"]
        assert data["thread_url"] == f"/research/thread/{data['thread_id']}"

        # The old thread URL redirects to the case page that now shows it.
        assert test_client.get(data["thread_url"]).status_code == 301
        thread_page = test_client.get(data["thread_url"], follow_redirects=True)
        assert thread_page.status_code == 200
        assert b"How did net profit change?" in thread_page.data

        threads_tab = test_client.get("/companies/HDFCBANK?tab=threads").data.decode()
    # Ask AI is a Conversation: it lands in the company page's Conversations section
    # (a filtered view of the same case the Cases page lists), linking to its thread.
    assert 'id="sec-cases"' in threads_tab
    assert f'{data["conversation_url"]}"' in threads_tab  # opens the conversation, where follow-ups happen
    assert "How did net profit change?" in threads_tab


# ------------------------------------------------------------------
# /companies/<id>/ask-async, /research/ask-async, /chat-async + /ask/status
# -- async+poll counterparts of the synchronous ask routes above, added
# after a real production incident: a slow question (or, as happened
# live, a company with a slow-to-parse uploaded PDF) can take long enough
# to exceed gunicorn's own worker timeout or the platform's fronting load
# balancer timeout, either of which kills a synchronous request out from
# under the browser ("Network error: Unexpected token '<' ... is not
# valid JSON") even though the answer would have finished fine left
# running. Same pattern as /investigate/generate-async.
# ------------------------------------------------------------------


def test_company_ask_async_runs_in_background_and_status_reaches_done(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "company_ask_async.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")

    with app.test_client() as test_client:
        start = test_client.post("/companies/HDFCBANK/ask-async", json={"question": "How did net profit change?"})
        assert start.status_code == 202
        job_id = start.get_json()["job_id"]

        import time

        status = None
        for _ in range(200):  # LLM is faked, but real embedding-model loading can be slow on a cold run
            status = test_client.get(f"/ask/status/{job_id}").get_json()
            if status["status"] != "running":
                break
            time.sleep(0.1)

        assert status["status"] == "done"
        result = status["result"]
        assert result["thread_id"]
        assert result["thread_url"] == f"/research/thread/{result['thread_id']}"

        thread_page = test_client.get(result["thread_url"], follow_redirects=True)
        assert thread_page.status_code == 200
        assert b"How did net profit change?" in thread_page.data


def test_ask_async_requires_a_question(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post("/research/ask-async", json={"question": "", "company_ids": []})
    assert response.status_code == 400


def test_ask_async_requires_api_key(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.post("/research/ask-async", json={"question": "test", "company_ids": []})
    assert response.status_code == 503


def test_ask_status_unknown_job_id_is_404(client) -> None:
    response = client.get("/ask/status/not-a-real-job-id")
    assert response.status_code == 404


def test_ask_async_reports_error_status_on_llm_failure(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)

    import anthropic

    def _failing_answer_question(*args, **kwargs):
        raise anthropic.APIConnectionError(request=SimpleNamespace())

    monkeypatch.setattr("web.app.answer_question", _failing_answer_question)

    start = client.post("/research/ask-async", json={"question": "test", "company_ids": ["HDFCBANK"]})
    assert start.status_code == 202
    job_id = start.get_json()["job_id"]

    import time

    status = None
    for _ in range(50):
        status = client.get(f"/ask/status/{job_id}").get_json()
        if status["status"] != "running":
            break
        time.sleep(0.05)

    assert status["status"] == "error"
    assert "assistant request failed" in status["error"]


def test_research_ask_async_and_chat_async_are_wired(client, monkeypatch) -> None:
    """Not full end-to-end runs (test_company_ask_async_... above already
    covers that) -- just confirms both other -async routes exist and
    accept a request, since they share _answer_question_async_response()
    with company_ask_async but are reached via different URLs."""
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    for url in ("/research/ask-async", "/chat-async"):
        response = client.post(url, json={"question": "test", "company_ids": []})
        assert response.status_code == 202
        assert response.get_json()["job_id"]


def test_research_ask_saves_a_thread(tmp_path: Path, monkeypatch) -> None:
    """Like the per-company Ask AI drawer, the Research tab's own composer
    (/research/ask, possibly multi-company) now also persists every answer
    into generated_reports — so it shows up on Investigations/Threads and
    (research/assistant.py's reuse-before-recompute) can be served again
    without a fresh LLM call. Previously this path stayed ephemeral; that
    silently broke the intent of an always-growing knowledge base."""
    db_path = tmp_path / "research_ask_saves_thread.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/ask", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["thread_id"] is not None
        assert data["thread_url"] is not None

        threads_tab = test_client.get("/companies/HDFCBANK?tab=threads").data.decode()
    assert "How did net profit change?" in threads_tab


def test_research_ask_appears_in_investigations_and_reuses_on_repeat(tmp_path: Path, monkeypatch) -> None:
    """End-to-end version of the above: the saved thread shows up on
    /investigations (not just the per-company Threads tab), and asking the
    same question again is served from that saved row — a second, real LLM
    call never happens (context/reuse.py, SIMILARITY_THRESHOLD=0.8)."""
    db_path = tmp_path / "research_ask_reuse.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Jev's complexity classification (docs/ADR/023) runs on EVERY request,
    # reuse hit or not -- it decides/tags the level before the reuse check
    # even happens, so it's not itself skipped by a reuse hit. Only the
    # real answer-generating call is skipped the second time.
    captured = _install_sequenced_fake_llm(monkeypatch, [
        '{"complexity_level": 3, "confidence": 0.8, "reason": "test"}',
        "Net profit rose. [FACT] x.",
    ])

    with app.test_client() as test_client:
        first = test_client.post(
            "/research/ask", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )
        assert first.status_code == 200
        assert len(captured) == 2  # Jev classification + one real answer LLM call

        investigations_page = test_client.get("/cases").data.decode()
        assert "How did net profit change?" in investigations_page

        second = test_client.post(
            "/research/ask", json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]}
        )
        assert second.status_code == 200
        assert len(captured) == 3  # +1 for the second request's Jev call; the answer itself was reused
        assert second.get_json()["answer_html"] == first.get_json()["answer_html"]


def test_research_thread_delete_removes_it(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "thread_delete.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")

    with app.test_client() as test_client:
        ask_response = test_client.post(
            "/companies/HDFCBANK/ask", json={"question": "How did net profit change?"}
        )
        thread_id = ask_response.get_json()["thread_id"]

        delete_response = test_client.post(f"/research/thread/{thread_id}/delete")
        assert delete_response.status_code == 200
        assert delete_response.get_json() == {"ok": True}

        assert test_client.get(f"/research/thread/{thread_id}").status_code == 404
        threads_tab = test_client.get("/companies/HDFCBANK?tab=threads").data.decode()
    assert "No cases yet" in threads_tab


def test_research_thread_delete_also_drops_it_from_watchlist(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "thread_delete_watchlist.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text="Net profit rose. [FACT] x.")

    with app.test_client() as test_client:
        ask_response = test_client.post(
            "/companies/HDFCBANK/ask", json={"question": "How did net profit change?"}
        )
        thread_id = ask_response.get_json()["thread_id"]
        test_client.post("/watchlist/add", data={"item_type": "thread", "item_ref": thread_id})

        test_client.post(f"/research/thread/{thread_id}/delete")

        listing = test_client.get("/watchlist").data.decode()
    assert "Nothing pinned yet" in listing


def test_research_thread_delete_unknown_thread_is_404(client) -> None:
    response = client.post("/research/thread/does-not-exist/delete")
    assert response.status_code == 404


def test_research_thread_delete_example_investigation_is_404(client) -> None:
    """The 3 hand-written example investigations (web/fixtures.py) aren't DB
    rows and can't be deleted."""
    response = client.post("/research/thread/bank-rates/delete")
    assert response.status_code == 404


# ------------------------------------------------------------------
# /watchlist — list, pin, unpin, and the pin/unpin toggle on the
# Company and Research-thread pages that post to it.
# ------------------------------------------------------------------


def test_watchlist_shows_empty_state_when_nothing_pinned(client) -> None:
    response = client.get("/watchlist")
    assert response.status_code == 200
    assert b"Nothing pinned yet" in response.data


def test_watchlist_add_company_then_appears_on_watchlist(client, monkeypatch) -> None:
    # watchlist() live-fetches news for every watchlisted company on each
    # page load (see web/app.py) -- stub it out so this test never makes a
    # real network call.
    monkeypatch.setattr("web.app.fetch_company_news", lambda *a, **kw: None)
    response = client.post(
        "/watchlist/add", data={"item_type": "company", "item_ref": "HDFCBANK", "next": "/watchlist"}
    )
    assert response.status_code == 302
    listing = client.get("/watchlist")
    assert b"HDFC Bank" in listing.data
    assert b"Nothing pinned yet" not in listing.data


def test_watchlist_add_thread_then_appears_on_watchlist(tmp_path: Path, monkeypatch) -> None:
    """Threads pinned to the watchlist must be real generated_reports rows —
    web/fixtures.py's THREADS mock is intentionally empty (every hand-written
    example there was swapped for a real, already-run Deep Dive investigation
    instead), so there's no more fixture thread id like the old "bank-rates"
    to pin against; watchlist_add() 404s on anything that isn't a real row."""
    db_path = tmp_path / "watchlist_thread.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_signals_llm(monkeypatch, text="## The Short Answer\nNet profit rose. [FACT] x.")

    with app.test_client() as test_client:
        generated = test_client.post(
            "/research/thread/generate",
            json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]},
        )
        thread_id = generated.get_json()["url"].rsplit("/", 1)[-1]

        add_response = test_client.post(
            "/watchlist/add", data={"item_type": "thread", "item_ref": thread_id, "next": "/watchlist"}
        )
        assert add_response.status_code == 302
        listing = test_client.get("/watchlist")

    assert b"How did net profit change?" in listing.data


def test_watchlist_add_unregistered_company_is_404(client) -> None:
    response = client.post("/watchlist/add", data={"item_type": "company", "item_ref": "NOPE"})
    assert response.status_code == 404


def test_watchlist_add_unknown_thread_is_404(client) -> None:
    response = client.post("/watchlist/add", data={"item_type": "thread", "item_ref": "nope"})
    assert response.status_code == 404


def test_watchlist_add_invalid_item_type_is_400(client) -> None:
    response = client.post("/watchlist/add", data={"item_type": "bogus", "item_ref": "HDFCBANK"})
    assert response.status_code == 400


def test_watchlist_remove_drops_it_from_the_list(client) -> None:
    client.post("/watchlist/add", data={"item_type": "company", "item_ref": "HDFCBANK"})
    client.post("/watchlist/remove", data={"item_type": "company", "item_ref": "HDFCBANK"})
    listing = client.get("/watchlist")
    assert b"Nothing pinned yet" in listing.data


def test_watchlist_add_redirects_to_next_when_same_site(client) -> None:
    response = client.post(
        "/watchlist/add",
        data={"item_type": "company", "item_ref": "HDFCBANK", "next": "/companies/HDFCBANK?tab=overview"},
    )
    assert response.headers["Location"] == "/companies/HDFCBANK?tab=overview"


def test_watchlist_add_ignores_off_site_next(client) -> None:
    """The `next` field must never send a redirect off this site (open-redirect guard)."""
    response = client.post(
        "/watchlist/add",
        data={"item_type": "company", "item_ref": "HDFCBANK", "next": "https://evil.example.com/phish"},
    )
    assert response.headers["Location"] == "/watchlist"


def test_company_page_toggle_reflects_watchlist_state(client) -> None:
    # "is-pinned" always appears once, in the stylesheet's rule for the class —
    # check the button's own class attribute, not substring presence anywhere.
    not_pinned = client.get("/companies/HDFCBANK")
    assert b"Add to watchlist" in not_pinned.data
    assert b'class="watchlist-toggle-btn is-pinned"' not in not_pinned.data

    client.post("/watchlist/add", data={"item_type": "company", "item_ref": "HDFCBANK"})

    pinned = client.get("/companies/HDFCBANK")
    assert b"Watchlisted" in pinned.data
    assert b'class="watchlist-toggle-btn is-pinned"' in pinned.data


def test_research_thread_toggle_reflects_watchlist_state(tmp_path: Path, monkeypatch) -> None:
    """Same fixture-removal reasoning as
    test_watchlist_add_thread_then_appears_on_watchlist above — needs a real
    generated_reports row instead of the old "bank-rates" THREADS fixture."""
    db_path = tmp_path / "thread_toggle.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_signals_llm(monkeypatch, text="## The Short Answer\nNet profit rose. [FACT] x.")

    with app.test_client() as test_client:
        generated = test_client.post(
            "/research/thread/generate",
            json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]},
        )
        thread_id = generated.get_json()["url"].rsplit("/", 1)[-1]

        not_pinned = test_client.get(f"/research/thread/{thread_id}", follow_redirects=True)
        assert b"Add to watchlist" in not_pinned.data

        test_client.post("/watchlist/add", data={"item_type": "thread", "item_ref": thread_id})

        pinned = test_client.get(f"/research/thread/{thread_id}", follow_redirects=True)
    assert b"Watchlisted" in pinned.data


def test_watchlist_news_unregistered_company_is_404(client) -> None:
    response = client.get("/watchlist/news/NOPE")
    assert response.status_code == 404


class _FakeOpenRouterResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": self._text}, "finish_reason": "end_turn"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }


def _install_fake_signals_llm(monkeypatch, text: str = "## The Short Answer\nGrew. [FACT] x."):
    """research/signals_report.py (and research/insights.py) default to
    OpenRouter (config.settings.OPENROUTER_MODEL_ID), not Anthropic, as of
    2026-09-27 — this fakes that HTTP call instead of the Anthropic client
    _install_fake_llm above mocks for research/assistant.py's auto-routing,
    which still prefers Claude."""
    import dataclasses

    from llm import capability_registry

    captured: list = []
    monkeypatch.setattr("llm.providers.openrouter_provider.OPENROUTER_API_KEY_SET", True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    # capability_registry.MODELS bakes ModelSpec.enabled in at import time
    # from OPENROUTER_API_KEY_SET (False in a real test env, no key set) —
    # monkeypatching that name doesn't reach the already-built ModelSpec, so
    # the pinned-model path in llm/router.py's _fallback_chain would still
    # see this candidate as disabled without patching the spec itself too.
    spec = capability_registry.get_model(capability_registry.OPENROUTER_MODEL_ID)
    enabled_spec = dataclasses.replace(spec, enabled=True)
    monkeypatch.setattr(
        capability_registry, "MODELS",
        [enabled_spec if m.model_id == spec.model_id else m for m in capability_registry.MODELS],
    )

    def _fake_post(url, **kwargs):
        captured.append(kwargs)
        return _FakeOpenRouterResponse(text)

    monkeypatch.setattr("llm.providers.openrouter_provider.requests.post", _fake_post)
    return captured


def test_research_thread_generate_creates_a_thread_and_page(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_signals_llm(monkeypatch, text="## The Short Answer\nNet profit rose. [FACT] x.")

    with app.test_client() as test_client:
        response = test_client.post(
            "/research/thread/generate",
            json={"question": "How did net profit change?", "company_ids": ["HDFCBANK"]},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["url"].startswith("/research/thread/")

        page = test_client.get(data["url"], follow_redirects=True)
        assert page.status_code == 200
        assert b"How did net profit change?" in page.data
        assert b"The Short Answer" in page.data
        assert b'<span class="tag tag-fact">[FACT]</span>' in page.data
        # Specifically the visible question block, not just an incidental
        # match in <title> -- the real (generated-report) branch used to
        # render no question at all (only the legacy example-thread branch
        # had a thread-question-block, keyed off a `thread` var this branch
        # never passes), so the question silently vanished from every real
        # saved report's page while the answer rendered fine.
        page_html = page.data.decode()
        question_block_start = page_html.index('class="thread-question-block"')
        question_block_end = page_html.index('class="signals-report-meta"', question_block_start)
        assert "How did net profit change?" in page_html[question_block_start:question_block_end]


def test_research_thread_generate_without_api_key_is_503(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.post(
        "/research/thread/generate", json={"question": "test", "company_ids": ["HDFCBANK"]}
    )
    assert response.status_code == 503


def test_research_thread_generate_without_question_is_400(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post(
        "/research/thread/generate", json={"question": "", "company_ids": ["HDFCBANK"]}
    )
    assert response.status_code == 400


def test_generated_report_appears_in_investigations(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    report_text = "# Is HDFC Bank still growing profit?\n\n## The Short Answer\nYes. [FACT] x.\n\n**Confidence:** High\n"
    _install_fake_signals_llm(monkeypatch, text=report_text)

    with app.test_client() as test_client:
        generate_response = test_client.post(
            "/research/thread/generate",
            json={"question": "Is HDFC Bank still growing profit?", "company_ids": ["HDFCBANK"]},
        )
        thread_id = generate_response.get_json()["thread_id"]

        page = test_client.get("/cases")

    assert page.status_code == 200
    body = page.data.decode()
    assert "Is HDFC Bank still growing profit?" in body
    assert "High confidence" in body
    # Type badge and company label are separate elements in the unified
    # investigations feed (web/templates/investigations.html), not
    # concatenated into one string any more. "Quick Answer" is the
    # user-facing label for a generated (single-pass) report.
    assert ">Investigation<" in body  # no Jev level on this legacy-shaped save
    assert "HDFC Bank" in body
    assert re.search(r'href="/cases/[0-9a-f]+"', body)  # the row opens the case page
    # The 3 hand-written EXAMPLES/THREADS fixtures (web/fixtures.py) are
    # illustrative, not real data — deliberately not mixed into this real,
    # growing feed (they still show up on the Research page's own "try an
    # example" showcase instead).
    assert "Kalyan Bank" not in body


def test_generated_report_appears_under_every_named_companys_threads_tab(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    register_company(conn, company_id="DELTACORP", legal_name="Delta Corp Ltd.", display_name="Delta Corp")
    for company_id in ("HDFCBANK", "ICICIBANK"):
        file_path = tmp_path / f"{company_id}.xlsx"
        _make_screener_workbook(file_path)
        ingest_file(conn, file_path, company_id=company_id, source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    report_text = "# Compare HDFC Bank and ICICI Bank profit growth\n\n## The Short Answer\nBoth grew. [FACT] x.\n\n**Confidence:** Moderate\n"
    _install_fake_signals_llm(monkeypatch, text=report_text)

    with app.test_client() as test_client:
        generate_response = test_client.post(
            "/research/thread/generate",
            json={
                "question": "Compare HDFC Bank and ICICI Bank profit growth",
                "company_ids": ["HDFCBANK", "ICICIBANK"],
            },
        )
        thread_id = generate_response.get_json()["thread_id"]

        hdfc_page = test_client.get("/companies/HDFCBANK?tab=threads").data.decode()
        icici_page = test_client.get("/companies/ICICIBANK?tab=threads").data.decode()
        unrelated_page = test_client.get("/companies/DELTACORP?tab=threads").data.decode()

    assert re.search(r'href="/cases/[0-9a-f]+"', hdfc_page)
    assert "Compare HDFC Bank and ICICI Bank profit growth" in hdfc_page
    assert "with ICICI Bank" in hdfc_page

    assert re.search(r'href="/cases/[0-9a-f]+"', icici_page)
    assert "with HDFC Bank" in icici_page

    assert 'href="/cases/' not in unrelated_page.split('id="sec-cases"')[1].split("</section>")[0]
    assert "No cases yet" in unrelated_page


def test_watchlisted_generated_report_appears_in_watchlist(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "signals_data.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    file_path = tmp_path / "HDFCBANK.xlsx"
    _make_screener_workbook(file_path)
    ingest_file(conn, file_path, company_id="HDFCBANK", source_id="screener")
    conn.close()

    app = _build_app(db_path, tmp_path, monkeypatch)
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    report_text = "# Is HDFC Bank still growing profit?\n\n## The Short Answer\nYes. [FACT] x.\n\n**Confidence:** High\n"
    _install_fake_signals_llm(monkeypatch, text=report_text)

    with app.test_client() as test_client:
        generate_response = test_client.post(
            "/research/thread/generate",
            json={"question": "Is HDFC Bank still growing profit?", "company_ids": ["HDFCBANK"]},
        )
        thread_id = generate_response.get_json()["thread_id"]

        test_client.post("/watchlist/add", data={"item_type": "thread", "item_ref": thread_id})
        watchlist_page = test_client.get("/watchlist")

    assert watchlist_page.status_code == 200
    body = watchlist_page.data.decode()
    # The redesigned Watchlist rail's "Pinned threads" group (web/templates/
    # watchlist.html) shows only each thread's title, no confidence
    # subtitle -- that line lived in the old flat-list layout this replaced.
    assert "Is HDFC Bank still growing profit?" in body


def test_add_note_sanitizes_html_and_renders_on_company_page(client) -> None:
    response = client.post(
        "/companies/HDFCBANK/notes/add",
        json={"html": "<div><b>bold point</b><script>alert(1)</script></div>"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert "<b>bold point</b>" in data["html"]
    assert "<script" not in data["html"]

    # The note's HTML is seeded into the page via #notes-initial-data (a
    # tojson-escaped JSON blob, rendered into the DOM by notes_panel.js at
    # runtime) rather than appearing as literal tags in the page source —
    # the sanitization itself is already verified above at the API-response
    # level; this just confirms the saved note actually reaches the page.
    page = client.get("/companies/HDFCBANK?tab=notes")
    assert b"bold point" in page.data


def test_add_note_rejects_blank_content(client) -> None:
    # A contenteditable box with nothing typed serializes to this, not "" —
    # see web/app.py's _is_blank_note_html.
    response = client.post("/companies/HDFCBANK/notes/add", json={"html": "<div><br></div>"})
    assert response.status_code == 400


def test_edit_note_sanitizes_html(client) -> None:
    add_response = client.post("/companies/HDFCBANK/notes/add", json={"html": "<div>original</div>"})
    note_id = add_response.get_json()["note_id"]

    edit_response = client.post(
        f"/companies/HDFCBANK/notes/{note_id}/edit",
        json={"html": "<div>edited<img src=x onerror=alert(1)></div>"},
    )
    assert edit_response.status_code == 200
    data = edit_response.get_json()
    assert "edited" in data["html"]
    assert "<img" not in data["html"]
    assert data["updated_at"] is not None


def test_delete_note_removes_it(client) -> None:
    add_response = client.post("/companies/HDFCBANK/notes/add", json={"html": "<div>gone soon</div>"})
    note_id = add_response.get_json()["note_id"]

    delete_response = client.post(f"/companies/HDFCBANK/notes/{note_id}/delete")
    assert delete_response.status_code == 200

    page = client.get("/companies/HDFCBANK?tab=notes")
    assert b"gone soon" not in page.data


def test_note_attachment_upload_serve_and_delete(client) -> None:
    add_response = client.post("/companies/HDFCBANK/notes/add", json={"html": "<div>see attached</div>"})
    note_id = add_response.get_json()["note_id"]

    upload_response = client.post(
        f"/companies/HDFCBANK/notes/{note_id}/attachments/add",
        data={"file": (BytesIO(b"hello world"), "memo.txt")},
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 200
    attachment = upload_response.get_json()
    assert attachment["filename"] == "memo.txt"
    assert attachment["size_bytes"] == len(b"hello world")

    file_response = client.get(attachment["url"])
    assert file_response.status_code == 200
    assert file_response.data == b"hello world"

    page = client.get("/companies/HDFCBANK?tab=notes")
    assert b"memo.txt" in page.data

    delete_response = client.post(
        f"/companies/HDFCBANK/notes/{note_id}/attachments/{attachment['attachment_id']}/delete"
    )
    assert delete_response.status_code == 200
    assert client.get(attachment["url"]).status_code == 404


def test_note_attachment_add_without_file_is_400(client) -> None:
    add_response = client.post("/companies/HDFCBANK/notes/add", json={"html": "<div>note</div>"})
    note_id = add_response.get_json()["note_id"]

    response = client.post(f"/companies/HDFCBANK/notes/{note_id}/attachments/add", data={})
    assert response.status_code == 400


def test_header_search_matches_by_name_and_id(client) -> None:
    response = client.get("/companies/search.json?q=HDFC")
    assert response.status_code == 200
    ids = [r["company_id"] for r in response.get_json()["results"]]
    assert "HDFCBANK" in ids


def test_header_search_empty_query_returns_no_results(client) -> None:
    response = client.get("/companies/search.json?q=")
    assert response.get_json()["results"] == []


def test_header_search_no_match_returns_empty_list(client) -> None:
    response = client.get("/companies/search.json?q=zzzznotarealcompanyzzzz")
    assert response.status_code == 200
    assert response.get_json()["results"] == []


# ------------------------------------------------------------------
# /admin/usage — LLM token/cost observability (storage.repositories.
# get_llm_usage_summary), admin-only since it's an operator concern.
# ------------------------------------------------------------------


def test_admin_usage_requires_login(client) -> None:
    response = client.get("/admin/usage")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_admin_usage_requires_admin_role(client) -> None:
    import config.settings as settings
    from storage.repositories import create_user
    from werkzeug.security import generate_password_hash

    conn = init_db(db_path=settings.DB_PATH)
    user_id = create_user(conn, "reader@example.com", generate_password_hash("pw"))
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    response = client.get("/admin/usage")
    assert response.status_code == 403


def test_admin_usage_shows_summary_for_admin(client) -> None:
    import config.settings as settings
    from llm import observability
    from llm.hardness import Tier, fixed
    from llm.providers.base import ProviderResponse
    from llm.router import Attempt, RouteResult
    from storage.repositories import get_user_by_login

    conn = init_db(db_path=settings.DB_PATH)
    admin_user = get_user_by_login(conn, "admin")
    result = RouteResult(
        response=ProviderResponse(
            text="answer", stop_reason="end_turn", input_tokens=1000, output_tokens=200,
            model="claude-sonnet-5", provider="anthropic",
        ),
        hardness=fixed(Tier.STANDARD, "test"),
        attempts=[Attempt("claude-sonnet-5", "anthropic", "success")],
        fallback_used=False, latency_ms=100.0,
    )
    observability.record(
        conn, task_name="assistant_qa", company_ids=["HDFCBANK"],
        question="How did net profit grow?", result=result,
    )
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["user_id"]

    response = client.get("/admin/usage")
    body = response.data.decode()

    assert response.status_code == 200
    assert "assistant_qa" in body
    assert "claude-sonnet-5" in body
    assert "How did net profit grow?" in body


def test_profile_dropdown_links_to_usage_for_admin(client) -> None:
    import config.settings as settings
    from storage.repositories import get_user_by_login

    conn = init_db(db_path=settings.DB_PATH)
    admin_user = get_user_by_login(conn, "admin")
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["user_id"]

    body = client.get("/").data.decode()
    assert 'href="/admin/usage"' in body


# ------------------------------------------------------------------
# /admin Companies panel pagination — with ~2,600 real companies, rendering
# one <form> per row (not just a display row) and querying index tags for
# every single one on each page load made the page hang. Only the current
# page's rows should be queried/rendered now.
# ------------------------------------------------------------------


def _admin_session(client) -> None:
    import config.settings as settings
    from storage.repositories import get_user_by_login

    conn = init_db(db_path=settings.DB_PATH)
    admin_user = get_user_by_login(conn, "admin")
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["user_id"]


def test_admin_companies_panel_is_paginated(client) -> None:
    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    for i in range(60):  # + the seeded HDFCBANK/ICICIBANK = 62 total
        register_company(conn, f"TESTCO{i:03d}", f"Test Co {i} Ltd", f"Test Co {i}")
    conn.close()

    _admin_session(client)

    page1 = client.get("/settings?panel=admin-companies").data.decode()
    assert page1.count("admin-row") <= 100  # ~50 rows worth of open+close markers, not 62
    assert "Page 1 of 2" in page1
    assert "Next &rarr;" in page1
    assert "&larr; Previous" not in page1

    page2 = client.get("/settings?panel=admin-companies&page=2").data.decode()
    assert "Page 2 of 2" in page2
    assert "&larr; Previous" in page2
    assert "Next &rarr;" not in page2


def test_admin_companies_panel_page_out_of_range_clamps(client) -> None:
    _admin_session(client)
    response = client.get("/settings?panel=admin-companies&page=999")
    assert response.status_code == 200  # clamped to the last real page, not a 404/500


def test_admin_companies_panel_no_pagination_ui_when_it_all_fits(client) -> None:
    _admin_session(client)
    body = client.get("/settings?panel=admin-companies").data.decode()
    assert 'class="admin-pagination"' not in body  # only 2 seeded companies — well under one page


# ------------------------------------------------------------------
# /admin Companies panel search/filters — mirrors /companies (index.html),
# but applied server-side before pagination so a match on a different page
# is still found, not just within the current page's ~50 rows.
# ------------------------------------------------------------------


def test_admin_companies_search_finds_a_match_on_a_later_page(client) -> None:
    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    for i in range(60):
        register_company(conn, f"TESTCO{i:03d}", f"Test Co {i} Ltd", f"Test Co {i}")
    register_company(conn, "NEEDLE", "Needle In A Haystack Ltd", "Needle Co", sector="Energy")
    conn.close()

    _admin_session(client)

    body = client.get("/settings?panel=admin-companies&q=needle").data.decode()
    assert "Needle Co" in body
    assert 'class="admin-pagination"' not in body  # one match — no pagination needed
    assert 'value="Test Co 0"' not in body  # the other 60 companies are filtered out (Import dropdown lists all, ignore that)


def test_admin_companies_sector_filter(client) -> None:
    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    register_company(conn, "ENERGYCO", "Energy Co Ltd", "Energy Co", sector="Energy")
    conn.close()

    _admin_session(client)

    body = client.get("/settings?panel=admin-companies&sector=Energy").data.decode()
    assert "Energy Co" in body
    assert 'value="HDFC Bank"' not in body  # seeded HDFCBANK is Financial Services, not Energy (Import dropdown lists all, ignore that)


def test_admin_companies_status_filter(client) -> None:
    _admin_session(client)

    archived = client.get("/settings?panel=admin-companies&status=archived").data.decode()
    assert 'value="HDFC Bank"' not in archived  # seeded companies are active, not archived (Import dropdown lists all, ignore that)

    active = client.get("/settings?panel=admin-companies&status=active").data.decode()
    assert 'value="HDFC Bank"' in active


def test_admin_companies_no_matches_shows_filtered_empty_state(client) -> None:
    _admin_session(client)
    body = client.get("/settings?panel=admin-companies&q=zzz-not-a-real-company-zzz").data.decode()
    assert "No companies match these filters." in body
    assert "No companies registered yet" not in body  # wrong empty-state message for this case


def test_admin_companies_clear_link_only_shown_when_filters_active(client) -> None:
    _admin_session(client)
    unfiltered = client.get("/settings?panel=admin-companies").data.decode()
    assert ">Clear</a>" not in unfiltered

    filtered = client.get("/settings?panel=admin-companies&q=hdfc").data.decode()
    assert ">Clear</a>" in filtered


# ------------------------------------------------------------------
# /admin "Sectors, Industries & Tags" panel — add/rename/delete lookup
# tables for sector, industry, and index tag, each with a company-usage
# count. Same admin-only gating as the rest of /admin.
# ------------------------------------------------------------------


def test_admin_taxonomy_panel_requires_admin(client) -> None:
    response = client.get("/settings?panel=admin-taxonomy")
    assert response.status_code == 302  # not logged in -> redirect to login


def test_admin_taxonomy_panel_lists_existing_values_with_counts(client) -> None:
    import config.settings as settings

    conn = init_db(db_path=settings.DB_PATH)
    from storage.repositories import add_sector

    add_sector(conn, "Energy")
    conn.close()

    _admin_session(client)
    body = client.get("/settings?panel=admin-taxonomy").data.decode()
    assert "Energy" in body
    assert "Financial Services" in body  # HDFCBANK's seeded sector


@pytest.mark.parametrize("kind,label", [("sector", "sector"), ("industry", "industry"), ("index-tag", "index tag")])
def test_admin_vocabulary_add_rename_delete_round_trip(client, kind: str, label: str) -> None:
    _admin_session(client)

    add = client.post(f"/admin/vocabulary/{kind}/add", data={"name": f"Test {label} A"})
    assert add.status_code == 302
    body = client.get("/settings?panel=admin-taxonomy").data.decode()
    assert f"Test {label} A" in body

    rename = client.post(
        f"/admin/vocabulary/{kind}/rename", data={"old_name": f"Test {label} A", "new_name": f"Test {label} B"}
    )
    assert rename.status_code == 302
    body = client.get("/settings?panel=admin-taxonomy").data.decode()
    assert f"Test {label} B" in body
    assert f"Test {label} A" not in body

    delete = client.post(f"/admin/vocabulary/{kind}/delete", data={"name": f"Test {label} B"})
    assert delete.status_code == 302
    body = client.get("/settings?panel=admin-taxonomy").data.decode()
    assert f"Test {label} B" not in body


def test_admin_vocabulary_unknown_kind_is_404(client) -> None:
    _admin_session(client)
    response = client.post("/admin/vocabulary/not-a-real-kind/add", data={"name": "x"})
    assert response.status_code == 404


def test_admin_vocabulary_add_requires_admin(client) -> None:
    response = client.post("/admin/vocabulary/sector/add", data={"name": "Energy"})
    assert response.status_code == 302  # redirected to login, not applied


def test_admin_company_row_custom_sector_lands_in_lookup_table(client) -> None:
    """The "+ Add new..." escape hatch on a company's own row must also
    register the new value in the lookup table — not just that one
    company's row — or it wouldn't show up as a future dropdown option or
    in the taxonomy panel."""
    _admin_session(client)
    response = client.post(
        "/admin/HDFCBANK",
        data={
            "action": "save", "display_name": "HDFC Bank", "legal_name": "HDFC Bank Limited",
            "sector": "__new__", "sector_other": "Brand New Sector",
            "industry": "__new__", "industry_other": "Brand New Industry",
        },
    )
    assert response.status_code == 302

    import config.settings as settings
    from storage.repositories import list_industries, list_sectors

    conn = init_db(db_path=settings.DB_PATH)
    assert "Brand New Sector" in list_sectors(conn)
    assert "Brand New Industry" in list_industries(conn)


def test_admin_company_save_preserves_fiscal_year_end_month(client) -> None:
    """Regression test: the save handler must pass the company's existing
    fiscal_year_end_month through to register_company() unchanged, or a US
    company (fiscal_year_end_month=12) would get silently reset to 3 (the
    register_company() default) on every Admin save, same class of bug the
    handler's own comment already warns about for country/currency."""
    import config.settings as settings
    from companies.registry import get_company, register_company

    conn = init_db(db_path=settings.DB_PATH)
    register_company(
        conn, "AAPL", "Apple Inc.", "Apple", country="US", currency="USD", fiscal_year_end_month=12
    )

    _admin_session(client)
    response = client.post(
        "/admin/AAPL",
        data={"action": "save", "display_name": "Apple", "legal_name": "Apple Inc."},
    )
    assert response.status_code == 302

    row = get_company(conn, "AAPL")
    assert row["fiscal_year_end_month"] == 12


def test_admin_add_and_delete_stock_action(client) -> None:
    import config.settings as settings
    from companies.stock_actions import list_stock_actions

    _admin_session(client)
    add_response = client.post(
        "/admin/HDFCBANK/stock-actions",
        data={"action_type": "split", "action_date": "2024-06-15", "ratio_from": "1", "ratio_to": "2"},
    )
    assert add_response.status_code == 302

    conn = init_db(db_path=settings.DB_PATH)
    actions = list_stock_actions(conn, "HDFCBANK")
    assert len(actions) == 1
    assert actions[0]["action_type"] == "split"

    delete_response = client.post(f"/admin/HDFCBANK/stock-actions/{actions[0]['action_id']}/delete")
    assert delete_response.status_code == 302
    assert list_stock_actions(conn, "HDFCBANK") == []


def test_admin_add_stock_action_invalid_type_flashes_error_not_500(client) -> None:
    import config.settings as settings
    from companies.stock_actions import list_stock_actions

    _admin_session(client)
    response = client.post(
        "/admin/HDFCBANK/stock-actions",
        data={"action_type": "merger", "action_date": "2024-06-15", "ratio_from": "1", "ratio_to": "2"},
    )
    assert response.status_code == 302  # redirected back with a flashed error, not a 500

    conn = init_db(db_path=settings.DB_PATH)
    assert list_stock_actions(conn, "HDFCBANK") == []


def test_admin_stock_actions_panel_requires_login(client) -> None:
    response = client.post(
        "/admin/HDFCBANK/stock-actions",
        data={"action_type": "split", "action_date": "2024-06-15", "ratio_from": "1", "ratio_to": "2"},
    )
    assert response.status_code in (302, 401, 403)
    if response.status_code == 302:
        assert "/login" in response.headers["Location"]


def _save_investigation(db_path: Path, investigation_id: str, company_ids: list[str], **kwargs) -> None:
    from storage.repositories import save_investigation

    conn = init_db(db_path=db_path)
    from storage.repositories import complete_research_case, create_research_case

    question = f"Why do {' and '.join(company_ids)} differ?"
    save_investigation(
        conn, investigation_id=investigation_id,
        question=question, company_ids=company_ids,
        statement_type="consolidated", strongest_explanation="Because of X.",
        unanswered_questions=[], additional_evidence_needed=[], **kwargs,
    )
    # Cases are the container every listing reads, so a directly-seeded
    # investigation needs its (completed) case to be visible anywhere.
    create_research_case(
        conn, investigation_id, kind="investigation", question=question, company_ids=company_ids,
        statement_type="consolidated", owner_id=None,
    )
    complete_research_case(conn, investigation_id, outcome="answered", result_json="{}", investigation_id=investigation_id)
    conn.close()


def test_company_page_lists_its_structured_investigations(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "web_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    _save_investigation(db_path, "inv_solo", ["HDFCBANK"])

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as client:
        body = client.get("/companies/HDFCBANK").data.decode()

    assert 'id="sec-cases"' in body
    assert 'href="/cases/inv_solo"' in body
    assert "Why do HDFCBANK differ?" in body


def test_a_cross_company_investigation_appears_under_every_company_it_covers(
    tmp_path: Path, monkeypatch
) -> None:
    """The spec's rule for investigations #4 and #5: one shared record, listed
    under each associated company, never duplicated per company."""
    db_path = tmp_path / "web_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    _save_investigation(db_path, "inv_pair", ["HDFCBANK", "ICICIBANK"])

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as client:
        hdfc = client.get("/companies/HDFCBANK").data.decode()
        icici = client.get("/companies/ICICIBANK").data.decode()

    for body, other in ((hdfc, "ICICI Bank"), (icici, "HDFC Bank")):
        assert 'href="/cases/inv_pair"' in body
        assert f"with {other}" in body  # named as a shared, not duplicated, record

    conn = init_db(db_path=db_path)
    assert conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 1
    conn.close()


def test_company_page_shows_an_empty_state_when_it_has_no_investigations(client) -> None:
    body = client.get("/companies/HDFCBANK").data.decode()
    assert 'id="company-cases-empty"' in body


def test_a_point_in_time_investigation_is_labelled_as_of_on_both_surfaces(
    tmp_path: Path, monkeypatch
) -> None:
    """A historical conclusion must state the information set it was reached
    under, or it can't be audited."""
    db_path = tmp_path / "web_test.db"
    conn = init_db(db_path=db_path)
    ensure_metric_vocabulary(conn)
    seed_companies(conn)
    conn.close()
    _save_investigation(db_path, "inv_hist", ["HDFCBANK"], as_of="2013-03-31")

    app = _build_app(db_path, tmp_path, monkeypatch)
    with app.test_client() as client:
        company_page = client.get("/companies/HDFCBANK").data.decode()
        investigation_page = client.get("/investigate/inv_hist", follow_redirects=True).data.decode()

    assert "as of 2013-03-31" in company_page
    assert "evidence as of 2013-03-31" in investigation_page


# ------------------------------------------------------------------
# /investigate/generate-async + /investigate/status — background-thread
# investigation run, polled instead of blocking one HTTP request (fixes a
# real production timeout: a broad, multi-hypothesis question can run past
# gunicorn's --timeout 120 or the platform's own load balancer timeout,
# which killed the request mid-flight and surfaced as "Network error:
# Unexpected token '<' ... is not valid JSON" in the browser).
# ------------------------------------------------------------------


def test_investigate_generate_async_requires_question(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.post("/investigate/generate-async", json={"question": "", "company_ids": []})
    assert response.status_code == 400


def test_investigate_generate_async_requires_api_key(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", False)
    response = client.post(
        "/investigate/generate-async", json={"question": "Why did margins fall?", "company_ids": ["HDFCBANK"]}
    )
    assert response.status_code == 503


def test_investigate_status_unknown_id_is_404(client, monkeypatch) -> None:
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    response = client.get("/investigate/status/not-a-real-id")
    assert response.status_code == 404


def test_investigate_generate_async_runs_in_background_and_status_reaches_done(client, monkeypatch) -> None:
    """The route must return 202 immediately (not block on run_investigation)
    and the investigation_id it hands back must be pollable via
    /investigate/status until it reports "done" with the real
    investigate_view URL — the same destination the old synchronous route
    used to hand straight back."""
    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    # Jev's complexity classification (docs/ADR/023) now runs before this
    # route even creates the case -- without a fake client installed, that
    # call would hit the real Anthropic API.
    _install_fake_llm(monkeypatch, text='{"complexity_level": 5, "confidence": 0.9, "reason": "test"}')

    def _fake_run_investigation(
        conn, question, company_ids, *, statement_type="consolidated", as_of=None, investigation_id=None,
        case_id=None, complexity_level=None,
    ):
        from research.investigation import Investigation

        assert investigation_id is not None, "the route must hand the pre-generated id through, not let a new one be minted"
        return Investigation(investigation_id=investigation_id, question=question, company_ids=company_ids)

    monkeypatch.setattr("web.app.run_investigation", _fake_run_investigation)

    start = client.post(
        "/investigate/generate-async", json={"question": "Why did margins fall?", "company_ids": ["HDFCBANK"]}
    )
    assert start.status_code == 202
    investigation_id = start.get_json()["investigation_id"]

    import time

    status = None
    for _ in range(50):  # background thread should finish almost instantly (fake run_investigation does no real work)
        status = client.get(f"/investigate/status/{investigation_id}").get_json()
        if status["status"] != "running":
            break
        time.sleep(0.05)

    assert status["status"] == "done"
    assert status["url"] == f"/investigate/{investigation_id}"


def test_investigate_generate_async_reports_error_status_on_failure(client, monkeypatch) -> None:
    from research.investigation import InvestigationError

    monkeypatch.setattr("web.app.ANTHROPIC_API_KEY_SET", True)
    _install_fake_llm(monkeypatch, text='{"complexity_level": 5, "confidence": 0.9, "reason": "test"}')

    def _failing_run_investigation(*args, **kwargs):
        raise InvestigationError("every hypothesis's evaluation failed")

    monkeypatch.setattr("web.app.run_investigation", _failing_run_investigation)

    start = client.post(
        "/investigate/generate-async", json={"question": "Why did margins fall?", "company_ids": ["HDFCBANK"]}
    )
    investigation_id = start.get_json()["investigation_id"]

    import time

    status = None
    for _ in range(50):
        status = client.get(f"/investigate/status/{investigation_id}").get_json()
        if status["status"] != "running":
            break
        time.sleep(0.05)

    assert status["status"] == "error"
    assert "every hypothesis's evaluation failed" in status["error"]


def test_eval_analytics_panel_requires_admin(client) -> None:
    response = client.get("/settings?panel=admin-eval_analytics")
    assert response.status_code == 302  # redirected to login, same as every other admin-* panel


def test_eval_analytics_panel_aggregates_routing_log_and_eval_history(client) -> None:
    """The Eval Analytics panel (docs/ADR/023's "future eval runner"
    observability surface) aggregates two independent sources -- every
    routed question's own audit row (signals_routing_log) for the
    volume/latency/cost/confidence-by-level breakdown, and the periodic
    golden-eval job's batch_job_runs/items history (job_name='signals_eval')
    for the accuracy trend -- into one panel context, without duplicating
    the raw per-run list Audit Log > Job Runs already shows. Asserted
    against the panel's own embedded JSON payload (#eval-analytics-data)
    rather than rendered numbers in prose, so the test doesn't depend on
    incidental digits also appearing elsewhere on the page."""
    import json

    import config.settings as settings
    from ingestion.batch_log import BatchRun
    from storage.repositories import insert_signals_routing_log

    conn = init_db(db_path=settings.DB_PATH)
    insert_signals_routing_log(
        conn, run_id="routing-r1", question="What was net profit in FY2024?", company_ids="HDFCBANK",
        jev_level=1, jev_confidence=0.9, jev_reason="retrieve a single reported figure", jev_source="jev",
        model_selected="claude-haiku-4-5", fallback_model_used=None, data_sources_json="[]",
        neo4j_used=False, planner_used=False, tools_executed_json="[]", calculations_performed_json="[]",
        evidence_identifiers_json="[]", missing_data_issues_json="[]", final_confidence=None,
        execution_status="answered", latency_ms=120.0, input_tokens=100, output_tokens=50,
        estimated_cost_usd=0.01, answer_reference=None,
    )
    insert_signals_routing_log(
        conn, run_id="routing-r2", question="Compare HDFC Bank and ICICI Bank's profitability",
        company_ids="HDFCBANK,ICICIBANK", jev_level=4, jev_confidence=0.7, jev_reason="cross-company comparison",
        jev_source="jev", model_selected="claude-sonnet-5", fallback_model_used=None, data_sources_json="[]",
        neo4j_used=True, planner_used=False, tools_executed_json="[]", calculations_performed_json="[]",
        evidence_identifiers_json="[]", missing_data_issues_json="[]", final_confidence="High",
        execution_status="answered", latency_ms=980.0, input_tokens=500, output_tokens=300,
        estimated_cost_usd=0.08, answer_reference=None,
    )

    with BatchRun(conn, "signals_eval", scope_label="golden eval set (2 cases)") as run:
        with run.item("case_match") as item:
            item.detail = "expected=L1 actual=L1 confidence=0.90 status=answered latency_ms=120"
        with run.item("case_mismatch"):
            raise RuntimeError("expected=L2 actual=L1 confidence=0.60 status=answered latency_ms=100 reason='wrong'")
    conn.close()

    _admin_session(client)
    response = client.get("/settings?panel=admin-eval_analytics")
    assert response.status_code == 200
    body = response.data.decode()

    match = re.search(
        r'<script type="application/json" id="eval-analytics-data">(.*?)</script>', body, re.DOTALL
    )
    assert match, "eval-analytics-data JSON payload not found in the rendered page"
    payload = json.loads(match.group(1))

    level_counts = {row["level"]: row["count"] for row in payload["level_breakdown"]}
    assert level_counts[1] == 1
    assert level_counts[4] == 1
    assert level_counts[2] == 0  # every level is present, even with zero routed questions

    level1 = next(row for row in payload["level_breakdown"] if row["level"] == 1)
    assert level1["avg_latency_ms"] == 120
    assert level1["answered"] == 1

    by_level = {row["level"]: row for row in payload["latest_eval_by_level"]}
    assert by_level[1] == {"level": 1, "label": "Retrieve", "matched": 1, "total": 1, "pass_rate": 1.0}
    assert by_level[2] == {"level": 2, "label": "Calculate", "matched": 0, "total": 1, "pass_rate": 0.0}

    assert len(payload["eval_history"]) == 1
    run_row = payload["eval_history"][0]
    assert run_row["matched"] == 1
    assert run_row["total"] == 2
    assert run_row["pass_rate"] == 0.5
