"""Live fetch from NSE's public financial-results filing listings — the fetch
step sources/nse_xbrl.py's module docstring flags as "not written yet".

Two listings, covering two successive eras of the same underlying filing
requirement (verified against the real site for IDFCFIRSTB — a clean
handoff, no gap or overlap): fetch_filing_index() (the older corporates-
financial-results API, filings through Q3 FY25) and
fetch_integrated_filing_index() (SEBI's newer "Integrated Filing"
framework, Q4 FY25 onward). Both listings feed the same NSEFilingRef shape
and the same sources/nse_xbrl.py adapter downstream — the adapter itself
already handles both taxonomy namespaces a filing from either era can use.

NSE's site blocks a cold API request (verified against the real site): a
session must first GET a real page so the WAF hands out its anti-bot
cookies, with a browser User-Agent — only then does the JSON API respond.
This module owns that session dance, the filing-index listing, and
downloading the XBRL files it points at; sources/nse_xbrl.py still owns
parsing an XBRL file already on disk, unchanged.

Guardrail: "Respect NSE rate limits with controlled pacing, retries, caching
and backoff" — every request goes through _get_with_retries() (paced,
exponential backoff, one session re-bootstrap on a 403 before giving up),
and the filing-index listing is cached to disk (_CACHE_TTL_SECONDS) so
re-running a fetch for the same company/period within an hour doesn't
re-hit NSE at all.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from config import settings

logger = logging.getLogger(__name__)

_BASE = "https://www.nseindia.com"
_BOOTSTRAP_PATH = "/companies-listing/corporate-filings-financial-results"
_API_PATH = "/api/corporates-financial-results"

# NSE migrated financial-results filing to SEBI's newer "Integrated Filing"
# framework partway through — verified against the real site for
# IDFCFIRSTB: _API_PATH's own listing stops dead at the quarter ended
# 31-Dec-2024 (filed 25-Jan-2025), while this endpoint's listing starts
# at the quarter ended 31-Mar-2025 and reaches all the way to the most
# recently filed quarter (30-Jun-2026 as of this check) — a clean handoff,
# no gap, no overlap. Same bootstrap/session as _API_PATH (verified — no
# separate cookie dance needed for this one).
_INTEGRATED_FILING_API_PATH = "/api/integrated-filing-results"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_REQUEST_TIMEOUT_SECONDS = 20
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 2.0
_REQUEST_PACING_SECONDS = 1.0  # courtesy delay between any two NSE requests
_CACHE_TTL_SECONDS = 3600  # don't re-list the same (symbol, period) more than hourly

#: NSE's own "Consolidated" | "Non-Consolidated" -> this app's statement_type
#: vocabulary (README/schema: consolidated | standalone everywhere else).
_STATEMENT_TYPE_MAP = {"Consolidated": "consolidated", "Non-Consolidated": "standalone"}

#: NSE's own period vocabulary this app currently requests — "Half-Yearly"
#: exists on the API but real banks/equities don't file it (verified: 0
#: rows for IDFCFIRSTB), so it's not offered here; add it if a taxonomy
#: that does file half-yearly shows up later.
NSE_PERIODS = ("Quarterly", "Annual")


class NSEFetchError(RuntimeError):
    """Raised once retries/backoff are exhausted for a single HTTP call."""


@dataclass(frozen=True)
class NSEFilingRef:
    """One row from the corporates-financial-results filing index — enough
    to fetch, place, and provenance-tag the XBRL file it points at, before
    it's ever downloaded or parsed. filing_date/seq_number are exactly the
    "filing date" / "filing identifier" provenance fields the source policy
    calls for (see NSEXbrlAdapter's own docstring for the rest — statement
    type, reporting period — which come from the XBRL content itself)."""

    symbol: str
    seq_number: str
    statement_type: str  # "consolidated" | "standalone"
    nse_period: str  # NSE's own "Quarterly" | "Annual"
    from_date: date
    to_date: date
    filing_date: str  # ISO-8601 timestamp, as reported by NSE
    xbrl_url: str


def _parse_nse_date(value: str) -> date:
    """"25-Jan-2025" -> date(2025, 1, 25) — NSE's own date format across
    fromDate/toDate; filingDate additionally carries "HH:MM" (dropped)."""
    return datetime.strptime(value.split(" ")[0], "%d-%b-%Y").date()


def _cache_path(cache_dir: Path, symbol: str, nse_period: str) -> Path:
    return cache_dir / f"{symbol}_{nse_period}.json"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    _bootstrap(session)
    return session


def _bootstrap(session: requests.Session) -> None:
    response = session.get(f"{_BASE}{_BOOTSTRAP_PATH}", timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()


def _get_with_retries(session: requests.Session, url: str, *, params: dict | None = None) -> requests.Response:
    last_exc: Exception | None = None
    rebootstrapped = False
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = session.get(
                url, params=params,
                headers={"Accept": "*/*", "Referer": f"{_BASE}{_BOOTSTRAP_PATH}"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 403 and not rebootstrapped:
                # Cookies gone stale is the common real-world case for a 403
                # here (verified against the real site) — one re-bootstrap
                # fixes it immediately, cheaper than burning every retry.
                logger.info("NSE returned 403 — re-bootstrapping session cookies and retrying")
                _bootstrap(session)
                rebootstrapped = True
                time.sleep(_REQUEST_PACING_SECONDS)
                continue
            if response.status_code == 429:
                raise NSEFetchError(f"rate-limited (429) fetching {url}")
            response.raise_for_status()
            return response
        except (requests.RequestException, NSEFetchError) as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "NSE request failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, _MAX_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
        finally:
            time.sleep(_REQUEST_PACING_SECONDS)
    raise NSEFetchError(f"exhausted {_MAX_ATTEMPTS} attempts fetching {url}") from last_exc


def fetch_filing_index(
    symbol: str,
    *,
    nse_period: str = "Quarterly",
    session: requests.Session | None = None,
    cache_dir: Path | None = None,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
) -> list[NSEFilingRef]:
    """List every financial-results filing NSE has on file for `symbol` at
    this nse_period ("Quarterly" | "Annual") — full history in one response,
    no date-range filtering needed here (NSE's own from_date/to_date params
    were verified not to add any rows beyond what an unfiltered call already
    returns; date-range selection happens client-side in the caller instead,
    same as everywhere else in this app that filters an already-fetched
    list rather than trusting a vendor's own date-filter to be complete).

    Cached to cache_dir for cache_ttl_seconds (default 1h) keyed on
    (symbol, nse_period) — a second call within that window never touches
    NSE at all.
    """
    if nse_period not in NSE_PERIODS:
        raise ValueError(f"nse_period must be one of {NSE_PERIODS}, got {nse_period!r}")

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = _cache_path(cache_dir, symbol, nse_period)
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_ttl_seconds:
                logger.info("Using cached filing index for %s/%s (%.0fs old)", symbol, nse_period, age)
                return _rows_to_refs(json.loads(cache_file.read_text()))

    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_API_PATH}",
            params={"index": "equities", "symbol": symbol, "period": nse_period},
        )
    finally:
        if owns_session:
            session.close()

    rows = response.json()
    if cache_dir is not None:
        cache_file.write_text(json.dumps(rows))
    return _rows_to_refs(rows)


def _has_real_xbrl_file(xbrl_url: str | None) -> bool:
    """False for NSE's own placeholder — a real, non-empty-looking URL
    string, but with a literal "-" as the filename (e.g.
    "https://nsearchives.nseindia.com/corporate/xbrl/-") — verified against
    real INFY filings: XBRL filing only became mandatory/available from
    30-Jun-2019 onward (real filings before that, several of them,
    including some the "format" field itself still labels "New", all carry
    this same placeholder, so "format" isn't a reliable enough signal on
    its own) — a plain `if not xbrl_url` check doesn't catch this because
    the string itself is non-empty. Distinguishing this up front (instead
    of just letting the 404 happen) matters for more than log noise: NSE's
    _get_with_retries() burns _MAX_ATTEMPTS worth of exponential backoff
    (up to ~14s) per guaranteed-404 request — one bulk 10-year fetch can hit
    dozens of these (64% of INFY's own listing, verified), turning a
    multi-minute fetch into a 25+ minute one for no real data at the end."""
    if not xbrl_url:
        return False
    filename = xbrl_url.rstrip("/").rsplit("/", 1)[-1]
    return filename not in ("", "-")


def _rows_to_refs(rows: list[dict]) -> list[NSEFilingRef]:
    refs: list[NSEFilingRef] = []
    for row in rows:
        statement_type = _STATEMENT_TYPE_MAP.get(row.get("consolidated", ""))
        xbrl_url = row.get("xbrl")
        if statement_type is None or not _has_real_xbrl_file(xbrl_url):
            # A row with no xbrl link (some older/PDF-only filings) or an
            # unrecognized consolidated/standalone label — nothing this
            # module can act on; skip rather than guess.
            continue
        refs.append(
            NSEFilingRef(
                symbol=row["symbol"],
                seq_number=str(row["seqNumber"]),
                statement_type=statement_type,
                nse_period=row["period"],
                from_date=_parse_nse_date(row["fromDate"]),
                to_date=_parse_nse_date(row["toDate"]),
                filing_date=row["filingDate"],
                xbrl_url=xbrl_url,
            )
        )
    return refs


def fetch_integrated_filing_index(
    symbol: str,
    *,
    session: requests.Session | None = None,
    cache_dir: Path | None = None,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
) -> list[NSEFilingRef]:
    """List financial-results filings from NSE's newer "Integrated Filing"
    listing (_INTEGRATED_FILING_API_PATH) — the continuation of
    fetch_filing_index()'s history from Q4 FY25 onward (see that constant's
    comment for the verified handoff point). Unlike fetch_filing_index()'s
    listing, this one isn't financial-results-only: a "Governance"
    integrated filing shares the same endpoint with `consolidated` as null
    — only rows with consolidated in ("Consolidated", "Standalone") are
    real financial-results filings with a numeric XBRL behind them, so
    those are the only ones translated into an NSEFilingRef here (a null
    or unrecognized `consolidated` value, or a row with no `xbrl` link, is
    skipped rather than guessed, same as fetch_filing_index()).

    Cached to cache_dir for cache_ttl_seconds (default 1h), same as
    fetch_filing_index() — keyed on symbol alone (no period param exists on
    this endpoint to key on)."""
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = _cache_path(cache_dir, symbol, "IntegratedFiling")
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_ttl_seconds:
                logger.info("Using cached integrated-filing index for %s (%.0fs old)", symbol, age)
                return _integrated_rows_to_refs(json.loads(cache_file.read_text()))

    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(
            session, f"{_BASE}{_INTEGRATED_FILING_API_PATH}",
            params={"index": "equities", "symbol": symbol},
        )
    finally:
        if owns_session:
            session.close()

    rows = response.json().get("data", [])
    if cache_dir is not None:
        cache_file.write_text(json.dumps(rows))
    return _integrated_rows_to_refs(rows)


#: This endpoint's own "Consolidated" | "Standalone" | null (a non-financials
#: integrated filing, e.g. Governance) — a different vocabulary than
#: _API_PATH's "Consolidated" | "Non-Consolidated" (_STATEMENT_TYPE_MAP), so
#: it gets its own map rather than overloading that one.
_INTEGRATED_STATEMENT_TYPE_MAP = {"Consolidated": "consolidated", "Standalone": "standalone"}


def _integrated_rows_to_refs(rows: list[dict]) -> list[NSEFilingRef]:
    """One ref per (statement_type, quarter-end) — NOT one per row.

    A quarter can legitimately have more than one row here: verified
    against real IDFCFIRSTB data (quarter ended 30-Sep-2025, standalone)
    that NSE keeps the original AND a later correction as separate rows —
    `type_Sub: "Original"` (seq_Id 120939) plus `type_Sub: "Revision"`
    (seq_Id 120955, `revision_Remark: "XBRL_Utility_Error"`) for the exact
    same quarter/statement_type. Downloading and ingesting both as if they
    were two different periods would silently double-count that quarter,
    with the buggy original as likely to "win" reconciliation as the fix
    (retrieved_at ties on an ingest batch are essentially arbitrary). NSE's
    own seq_Id is monotonically increasing per filing event (a later filing
    — new or a revision of an earlier one — always gets a higher seq_Id
    than what it follows), so keeping only the max seq_Id per group is a
    correct, general "latest wins" rule without needing to special-case
    type_Sub/revised_Date parsing."""
    candidates: dict[tuple[str, "date"], dict] = {}
    for row in rows:
        statement_type = _INTEGRATED_STATEMENT_TYPE_MAP.get(row.get("consolidated") or "")
        xbrl_url = row.get("xbrl")
        qe_date = row.get("qe_Date")
        seq_id = row.get("seq_Id")
        if statement_type is None or not _has_real_xbrl_file(xbrl_url) or not qe_date or seq_id is None:
            continue
        to_date = _parse_nse_date(qe_date)
        key = (statement_type, to_date)
        existing = candidates.get(key)
        if existing is None or int(seq_id) > int(existing["seq_Id"]):
            candidates[key] = row

    refs: list[NSEFilingRef] = []
    for (statement_type, to_date), row in candidates.items():
        refs.append(
            NSEFilingRef(
                symbol=row.get("symbol", ""),
                seq_number=str(row["seq_Id"]),
                statement_type=statement_type,
                nse_period="Quarterly",
                # Only to_date is ever read downstream (date-range filtering
                # and the destination filename) — from_date has no
                # independent meaning here (this endpoint reports a single
                # quarter-end date, not a from/to range), so it's set equal
                # to to_date rather than estimated.
                from_date=to_date,
                to_date=to_date,
                filing_date=row.get("broadcast_Date") or row.get("creation_Date") or "",
                xbrl_url=row["xbrl"],
            )
        )
    return refs


def download_filing(filing: NSEFilingRef, dest_path: Path, *, session: requests.Session | None = None) -> bool:
    """Download one filing's XBRL file to dest_path. Returns False (no
    request made) if dest_path already exists and is non-empty — the
    file-level half of "caching" alongside fetch_filing_index()'s own
    listing cache: a filing already on disk is never re-downloaded."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return False

    owns_session = session is None
    session = session or _new_session()
    try:
        response = _get_with_retries(session, filing.xbrl_url)
    finally:
        if owns_session:
            session.close()

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(response.content)
    return True


def filter_last_n_years(filings: list[NSEFilingRef], years: int, *, as_of: date | None = None) -> list[NSEFilingRef]:
    """Filings whose reporting period ends within the trailing `years` years
    of as_of (default: today, UTC)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    cutoff = as_of.replace(year=as_of.year - years)
    return [f for f in filings if f.to_date >= cutoff]


_DEFAULT_XBRL_CACHE_DIR = settings.DATA_DIR / ".cache" / "nse_xbrl"


@dataclass
class RefreshResult:
    """What one refresh_company_filings() call actually did — enough for
    either caller (CLI print, or a web route's flash message) to report a
    result without re-deriving it."""

    downloaded_files: list[Path] = field(default_factory=list)
    skipped_count: int = 0  # already on disk, not re-downloaded
    error_count: int = 0
    most_recent_date: date | None = None  # most recent reporting period end NSE has on file, in the requested window


def refresh_company_filings(
    symbol: str,
    dest_dir: Path,
    *,
    periods: tuple[str, ...] = NSE_PERIODS,
    years: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    cache_dir: Path | None = None,
) -> RefreshResult:
    """List + download every NSE quarterly-results filing not already on
    disk under dest_dir, for one company. The one place this app's fetch+
    filter+download logic lives — scripts/fetch_nse_xbrl.py's CLI and the
    company-page "Refresh" web route (web/app.py's admin_refresh_company)
    both call this directly rather than each keeping their own copy (this
    app's "one ingestion capability, two triggers" principle).

    Never touches the database — same as the CLI this replaced: staging
    files under dest_dir is as far as this goes. Ingesting what it
    downloads is a separate, explicit step the caller runs afterward (see
    ingestion.pipeline.ingest_file()).

    periods defaults to both Quarterly and Annual (matches this app's own
    batch-fetch convention — see NIFTY500_USA_XBRL_BATCHES.md); the CLI
    passes a single period through here to preserve its existing
    --period flag's exact prior behavior."""
    cache_dir = cache_dir if cache_dir is not None else _DEFAULT_XBRL_CACHE_DIR
    result = RefreshResult()

    all_filings: list[NSEFilingRef] = []
    for nse_period in periods:
        try:
            filings = fetch_filing_index(symbol, nse_period=nse_period, cache_dir=cache_dir)
        except NSEFetchError as exc:
            logger.warning("refresh_company_filings: failed to list %s filings for %s: %s", nse_period, symbol, exc)
            result.error_count += 1
            continue
        # NSE migrated financial-results filing to SEBI's newer "Integrated
        # Filing" framework partway through — see fetch_integrated_filing_index()'s
        # own docstring; only relevant for Quarterly, the only cadence that
        # framework reports at.
        if nse_period == "Quarterly":
            try:
                filings = filings + fetch_integrated_filing_index(symbol, cache_dir=cache_dir)
            except NSEFetchError as exc:
                logger.warning("refresh_company_filings: failed to list Integrated Filing results for %s: %s", symbol, exc)
                result.error_count += 1
        all_filings.extend(filings)

    if years is not None:
        all_filings = filter_last_n_years(all_filings, years)
    if from_date is not None:
        all_filings = [f for f in all_filings if f.to_date >= from_date]
    if to_date is not None:
        all_filings = [f for f in all_filings if f.to_date <= to_date]

    if all_filings:
        result.most_recent_date = max(f.to_date for f in all_filings)

    for filing in all_filings:
        dest_path = dest_dir / f"{filing.to_date.isoformat()}_{filing.statement_type}_{filing.seq_number}.xml"
        try:
            fetched = download_filing(filing, dest_path)
        except NSEFetchError as exc:
            result.error_count += 1
            logger.warning(
                "refresh_company_filings: failed to download %s %s seq=%s: %s",
                filing.to_date, filing.statement_type, filing.seq_number, exc,
            )
            continue
        if fetched:
            result.downloaded_files.append(dest_path)
        else:
            result.skipped_count += 1

    return result
