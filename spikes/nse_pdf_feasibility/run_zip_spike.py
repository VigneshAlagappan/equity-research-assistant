"""Section 8 follow-up: what's actually inside the pre-2019 .zip attachments
NSE's corporate-announcements system used for quarterly results before PDF
attachments became standard (per the original spike's Section 8/Section 3
finding). ZIP URLs reused verbatim from data/results.json's `~10y_ago`
period (already discovered by the first spike run — no re-discovery here).

Downloads + unzips to spikes/nse_pdf_feasibility/data/zips/<SYMBOL>/ — local
scratch only, never the real S3 bucket (storage/document_store.py not
imported or touched). Same hard rule as every other request in this spike:
explicit timeout, non-blocking failure handling, no retry loops.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_zip_spike")

DATA_DIR = Path(__file__).parent / "data"
ZIP_DIR = DATA_DIR / "zips"

_REQUEST_TIMEOUT_SECONDS = 15
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ZIP_URLS = {
    "HDFCBANK": "https://nsearchives.nseindia.com/corporate/Result30062016_21072016115151.zip",
    "RELIANCE": "https://nsearchives.nseindia.com/corporate/4185_001_15072016170723.zip",
    "ICICIBANK": "https://nsearchives.nseindia.com/corporate/BSE_NSE_29072016_f_29072016163907.zip",
    "TCS": "https://nsearchives.nseindia.com/corporate/TCSQ1FY17_14072016161204.zip",
}


def _bounded_get(session: requests.Session, url: str) -> requests.Response | None:
    """Single bounded GET, never raises past this function — mirrors
    nse_pdf_fetch.py's own rule (explicit timeout, caught failure, no
    retry loop). These archive downloads don't need the anti-bot session
    dance (nsearchives.nseindia.com, not www.nseindia.com — verified in the
    original spike run, every PDF under this same host downloaded fine
    without a fresh bootstrap)."""
    t0 = time.monotonic()
    try:
        resp = session.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - t0
        logger.info("GET %s -> %s (%.2fs)", url, resp.status_code, elapsed)
        if resp.status_code in (403, 429):
            logger.warning("Blocked (status=%s): %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        elapsed = time.monotonic() - t0
        logger.warning("Request failed after %.2fs: %s (%s)", elapsed, url, exc)
        return None


def process_company(symbol: str, url: str, session: requests.Session) -> dict:
    result: dict = {"symbol": symbol, "zip_url": url}
    company_dir = ZIP_DIR / symbol
    company_dir.mkdir(parents=True, exist_ok=True)
    zip_path = company_dir / "download.zip"

    resp = _bounded_get(session, url)
    if resp is None:
        result["downloaded"] = False
        result["error"] = "download failed or blocked"
        return result

    zip_path.write_bytes(resp.content)
    result["downloaded"] = True
    result["zip_size_bytes"] = len(resp.content)
    result["local_zip_path"] = str(zip_path)

    extract_dir = company_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            result["zip_valid"] = True
            result["contents"] = []
            for name in names:
                info = zf.getinfo(name)
                result["contents"].append({"name": name, "size_bytes": info.file_size})
            zf.extractall(extract_dir)
            result["extracted_to"] = str(extract_dir)
    except zipfile.BadZipFile as exc:
        result["zip_valid"] = False
        result["error"] = f"not a valid zip: {exc}"
        # Peek at the raw bytes — sometimes a ".zip"-named URL is actually
        # something else entirely (e.g. an HTML error page, or a
        # differently-formatted archive) — report what it really is rather
        # than just failing silently.
        head = zip_path.read_bytes()[:16]
        result["raw_head_bytes"] = head.hex()
        result["looks_like_pdf"] = head[:4] == b"%PDF"
        result["looks_like_html"] = b"<html" in head.lower() or b"<!doc" in head.lower()

    return result


def main() -> None:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    all_results = {}
    for symbol, url in ZIP_URLS.items():
        logger.info("=== %s ===", symbol)
        all_results[symbol] = process_company(symbol, url, session)
        time.sleep(1.0)  # same courtesy pacing as every other NSE request in this spike

    (DATA_DIR / "zip_results.json").write_text(json.dumps(all_results, indent=2, default=str))
    logger.info("Done. Wrote %s", DATA_DIR / "zip_results.json")


if __name__ == "__main__":
    main()
