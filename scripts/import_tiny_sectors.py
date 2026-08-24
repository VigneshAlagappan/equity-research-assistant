"""One-off importer: files the 19 smallest sector folders (1-10 files each,
49 files total) from the proprietary AnnualReports archive — Bearings,
Breweries, Cigrettes, Dry Cells, Electrical, Engg, Insurance, Media,
Plantations, Pumps, Space, Consumer Goods, Health Care Life Science, Sugar,
Textile, Construction Materials, Consumer Durables, Fertilizers, Glass.

Same discipline as import_banks_sector.py / import_finance_sector.py:
letterhead/content checked before assigning company_id and fiscal_year,
byte-identical duplicates deduped by hash.

Not filed, and why:
- Bearings, Cigrettes, Dry Cells folders hold only a personal research
  readme.txt each (sector notes, not company documents) — nothing to file.
- Pratibha Industries (Engg), Mandhana Industries (Textile), and Opto
  Circuits (Health Care Life Science) have no active match in this app's
  registry — all three went through insolvency/delisting in the real world
  (~2017-18), consistent with not being in a current-listed-company
  registry snapshot.
- Third-party research/forum content: a Mandhana "Firstcall" broker note
  and an "India Textiles" conference report (Textile), a Kirloskar Brothers
  SWOT-analysis writeup (Pumps), a Deepak Fertilisers profile piece from
  Forbes India (Fertilizers), a ValuePickr forum post about La Opala
  (Glass) — none are company-issued.
- SpaceX (Space) is not an Indian-listed company.
- GM Breweries' "35th-AGM-Notice1.pdf" (Breweries) is only 8 pages — the
  AGM meeting notice/agenda, not the actual annual report — despite its
  title line reading "35th Annual Report 2017-2018".
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import RAW_DIR, DOCUMENTS_DIR
from companies.registry import get_company
from ingestion.pipeline import ingest_file
from storage.database import init_db
from storage.repositories import save_company_document

ARCHIVE_ROOT = Path("/Users/radhamurugesan/work/AnnualReports")

XLSX_PLAN: dict[str, list[str]] = {
    "PRAJIND": ["Engg/Equity Analysis - Praj Industries.xlsx"],
    "LAOPALA": ["Glass/Equity Analysis - La opala RG.xlsx"],
    "DEEPAKFERT": ["Fertilizers/Equity Analysis - Deepak Fertilizers.xlsx"],
    "LMW": ["Textile/Equity Analysis - LMW.xlsx"],
}


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


PDF_PLAN: dict[str, list[DocPlan]] = {
    "KOTHARIPRO": [
        DocPlan("Cigrettes/KOTHARI-AUDITED-RESULTS-31-03-2013.pdf", "financial_result", "FY2013"),
    ],
    "BBL": [
        DocPlan("Electrical/Bharati Biljee/PressReleaseMarch2014.pdf", "financial_result", "FY2014", "Q4"),
    ],
    "HDFCLIFE": [
        DocPlan("Insurance/HDFC Life/AR-2025-26.pdf", "annual_report", "FY2026"),
    ],
    "NDTV": [
        DocPlan("Media/NDTV/Annual_Report_2012-13.pdf", "annual_report", "FY2013"),
    ],
    "BBTC": [
        DocPlan(
            "Plantations/Bombay Burmah - Wadia group/bombay-burmah-annual-report2012.pdf",
            "annual_report", "FY2012",
        ),
        DocPlan(
            "Plantations/Bombay Burmah - Wadia group/bombay-burmah-annual-report2013.pdf",
            "annual_report", "FY2013",
        ),
    ],
    "BLUESTARCO": [
        DocPlan("Consumer Goods/Bluestar/2021-22/Q4-2022.pdf", "financial_result", "FY2022", "Q4"),
        DocPlan("Consumer Durables/Bluestar-HVAC/AR-2024-25.pdf", "annual_report", "FY2025"),
        DocPlan("Consumer Durables/Bluestar-HVAC/AR-2023-24.pdf", "annual_report", "FY2024"),
    ],
    "EIDPARRY": [
        # "InvestorMeetEID_ParryNov2012 (1).pdf" is a byte-identical duplicate
        # of the un-suffixed file (hash-checked) — filed once.
        DocPlan("Sugar/EID parry/InvestorMeetEID_ParryNov2012.pdf", "investor_presentation", "FY2013", "Q3"),
        DocPlan("Sugar/EID parry/InvestorMeetEIDParryJune2012.pdf", "investor_presentation", "FY2013", "Q1"),
    ],
    "EVERESTIND": [
        DocPlan(
            "Construction Materials/Everest Industries/2017-18/Q2FY18Earnings.pdf",
            "financial_result", "FY2018", "Q2",
        ),
        # "Schedule _ Presentation_27_mar_17.pdf" is a byte-identical
        # duplicate of this one (hash-checked, filename differs only by an
        # ampersand vs. underscore) — filed once.
        DocPlan(
            "Construction Materials/Everest Industries/2016-17/Schedule & Presentation_27_mar_17.pdf",
            "investor_presentation", "FY2017", "Q4",
        ),
        DocPlan("Construction Materials/Everest Industries/2016-17/AR2016.pdf", "annual_report", "FY2017"),
        DocPlan(
            "Construction Materials/Everest Industries/2015-16/Everest Annual Report 2015-16.pdf",
            "annual_report", "FY2016",
        ),
    ],
    "VOLTAS": [
        DocPlan("Consumer Durables/Voltas-HVAC/AR-2025-26.pdf", "annual_report", "FY2026"),
        DocPlan("Consumer Durables/Voltas-HVAC/AR-2023-24.pdf", "annual_report", "FY2024"),
    ],
    "DEEPAKFERT": [
        DocPlan(
            "Fertilizers/Deepak Fertilizers/DFPCL-Company-Presentation-Q4-FY13-IN-INR.pdf",
            "investor_presentation", "FY2013", "Q4",
        ),
        DocPlan(
            "Fertilizers/Deepak Fertilizers/DFPCL-CompanyPresentation-141113.pdf",
            "investor_presentation", "FY2014",
        ),
        DocPlan(
            "Fertilizers/Deepak Fertilizers/2014-15/Deepak Fertilisers Q1FY15 Concall Transcript.pdf",
            "transcript", "FY2015", "Q1",
        ),
    ],
    "LAOPALA": [
        DocPlan("Glass/La Opala RG/Annual-Report-2012-Final.pdf", "annual_report", "FY2012"),
        DocPlan("Glass/La Opala RG/AnnualReport2008-09Low.pdf", "annual_report", "FY2009"),
    ],
}

ADDED_BY = "proprietary-import:AnnualReports/tiny-sectors"


def _copy_xlsx(company_id: str, rel_path: str) -> Path:
    src = ARCHIVE_ROOT / rel_path
    dest_dir = RAW_DIR / company_id / "proprietary"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(rel_path).name
    if dest.exists():
        if dest.read_bytes() == src.read_bytes():
            return dest
        dest = dest_dir / f"{dest.stem}__{src.stat().st_mtime_ns}{dest.suffix}"
    shutil.copy2(src, dest)
    return dest


def _copy_pdf(company_id: str, rel_path: str) -> Path:
    src = ARCHIVE_ROOT / rel_path
    dest_dir = DOCUMENTS_DIR / company_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    dest = dest_dir / f"{stamp}__{Path(rel_path).name}"
    shutil.copy2(src, dest)
    return dest


def main() -> None:
    conn = init_db()

    print("=== XLSX (proprietary source) ===")
    for company_id, rel_paths in XLSX_PLAN.items():
        if get_company(conn, company_id) is None:
            print(f"{company_id:15s} SKIPPED — not registered")
            continue
        for rel_path in rel_paths:
            src = ARCHIVE_ROOT / rel_path
            if not src.exists():
                print(f"{company_id:15s} MISSING SOURCE  {rel_path}")
                continue
            dest = _copy_xlsx(company_id, rel_path)
            try:
                result = ingest_file(conn, dest, company_id=company_id, source_id="proprietary")
                print(
                    f"{company_id:15s} OK  parsed={result.parsed_count:3d} "
                    f"inserted={result.inserted_count:3d} reconciled={result.reconciled_count:3d}"
                    f"  <- {Path(rel_path).name}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{company_id:15s} FAILED  {type(exc).__name__}: {exc}  <- {Path(rel_path).name}")

    print("\n=== PDFs (documents table) ===")
    for company_id, docs in PDF_PLAN.items():
        if get_company(conn, company_id) is None:
            print(f"{company_id:15s} SKIPPED — not registered")
            continue
        for doc in docs:
            src = ARCHIVE_ROOT / doc.rel_path
            if not src.exists():
                print(f"{company_id:15s} MISSING SOURCE  {doc.rel_path}")
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
                f"{company_id:15s} OK  document_id={row['document_id']:4d} "
                f"{doc.document_type:22s} {doc.fiscal_year}{('/' + doc.quarter) if doc.quarter else '':5s}"
                f"  <- {Path(doc.rel_path).name}"
            )

    conn.close()


if __name__ == "__main__":
    main()
