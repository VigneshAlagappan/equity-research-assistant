"""One-off importer: files the Finance/ sector of the proprietary AnnualReports
archive (~/work/AnnualReports/Finance) into this repo's data/raw and
data/documents conventions, then runs the existing ingestion pipeline.

Scope: deliberately EXCLUDES the large/ambiguous subfolders flagged during
audit and left for later manual review — L&TFinance Holding (68 files),
MFIN (25 files — not a company, it's the Microfinance Institutions Network
industry body), SRG housing finance (23 files, mostly prospectus/third-party
research), Capital First and IDFC (pre-merger predecessor entities of IDFC
First Bank, several corporate hops back), HDFC (that's HDFC Ltd, a distinct
entity merged into HDFC Bank in 2023, not HDFC Bank's own document), and
IL&FS (mixed generic/academic content, ambiguous which subsidiary).

Every PDF filed here was letterhead/content-checked (pypdf) before being
assigned a company_id and fiscal_year/quarter — several filenames alone would
have misled: a "BajajFinservInvestorPresentation...pdf" sitting in the Bajaj
Finance folder is byte-identical to the real one in the Bajaj Finserv folder
(filed under Finserv only); a "Chola Finance" folder's own content confirms
Cholamandalam INVESTMENT AND FINANCE (CHOLAFIN), not the Holdings company;
an "Equity Analysis - Muthoot Finance.xlsx" sitting inside the "Indiabulls
Securities" folder is a byte-identical stray copy of the real one at
Finance/'s top level (filed once). Files with no reliable date and files
that turned out to be SEBI takeover/open-offer filings, prospectuses,
third-party analyst notes, IPO-info pages, or plain unreadable/undated scans
were left out rather than guessed.

DHFL, Poonawalla Fincorp, and SBFC Finance each have a real registry
oddity: DHFL is marked 'active' in the registry despite being defunct/
delisted in the real world post-2021 insolvency (left as-is, not this
script's job to fix); Poonawalla Fincorp and SBFC Finance each have TWO
active company_ids for the same real company (confirmed by the user) —
this script routes to whichever one already carries real ingested data
(POONAWALLAFIN, SBFCFINANCE), not the newer/emptier duplicate.

Idempotent-ish in the same sense as import_banks_sector.py: an xlsx already
present at its destination path (byte-identical) is skipped; PDFs are not
deduped against existing documents rows, so re-running duplicates them.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import RAW_DIR, DOCUMENTS_DIR, to_repo_relative
from companies.registry import get_company
from ingestion.pipeline import ingest_file
from storage.database import init_db
from storage.repositories import save_company_document

ARCHIVE_ROOT = Path(os.environ.get("EQUITY_RESEARCH_ARCHIVE_ROOT", Path.home() / "work" / "AnnualReports")) / "Finance"

# ------------------------------------------------------------------
# 1. Proprietary xlsx workbooks -> data/raw/<company_id>/proprietary/
# ------------------------------------------------------------------

XLSX_PLAN: dict[str, list[str]] = {
    "CANFINHOME": ["Equity Analysis - CAN fin Homes.xlsx"],
    "CAPTRUST": ["Equity Analysis - Capital Trust.xlsx"],
    "DHFL": ["Equity Analysis - DHFL.xlsx"],
    "EDELWEISS": ["Equity Analysis - Edelweiss.xlsx"],
    "INDOSTAR": ["Equity Analysis - IndoStar Capital.xlsx"],
    "JMFINANCIL": ["Equity Analysis - JM Financial.xlsx"],
    # 6 genuinely distinct revisions (hash-checked, no two identical).
    "LICHSGFIN": [
        "Equity Analysis - LIC Housing Finance.xlsx",
        "0. old/Equity Analysis - LIC HFL.xlsx",
        "0. old/Equity Analysis - LIC Home Finance Ltd - 2014.xlsx",
        "0. old/Equity Analysis - LIC Home Finance Ltd - Dec.xlsx",
        "0. old/Equity Analysis - LIC Home Finance Ltd.xlsx",
        "0. old/Equity Analysis - LIC Housing Finance.xlsx",
    ],
    "MASFIN": ["Equity Analysis - MAS Financial Services.xlsx"],
    # The copy at Indiabulls Securities/Equity Analysis - Muthoot Finance.xlsx
    # is byte-identical to this one (hash-checked) — a stray misfile, not a
    # second revision. Not filed twice.
    "MUTHOOTFIN": ["Equity Analysis - Muthoot Finance.xlsx"],
    "PNBHOUSING": [
        "Equity Analysis - PNB housing finance.xlsx",
        "0. old/Equity Analysis - PNB housing finance - old.xlsx",
    ],
    "REPCOHOME": ["Equity Analysis - Repco Finance.xlsx"],
    "SBFCFINANCE": ["Equity Analysis - SBFC.xlsx"],
    # Both predecessors (Shriram Transport Finance, Shriram City Union
    # Finance) are archived with successor_company_id=SHRIRAMFIN — same
    # pattern as IDFC Bank -> IDFC First Bank in the Banks import. The
    # top-level and 0.old copies of "Shriram Transport Finance.xlsx" are
    # byte-identical (hash-checked); only one is filed, plus the genuinely
    # distinct -2014 revision.
    "SHRIRAMFIN": [
        "Equity Analysis - Shriram City Union Finance-old.xlsx",
        "Equity Analysis - Shriram City Union Finance.xlsx",
        "Equity Analysis - Shriram Transport Finance.xlsx",
        "0. old/Equity Analysis - Shriram Transport Finance-2014.xlsx",
    ],
    # Root copy and Tata Investment/'s own copy are byte-identical
    # (hash-checked) — filed once. "Calc.xlsx" in that same subfolder is a
    # generic worksheet, not an Equity Analysis workbook — not filed.
    "TATAINVEST": ["Equity Analysis - Tata Investment.xlsx"],
}

# ------------------------------------------------------------------
# 2. PDFs -> data/documents/<company_id>/, one documents row each.
# ------------------------------------------------------------------


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


PDF_PLAN: dict[str, list[DocPlan]] = {
    "AAVAS": [
        DocPlan("Aavas Financiers/2014-15/annual-report-2014-15.pdf", "annual_report", "FY2015"),
    ],
    "BAJAJHLDNG": [
        DocPlan(
            "Baja Holdings & Investments/Press-Release-Q3-12-13-BHIL.pdf",
            "financial_result", "FY2013", "Q3",
        ),
        DocPlan("Baja Holdings & Investments/BHIL-2012.pdf", "annual_report", "FY2012"),
    ],
    "BAJFINANCE": [
        DocPlan("Bajaj Finance/investor-presentation-Q2-2013-14.pdf", "investor_presentation", "FY2014", "Q2"),
    ],
    "BAJAJFINSV": [
        DocPlan(
            "Bajaj Finserv/BajajFinservInvestorPresentation-Q3FY2012-13.pdf",
            "investor_presentation", "FY2013", "Q3",
        ),
        DocPlan(
            "Bajaj Finserv/Bajaj_Finserv_Investor_Presentation_Q2_FY2013-14.pdf",
            "investor_presentation", "FY2014", "Q2",
        ),
    ],
    "CAMS": [
        DocPlan("CAMS/CAMS AR 23-24.pdf", "annual_report", "FY2024"),
        DocPlan("CAMS/CAMSAnnualReport_2019_2020.pdf", "annual_report", "FY2020"),
        DocPlan("CAMS/Q4-2025-Investor.pdf", "investor_presentation", "FY2025", "Q4"),
        DocPlan("CAMS/Q4 FY 25 Earnings Conference Call.pdf", "transcript", "FY2025", "Q4"),
    ],
    "CANFINHOME": [
        DocPlan(
            "CAN fin homes/180629191038_Q4-Investor-Presentation.pdf",
            "investor_presentation", "FY2018", "Q4",
        ),
        DocPlan("CAN fin homes/2015-16/Q4.pdf", "investor_presentation", "FY2016", "Q4"),
    ],
    "CDSL": [
        DocPlan("CDSL/CDSL Annual Report-2017-2018.pdf", "annual_report", "FY2018"),
        DocPlan("CDSL/CDSL Annual Report2013-14.pdf", "annual_report", "FY2014"),
        DocPlan("CDSL/CDSL-22 January -2018.pdf", "transcript", "FY2018", "Q3"),
    ],
    "CHOLAFIN": [
        DocPlan("Chola Finance/IP_Mar14.pdf", "investor_presentation", "FY2014", "Q4"),
        DocPlan(
            "Chola Finance/2019-20/Chola-Investor-presentation-Sep-2019.pdf",
            "investor_presentation", "FY2020", "Q2",
        ),
        DocPlan(
            "Chola Finance/2019-20/Investor-Presentation-June-2019.pdf",
            "investor_presentation", "FY2020", "Q1",
        ),
        DocPlan("Chola Finance/2019-20/Q2 - concal transcripts.pdf", "transcript", "FY2020", "Q2"),
    ],
    "CREDITACC": [
        DocPlan("CreditAccess Grameen/Investor-Presentation-Q2FY19.pdf", "investor_presentation", "FY2019", "Q2"),
    ],
    "DHFL": [
        DocPlan("DHFL/2016-17/DHFL-Annual-Report-FY-2016-17.pdf", "annual_report", "FY2017"),
        DocPlan(
            "DHFL/2017-18/corporate-presentation-q1-fy-2018-2019.pdf",
            "investor_presentation", "FY2019", "Q1",
        ),
        DocPlan(
            "DHFL/2018-19/corporate_presentation_q2_fy_2018_2019_1274.pdf",
            "investor_presentation", "FY2019", "Q2",
        ),
    ],
    "EDELWEISS": [
        DocPlan("Edelweiss/2019-20/ConCall Q4 and FY19-20.pdf", "transcript", "FY2020", "Q4"),
        DocPlan("Edelweiss/2019-20/Q4-Addendum.pdf", "financial_result", "FY2020", "Q4"),
        DocPlan("Edelweiss/2019-20/Earnings Update Q3FY20.pdf", "financial_result", "FY2020", "Q3"),
    ],
    "GEOJITFSL": [
        DocPlan("Geojit/2017-18/Shareholder_Presentation_FY_2017-18.pdf", "investor_presentation", "FY2018"),
    ],
    "INDOSTAR": [
        DocPlan(
            "Indostar Capital Finance/2019-20/Indostar-Nov08-2019 Conference call transcript.pdf",
            "transcript", "FY2020", "Q2",
        ),
        DocPlan(
            "Indostar Capital Finance/2018-19/Indostar Q4 FY19- Earning Call Transcript.pdf",
            "transcript", "FY2019", "Q4",
        ),
        DocPlan(
            "Indostar Capital Finance/2018-19/IndoStar Capital - conference call transcript 30may18.pdf",
            "transcript", "FY2019", "Q1",
        ),
    ],
    "JMFINANCIL": [
        DocPlan("JM Financial/2017-18/JMFL_ARC_Financial_Results_Mar_2018.pdf", "financial_result", "FY2018", "Q4"),
        DocPlan("JM Financial/2017-18/JMFL_Presentation_Q4_2017-18.pdf", "investor_presentation", "FY2018", "Q4"),
        DocPlan("JM Financial/2002-03/AnnualRep.pdf", "annual_report", "FY2003"),
        DocPlan("JM Financial/2004-05/AnnualRep330317.pdf", "annual_report", "FY2005"),
    ],
    "KFINTECH": [
        DocPlan("KFin/Annual-Report_2017-18.pdf", "annual_report", "FY2018"),
        # The "Interactive-Version" is the same report with navigation/forms
        # added — not filed a second time.
        DocPlan(
            "KFin/KFintech_Annual-Report-2023-24_Non-Interactive-Version.pdf",
            "annual_report", "FY2024",
        ),
    ],
    "KEYFINSERV": [
        DocPlan(
            "Keynote Corporate Services/KeynoteCorporateServicesLtd_AnnualReport_2013_2014.pdf",
            "annual_report", "FY2014",
        ),
    ],
    "LICHSGFIN": [
        DocPlan("LIC House Finance Ltd/2019-20/Q2FY20_LICHF_Transcript.pdf", "transcript", "FY2020", "Q2"),
        DocPlan("LIC House Finance Ltd/2019-20/Q1FY20_LICHF_Transcript.pdf", "transcript", "FY2020", "Q1"),
    ],
    "M&MFIN": [
        DocPlan("M&M Finance/MMFSL_Corporate_Presentation_14.pdf", "investor_presentation", "FY2014", "Q4"),
    ],
    "MASFIN": [
        DocPlan("MAS Financial Services/2018-19/INVESTOR-PRESENTATION-Q4-FY19.pdf", "investor_presentation", "FY2019", "Q4"),
        DocPlan("MAS Financial Services/2018-19/INVESTOR-PRESENTATION-Q1-FY1915102018.pdf", "investor_presentation", "FY2019", "Q1"),
        DocPlan("MAS Financial Services/2018-19/INVESTOR-PRESENTATION-Q2FY19-01112018.pdf", "investor_presentation", "FY2019", "Q2"),
        DocPlan("MAS Financial Services/2018-19/INVESTOR-PRESENTATION-Q3FY19-30012019.pdf", "investor_presentation", "FY2019", "Q3"),
        DocPlan("MAS Financial Services/2018-19/INVESTOR-PRESENTATION-08-10-2018.pdf", "investor_presentation", "FY2019"),
    ],
    "MANAPPURAM": [
        DocPlan("Manappuram Finance/2018-19/Q4 FY19 Results Update.pdf", "financial_result", "FY2019", "Q4"),
    ],
    "MOTILALOFS": [
        DocPlan("Motilal Oswal/AR 2022-23.pdf", "annual_report", "FY2023"),
        DocPlan("Motilal Oswal/AR-2023-24.pdf", "annual_report", "FY2024"),
        DocPlan("Motilal Oswal/AR 2021-22.pdf", "annual_report", "FY2022"),
    ],
    "MUTHOOTCAP": [
        DocPlan("Muthoot Capital/Annual_Report_2010-2011.pdf", "annual_report", "FY2011"),
        DocPlan("Muthoot Capital/Annual_Report_2011-2012.pdf", "annual_report", "FY2012"),
    ],
    "MUTHOOTFIN": [
        DocPlan("Muthoot Finance/FY2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("Muthoot Finance/1466144787DEC 2015.pdf", "investor_presentation", "FY2016", "Q3"),
        DocPlan("Muthoot Finance/Q2-FY2013.pdf", "financial_result", "FY2013", "Q2"),
        DocPlan("Muthoot Finance/2018-19/MFIN Q4 FY19 investor presentation.pdf", "investor_presentation", "FY2019", "Q4"),
    ],
    "NAHARCAP": [
        # "NCFSL" = Nahar Capital and Financial Services Ltd.
        DocPlan("Nahar Group/NCFSL Annual Report 2011.pdf", "annual_report", "FY2011"),
    ],
    "PNBHOUSING": [
        DocPlan("PNB Housing/2017-18/PNBHFL-Eranings-Call-Transcript-04.08.2017.pdf", "transcript", "FY2018", "Q1"),
        DocPlan(
            "PNB Housing/2017-18/PNB-Housing-Finance-Transcript-for-Q2-FY18-Earnings-Call_25Oct2017.pdf",
            "transcript", "FY2018", "Q2",
        ),
        DocPlan(
            "PNB Housing/2017-18/PNB-Housing-Finance-Q3-FY17-18-earnings-call-transcript_24Jan18.pdf",
            "transcript", "FY2018", "Q3",
        ),
        DocPlan(
            "PNB Housing/2019-20/PNB-Housing-Finance-Earnings-Call-Transcript_30July2019.pdf",
            "transcript", "FY2020", "Q1",
        ),
    ],
    # Routed to the duplicate that already carries real data (user-confirmed
    # POONAWALLAFIN == POONAWALLA is the same company).
    "POONAWALLAFIN": [
        DocPlan(
            "Poonawalla FinCorp/PFL-Q1FY25-Earnings-Call-Transcript-26072024.pdf",
            "transcript", "FY2025", "Q1",
        ),
    ],
    "REPCOHOME": [
        DocPlan(
            "Repco Home Finance/2017-18/Repco Home discusses Q2 FY18 earnings - Earnings Call Transcript.pdf",
            "transcript", "FY2018", "Q2",
        ),
    ],
    # Routed to the duplicate that already carries real data (user-confirmed
    # SBFC == SBFCFINANCE is the same company). "(1).pdf" is a byte-identical
    # duplicate of the investor presentation (hash-checked) — filed once.
    "SBFCFINANCE": [
        DocPlan("SBFC/SBFC AR 2025-26.pdf", "annual_report", "FY2026"),
        DocPlan("SBFC/SBFC AR 2024-25.pdf", "annual_report", "FY2025"),
        DocPlan("SBFC/SBFC AR 2023-24.pdf", "annual_report", "FY2024"),
        DocPlan("SBFC/SBFC AR 2022-23.pdf", "annual_report", "FY2023"),
        DocPlan("SBFC/SBFC AR 2021-22.pdf", "annual_report", "FY2022"),
        DocPlan("SBFC/SBFC AR 2020-21.pdf", "annual_report", "FY2021"),
        DocPlan("SBFC/SBFCInvestorPresentationMarch2025.pdf", "investor_presentation", "FY2025"),
    ],
    "SBICARD": [
        DocPlan("SBI Cards/AR-2024-25.pdf", "annual_report", "FY2025"),
        DocPlan("SBI Cards/Q1-2026.pdf", "financial_result", "FY2026", "Q1"),
        DocPlan("SBI Cards/AR-2023-24.pdf", "annual_report", "FY2024"),
    ],
    "SUMMITSEC": [
        DocPlan("Summit Securities/Summit Annual Report 2011-12.pdf", "annual_report", "FY2012"),
    ],
}

ADDED_BY = "proprietary-import:AnnualReports/Finance"


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
            except Exception as exc:  # noqa: BLE001
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
                raw_file_path=to_repo_relative(dest),
            )
            print(
                f"{company_id:20s} OK  document_id={row['document_id']:4d} "
                f"{doc.document_type:22s} {doc.fiscal_year}{('/' + doc.quarter) if doc.quarter else '':5s}"
                f"  <- {Path(doc.rel_path).name}"
            )

    conn.close()


if __name__ == "__main__":
    main()
