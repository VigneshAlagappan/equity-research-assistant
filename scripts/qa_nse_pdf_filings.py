"""QA / spot-check tool for the NSE PDF discovery + extraction pipeline —
the feasibility report's own Section 11/12.4 recommendation ("an initial
backfill with the filter's matches logged for manual spot-check on a
sample... before trusting it unsupervised") made concrete and runnable.

READ-ONLY against NSE and against this app's own database: this script
never calls insert_financial_observations / save_company_document / any
other write path. It downloads PDFs to a scratch directory (same as
scripts/fetch_nse_xbrl.py's "staging, not ingestion" convention) purely so
extraction can be spot-checked, and prints one report per company:

  - every matched filing's desc / attchmntText / attchmntFile / seq_id /
    match_confidence (so a reviewer can independently judge whether the
    date-window matcher picked the right document, per Section 12.2's own
    false-positive/false-negative findings)
  - the extraction outcome (extracted / needs_ocr / not_found) and, when
    extracted, every fact pulled out (metric_key, period, statement_type,
    value) for direct comparison against the PDF

Run this BEFORE scripts/backfill_nse_pdf_filings.py on any company/year
range that hasn't been spot-checked yet.
"""

from __future__ import annotations

import argparse
import json
import logging
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

from companies.registry import get_company
from sources.nse_fetch import _new_session
from sources.nse_pdf_extractor import extract_from_pdf
from sources.nse_pdf_filings import discover_result_filings, download_filing, quarter_end_dates
from storage.database import init_db

logging.basicConfig(level=logging.WARNING)  # keep stdout clean for the report itself
logger = logging.getLogger("qa_nse_pdf_filings")

_SCRATCH_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "_nse_pdf_qa_scratch"
_XBRL_ERA_START_YEAR = 2019


def _unwrap_zip_if_needed(path: Path) -> Path | None:
    if not zipfile.is_zipfile(path):
        return path
    extract_dir = path.parent / f"{path.stem}_extracted"
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        return None
    pdfs = [n for n in names if n.lower().endswith(".pdf")]
    return (extract_dir / pdfs[0]) if pdfs else None


def qa_company(session, company_id: str, nse_symbol: str, *, from_year: int, to_year: int) -> dict:
    start, end = date(from_year, 1, 1), date(to_year, 12, 31)
    matches = discover_result_filings(nse_symbol, start=start, end=end, session=session)
    quarters = quarter_end_dates(start, end)

    report: dict = {"company_id": company_id, "nse_symbol": nse_symbol, "quarters": []}
    for qe in quarters:
        match = next((m for m in matches if m.period_end == qe), None)
        entry: dict = {"period_end": qe.isoformat()}
        if match is None:
            entry["status"] = "not_found"
            report["quarters"].append(entry)
            continue

        entry.update({
            "fiscal_year": match.fiscal_year,
            "quarter": match.quarter,
            "seq_id": match.seq_id,
            "desc": match.desc,
            "attchmnt_text": match.attchmnt_text,
            "source_url": match.source_url,
            "match_confidence": match.match_confidence,
            "attachment_format": match.attachment_format,
        })

        if match.attachment_format not in ("pdf", "zip"):
            entry["status"] = "not_found"
            report["quarters"].append(entry)
            continue

        dest = _SCRATCH_DIR / company_id / f"{match.fiscal_year}_{match.quarter}_{match.seq_id}.{match.attachment_format}"
        if not download_filing(session, match, dest):
            entry["status"] = "download_failed"
            report["quarters"].append(entry)
            continue

        pdf_path = _unwrap_zip_if_needed(dest) if match.attachment_format == "zip" else dest
        if pdf_path is None:
            entry["status"] = "needs_ocr"
            entry["notes"] = "zip had no usable PDF inside"
            report["quarters"].append(entry)
            continue

        include_pnl = int(match.fiscal_year.replace("FY", "")) < _XBRL_ERA_START_YEAR
        facts = extract_from_pdf(
            pdf_path, expected_quarter_end=qe, expected_fiscal_year=match.fiscal_year,
            expected_quarter=match.quarter, include_pnl=include_pnl,
        )
        if not facts:
            entry["status"] = "needs_ocr"
        else:
            entry["status"] = "extracted"
            entry["facts"] = [asdict(f) for f in facts]
        report["quarters"].append(entry)

    return report


def _print_human_report(report: dict) -> None:
    print(f"\n===== {report['company_id']} ({report['nse_symbol']}) =====")
    for entry in report["quarters"]:
        status = entry["status"]
        label = f"{entry.get('fiscal_year', '?')}{entry.get('quarter', '')} (period_end={entry['period_end']})"
        print(f"\n  {label} -- {status}")
        if status == "not_found":
            continue
        print(f"    desc={entry.get('desc')!r}")
        print(f"    attchmnt_text={entry.get('attchmnt_text', '')[:160]!r}")
        print(f"    source_url={entry.get('source_url')}")
        print(f"    seq_id={entry.get('seq_id')} match_confidence={entry.get('match_confidence')} format={entry.get('attachment_format')}")
        if status == "extracted":
            for fact in entry["facts"]:
                print(
                    f"      {fact['statement_type']:<12} {fact['period_type']:<9} "
                    f"{fact['fiscal_year']}{fact['quarter'] or '':<3} {fact['metric_key']:<28} = {fact['value']}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", action="append", required=True, help="company_id, e.g. HDFCBANK (repeatable)")
    parser.add_argument("--from-year", type=int, default=2019)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument("--json-out", type=str, default=None, help="also write the full structured report here")
    args = parser.parse_args()

    conn = init_db()
    session = _new_session()
    reports = []
    try:
        for company_id in args.company:
            company_id = company_id.upper()
            company = get_company(conn, company_id)
            if company is None or not company["nse_symbol"]:
                print(f"{company_id}: not registered or no nse_symbol on file — skipping")
                continue
            report = qa_company(session, company_id, company["nse_symbol"], from_year=args.from_year, to_year=args.to_year)
            reports.append(report)
            _print_human_report(report)
    finally:
        session.close()
        conn.close()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, indent=2, default=str))
        print(f"\nFull structured report written to {args.json_out}")


if __name__ == "__main__":
    main()
