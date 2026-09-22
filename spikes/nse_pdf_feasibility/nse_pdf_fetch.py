"""Spike: discover + download NSE quarterly financial-results PDFs.

Reuses the exact session-bootstrap / retry pattern already proven in
sources/nse_fetch.py (this repo's production XBRL fetcher) rather than
reinventing cookie/anti-bot handling — see that file's own docstring for why
the bootstrap step exists. This module only adds: (a) reading the PDF
attachment field the same corporates-financial-results/integrated-filing-
results rows already carry alongside the xbrl field, and (b) downloading it.

HARD RULE (per task spec): every network call has an explicit timeout and is
wrapped so a failure/timeout is caught, logged, and the caller moves on —
never retried in a tight loop, never blocks indefinitely. This module keeps
its own tiny retry budget (a couple of attempts, short backoff) deliberately
smaller than production's, since a spike run should fail fast and record a
finding rather than spend minutes retrying a company that's blocked.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_BASE = "https://www.nseindia.com"
_BOOTSTRAP_PATH = "/companies-listing/corporate-filings-financial-results"
_API_PATH = "/api/corporates-financial-results"
_INTEGRATED_FILING_API_PATH = "/api/integrated-filing-results"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_REQUEST_TIMEOUT_SECONDS = 15  # hard cap per the task's network-safety rule
_SPIKE_MAX_ATTEMPTS = 2        # deliberately small — spike should fail fast, not hang
_BACKOFF_SECONDS = 2.0
# Matches sources/nse_xbrl.py's own _REQUEST_PACING_SECONDS — a courtesy
# delay applied after every real NSE request (bootstrap or data call), not
# just data calls, so a 50-company run behaves like production's own batch
# job would, rather than this spike's earlier faster dev-iteration pacing.
_REQUEST_PACING_SECONDS = 1.0


class NSESpikeFetchError(RuntimeError):
    pass


@dataclass
class FetchAttempt:
    """One logged network attempt — every call made during the spike is
    recorded here regardless of outcome, so the report can cite concrete
    URLs/status codes/errors rather than a summary."""
    url: str
    method: str
    status_code: int | None
    ok: bool
    error: str | None
    elapsed_s: float
    timestamp: str


ATTEMPT_LOG: list[FetchAttempt] = []


def _record(url: str, method: str, status_code: int | None, ok: bool, error: str | None, elapsed_s: float) -> None:
    ATTEMPT_LOG.append(FetchAttempt(url, method, status_code, ok, error, round(elapsed_s, 2), datetime.utcnow().isoformat()))


def new_session() -> requests.Session | None:
    """Bootstrap an NSE session (cookie dance). Returns None (never raises)
    on failure/timeout — caller must treat None as "blocked, move on"."""
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    url = f"{_BASE}{_BOOTSTRAP_PATH}"
    t0 = time.monotonic()
    try:
        resp = session.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - t0
        _record(url, "GET", resp.status_code, resp.ok, None, elapsed)
        resp.raise_for_status()
        return session
    except requests.RequestException as exc:
        elapsed = time.monotonic() - t0
        _record(url, "GET", getattr(exc.response, "status_code", None), False, str(exc), elapsed)
        logger.warning("NSE bootstrap failed: %s", exc)
        return None
    finally:
        time.sleep(_REQUEST_PACING_SECONDS)


def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response | None:
    """A single bounded GET. Never raises past this function — returns None
    (logged) on any failure so callers can move to the next test case
    unconditionally, satisfying the "never wait indefinitely / never retry
    in a tight loop" hard rule. Small budget (_SPIKE_MAX_ATTEMPTS) of
    immediate retries only for transient network errors, not for 403/429
    (those are recorded as a blocking finding, not retried)."""
    last_err = None
    try:
        for attempt in range(1, _SPIKE_MAX_ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                resp = session.get(
                    url, params=params,
                    headers={"Accept": "*/*", "Referer": f"{_BASE}{_BOOTSTRAP_PATH}"},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                elapsed = time.monotonic() - t0
                _record(url, "GET", resp.status_code, resp.ok, None, elapsed)
                if resp.status_code in (403, 429):
                    logger.warning("NSE blocked request (status=%s): %s", resp.status_code, url)
                    return resp  # return it so caller can inspect/log the block, not None
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                elapsed = time.monotonic() - t0
                _record(url, "GET", getattr(exc.response, "status_code", None), False, str(exc), elapsed)
                last_err = exc
                if attempt < _SPIKE_MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS)
        logger.warning("NSE request failed after %d attempts: %s (%s)", _SPIKE_MAX_ATTEMPTS, url, last_err)
        return None
    finally:
        # Courtesy pacing after every real request this function made,
        # matching sources/nse_xbrl.py's own _REQUEST_PACING_SECONDS —
        # applies even on failure so a string of errors doesn't turn into a
        # tight loop against NSE.
        time.sleep(_REQUEST_PACING_SECONDS)


def fetch_filing_index_raw(session: requests.Session, symbol: str, nse_period: str = "Quarterly") -> list[dict] | None:
    """Raw JSON rows from the older corporates-financial-results listing —
    kept RAW (not translated into NSEFilingRef) so the spike can inspect
    whatever PDF/attachment fields exist, which sources/nse_fetch.py's own
    NSEFilingRef intentionally drops (it only cares about xbrl_url)."""
    resp = _get(session, f"{_BASE}{_API_PATH}", params={"index": "equities", "symbol": symbol, "period": nse_period})
    if resp is None or resp.status_code in (403, 429):
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("Non-JSON response for %s filing index", symbol)
        return None


def fetch_integrated_filing_index_raw(session: requests.Session, symbol: str) -> list[dict] | None:
    resp = _get(session, f"{_BASE}{_INTEGRATED_FILING_API_PATH}", params={"index": "equities", "symbol": symbol})
    if resp is None or resp.status_code in (403, 429):
        return None
    try:
        return resp.json().get("data", [])
    except ValueError:
        logger.warning("Non-JSON response for %s integrated filing index", symbol)
        return None


def download_pdf(session: requests.Session, pdf_url: str, dest_path: Path) -> bool:
    """Download one PDF. False (logged) on any failure — never raises."""
    resp = _get(session, pdf_url)
    if resp is None or resp.status_code in (403, 429):
        return False
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not resp.content[:5] == b"%PDF-":
        logger.warning("Downloaded content for %s doesn't look like a PDF (Content-Type=%s)", pdf_url, content_type)
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(resp.content)
    return True


def dump_attempt_log(dest_path: Path) -> None:
    dest_path.write_text(json.dumps([asdict(a) for a in ATTEMPT_LOG], indent=2))
