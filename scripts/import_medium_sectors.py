"""One-off importer: files the 16 medium-sized sector folders (11-30 files
each, ~298 files total) from the proprietary AnnualReports archive — Agri,
Auto OEM, Auto Suppliers, Ceramics Tiles Granite Quartz, Chemicals, Education,
Energy, Engines, FMCG, Food processing, Infra, Jewellery, Leisure, Minerals
Natural Resources, Pharma, Telecom — plus Domestic Appliance and Lubricants
(7 files each), two small sectors that fell just under the "tiny" cutoff
used for import_tiny_sectors.py and were folded in here instead.

Same discipline as the Banks/Finance/tiny-sector importers: byte-identical
duplicates deduped by hash (found extensively here — Pidilite alone had 7
xlsx candidates, none identical; Delta Corp had 6, all genuinely distinct;
by contrast BHEL's "- old.xlsx" was the same file copied into 3 different
folders). Recurring pattern discovered in this batch: several
"<Company>_Ltd_DDMMYY.pdf" files (Hero MotoCorp, NMDC, Coal India, Cummins,
Pidilite — all with page-1 text that fails to extract) turned out to be a
consistent family of blank/unreadable short scanned documents wherever they
appear — none filed.

Not filed, and why (grouped, not exhaustive — see inline comments for
specific per-file calls):
- No active registry match: Agro Tech Foods, Kwality (ambiguous which of 2
  same-named registry entries, neither convincingly matches "Food
  processing" dairy business), Bharti Infratel (merged into Indus Towers),
  Nu Tek India, Sesa Goa / Cairn India (both merged into Vedanta Ltd,
  2013/2017), Selan Exploration, HSIL (demerged into Hindware/Somany Home
  Innovation 2019, pre-demerger docs ambiguous which successor), Honda Siel,
  Shriram EPC, Lykis, Inox Leisure (PVR/Inox merged 2023 into PVRINOX, but
  its "Equity Analysis - Innox.xlsx" is financial data — not redirected to
  the merged entity without the same kind of explicit confirmation the user
  gave for Equitas/Ujjivan), Cinemax, Sterling Holidays, CKP Leisure,
  Educomp, Core Education, TCP, Omkar Speciality, INEOS Styrolution (a real
  listed company but genuinely absent from this app's registry snapshot).
- Third-party/generic content: broker notes (AnandRathi, IDirect, HDFC
  Securities, CRISIL, Edelweiss-style), industry overviews (most of Pharma's
  top-level files, "1. Industry" folders throughout), forum/magazine
  articles, a lawyer profile piece misfiled in Delta Corp's folder.
- Wrong type for the documents.document_type enum: prospectuses/DRHPs/IPO
  notes, shareholding patterns, SEBI orders, postal ballots, merger scheme
  docs, meeting notices/AGM proceedings, a share allotment notice, a demerger
  scheme document, a "who to avoid" internal readme with no company content.
- Unsupported file formats for this pipeline: .docx, .pptx, .doc, .lnk —
  only PDF (documents) and .xlsx (proprietary financials) are handled.
- No reliably-confirmable fiscal year: several presentations/results named
  only "Q4.pdf", "IZ_Q2Results.pdf", or similar with no date anywhere in the
  filename, folder, or extractable page-1 text.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import RAW_DIR, DOCUMENTS_DIR, to_repo_relative
from companies.registry import get_company
from ingestion.pipeline import ingest_file
from storage.database import init_db
from storage.repositories import save_company_document

ARCHIVE_ROOT = Path("/Users/radhamurugesan/work/AnnualReports")

XLSX_PLAN: dict[str, list[str]] = {
    "RALLIS": ["Agri/Equity Analysis - Rallis India.xlsx"],
    "BAJAJAUTO": [
        "Auto OEM/Equity Analysis - Bajaj Auto - 2014.xlsx",
        "Auto OEM/Equity Analysis - Bajaj Auto.xlsx",
    ],
    "HEROMOTOCO": ["Auto OEM/Equity Analysis - Hero Motor Corp - 2014.xlsx"],
    "LTFOODS": ["Food processing/Equity Analysis - LT Foods.xlsx"],
    "KOHINOOR": ["Food processing/Equity Analysis - Kohinoor Foods.xlsx"],
    "KRBL": [
        "Food processing/Equity Analysis - KRBL.xlsx",
        "Food processing/0.old/Equity Analysis - KRBL - 2016.xlsx",
    ],
    "TITAN": [
        "Jewellery/Equity Analysis - Titan Ind-2014.xlsx",
        "Jewellery/Equity Analysis - Titan Ind.xlsx",
    ],
    "PCJEWELLER": ["Jewellery/Equity Analysis - PC Jewellers.xlsx"],
    "RAJESHEXPO": ["Jewellery/Equity Analysis - Rajesh Exports.xlsx"],
    # -old.xlsx exists twice under different folder names with DIFFERENT
    # content each time (hash-checked) — both are genuine distinct revisions.
    "ARE&M": [
        "Auto Suppliers/Equity Analysis - Amara Raja Battries.xlsx",
        "Auto Suppliers/old/Equity Analysis - Amara Raja Battries-old.xlsx",
        "Auto Suppliers/0. old/Equity Analysis - Amara Raja Battries-old.xlsx",
        "Auto Suppliers/0. old/Equity Analysis - Amara Raja Battries-2018.xlsx",
    ],
    "EXIDEIND": [
        "Auto Suppliers/Equity Analysis - Exide Industries.xlsx",
        "Auto Suppliers/old/Equity Analysis - Exide Industries-old.xlsx",
        "Auto Suppliers/0. old/Equity Analysis - Exide Industries-2016.xlsx",
    ],
    # "Airtel-2014.xlsx" at the top level and inside "0. Old/" are DIFFERENT
    # files (hash-checked) despite the identical name — both filed.
    "BHARTIARTL": [
        "Telecom/Equity Analysis - Airtel-2014-new.xlsx",
        "Telecom/Equity Analysis - Airtel-2014.xlsx",
        "Telecom/0. Old/Equity Analysis - Airtel-2014.xlsx",
    ],
    "IDEA": [
        "Telecom/Equity Analysis - Idea.xlsx",
        "Telecom/Equity Analysis - Idea-2014.xlsx",
    ],
    "NOVARTIND": ["Pharma/Equity Analysis - Novartis.xlsx"],
    "CUMMINSIND": [
        "Engines/Equity Analysis - Cummins - 2014.xlsx",
        "Engines/Equity Analysis - Cummins.xlsx",
    ],
    "KIRLOSENG": ["Engines/Equity Analysis - Kirloskar Oil Engines.xlsx"],
    "KIRLOSIND": ["Engines/Equity Analysis - Kirloskar Industries - NBFC now.xlsx"],
    "COALINDIA": [
        "Minerals Natural Resources/Equity Analysis - Coal India-2014.xlsx",
        "Minerals Natural Resources/Equity Analysis - Coal India-2016.xlsx",
        "Minerals Natural Resources/0. Old/Equity Analysis - Coal India.xlsx",
    ],
    # "NMDC - May2013.xlsx" at top level and in "0. Old/" are identical
    # (hash-checked) — filed once.
    "NMDC": [
        "Minerals Natural Resources/Equity Analysis - NMDC.xlsx",
        "Minerals Natural Resources/Equity Analysis - NMDC - May2013.xlsx",
        "Minerals Natural Resources/Equity Analysis - NMDC-2014.xlsx",
    ],
    # "L_T(1).xlsx" and "L_T.xlsx" are identical (hash-checked, filed once);
    # "L_T - Dec.xlsx" and "L_T - Dec(1).xlsx" in 0. old/ are also identical
    # to each other (filed once).
    "LT": [
        "Infra/Equity Analysis - L_T.xlsx",
        "Infra/0. old/Equity Analysis - L_T - Dec.xlsx",
    ],
    # BHEL's "- old.xlsx" is the SAME file copied into 3 folders (root,
    # old/, 0. old/ — hash-checked, filed once); "20Feb" and "3Jan" are
    # genuinely distinct revisions from the current one.
    "BHEL": [
        "Infra/Equity Analysis - BHEL.xlsx",
        "Infra/Equity Analysis - BHEL - old.xlsx",
        "Infra/0. old/Equity Analysis - BHEL - 20Feb.xlsx",
        "Infra/0. old/Equity Analysis - BHEL - 3Jan.xlsx",
    ],
    "CERA": ["Ceramics Tiles Granite Quartz/Equity Analysis - Cera Sanitary.xlsx"],
    "SOMANYCERA": ["Ceramics Tiles Granite Quartz/Equity Analysis - Somany Ceramics.xlsx"],
    "POKARNA": ["Ceramics Tiles Granite Quartz/Equity Analysis - Pokarna.xlsx"],
    "KAJARIACER": ["Ceramics Tiles Granite Quartz/Equity Analysis - Kajaria Ceramics.xlsx"],
    "KANSAINER": ["Chemicals/Equity Analysis - Nerolac.xlsx"],
    # "Asian Paints.xlsx" at top level and in "0. old/" are identical
    # (hash-checked, filed once).
    "ASIANPAINT": [
        "Chemicals/Equity Analysis - Asian Paints-2014.xlsx",
        "Chemicals/Equity Analysis - Asian Paints.xlsx",
    ],
    "BERGEPAINT": [
        "Chemicals/Equity Analysis - Berger Paints.xlsx",
        "Chemicals/0. old/Equity Analysis - Berger Paints-old.xlsx",
    ],
    # All 7 candidates hash-checked — every one is genuinely distinct, none
    # are duplicates despite the very similar names/dates.
    "PIDILITIND": [
        "Chemicals/Equity Analysis - Pidilite Ind.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind - 3Jan.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind - 3Dec.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind-2014-new.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind-2014.xlsx",
        "Chemicals/0. old/Equity Analysis - Pidilite Ind - 2 Feb.xlsx",
    ],
    "DEEPINDS": ["Energy/Equity Analysis - Deep Industries.xlsx"],
    "ABAN": ["Energy/Equity Analysis - Aban Offshore.xlsx"],
    "ONGC": ["Energy/Equity Analysis - ONGC.xlsx"],
    "HINDOILEXP": ["Energy/Equity Analysis - HOEC.xlsx"],
    "OIL": ["Energy/Equity Analysis - Oil India.xlsx"],
    # "PVR.xlsx" at top level and in "0. old/" are DIFFERENT (hash-checked)
    # — both filed.
    "PVRINOX": [
        "Leisure/Equity Analysis - PVR.xlsx",
        "Leisure/0. old/Equity Analysis - PVR.xlsx",
    ],
    # All 6 candidates hash-checked — every one is genuinely distinct.
    "DELTACORP": [
        "Leisure/Equity Analysis - Delta Corp.xlsx",
        "Leisure/Delta/Equity Analysis - Delta.xlsx",
        "Leisure/0. old/Equity Analysis - Delta Corp.xlsx",
        "Leisure/0. old/Equity Analysis - Delta Corp-2014.xlsx",
        "Leisure/0. old/Equity Analysis - Delta Corp-old.xlsx",
        "Leisure/0. old/Equity Analysis - Delta Corp Nov 2013.xlsx",
    ],
    "THOMASCOOK": [
        "Leisure/Equity Analysis - Thomas Cook.xlsx",
        "Leisure/0. old/Equity Analysis - Thomas Cook-2018.xlsx",
    ],
    "HAWKINCOOK": ["Domestic Appliance/Equity Analysis - Hawkins cooker.xlsx"],
    "TTKPRESTIG": ["Domestic Appliance/Equity Analysis - TTK Prestige.xlsx"],
    "CASTROLIND": [
        "Lubricants/Equity Analysis - Castrol-2013.xlsx",
        "Lubricants/Equity Analysis - Castrol.xlsx",
        "Lubricants/Equity Analysis - Castrol-2014.xlsx",
    ],
}


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


PDF_PLAN: dict[str, list[DocPlan]] = {
    "MARUTI": [
        DocPlan("Auto OEM/Maruti/c635c1bf-fe34-40f0-baf4-0cf68e392fd9.pdf", "financial_result", "FY2024", "Q2"),
        DocPlan("Auto OEM/Maruti/2023-24/a1a98a12-592a-4d04-9a62-8448987b030c.pdf", "financial_result", "FY2024", "Q1"),
        DocPlan("Auto OEM/Maruti/2022-23/q4.pdf", "financial_result", "FY2023", "Q4"),
        DocPlan(
            "Auto OEM/Maruti/2022-23/Transcript_earnings_call-Maruti_Suzuki_Q4_FY23_New.pdf",
            "transcript", "FY2023", "Q4",
        ),
    ],
    "ATHERENERG": [
        DocPlan("Auto OEM/Ather Energy/Q1-2026.pdf", "financial_result", "FY2026", "Q1"),
    ],
    "LTFOODS": [
        DocPlan("Food processing/LT Foods/2011-12/Annual Report_2011-12.pdf", "annual_report", "FY2012"),
    ],
    "PCJEWELLER": [
        DocPlan("Jewellery/PC Jewellers/2018-19/Conference-Call-10-08-2018.pdf", "transcript", "FY2019", "Q2"),
    ],
    "ARE&M": [
        DocPlan("Auto Suppliers/Amararaja Batteries/2015-16/Annual Report 2016.pdf", "annual_report", "FY2016"),
        DocPlan(
            "Auto Suppliers/Amararaja Batteries/2015-16/ARBL -Investor call transcript - Q2 FY 2015-16.pdf",
            "transcript", "FY2016", "Q2",
        ),
    ],
    "SUNDRMFAST": [
        DocPlan("Auto Suppliers/Sundaram fasterners/Financial Results 2013-14.pdf", "financial_result", "FY2014"),
    ],
    "BHARTIARTL": [
        DocPlan(
            "Telecom/Airtel/Bharti-Management-Presentation-vfc-Aug-2014.pdf",
            "investor_presentation", "FY2015", "Q2",
        ),
        DocPlan(
            "Telecom/Airtel/Bharti-Airtel-Full-Annual-Report-2012-13_for-Web-new.pdf",
            "annual_report", "FY2013",
        ),
        DocPlan("Telecom/Airtel/AR-13-14.pdf", "annual_report", "FY2014"),
        DocPlan("Telecom/Airtel/Bharti-Airtel-Annual-Report-2012.pdf", "annual_report", "FY2012"),
        DocPlan("Telecom/Airtel/IR-PPT-May-12.pdf", "investor_presentation", "FY2012", "Q4"),
        DocPlan(
            "Telecom/Airtel/Bharti_Airtel_annual_report_full_2010-2011.pdf",
            "annual_report", "FY2011",
        ),
    ],
    "CAPLIPOINT": [
        DocPlan("Pharma/Caplin Point/2020-21/AR-2021.pdf", "annual_report", "FY2021"),
    ],
    "KIRLOSIND": [
        DocPlan("Engines/KIL Annual Report - 2012 - 2013.pdf", "annual_report", "FY2013"),
    ],
    "CUMMINSIND": [
        DocPlan("Engines/Cummins/Press Release CIL Q4 2013-14 ajt.pdf", "financial_result", "FY2014", "Q4"),
        DocPlan("Engines/Cummins/Transcript - May 23, 2014.pdf", "transcript", "FY2015", "Q1"),
    ],
    "KIRLOSENG": [
        DocPlan("Engines/Kirloskar Oil engines/KOEL_Investor_Presentation_Dec_2013.pdf", "investor_presentation", "FY2014", "Q3"),
    ],
    "COALINDIA": [
        DocPlan("Minerals Natural Resources/Coal India/Coal_India_AR_2011_-_2012_17082012.pdf", "annual_report", "FY2012"),
    ],
    "HINDALCO": [
        DocPlan("Minerals Natural Resources/Hindalco/Hindalco_Q1FY13-14_presentation.pdf", "investor_presentation", "FY2014", "Q1"),
        DocPlan("Minerals Natural Resources/Hindalco/Hindalco_Annual_Report_2012-13.pdf", "annual_report", "FY2013"),
    ],
    "LT": [
        DocPlan("Infra/L_T/2009-10/L_TAnnualReport2009-10Fullfile.pdf", "annual_report", "FY2010"),
        DocPlan("Infra/L_T/2013-14/Analyst__Presentation-_H1_FY14(1).pdf", "investor_presentation", "FY2014", "Q2"),
    ],
    "BHEL": [
        DocPlan("Infra/BHEL/2011-12/BHEL_Q3FY13_ConCall_Transcript_010213.pdf", "transcript", "FY2013", "Q3"),
    ],
    "THERMAX": [
        DocPlan("Infra/Thermax/Annual-Report-2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("Infra/Thermax/Annual-Report-2012-13.pdf", "annual_report", "FY2013"),
    ],
    "CROMPTON": [
        DocPlan("Infra/Crompton Greeves/AR1213.pdf", "annual_report", "FY2013"),
        DocPlan("Infra/Crompton Greeves/Analyst-Investor-PresentationMay13.pdf", "investor_presentation", "FY2013", "Q4"),
    ],
    "ENGINERSIN": [
        DocPlan("Infra/EIL/67_Download_Annual Report 12 - 13.pdf", "annual_report", "FY2013"),
        DocPlan("Infra/EIL/58_Download_Annual Result 12.pdf", "financial_result", "FY2012"),
    ],
    "EMAMILTD": [
        DocPlan("FMCG/Emami/Oct2013 - earnings call.pdf", "transcript", "FY2014", "Q2"),
        DocPlan("FMCG/Emami/May2013 - earnings call.pdf", "transcript", "FY2014", "Q1"),
    ],
    "COLPAL": [
        DocPlan("FMCG/Colgate/AR-2022-23.pdf", "annual_report", "FY2023"),
    ],
    "BRITANNIA": [
        DocPlan("FMCG/Britannia/BRITANNIA_31mar2013_B.pdf", "financial_result", "FY2013", "Q4"),
        DocPlan("FMCG/Britannia/brit_071112.pdf", "financial_result", "FY2013", "Q2"),
        DocPlan("FMCG/Britannia/BILQ1_2012_13Results.pdf", "financial_result", "FY2013", "Q1"),
    ],
    "DABUR": [
        DocPlan("FMCG/Dabur/2014-15/DIL-Transcript-Inv-Conf-Call-July14.pdf", "transcript", "FY2015", "Q1"),
        DocPlan("FMCG/Dabur/2013-14/DIL-AR-2013-14.pdf", "annual_report", "FY2014"),
    ],
    "JYOTHYLAB": [
        DocPlan("FMCG/Jyothi Labs/2010-11/Annual Report 2011.pdf", "annual_report", "FY2011"),
    ],
    "POKARNA": [
        DocPlan("Ceramics Tiles Granite Quartz/Pokarna/2016-17/Q4_FY16-17_Presentation.pdf", "investor_presentation", "FY2017", "Q4"),
    ],
    "KAJARIACER": [
        DocPlan("Ceramics Tiles Granite Quartz/Kajaria/KAJARIACER_31mar2014_B.pdf", "financial_result", "FY2014", "Q4"),
    ],
    "KANSAINER": [
        DocPlan("Chemicals/Nerolac/Kansai_Nerolac_AR_2012_13.pdf", "annual_report", "FY2013"),
    ],
    "ASIANPAINT": [
        DocPlan("Chemicals/Asian Paints/Q3FY14 results conf call.pdf", "transcript", "FY2014", "Q3"),
    ],
    "PIDILITIND": [
        DocPlan(
            "Chemicals/Pidilite/latest-transcripts-investor-presentation_May29_2014.pdf",
            "investor_presentation", "FY2015", "Q1",
        ),
        DocPlan(
            "Chemicals/Pidilite/latest-transcripts-investor-presentation_Oct30_2013.pdf",
            "investor_presentation", "FY2014", "Q3",
        ),
    ],
    "OIL": [
        DocPlan("Energy/Oil India Ltd/2023/AR-2023.pdf", "annual_report", "FY2023"),
    ],
    "PVRINOX": [
        DocPlan("Leisure/PVR/PVRCinemas_Annual_Report_2013-14.pdf", "annual_report", "FY2014"),
    ],
    "DELTACORP": [
        DocPlan("Leisure/Delta/2013/DCL-Final-Results-2013.pdf", "financial_result", "FY2013"),
    ],
    "WONDERLA": [
        DocPlan("Leisure/Wonderla/2017-18/CCQ4FY18_rcvabb.pdf", "transcript", "FY2018", "Q4"),
    ],
    "MTEDUCARE": [
        DocPlan(
            "Education/MT Educare/InvestorPresentation-QuarterlyUpdate_Q1FY_14-15.pdf",
            "investor_presentation", "FY2015", "Q1",
        ),
    ],
    # Root-level copies of these same 3 files are byte-identical duplicates
    # (hash-checked) — only the subfolder copies are filed.
    "CPEDU": [
        DocPlan(
            "Education/Career Point/Career Point_Investor Presentation_FY12.pdf",
            "investor_presentation", "FY2012",
        ),
        DocPlan("Education/Career Point/cpil_annual_report_2010_11.pdf", "annual_report", "FY2011"),
        DocPlan("Education/Career Point/corporate_presentation_2011.pdf", "investor_presentation", "FY2011"),
    ],
    "TTKPRESTIG": [
        DocPlan("Domestic Appliance/TTK/TTK_Annual_Report_-_2003-04.pdf", "annual_report", "FY2004"),
        DocPlan("Domestic Appliance/TTK/TTK_Annual_Report_-_2013_14.pdf", "annual_report", "FY2014"),
    ],
    "HAWKINCOOK": [
        DocPlan("Domestic Appliance/Hawkins/Hawkins Annual Report 2013-14.pdf", "annual_report", "FY2014"),
    ],
    "CASTROLIND": [
        DocPlan("Lubricants/Castrol/Castrol_Annual_report_2012.pdf", "annual_report", "FY2012"),
    ],
    "VEEDOL": [
        DocPlan("Lubricants/Veedol/ANNUAL_REPORT12-13.pdf", "annual_report", "FY2013"),
    ],
}

ADDED_BY = "proprietary-import:AnnualReports/medium-sectors"


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
                raw_file_path=to_repo_relative(dest),
            )
            print(
                f"{company_id:15s} OK  document_id={row['document_id']:4d} "
                f"{doc.document_type:22s} {doc.fiscal_year}{('/' + doc.quarter) if doc.quarter else '':5s}"
                f"  <- {Path(doc.rel_path).name}"
            )

    conn.close()


if __name__ == "__main__":
    main()
