"""SEC EDGAR 10-K document discovery -- storage-only counterpart to
sources/sec_edgar.py's XBRL company-facts ingestion.

sources/sec_edgar.py already gives this app full structured balance-sheet/
cash-flow/income-statement facts for US companies going back to ~2006-2008
(verified live against production Neon for AAPL/MSFT/AMZN) -- there is no
NSE-style "balance sheet is missing" gap to fix here. What's genuinely
missing is the *narrative* content of each 10-K (MD&A, risk factors,
business description) for future RAG/evidence use -- this module discovers
and this raises no financial-data question, it just locates real filed
documents.

Unlike NSE's `/api/corporate-announcements` (a mixed feed needing a
two-stage false-positive filter), SEC's submissions API tags every filing
with an unambiguous `form` field -- filtering to `form == "10-K"` needs no
heuristics. SEC also requires no anti-bot session bootstrap, only a plain
identifying User-Agent header (sources/sec_edgar.py's `_headers()`,
reused here verbatim) -- SEC explicitly designs data.sec.gov for
programmatic access (fair-access policy: an identifying UA, a soft
~10 req/sec cap), unlike NSE's WAF-protected site.
"""

from __future__ import annotations

import logging
import time

import requests

from sources.sec_edgar import SECFetchError, _headers, get_cik_for_ticker

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# Polite pacing -- SEC's fair-access policy asks for no more than ~10
# req/sec; this stays well under that (same conservative-pacing philosophy
# as sources/nse_fetch.py's 1 req/sec, just not as strict since SEC's own
# policy is more permissive and this endpoint isn't WAF-protected).
_REQUEST_PACING_SECONDS = 0.3


def _get_with_retries(url: str, *, max_attempts: int = 4) -> requests.Response:
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, headers=_headers(), timeout=20)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("SEC request failed (attempt %d/%d): %s -- retrying in %.1fs", attempt, max_attempts, exc, delay)
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= 2
    raise SECFetchError(f"Failed to fetch {url} after {max_attempts} attempts: {last_exc}")


def discover_10k_filings(ticker: str) -> list[dict]:
    """Every 10-K filing on file for `ticker`, oldest and newest included --
    walks the "recent" block plus any paginated older-filings files SEC
    splits long filing histories into (verified live: AAPL's history goes
    back to 1994 via one such paginated file). Returns
    [{accession_number, filing_date, report_date, primary_document, doc_url}],
    newest first, or [] if the ticker has no resolvable CIK or no 10-Ks on
    file (never raises for "no filings" -- only for a genuine fetch
    failure)."""
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        logger.warning("SEC EDGAR: no CIK found for ticker %s", ticker)
        return []

    time.sleep(_REQUEST_PACING_SECONDS)
    resp = _get_with_retries(_SUBMISSIONS_URL.format(cik=cik))
    data = resp.json()

    blocks = [data["filings"]["recent"]]
    for older in data["filings"].get("files", []):
        time.sleep(_REQUEST_PACING_SECONDS)
        older_resp = _get_with_retries(f"https://data.sec.gov/submissions/{older['name']}")
        blocks.append(older_resp.json())

    filings: list[dict] = []
    for block in blocks:
        forms = block.get("form", [])
        for i, form in enumerate(forms):
            if form != "10-K":
                continue
            accession = block["accessionNumber"][i]
            primary_doc = block["primaryDocument"][i]
            accession_nodash = accession.replace("-", "")
            filings.append({
                "accession_number": accession,
                "filing_date": block["filingDate"][i],
                "report_date": block.get("reportDate", [None] * len(forms))[i],
                "primary_document": primary_doc,
                "doc_url": f"{_ARCHIVES_BASE}/{cik}/{accession_nodash}/{primary_doc}",
            })

    filings.sort(key=lambda f: f["filing_date"], reverse=True)
    return filings


def download_filing(doc_url: str) -> bytes:
    time.sleep(_REQUEST_PACING_SECONDS)
    resp = _get_with_retries(doc_url)
    return resp.content
