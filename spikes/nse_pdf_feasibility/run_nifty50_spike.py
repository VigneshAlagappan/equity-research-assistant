"""Nifty 50 extension of the 4-company spike (see FEASIBILITY_REPORT.md
Section 11's own "next step" recommendation).

Two tiers, one pass per company (one bootstrap + one corporate-announcements
call per company — the PDF spot-check subset reuses that SAME session/fetch
rather than re-fetching, to keep total NSE traffic proportionate):

  Tier 1 (all 50): discovery + two-stage verification filter
  (run_spike._is_fin_result_row) applied to each company's most recent
  fiscal year of announcements (~370 days). Every matched row is logged to
  data/nifty50_filter_matches.csv for manual false-positive/false-negative
  review. Company-level match counts are used to flag likely false
  negatives (companies where the filter found suspiciously few genuine
  filings for a full fiscal year).

  Tier 2 (a ~12-company, sector-diverse subset, chosen to NOT repeat the
  original 4-company sample): download the most recent verified filing's
  PDF and record pypdf extraction quality, same method as the original
  spike.

Pacing matches sources/nse_xbrl.py's own _REQUEST_PACING_SECONDS (see
nse_pdf_fetch.py) — this run is meant to behave like a real batch job would,
not like this spike's earlier faster dev-iteration pacing.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nse_pdf_fetch as npf
from run_spike import _is_fin_result_row, _has_real_pdf, _extract_period_end, _FIN_TEXT_MARKERS, _EXCLUDED_DESC_CATEGORIES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_nifty50_spike")

DATA_DIR = Path(__file__).parent / "data"
PDF_DIR = DATA_DIR / "pdfs_nifty50"

TODAY = datetime(2026, 9, 17)
LOOKBACK_DAYS = 370  # "one full recent fiscal year" with a little slack

# Sector-diverse subset for the PDF download/extraction spot-check —
# deliberately NOT re-using HDFCBANK/RELIANCE/ICICIBANK/TCS (already tested
# in the original 4-company spike). One or two per major GICS-like sector
# bucket seen in company_index_membership's own sector column.
PDF_SPOTCHECK_SYMBOLS = {
    "SBIN", "KOTAKBANK",       # banks (different from HDFCBANK/ICICIBANK)
    "INFY", "WIPRO",           # IT (different from TCS)
    "ONGC", "COALINDIA",       # energy/fuels (different from RELIANCE)
    "HINDUNILVR", "ITC",       # FMCG
    "MARUTI",                  # auto
    "SUNPHARMA",               # pharma
    "TATASTEEL",               # metals
    "BHARTIARTL",              # telecom
}


def _parse_sort_date(d: dict) -> datetime | None:
    try:
        return datetime.strptime(d["sort_date"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def process_company(company: dict, csv_writer, pdf_subset: set[str]) -> dict:
    symbol = company["nse_symbol"]
    summary = {"symbol": symbol, "sector": company.get("sector"), "industry": company.get("industry")}

    session = npf.new_session()
    if session is None:
        summary["blocked"] = "bootstrap failed"
        return summary

    resp = npf._get(session, "https://www.nseindia.com/api/corporate-announcements",
                     params={"index": "equities", "symbol": symbol})
    if resp is None:
        summary["blocked"] = "corporate-announcements fetch failed (network error)"
        return summary
    if resp.status_code in (403, 429):
        summary["blocked"] = f"corporate-announcements blocked (status {resp.status_code})"
        return summary
    try:
        ann_rows = resp.json()
    except ValueError:
        summary["blocked"] = "non-JSON response"
        return summary

    cutoff = TODAY - timedelta(days=LOOKBACK_DAYS)
    recent_rows = [r for r in ann_rows if (d := _parse_sort_date(r)) is not None and d >= cutoff]
    matches = [r for r in recent_rows if _is_fin_result_row(r)]

    summary["total_announcements"] = len(ann_rows)
    summary["recent_year_announcements"] = len(recent_rows)
    summary["filter_matches_last_year"] = len(matches)
    summary["match_descs"] = sorted(set(m.get("desc", "") for m in matches))

    for m in matches:
        csv_writer.writerow({
            "symbol": symbol,
            "sort_date": m.get("sort_date"),
            "desc": m.get("desc"),
            "attchmntText": (m.get("attchmntText") or "")[:300],
            "attchmntFile": m.get("attchmntFile"),
            "has_real_pdf": _has_real_pdf(m),
            "reported_period_end": (pe.isoformat() if (pe := _extract_period_end(m.get("attchmntText") or "")) else None),
        })

    # Tier 2: PDF download for the sector-diverse subset, most recent
    # verified+PDF-bearing match only (reuses this same session/announcements
    # fetch — no extra discovery request for these companies).
    if symbol in pdf_subset:
        pdf_matches = [m for m in matches if _has_real_pdf(m)]
        if pdf_matches:
            most_recent = max(pdf_matches, key=lambda m: m.get("sort_date", ""))
            dest = PDF_DIR / f"{symbol}_{most_recent.get('seq_id', 'noseq')}.pdf"
            ok = npf.download_pdf(session, most_recent["attchmntFile"], dest)
            summary["pdf_spotcheck"] = {
                "attempted": True,
                "downloaded": ok,
                "sort_date": most_recent.get("sort_date"),
                "desc": most_recent.get("desc"),
                "local_path": str(dest) if ok else None,
            }
            if ok:
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(dest)
                    text = "\n".join((p.extract_text() or "") for p in reader.pages)
                    summary["pdf_spotcheck"]["pages"] = len(reader.pages)
                    summary["pdf_spotcheck"]["extracted_chars"] = len(text)
                    summary["pdf_spotcheck"]["extraction_quality"] = (
                        "clean" if len(text) > 5000 else "sparse/scanned"
                    )
                except Exception as exc:
                    summary["pdf_spotcheck"]["extraction_error"] = str(exc)
        else:
            summary["pdf_spotcheck"] = {"attempted": False, "reason": "no PDF-bearing verified match in lookback window"}

    return summary


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    companies = json.loads((DATA_DIR / "nifty50_companies.json").read_text())
    logger.info("Loaded %d Nifty 50 companies from real company_index_membership data", len(companies))

    csv_path = DATA_DIR / "nifty50_filter_matches.csv"
    all_summaries = []
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "sort_date", "desc", "attchmntText", "attchmntFile", "has_real_pdf", "reported_period_end",
        ])
        writer.writeheader()
        for i, company in enumerate(companies, 1):
            logger.info("[%d/%d] %s (%s)", i, len(companies), company["nse_symbol"], company.get("sector"))
            summary = process_company(company, writer, PDF_SPOTCHECK_SYMBOLS)
            all_summaries.append(summary)
            f.flush()

    (DATA_DIR / "nifty50_results.json").write_text(json.dumps(all_summaries, indent=2, default=str))
    npf.dump_attempt_log(DATA_DIR / "nifty50_attempt_log.json")

    blocked = [s for s in all_summaries if "blocked" in s]
    low_match = [s for s in all_summaries if "blocked" not in s and s.get("filter_matches_last_year", 0) < 2]
    logger.info("Done. %d/%d companies processed without blocking. %d flagged low-match (<2 in a year) for review.",
                len(all_summaries) - len(blocked), len(all_summaries), len(low_match))
    if blocked:
        logger.warning("Blocked companies: %s", [s["symbol"] for s in blocked])
    if low_match:
        logger.warning("Low-match companies (possible false negatives): %s", [s["symbol"] for s in low_match])


if __name__ == "__main__":
    main()
