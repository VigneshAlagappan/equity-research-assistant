"""Scrape earnings presentations, annual reports, and concall recordings
straight from a company's own investor-relations website -- the automated
fetch source the disabled "Document analysis" Schedule row (web/app.py)
flags as missing ("no automated fetch source — this would only ever
process what's already been manually uploaded"). Feeds
storage.repositories.save_company_document() the same way the Docs tab's
manual Add form does, except added_by_user=None (that field's own docstring:
"NULL = officially sourced; set = manually added via the Docs tab, by
whom" -- this is the "future data-provider ingestion path" it was already
anticipating).

Two genuinely different site architectures, verified against the real
sites, not assumed:

- Q4 Inc. platform (Amazon, Alphabet, and most large-cap US IR sites --
  confirmed by the "Powered By Q4 Inc." footer both carry): a JS-rendered
  document-list widget. A plain crawl catches it mid-render maybe half the
  time (verified: the exact same URL returned 0 links on one crawl and 90
  on the next) -- _fetch_q4_page() always passes an explicit `wait_for`
  condition (wait for at least one .pdf link to exist in the DOM) instead
  of trusting crawl4ai's default "page looks settled" heuristic, which is
  what actually fixes the flakiness.
- Berkshire Hathaway's site: plain static HTML (literally a Microsoft-Word
  export -- verified in the page source), no JavaScript, but served
  Brotli-compressed regardless of what Accept-Encoding is sent, which
  requests can only decode if the `brotli` package is installed (this
  project's own requirements.txt now includes it). No crawl4ai/Playwright
  needed for this one -- _fetch_berkshire_reports() is a plain requests
  call.

10-K/10-Q/proxy-statement PDFs that also turn up on these pages are
deliberately skipped here: this app already has SEC EDGAR's structured
XBRL data for the same filings (sources/sec_edgar.py) -- downloading the
PDF copy too would be a redundant, unstructured duplicate of data already
on file in a queryable form, not a source of anything new. This module is
for the narrative content SEC EDGAR doesn't carry: presentations, annual
reports/shareholder letters, and earnings-call recordings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from config import settings
from config.settings import to_repo_relative
from storage.document_store import default_document_store

logger = logging.getLogger(__name__)

SOURCE_ID = "investor_relations"  # config/settings.py's DEFAULT_SOURCES already reserves this row

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_REQUEST_TIMEOUT_SECONDS = 20


class IRFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class IRDocumentRef:
    """One document found on a company's IR site -- enough to download and
    register it via storage.repositories.save_company_document(), before
    it's ever downloaded. fiscal_year/quarter are best-effort (regex over
    the link text/URL, see _parse_fiscal_period()) -- None when the page
    gives no parseable period (e.g. Berkshire's pre-2003 links), never
    guessed."""

    document_type: str  # investor_presentation | annual_report | financial_result | concall_recording
    title: str
    url: str
    fiscal_year: str | None
    quarter: str | None


# ============================================================
# Period parsing -- shared by both platforms, since both express a
# quarter/year somewhere in the link text or URL, just in different
# formats ("Q2 2026", "Q226", "2026q2", "1stqtr03").
# ============================================================

_QUARTER_WORDS = {"1st": "Q1", "2nd": "Q2", "3rd": "Q3", "4th": "Q4"}
_PERIOD_PATTERNS = [
    re.compile(r"Q([1-4])\s*[\s_-]?(\d{4})", re.IGNORECASE),
    re.compile(r"(\d{4})\s*[\s_-]?Q([1-4])", re.IGNORECASE),
    re.compile(r"Q([1-4])(\d{2})(?!\d)", re.IGNORECASE),
    re.compile(r"(1st|2nd|3rd|4th)qtr(\d{2})", re.IGNORECASE),
]
_YEAR_ONLY_PATTERN = re.compile(r"(20\d{2})")


def _two_digit_year(year: int) -> int:
    """"03" -> 2003, but "98" -> 1998, not 2098 -- a real bug this fixes,
    not a hypothetical: Berkshire's own qtrly/1stqtr98.html etc. (1990s
    filings, still live on their site) parsed as FY2098 under a naive
    "always add 2000" rule. Pivots on the current year's own last two
    digits: a two-digit year no greater than that is assumed current-
    century, anything higher is assumed previous-century -- correct for
    every source this module covers (nothing here predates 1994)."""
    century_pivot = date.today().year % 100 + 1
    return 2000 + year if year <= century_pivot else 1900 + year


def _parse_fiscal_period(*texts: str) -> tuple[str | None, str | None]:
    """(fiscal_year, quarter) best-effort from whichever of the given
    strings (link text, URL) actually carries it -- tries each pattern
    against each text in order, first match wins."""
    for text in texts:
        if not text:
            continue
        for pattern in _PERIOD_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            groups = m.groups()
            if groups[0] in _QUARTER_WORDS:
                quarter = _QUARTER_WORDS[groups[0]]
                year = int(groups[1])
            elif len(groups[0]) == 4:
                year, quarter = int(groups[0]), f"Q{groups[1]}"
            else:
                quarter, year = f"Q{groups[0]}", int(groups[1])
            year = _two_digit_year(year) if year < 100 else year
            return f"FY{year}", quarter
    for text in texts:
        m = _YEAR_ONLY_PATTERN.search(text or "")
        if m:
            return f"FY{m.group(1)}", None
    return None, None


# ============================================================
# Q4 Inc. platform (Amazon, Alphabet, ...) -- crawl4ai/Playwright.
# ============================================================

#: link-text substring (lowercased) -> document_type. Order matters: more
#: specific labels first (e.g. "webcast slides" before a bare "webcast").
_Q4_LABEL_MAP: list[tuple[str, str]] = [
    ("slides", "investor_presentation"),
    ("presentation", "investor_presentation"),
    ("earnings release", "financial_result"),
    ("annual report", "annual_report"),
    ("letter to shareholders", "annual_report"),
    ("shareholder letter", "annual_report"),
    ("webcast", "concall_recording"),
]
#: Skipped entirely -- see module docstring's "10-K/10-Q/proxy" paragraph.
_Q4_SKIP_SUBSTRINGS = ("10-k", "10-q", "proxy statement", "ical", "calendar")


def _classify_q4_link(text: str, href: str) -> str | None:
    haystack = f"{text} {href}".lower()
    if any(skip in haystack for skip in _Q4_SKIP_SUBSTRINGS):
        return None
    for substring, doc_type in _Q4_LABEL_MAP:
        if substring in haystack:
            return doc_type
    return None


async def _fetch_q4_page(url: str) -> list[IRDocumentRef]:
    """One Q4 Inc.-hosted IR page (a quarterly-results/earnings or
    events listing) -> every presentation/earnings-release/annual-report/
    webcast link on it. The `wait_for` condition is the load-bearing part
    -- see module docstring; omitting it is what makes this flaky."""
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, wait_for="css:a[href*='.pdf']", page_timeout=20000)
    async with AsyncWebCrawler() as crawler:
        try:
            result = await crawler.arun(url=url, config=config)
        except Exception as exc:
            raise IRFetchError(f"crawl4ai failed fetching {url}: {exc}") from exc

    if not result.success:
        raise IRFetchError(f"crawl4ai reported failure fetching {url}: status={result.status_code}")

    refs: list[IRDocumentRef] = []
    all_links = (result.links or {}).get("internal", []) + (result.links or {}).get("external", [])
    for link in all_links:
        href = link.get("href") or ""
        text = (link.get("text") or "").strip()
        if ".pdf" not in href.lower() and not href.lower().endswith((".mp3", ".wav")):
            continue
        doc_type = _classify_q4_link(text, href)
        if doc_type is None:
            continue
        fiscal_year, quarter = _parse_fiscal_period(text, href)
        refs.append(IRDocumentRef(document_type=doc_type, title=text or href.rsplit("/", 1)[-1], url=href, fiscal_year=fiscal_year, quarter=quarter))
    return refs


# ============================================================
# Berkshire Hathaway -- plain static HTML, no browser needed.
# ============================================================

_BERKSHIRE_REPORTS_URL = "https://www.berkshirehathaway.com/reports.html"
_BERKSHIRE_HREF_RE = re.compile(r'href\s*=\s*["\']?([^"\'>\s]+)', re.IGNORECASE)


def _classify_berkshire_link(href: str) -> str | None:
    lower = href.lower()
    if "qtrly" in lower:
        return "financial_result"
    if "ar/" in lower or lower.startswith(tuple(f"{y}ar" for y in range(1994, 2100))):
        return "annual_report"
    return None


def fetch_berkshire_reports() -> list[IRDocumentRef]:
    """Berkshire's own reports.html -- a flat index of every annual and
    quarterly report/letter back to the 1990s, as relative links. No
    crawl4ai/browser needed (see module docstring); the `brotli` package
    is required for requests to decode the response at all -- Berkshire's
    host (Sucuri) serves Content-Encoding: br unconditionally, verified
    against the real site (still br even with Accept-Encoding: gzip,
    deflate explicitly sent instead)."""
    try:
        response = requests.get(_BERKSHIRE_REPORTS_URL, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise IRFetchError(f"failed fetching {_BERKSHIRE_REPORTS_URL}: {exc}") from exc

    refs: list[IRDocumentRef] = []
    seen_hrefs: set[str] = set()
    for href in _BERKSHIRE_HREF_RE.findall(response.text):
        if href in seen_hrefs:
            continue
        doc_type = _classify_berkshire_link(href)
        if doc_type is None:
            continue
        seen_hrefs.add(href)
        full_url = href if href.startswith("http") else f"https://www.berkshirehathaway.com/{href.lstrip('/')}"
        fiscal_year, quarter = _parse_fiscal_period(href)
        refs.append(IRDocumentRef(document_type=doc_type, title=href, url=full_url, fiscal_year=fiscal_year, quarter=quarter))
    return refs


# ============================================================
# Per-company config -- which platform, which pages. Add a company by
# adding one entry here; nothing else in this module needs to change for
# another Q4-hosted company.
# ============================================================

Q4_COMPANIES: dict[str, list[str]] = {
    "AMZN": [
        "https://ir.aboutamazon.com/quarterly-results/default.aspx",
        "https://ir.aboutamazon.com/annual-reports-proxies-and-shareholder-letters/default.aspx",
        "https://ir.aboutamazon.com/events/default.aspx",
    ],
    "GOOGL": [
        "https://abc.xyz/investor/Earnings/default.aspx",
        "https://abc.xyz/investor/events/default.aspx",
    ],
}

#: Company_ids whose documents should come from fetch_berkshire_reports()
#: instead of the Q4 path -- both share classes point at the same filer,
#: same reasoning sources/yfinance_prices.py's US_TICKER_OVERRIDES treats
#: them as the same underlying company for anything not price/share-count
#: specific.
BERKSHIRE_COMPANY_IDS = ("BRKB", "BRKA")


async def fetch_company_documents(company_id: str) -> list[IRDocumentRef]:
    """Every IR document this module knows how to find for one company_id.
    Raises IRFetchError if company_id isn't in Q4_COMPANIES/
    BERKSHIRE_COMPANY_IDS -- callers (scripts/fetch_investor_relations.py)
    are expected to check membership themselves first for a cleaner error,
    this is the belt-and-suspenders case."""
    if company_id in BERKSHIRE_COMPANY_IDS:
        return fetch_berkshire_reports()
    if company_id in Q4_COMPANIES:
        refs: list[IRDocumentRef] = []
        for url in Q4_COMPANIES[company_id]:
            refs.extend(await _fetch_q4_page(url))
        return refs
    raise IRFetchError(f"no investor_relations fetch config for company_id={company_id!r}")


def download_document(ref: IRDocumentRef, dest_dir: Path) -> str:
    """Download one document's file into dest_dir, named from its own URL --
    skips the request if already stored (same "a file already there is
    never re-downloaded" rule as sources/nse_fetch.py's download_filing()).

    Routed through the active DocumentStore (storage/document_store.py)
    rather than Path.write_bytes() directly, so this works unchanged
    whether DOCUMENT_STORE_BACKEND is "local" (default, identical on-disk
    behaviour under dest_dir) or "s3". Returns the storage_object_key the
    document is stored under (the caller persists this as
    documents.raw_file_path/storage_object_key), not a filesystem Path."""
    filename = ref.url.rstrip("/").rsplit("/", 1)[-1]
    key = to_repo_relative(dest_dir / filename)
    store = default_document_store()
    if store.exists(key):
        return key

    try:
        response = requests.get(ref.url, headers={"User-Agent": _USER_AGENT}, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise IRFetchError(f"failed downloading {ref.url}: {exc}") from exc

    return store.store(key, response.content)


DEFAULT_IR_DOCUMENTS_DIR = settings.DOCUMENTS_DIR / "investor_relations"
