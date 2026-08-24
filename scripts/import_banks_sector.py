"""One-off importer: files the Banks/ sector of the proprietary AnnualReports
archive (~/work/AnnualReports/Banks) into this repo's data/raw and
data/documents conventions, then runs the existing ingestion pipeline.

Every mapping below was decided by hand after auditing the source folder —
letterhead/trading-symbol text was checked in every PDF this script files
(pypdf) to catch misfiled documents (two were found: a Ujjivan Small Finance
Bank annual report copied into Equitas's folder, and an Ujjivan Financial
Services report sitting in the Ujjivan Small Finance Bank folder — both are
routed to the correct company below, not the folder they were found in).
Byte-identical duplicate files were deduped by content hash, not filename.

Explicitly NOT filed (see SKIPPED below for the full list and why):
- Companies with no active match in this app's registry (Janalakshmi Small
  Finance Bank and Lakshmi Vilas Bank are both delisted/absorbed; Standard
  Chartered PLC is not NSE/BSE-listed).
- Sector-wide research notes and industry overviews not tied to one company.
- Third-party analyst research (e.g. an Edelweiss report) — not a type this
  app's documents.document_type enum covers, and not company-issued.
- A handful of PDFs whose type didn't cleanly fit the document_type enum
  (an ESG report, an NCLT merger-scheme filing, an unlabeled 88-page filing)
  or that turned out to be a different, unlisted legal entity (HDFC Bank's
  folder held HDFC Limited's own Integrated Report — a distinct company that
  merged into HDFC Bank in 2023, not HDFC Bank's own report) — filed on a
  guess is worse than not filed, so these are left for manual review.

Two workbooks were initially held back over identity concerns and filed in a
follow-up pass once the user confirmed the mapping: "Equity Analysis - Bank -
Equitas Financial.xlsx" (blank "Company Name" field, name didn't exactly
match a registry entry — confirmed same company as Equitas SFB) and
"Equity Analysis - Bank - Ujjivan Financial.xlsx" (its issuer, Ujjivan
Financial Services, is archived with successor_company_id=UJJIVANSFB, but
was initially left unfiled since it's a different pre-merger balance sheet
from the bank subsidiary — confirmed the same company for this app's
purposes). Both are reflected in the mappings below as filed.

Idempotent-ish: re-running skips an xlsx source file that's already present
at its destination path (byte-identical), but does NOT check whether a PDF
was already filed as a documents row, so re-running will create duplicate
document rows — this is a one-shot script, not a sync job.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import DATA_DIR, RAW_DIR, DOCUMENTS_DIR
from companies.registry import get_company
from ingestion.pipeline import ingest_file
from storage.database import init_db
from storage.repositories import save_company_document

ARCHIVE_ROOT = Path("/Users/radhamurugesan/work/AnnualReports/Banks")

# ------------------------------------------------------------------
# 1. Proprietary xlsx workbooks -> data/raw/<company_id>/proprietary/
# ------------------------------------------------------------------

XLSX_PLAN: dict[str, list[str]] = {
    "AXISBANK": ["Equity Analysis - Bank - Axis.xlsx"],
    "CANBK": [
        "Equity Analysis - Bank - Canara Bank-2015.xlsx",
        "0. old/Equity Analysis - Bank - Canara Bank.xlsx",
    ],
    # EQUITASSFB is archived (duplicate, successor_company_id=EQUITASBNK) —
    # all three Equitas workbooks go to the active successor. "Equitas
    # Financial.xlsx" was initially held back — its own "Company Name" field
    # is blank and the name doesn't match any registry entry exactly — but
    # the user confirmed it's the same company as Equitas SFB.
    "EQUITASBNK": [
        "Equity Analysis - Bank - Equitas SFB.xlsx",
        "0. old/Equity Analysis - Bank - Equitas-2018.xlsx",
        "Equity Analysis - Bank - Equitas Financial.xlsx",
    ],
    "HDFCBANK": ["Equity Analysis - Bank - HDFC Bank.xlsx"],
    "ICICIBANK": [
        "Equity Analysis - Bank - ICICI Bank.xlsx",
        "0. old/Equity Analysis - Bank - icici-2013.xlsx",
    ],
    # IDFCBANK is archived (acquired) with no successor_company_id set in the
    # registry — ingesting into it fails assert_active(). All four "IDFC
    # Bank"-named workbooks (including the ones that predate the Dec-2018
    # Capital First merger) are filed under IDFCFIRSTB instead, the active
    # entity carrying this banking franchise's real data today — same as the
    # three "IDFC First Bank"-named workbooks below.
    "IDFCFIRSTB": [
        "Equity Analysis - Bank - IDFC Bank.xlsx",
        "0. old/Equity Analysis - Bank - IDFC Bank - 2017.xlsx",
        "0. old/Equity Analysis - Bank - IDFC Bank-2018.xlsx",
        "0. old/Equity Analysis - Bank - IDFC Bank-2023.xlsx",
        "0. old/Equity Analysis - Bank - IDFC First Bank - 2019.xlsx",
        "0. old/Equity Analysis - Bank - IDFC First Bank - old.xlsx",
        "0. old/Equity Analysis - Bank - IDFC First Bank-2023.xlsx",
    ],
    "INDUSINDBK": ["Equity Analysis - Bank - IndusInd.xlsx"],
    "KOTAKBANK": [
        "Equity Analysis - Bank - Kotak Bank.xlsx",
        "0. old/Equity Analysis - Bank - Kotak Bank - 2014.xlsx",
        "0. old/Equity Analysis - Bank - Kotak Mahindra Bank - old.xlsx",
        "0. old/Equity Analysis - Bank - Kotak Mahindra Bank FY2013.xlsx",
        "0. old/Equity Analysis - Bank - Kotak Mahindra Bank FY2014.xlsx",
    ],
    "PNB": [
        "Equity Analysis - Bank - PNB.xlsx",
        "0. old/Equity Analysis - Bank - PNB - 2013.xlsx",
    ],
    # UJJIVANFINANCIAL is archived (merged into UJJIVANSFB per the registry's
    # own successor_company_id) — ingesting into it fails assert_active().
    # Initially left unfiled over a concern that Ujjivan Financial Services
    # (the former NBFC holding company) and Ujjivan Small Finance Bank (the
    # bank subsidiary) were different balance sheets — the user confirmed
    # they're the same company for this app's purposes, so it's filed under
    # the active successor, same treatment as IDFCBANK -> IDFCFIRSTB above.
    "UJJIVANSFB": [
        "Equity Analysis - Bank - Ujjivan SFB.xlsx",
        "Equity Analysis - Bank - Ujjivan Financial.xlsx",
    ],
    "YESBANK": [
        "Equity Analysis - Bank - Yes Bank.xlsx",
        "0. old/Equity Analysis - Bank - Yes Bank - 2014 - new.xlsx",
        "0. old/Equity Analysis - Bank - Yes Bank - 2014.xlsx",
        "0. old/Equity Analysis - Bank - Yes Bank - old.xlsx",
        "0. old/Equity Analysis - Bank - Yes Bank-2017.xlsx",
    ],
    # The un-spaced "Equity Analysis - Bank -kvb.xlsx" is a byte-identical
    # duplicate of this one (confirmed by hash) — only filed once.
    "KARURVYSYA": ["Equity Analysis - Bank - kvb.xlsx"],
}

# ------------------------------------------------------------------
# 2. PDFs -> data/documents/<company_id>/, one documents row each.
#    (relative_path, document_type, fiscal_year, quarter)
# ------------------------------------------------------------------


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


PDF_PLAN: dict[str, list[DocPlan]] = {
    "AUBANK": [
        DocPlan("AU SFB/2023-24-Q3.pdf", "financial_result", "FY2024", "Q3"),
        DocPlan("AU SFB/AR-2023-24.pdf", "annual_report", "FY2024"),
        DocPlan("AU SFB/AR-2024-25.pdf", "annual_report", "FY2025"),
    ],
    "BANDHANBNK": [
        DocPlan("Bandhan Bank/AR-2024-25.pdf", "annual_report", "FY2025"),
    ],
    "BANKBARODA": [
        DocPlan("BoB/2011-12/Annualreport2011-12.pdf", "annual_report", "FY2012"),
    ],
    "EQUITASBNK": [
        DocPlan(
            "Equitas Small Finance Bank/2015-16/Equitas_AnnualReport_FY2015-2016-1.pdf",
            "annual_report", "FY2016",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2017-18/EHLAGMpresentation27072018.pdf",
            "investor_presentation", "FY2018",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2017-18/Equitas-concall-Q2-FY-17-18.pdf",
            "transcript", "FY2018", "Q2",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2017-18/q4fy18-earnings-call-transcript.pdf",
            "transcript", "FY2018", "Q4",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2018-19/Q1-fy19-Investor Presentation.pdf",
            "investor_presentation", "FY2019", "Q1",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2018-19/Q4fy19-conference-call-transcript.pdf",
            "transcript", "FY2019", "Q4",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2019-20/Q3-Investor presentation.pdf",
            "investor_presentation", "FY2020", "Q3",
        ),
        DocPlan(
            "Equitas Small Finance Bank/2021-22/ESFB_Q3FY22_Investor-Presentation-Final.pdf",
            "investor_presentation", "FY2022", "Q3",
        ),
        # "AR 2023.pdf" in this same folder is NOT filed here — it is
        # byte-identical to Ujjivan Small Finance Bank's own AR (letterhead
        # confirms "Symbol: UJJIVANSFB"), a misfile in the source archive.
        DocPlan("Equitas Small Finance Bank/2022-23/AR-2022-23.pdf", "annual_report", "FY2023"),
    ],
    "FEDERALBNK": [
        DocPlan("Federal Bank/AR-2022-23.pdf", "annual_report", "FY2023"),
    ],
    "HDFCBANK": [
        DocPlan("HDFC Bank/AR-2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("HDFC Bank/AR-2023-24-HDFC.pdf", "annual_report", "FY2024"),
        DocPlan("HDFC Bank/AR-2024-25.pdf", "annual_report", "FY2025"),
        # "Integrated Report 22-23.pdf" and "HDFC Bank Limited.pdf" are
        # deliberately NOT filed here — see module docstring.
    ],
    # Only the two documents that genuinely predate the Dec-2018 Capital
    # First merger stay under IDFCBANK (archived, but still browsable —
    # documents aren't gated on active status the way xlsx ingestion is).
    # Everything from 2022-23 onward carries an "IDFC FIRST Bank Limited"
    # letterhead (verified per-file) despite sitting in a folder named
    # "IDFC Bank" — those go to IDFCFIRSTB below, not here.
    "IDFCBANK": [
        DocPlan(
            "IDFC Bank/2014-15/IDFC_Bank_AnnualReport_2014-15.pdf",
            "annual_report", "FY2015",
        ),
        DocPlan(
            "IDFC Bank/MergerDetails/IDFC-Shriram merger/IDFC-Shriram-Presentation.pdf",
            "investor_presentation", "FY2018",
        ),
    ],
    "IDFCFIRSTB": [
        DocPlan("IDFC Bank/2022-23/AR-2022-23.pdf", "annual_report", "FY2023"),
        DocPlan("IDFC Bank/2023-24/AR-2023-24-IDFC.pdf", "annual_report", "FY2024"),
        DocPlan(
            "IDFC Bank/2023-24/Concall-Transcript-of-IDFC-FIRST-Bank-Q4-FY24-Results-1.pdf",
            "transcript", "FY2024", "Q4",
        ),
        DocPlan("IDFC Bank/2024-25/AR 2024-25.pdf", "annual_report", "FY2025"),
        DocPlan("IDFC Bank/2024-25/Q4-2025.pdf", "financial_result", "FY2025", "Q4"),
    ],
    "IOB": [
        DocPlan("IOB/2014-15/IOB_Annual_Report_ 2014-15.pdf", "annual_report", "FY2015"),
        DocPlan("IOB/2016-17/Annual_report2016-2017.pdf", "annual_report", "FY2017"),
    ],
    "KOTAKBANK": [
        DocPlan("Kotak Bank/2022-23/AR-2022-23.pdf", "annual_report", "FY2023"),
        DocPlan("Kotak Bank/2023-24/AR-2023-24.pdf", "annual_report", "FY2024"),
        DocPlan("Kotak Bank/2024-25/AR-2024-25.pdf", "annual_report", "FY2025"),
    ],
    "RBLBANK": [
        DocPlan("RBL/2019-20/Annual update.pdf", "financial_result", "FY2020"),
        DocPlan("RBL/2019-20/Call_Transcript_Q1_FY_20.pdf", "transcript", "FY2020", "Q1"),
        DocPlan("RBL/2023-24/RBL-AR-2023-24.pdf", "annual_report", "FY2024"),
    ],
    "UJJIVANSFB": [
        DocPlan(
            "Ujjivan Small Finance Bank/2021-22/Q1-Concal script.pdf",
            "transcript", "FY2022", "Q1",
        ),
        DocPlan(
            "Ujjivan Small Finance Bank/2022-23/AR 2022-23-SFB.pdf",
            "annual_report", "FY2023",
        ),
        # This file's own letterhead reads "Trading Symbol: UJJIVAN" — that's
        # Ujjivan Financial Services, not the small finance bank whose folder
        # it was sitting in. Initially filed under a separate UJJIVANFINANCIAL
        # company_id (routed by content, not folder) but the user confirmed
        # it's the same company as Ujjivan Small Finance Bank, so it's filed
        # here alongside the bank's own report.
        DocPlan(
            "Ujjivan Small Finance Bank/2022-23/AR2022-23.pdf",
            "annual_report", "FY2023",
        ),
    ],
    "YESBANK": [
        DocPlan("Yes bank/2012-13/AR_12_13.pdf", "annual_report", "FY2013"),
    ],
}

ADDED_BY = "proprietary-import:AnnualReports/Banks"


def _copy_xlsx(company_id: str, rel_path: str) -> Path:
    src = ARCHIVE_ROOT / rel_path
    dest_dir = RAW_DIR / company_id / "proprietary"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(rel_path).name
    if dest.exists():
        if dest.read_bytes() == src.read_bytes():
            return dest  # already placed (e.g. HDFCBANK, copied by hand earlier)
        # Same target filename, different content — disambiguate rather than
        # silently overwrite a revision that's already there.
        dest = dest_dir / f"{dest.stem}__{src.stat().st_mtime_ns}{dest.suffix}"
    shutil.copy2(src, dest)
    return dest


def _copy_pdf(company_id: str, rel_path: str) -> Path:
    src = ARCHIVE_ROOT / rel_path
    dest_dir = DOCUMENTS_DIR / company_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    safe_name = Path(rel_path).name
    dest = dest_dir / f"{stamp}__{safe_name}"
    shutil.copy2(src, dest)
    return dest


def main() -> None:
    conn = init_db()

    print("=== XLSX (proprietary source) ===")
    for company_id, rel_paths in XLSX_PLAN.items():
        if get_company(conn, company_id) is None:
            print(f"{company_id:20s} SKIPPED — not registered")
            continue
        for rel_path in rel_paths:
            src = ARCHIVE_ROOT / rel_path
            if not src.exists():
                print(f"{company_id:20s} MISSING SOURCE  {rel_path}")
                continue
            dest = _copy_xlsx(company_id, rel_path)
            try:
                result = ingest_file(conn, dest, company_id=company_id, source_id="proprietary")
                print(
                    f"{company_id:20s} OK  parsed={result.parsed_count:3d} "
                    f"inserted={result.inserted_count:3d} reconciled={result.reconciled_count:3d} "
                    f"skipped={result.skipped_count:3d}  <- {Path(rel_path).name}"
                )
                if result.skip_reasons:
                    for reason in result.skip_reasons[:3]:
                        print(f"{'':20s}     skip: {reason}")
            except Exception as exc:  # noqa: BLE001 — report and keep going
                print(f"{company_id:20s} FAILED  {type(exc).__name__}: {exc}  <- {Path(rel_path).name}")

    print("\n=== PDFs (documents table) ===")
    for company_id, docs in PDF_PLAN.items():
        if get_company(conn, company_id) is None:
            print(f"{company_id:20s} SKIPPED — not registered")
            continue
        for doc in docs:
            src = ARCHIVE_ROOT / doc.rel_path
            if not src.exists():
                print(f"{company_id:20s} MISSING SOURCE  {doc.rel_path}")
                continue
            dest = _copy_pdf(company_id, doc.rel_path)
            row = save_company_document(
                conn,
                company_id,
                document_type=doc.document_type,
                fiscal_year=doc.fiscal_year,
                quarter=doc.quarter,
                added_by_user=ADDED_BY,
                raw_file_path=str(dest),
            )
            print(
                f"{company_id:20s} OK  document_id={row['document_id']:4d} "
                f"{doc.document_type:22s} {doc.fiscal_year}{('/' + doc.quarter) if doc.quarter else '':5s}"
                f"  <- {Path(doc.rel_path).name}"
            )

    conn.close()


if __name__ == "__main__":
    main()
