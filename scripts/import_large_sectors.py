"""One-off importer: files the 9 large sector folders (34-98 files each,
~518 files total) from the proprietary AnnualReports archive — Rating
Agency, Cements, Hospitality, Realty, Misc, Conglomerate, IT, Tyres, Power
(Power's own "Logistics Transport Courier Shipping" sub-folder is a
misfiled different sector — Adani Ports, Blue Dart, Great Eastern Shipping,
TCI — handled here by content, not by its folder placement).

Same discipline as every other importer in this set: byte-identical
duplicates deduped by hash, letterhead/company-name content checked where
a filename was ambiguous. One cross-company mixup found here worth calling
out: `Tyres/old/Equity Analysis - MRF 2014.xlsx` and
`Tyres/old/Equity Analysis - Ceat - 2014.xlsx` are byte-identical to each
other despite the different company names in their filenames, and the
workbook's own "Company Name" field is blank — genuinely unresolvable which
company it is, so neither is filed from that particular file (Ceat's
distinctly-named, distinctly-hashed current-level file is filed instead).

Not filed, and why:
- No active registry match: Majestic Research, Jaypee Cements (JIL/JAL —
  Jaiprakash Associates/Infratech, distinct from the one Jaiprakash entity
  that IS registered, Jaiprakash Power Ventures), Emami Cements (DRHP-only
  anyway), HDIL... wait HDIL matched — see below; DB Realty matched under
  its renamed identity Valor Estate; Indiabulls Real Estate, Nitesh Estates,
  Orbit Corp, Embassy Office Parks (a distinct entity from the registered
  Embassy Developments), Indiabulls Wholesale, Jubilant Industries (distinct
  from the registered Jubilant Foodworks), Sintex Industries, Agrimony
  Commodities, Dakshana Foundation (a non-profit, not equity-listed), Max
  Group/Max India's older diversified holding entity, Welspun Group (three+
  listed Welspun entities exist — folder doesn't disambiguate), Piramal
  Enterprises (demerged 2022 into Piramal Pharma and Piramal Finance —
  neither convincingly the same entity as the pre-demerger diversified
  holding company these 2012-13 docs describe), Mindtree/LTIMindtree (2022
  merger product — genuinely absent from this registry snapshot), 8K Miles,
  Cognizant (NASDAQ-listed, no separate Indian listing), Polaris (merged
  into Virtusa 2016), Zylog, Gati (logistics — genuinely absent), Indiabulls
  Power.
- Third-party/generic content: broker and rating-agency notes throughout
  (CRISIL grading reports, Niveshaay, HDFC Securities, ICICIDirect, Motilal
  Oswal, B&K, IDFC research notes on Sintex), magazine/forum/news articles
  (Ambani family profiles, Ranbaxy heir pieces, Quora posts, Business
  Standard/Livemint/Economic Times pieces), an entire saved-webpage asset
  bundle under Hospitality/Royal Orchids Hotels (images, JS, CSS — not a
  document at all).
- Wrong type for documents.document_type: prospectuses/DRHPs/RHPs/IPO notes
  everywhere, shareholding patterns, a postal ballot notice, MOA/AOA filings,
  a demerger scheme document, SAST filings, an EGM notice.
- Unsupported formats: .pptx, .docx, .zip, .lnk, .xls (old Excel binary),
  .htm.
- No reliably-confirmable fiscal year: several presentations/results with
  no date anywhere in filename, folder, or content (Wipro's bare
  "2015-16.pdf", MRF's "Financial Results.pdf", several Adani Mundraport
  marketing PDFs).
- Recurring blank/unreadable "<Company>_Ltd_DDMMYY.pdf"-family scans (same
  pattern discovered in the medium-sector pass) appear again here (MRF,
  CEAT, Adani) — none filed.
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

ARCHIVE_ROOT = Path(os.environ.get("EQUITY_RESEARCH_ARCHIVE_ROOT", Path.home() / "work" / "AnnualReports"))

XLSX_PLAN: dict[str, list[str]] = {
    "CRISIL": [
        "Rating Agency/Equity Analysis - CRISIL.xlsx",
        "Rating Agency/Equity Analysis - CRISIL-2014.xlsx",
        "Rating Agency/0. old/Equity Analysis - Crisil.xlsx",
        "Rating Agency/0. old/Equity Analysis - CRISIL - old.xlsx",
    ],
    # Top-level and "0. old/" copies of CARE.xlsx are byte-identical
    # (hash-checked) — filed once.
    "CARERATING": [
        "Rating Agency/Equity Analysis - CARE.xlsx",
        "Rating Agency/Equity Analysis - CARE-2014.xlsx",
    ],
    "ICRA": [
        "Rating Agency/Equity Analysis - ICRA.xlsx",
        "Rating Agency/0. old/Equity Analysis - ICRA-Mar.xlsx",
    ],
    "INDIACEM": ["Cements/Equity Analysis - India Cements.xlsx"],
    "AMBUJACEM": [
        "Cements/Equity Analysis - Ambuja Cements-2015.xlsx",
        "Cements/0. Old/Equity Analysis - Ambuja Cements.xlsx",
    ],
    "SPECIALITY": ["Hospitality/Equity Analysis - Speciality Restaurants - 2014.xlsx"],
    # "Westlife dev- McDonalds.xlsx" at top level and in "0. old/" are
    # byte-identical (hash-checked) — filed once.
    "WESTLIFE": [
        "Hospitality/Equity Analysis - Westlife - McDonalds.xlsx",
        "Hospitality/Equity Analysis - Westlife - McDonalds-2014.xlsx",
        "Hospitality/Equity Analysis - Westlife dev- McDonalds.xlsx",
    ],
    "JUBLFOOD": ["Hospitality/Equity Analysis - Jubilant - Dominos.xlsx"],
    "ROHLTD": ["Hospitality/Royal Orchids Analysis.xlsx"],
    "OBEROIRLTY": ["Realty/Equity Analysis - Oberoi Realty.xlsx"],
    "SUNTECK": ["Realty/Equity Analysis - Sunteck.xlsx"],
    "HDIL": ["Realty/Equity Analysis - HDIL.xlsx"],
    "DBREALTY": ["Realty/Equity Analysis - DB Reality.xlsx"],
    "KOLTEPATIL": ["Realty/Equity Analysis - Kolte-Patil Developers.xlsx"],
    "FINCABLES": ["Misc/Equity Analysis - Finolex Cables.xlsx"],
    "FINPIPE": ["Misc/Equity Analysis - Finolex Industries.xlsx"],
    "MCX": ["Misc/Equity Analysis - MCX-2014.xlsx"],
    "VAKRANGEE": ["Misc/Vakrange/Vakrange.xlsx"],
    "ADANIENT": [
        "Conglomerate/Equity Analysis - Adani Enterprise.xlsx",
        "Conglomerate/0. old/Equity Analysis - Adani Enterprise - old.xlsx",
    ],
    "RELIANCE": [
        "Conglomerate/Equity Analysis - RIL - FY2014.xlsx",
        "Conglomerate/Reliance Industries - Diversified/Equity Analysis - RIL.xlsx",
        "Conglomerate/0. old/Equity Analysis - RIL.xlsx",
        "Conglomerate/0. old/Equity Analysis - RIL - 12Jan.xlsx",
        "Conglomerate/0. old/Equity Analysis - RIL - FY2013.xlsx",
        "Conglomerate/0. old/Equity Analysis - RIL - 2013.xlsx",
    ],
    "VEDL": ["Conglomerate/Equity Analysis - Vedanta.xlsx"],
    "SASKEN": ["IT/Equity Analysis - Sasken.xlsx"],
    "WIPRO": ["IT/Equity Analysis - Wipro.xlsx"],
    "TCS": ["IT/Equity Analysis - TCS.xlsx"],
    "JKTYRE": ["Tyres/Equity Analysis - JK Tyre-2014.xlsx"],
    "BALKRISIND": ["Tyres/Equity Analysis - Balkrishna Ind-BKT-2014.xlsx"],
    "APOLLOTYRE": [
        "Tyres/Equity Analysis - Apollo.xlsx",
        "Tyres/old/Equity Analysis - Apollo-2014.xlsx",
        "Tyres/old/Equity Analysis - Apollo-2016-ld.xlsx",
        "Tyres/0. Old/Equity Analysis - Apollo-2016.xlsx",
    ],
    "MRF": [
        "Tyres/Equity Analysis - MRF.xlsx",
        "Tyres/old/Equity Analysis - MRF-2014 - new.xlsx",
        "Tyres/old/Equity Analysis - MRF-2014 - new1.xlsx",
        "Tyres/old/Equity Analysis - MRF.xlsx",
        "Tyres/old/Equity Analysis - MRF-2016.xlsx",
        "Tyres/old/Equity Analysis - MRF-2014.xlsx",
        # NOT filed: "old/Equity Analysis - MRF 2014.xlsx" — see module
        # docstring, byte-identical to the Ceat-named file next to it, blank
        # Company Name field, genuinely unresolvable which company it is.
    ],
    "CEATLTD": ["Tyres/Equity Analysis - Ceat-2014.xlsx"],
    "GOODYEAR": ["Tyres/Equity Analysis - Good year India-2014.xlsx"],
    "ADANIPOWER": [
        "Power/Equity Analysis - Adani Power.xlsx",
        "Power/0. old/Equity Analysis - Adani Power-2016.xlsx",
        "Power/0. old/Equity Analysis - Adani Power.xlsx",
        "Power/0. old/Equity Analysis - Adani Power-2015.xlsx",
        "Power/0. old/Equity Analysis - Adani Power-2014.xlsx",
        "Power/0. old/Equity Analysis - Adani Power - old.xlsx",
    ],
    "SUZLON": [
        "Power/Equity Analysis - Suzlon.xlsx",
        "Power/Suzlon/Equity Analysis - Suzlon - old.xlsx",
        "Power/0. old/Equity Analysis - Suzlon - old.xlsx",
    ],
    "NTPC": [
        "Power/Equity Analysis - NTPC.xlsx",
        "Power/0. old/Equity Analysis - NTPC - 21Jun.xlsx",
    ],
    "NLCINDIA": ["Power/Equity Analysis - NLC.xlsx"],
    "GIPCL": ["Power/Gujrat/Equity Analysis - Gujrat Industries Power Ltd.xlsx"],
    "ADANIPORTS": [
        "Power/Logistics Transport Courier Shipping/Equity Analysis - Adani Ports and SEZ.xlsx",
        "Power/Logistics Transport Courier Shipping/Equity Analysis - Adani Ports and SEZ - 2014.xlsx",
        "Power/Logistics Transport Courier Shipping/0. old/Equity Analysis - Adani Ports and SEZ.xlsx",
    ],
    # Top-level and "0. old/" copies of Bluedart.xlsx are byte-identical
    # (hash-checked) — filed once.
    "BLUEDART": [
        "Power/Logistics Transport Courier Shipping/Equity Analysis - Bluedart-2014.xlsx",
        "Power/Logistics Transport Courier Shipping/Equity Analysis - Bluedart.xlsx",
    ],
    "GESHIP": ["Power/Logistics Transport Courier Shipping/Equity Analysis - GE Shipping.xlsx"],
}


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


PDF_PLAN: dict[str, list[DocPlan]] = {
    "CARERATING": [
        DocPlan("Rating Agency/CARE/CARE Ratings Q1 and FY 2015 Result Presentation.pdf", "investor_presentation", "FY2015", "Q1"),
        DocPlan("Rating Agency/CARE/Audited Financial Results FY14.pdf", "financial_result", "FY2014"),
        DocPlan("Rating Agency/CARE/Unaudited Financial Results for Q1-FY15.pdf", "financial_result", "FY2015", "Q1"),
        DocPlan("Rating Agency/CARE/CARE Ratings Presentation - Q1 FY14.pdf", "investor_presentation", "FY2014", "Q1"),
        DocPlan("Rating Agency/CARE/Q4 FY14 and FY14 Analyst Presentation.pdf", "investor_presentation", "FY2014", "Q4"),
        DocPlan("Rating Agency/CARE/2014-jun-audited-income-statement.pdf", "financial_result", "FY2014"),
    ],
    "CRISIL": [
        DocPlan("Rating Agency/CRISIL/2012-annual-report-crisil.pdf", "annual_report", "FY2012"),
        DocPlan("Rating Agency/CRISIL/2010-annual-report-crisil.pdf", "annual_report", "FY2010"),
        DocPlan("Rating Agency/CRISIL/2011-annual-report-crisil.pdf", "annual_report", "FY2011"),
    ],
    "SHREECEM": [
        DocPlan("Cements/Shree Cement/ar2012-13.pdf", "annual_report", "FY2013"),
    ],
    "AMBUJACEM": [
        DocPlan(
            "Cements/Ambuja Cements/annual_report_2013_accounts_to_be_approved_by_the_shareholders_at_the_AGM_on_10042014.pdf",
            "annual_report", "FY2013",
        ),
        DocPlan("Cements/Ambuja Cements/Investor_Presentation7thSept2013.pdf", "investor_presentation", "FY2014", "Q2"),
        DocPlan("Cements/Ambuja Cements/report-2012.pdf", "annual_report", "FY2012"),
        DocPlan("Cements/Ambuja Cements/Investor Relations Presentation 24th July2013.pdf", "investor_presentation", "FY2014", "Q1"),
    ],
    "INDIACEM": [
        DocPlan("Cements/India Cements/areport2012.pdf", "annual_report", "FY2012"),
        DocPlan("Cements/India Cements/areport2013.pdf", "annual_report", "FY2013"),
        DocPlan("Cements/India Cements/areport2005.pdf", "annual_report", "FY2005"),
    ],
    "RAIN": [
        DocPlan("Cements/Rain Industries/RainIndustries-CorporatePresentation-August,2017.pdf", "investor_presentation", "FY2018", "Q2"),
    ],
    "JUBLFOOD": [
        DocPlan("Misc/Jubilant FoodWorks/JFL_Annual_Report_2013.pdf", "annual_report", "FY2013"),
        DocPlan("Misc/Jubilant FoodWorks/JFL-Q2FY14-Concall-Transcript.pdf", "transcript", "FY2014", "Q2"),
    ],
    "KOLTEPATIL": [
        DocPlan("Realty/Kolte Patil/AR 2009-10.pdf", "annual_report", "FY2010"),
        DocPlan("Realty/Kolte Patil/AR 2010-11.pdf", "annual_report", "FY2011"),
        DocPlan("Realty/Kolte Patil/AR 2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("Realty/Kolte Patil/AR 2012-13.pdf", "annual_report", "FY2013"),
        DocPlan("Realty/Kolte Patil/AR 2013-14.pdf", "annual_report", "FY2014"),
        DocPlan("Realty/Kolte Patil/AR 2014-15.pdf", "annual_report", "FY2015"),
        DocPlan("Realty/Kolte Patil/AR 2015-16.pdf", "annual_report", "FY2016"),
        DocPlan("Realty/Kolte Patil/AR 2016-17.pdf", "annual_report", "FY2017"),
        DocPlan("Realty/Kolte Patil/AR 2017-18.pdf", "annual_report", "FY2018"),
        DocPlan("Realty/Kolte Patil/AR 2018-19.pdf", "annual_report", "FY2019"),
        DocPlan("Realty/Kolte Patil/AR 2019-20.pdf", "annual_report", "FY2020"),
    ],
    "SUNTECK": [
        DocPlan("Realty/Sunteck Realty/2019-20/conference-call-transcript-Q1.pdf", "transcript", "FY2020", "Q1"),
        DocPlan("Realty/Sunteck Realty/2018-19/conference-call-transcript-Q4.pdf", "transcript", "FY2019", "Q4"),
        DocPlan("Realty/Sunteck Realty/2018-19/conference-call-transcript-Q3.pdf", "transcript", "FY2019", "Q3"),
        DocPlan("Realty/Sunteck Realty/2018-19/conference-call-transcript-Q2.pdf", "transcript", "FY2019", "Q2"),
        DocPlan("Realty/Sunteck Realty/2018-19/conference-call-transcript-Q1.pdf", "transcript", "FY2019", "Q1"),
        DocPlan("Realty/Sunteck Realty/2018-19/results-presentation-q1.pdf", "investor_presentation", "FY2019", "Q1"),
        DocPlan("Realty/Sunteck Realty/2018-19/results-presentation-q2.pdf", "investor_presentation", "FY2019", "Q2"),
        DocPlan("Realty/Sunteck Realty/2018-19/results-presentation-q3.pdf", "investor_presentation", "FY2019", "Q3"),
        DocPlan("Realty/Sunteck Realty/2018-19/results-presentation-q4.pdf", "investor_presentation", "FY2019", "Q4"),
    ],
    "HDIL": [
        DocPlan("Realty/HDIL/Q3-Analyst-Presentation-FY-12-13.pdf", "investor_presentation", "FY2013", "Q3"),
        DocPlan("Realty/HDIL/HDIL-Annual-Report-2011-12-Final.pdf", "annual_report", "FY2012"),
    ],
    "DBREALTY": [
        DocPlan("Realty/DB Realty/Investor Presentation Q3 FY 2013.pdf", "investor_presentation", "FY2013", "Q3"),
        DocPlan("Realty/DB Realty/Annual Report FY 2011 - 2012.pdf", "annual_report", "FY2012"),
        DocPlan("Realty/DB Realty/Annual Report 2010 - 2011.pdf", "annual_report", "FY2011"),
    ],
    "FINPIPE": [
        DocPlan("Misc/Finolex Industries/FIL_Annual_Report_2012_2013.pdf", "annual_report", "FY2013"),
        DocPlan("Misc/Finolex Industries/Q2FY14 Presentation.pdf", "investor_presentation", "FY2014", "Q2"),
    ],
    "FINCABLES": [
        DocPlan("Misc/Finolex Cables/FCL ANNUAL REPORT 2012-13.pdf", "annual_report", "FY2013"),
    ],
    "ETERNAL": [
        DocPlan("Misc/Zomato Eternal/Q1-2026-letter.pdf", "financial_result", "FY2026", "Q1"),
        DocPlan("Misc/Zomato Eternal/AR-2024-25.pdf", "annual_report", "FY2025"),
        DocPlan("Misc/Zomato Eternal/Eternal_Ltd_Company_overview_April_2025.pdf", "investor_presentation", "FY2025"),
    ],
    "SWIGGY": [
        DocPlan("Misc/Swiggy/AR-2024-25.pdf", "annual_report", "FY2025"),
    ],
    "MMTC": [
        DocPlan("Misc/MMTC/57_Annual Report  2012-13.pdf", "annual_report", "FY2013"),
    ],
    "PCJEWELLER": [
        DocPlan("Misc/PC Jewellers/AR2008_09_highlights.pdf", "annual_report", "FY2009"),
    ],
    "A2ZINFRA": [
        DocPlan("Misc/A2Z Group/Annual_Report_2011_12.pdf", "annual_report", "FY2012"),
    ],
    "ADANIENT": [
        DocPlan("Conglomerate/Adani Enterprise - Integrated/Annual Report 2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("Conglomerate/Adani Enterprise - Integrated/AEL_Annual Report_2014_15.pdf", "annual_report", "FY2015"),
        DocPlan("Conglomerate/Adani Enterprise - Integrated/Annual-Report-2010-11.pdf", "annual_report", "FY2011"),
        DocPlan("Conglomerate/Adani Enterprise - Integrated/Adani Group Presentation_170412.pdf", "investor_presentation", "FY2012", "Q4"),
    ],
    "VEDL": [
        DocPlan("Conglomerate/Vedanta - Diversified/22883_vedanta_ar2015_final.pdf", "annual_report", "FY2015"),
        DocPlan("Conglomerate/Vedanta - Diversified/Vedanta resources corporate presentation - sept 2015.pdf", "investor_presentation", "FY2016", "Q2"),
    ],
    "GODREJIND": [
        DocPlan("Conglomerate/Godrej - Diversified/GIL_AR_2012.pdf", "annual_report", "FY2012"),
        DocPlan("Conglomerate/Godrej - Diversified/Corporate_Presentation_June2012.pdf", "investor_presentation", "FY2013", "Q1"),
    ],
    "COFFEEDAY": [
        DocPlan("Conglomerate/Coffee Day/Coffee-day-annual-report-2015.pdf", "annual_report", "FY2015"),
    ],
    "WIPRO": [
        DocPlan("IT/Wipro/Q1-FY-10-11-Analyst-Data-Sheet.pdf", "investor_presentation", "FY2011", "Q1"),
        DocPlan("IT/Wipro/AR2008_09_highlights.pdf", "annual_report", "FY2009"),
    ],
    "TCS": [
        DocPlan("IT/TCS/2023-24/Q4 2023-24 Fact Sheet.pdf", "financial_result", "FY2024", "Q4"),
        DocPlan("IT/TCS/2022-23/AR2022-23.pdf", "annual_report", "FY2023"),
        DocPlan("IT/TCS/2010-11/TCS_Annual_Report_2010-2011.PDF", "annual_report", "FY2011"),
        DocPlan("IT/TCS/2007-08/TCS_Annual_Report_2007_2008.PDF", "annual_report", "FY2008"),
    ],
    "GOOGL": [
        DocPlan("IT/Google/2009-AR.pdf", "annual_report", "FY2009"),
        DocPlan("IT/Google/2022-alphabet-annual-report.pdf", "annual_report", "FY2022"),
        DocPlan("IT/Google/2015/2015_alphabet_annual_report.pdf", "annual_report", "FY2015"),
    ],
    "SASKEN": [
        DocPlan("IT/Sasken/2012 - Sasken - Annual Report final.pdf", "annual_report", "FY2012"),
    ],
    "HCLTECH": [
        DocPlan("IT/HCL/annual_report_2010-11_1.pdf", "annual_report", "FY2011"),
    ],
    "JKTYRE": [
        DocPlan("Tyres/JK Tyres/AR2009-10.pdf", "annual_report", "FY2010"),
        DocPlan("Tyres/JK Tyres/AR2010-11.pdf", "annual_report", "FY2011"),
        DocPlan("Tyres/JK Tyres/AR2012-13.pdf", "annual_report", "FY2013"),
        DocPlan("Tyres/JK Tyres/AR2006-07.pdf", "annual_report", "FY2007"),
    ],
    "BALKRISIND": [
        DocPlan("Tyres/Balkrishna Industries/annual_report_2010-11.pdf", "annual_report", "FY2011"),
        DocPlan("Tyres/Balkrishna Industries/BIL_4th_QUARTER_201314.pdf", "financial_result", "FY2014", "Q4"),
        DocPlan("Tyres/Balkrishna Industries/BKTAnnualReport2008-09.pdf", "annual_report", "FY2009"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Annual Reprot 2013_Low Rais.pdf", "annual_report", "FY2013"),
        DocPlan("Tyres/Balkrishna Industries/BKTAnnualReport2009-10.pdf", "annual_report", "FY2010"),
        DocPlan("Tyres/Balkrishna Industries/50th Annual Report for year 31.03.2012 along with Notice.pdf", "annual_report", "FY2012"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_jan_ 2014.pdf", "investor_presentation", "FY2014", "Q4"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_Oct 2011.pdf", "investor_presentation", "FY2012", "Q3"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_October 2013.pdf", "investor_presentation", "FY2014", "Q3"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_August 11.pdf", "investor_presentation", "FY2012", "Q2"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_November 2012.pdf", "investor_presentation", "FY2013", "Q3"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_June 2014.pdf", "investor_presentation", "FY2015", "Q1"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor_Presentation_Feb_2012.pdf", "investor_presentation", "FY2012", "Q4"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_May 2012.pdf", "investor_presentation", "FY2013", "Q1"),
        DocPlan("Tyres/Balkrishna Industries/BKT_Investor Presentation_May 2013.pdf", "investor_presentation", "FY2014", "Q1"),
    ],
    "APOLLOTYRE": [
        DocPlan("Tyres/Apollo/2015-16/transcript-Q3-FY-2016.pdf", "transcript", "FY2016", "Q3"),
    ],
    "MRF": [
        DocPlan("Tyres/MRF/AnnualReport2012-2013.pdf", "annual_report", "FY2013"),
        DocPlan("Tyres/MRF/AnnualReport2011-2012.pdf", "annual_report", "FY2012"),
        DocPlan("Tyres/MRF/AnnualReport_10-11.pdf", "annual_report", "FY2011"),
    ],
    "CEATLTD": [
        DocPlan("Tyres/Ceat/Transcription-ceat-Q3FY13.pdf", "transcript", "FY2013", "Q3"),
        DocPlan("Tyres/Ceat/CEAT_Q2_Transcript_Oct-28-2013.pdf", "transcript", "FY2014", "Q2"),
        DocPlan("Tyres/Ceat/Investor-Concall-transciption-for-Q1-FY-13-14.pdf", "transcript", "FY2014", "Q1"),
        DocPlan("Tyres/Ceat/Investor-Concall-transcription-for-Q4 FY-13-14.pdf", "transcript", "FY2014", "Q4"),
        DocPlan("Tyres/Ceat/Investor-concall-Transcription-for-Q3 FY-13-14.pdf", "transcript", "FY2014", "Q3"),
        DocPlan("Tyres/Ceat/Investor-Presentation-Q3FY14.pdf", "investor_presentation", "FY2014", "Q3"),
        DocPlan("Tyres/Ceat/CEAT Q4 FY13 Investor presentation.pdf", "investor_presentation", "FY2013", "Q4"),
        DocPlan("Tyres/Ceat/Transcription_CEAT_10th_November_2011_Q2.pdf", "transcript", "FY2012", "Q2"),
        DocPlan("Tyres/Ceat/CEAT-Annual_Report_2007_08.pdf", "annual_report", "FY2008"),
        DocPlan("Tyres/Ceat/CEAT_Annual_Report_2011_12.pdf", "annual_report", "FY2012"),
        DocPlan("Tyres/Ceat/Transcription_CEAT_24th_Jan_2012.pdf", "transcript", "FY2012", "Q3"),
        DocPlan("Tyres/Ceat/InvestorPresentation_Q1FY14.pdf", "investor_presentation", "FY2014", "Q1"),
        DocPlan("Tyres/Ceat/Transcription-ceat-Q4 FY-11- 12.pdf", "transcript", "FY2012", "Q4"),
        DocPlan("Tyres/Ceat/CEAT-Annual_Report_2009_10.pdf", "annual_report", "FY2010"),
        DocPlan("Tyres/Ceat/CEAT Investor Presentation_Q4FY14.pdf", "investor_presentation", "FY2014", "Q4"),
        DocPlan("Tyres/Ceat/InvestorPresentation_Q1_FY13.pdf", "investor_presentation", "FY2013", "Q1"),
        DocPlan("Tyres/Ceat/Q4FY13 CEAT Earnings call transcript-May07-2013.pdf", "transcript", "FY2013", "Q4"),
        DocPlan("Tyres/Ceat/CEAT-Annual_Report_2008_09.pdf", "annual_report", "FY2009"),
        DocPlan("Tyres/Ceat/InvestorPresentation_Q2FY14.pdf", "investor_presentation", "FY2014", "Q2"),
        DocPlan("Tyres/Ceat/CEAT-Annual_Report_2012-13.pdf", "annual_report", "FY2013"),
        DocPlan("Tyres/Ceat/CEAT_Annual_Report_2010_11.pdf", "annual_report", "FY2011"),
    ],
    "GOODYEAR": [
        DocPlan("Tyres/Goodyear India/Annual_Report_2013.pdf", "annual_report", "FY2013"),
    ],
    "ELGIEQUIP": [
        DocPlan("Tyres/Elgi/AR201213.pdf", "annual_report", "FY2013"),
    ],
    "TIINDIA": [
        DocPlan("Tyres/TI/INVESTORS-AnnualReport-TI_Annual Report 2015-16.pdf", "annual_report", "FY2016"),
    ],
    "NTPC": [
        DocPlan("Power/NTPC/20120910-AnalystsMeetPresentation.pdf", "investor_presentation", "FY2013", "Q2"),
        DocPlan("Power/NTPC/Q3-FY2012.pdf", "financial_result", "FY2012", "Q3"),
        DocPlan("Power/NTPC/NTPC-AR-2011-12.pdf", "annual_report", "FY2012"),
    ],
    "GIPCL": [
        DocPlan("Power/Gujrat/Annual Report 2011-2012.pdf", "annual_report", "FY2012"),
    ],
    "GVKPIL": [
        DocPlan("Power/GVK/2011__12.pdf", "annual_report", "FY2012"),
    ],
    "TORNTPOWER": [
        DocPlan("Power/Torrent Power/tpl-annual-report-11-12.pdf", "annual_report", "FY2012"),
    ],
    "TATAPOWER": [
        DocPlan("Power/Tata Power/93Annual-report-2011-12.pdf", "annual_report", "FY2012"),
        DocPlan("Power/Tata Power/call-transcript-feb-2014.pdf", "transcript", "FY2014", "Q4"),
        DocPlan("Power/Tata Power/investor-presentation-august-2014.pdf", "investor_presentation", "FY2015", "Q2"),
    ],
    "POWERGRID": [
        DocPlan("Power/Power Grid/AR_2011_12.pdf", "annual_report", "FY2012"),
    ],
    "SUZLON": [
        DocPlan("Power/Suzlon/2012-13/Q4.pdf", "financial_result", "FY2013", "Q4"),
    ],
    "NLCINDIA": [
        DocPlan("Power/NLC/annual_report1112.pdf", "annual_report", "FY2012"),
    ],
    "ADANIPOWER": [
        DocPlan("Power/Adani Power/2016-17/Analyst+Presentation+-+APL+4QFY16.pdf", "investor_presentation", "FY2016", "Q4"),
        DocPlan("Power/Adani Power/2016-17/Q42016-17.pdf", "financial_result", "FY2017", "Q4"),
        DocPlan("Power/Adani Power/2009-10/Annual_Report_for_the_year_2009-2010.pdf", "annual_report", "FY2010"),
        DocPlan("Power/Adani Power/2015-16/ADANI_POWER_LTD_AR_2015-16.pdf", "annual_report", "FY2016"),
        DocPlan("Power/Adani Power/2015-16/Q3 2015-16.pdf", "financial_result", "FY2016", "Q3"),
    ],
    "TCI": [
        DocPlan("Power/Logistics Transport Courier Shipping/TCI/TCI PRESENTATION Q2-H1 Sep 13.pdf", "investor_presentation", "FY2014", "Q2"),
    ],
}

ADDED_BY = "proprietary-import:AnnualReports/large-sectors"


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
