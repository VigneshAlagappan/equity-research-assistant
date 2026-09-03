"""One-off importer: files Finance/L&TFinance Holding/ (68 files) — the one
deferred Finance subfolder the user asked to actually process (the other
six stay deferred: MFIN routed elsewhere, Capital First/IDFC/HDFC ignored
as pre-merger predecessor entities, IL&FS ignored).

LTFINANCEHOLDING is archived with successor_company_id=LTF ("L&T Finance",
the post-2022 renamed entity) — same pattern as IDFC Bank -> IDFC First Bank
in the Banks import, so everything here routes to LTF.

The "Subsidary Reports/L&T Finance/" subfolder (FY07-FY11 annual reports)
is a distinct pre-holding-company subsidiary, but the same underlying
lending business LTF represents today — filed as historical LTF material.
"Subsidary Reports/L&T Infra/" is a genuinely different NBFC subsidiary
(infrastructure lending, a separate business line) — NOT filed, same
caution as not merging a distinct entity's data without a clear signal.

Not filed, and why:
- Root-level items with no reliable fiscal year or wrong type: two images,
  two .pptx (unsupported format), a product note, an information
  memorandum, two third-party research reports, and a "TC_LT_Pref.pdf"
  whose content is ambiguous.
- A handful of near-duplicate presentations per quarter where a second,
  differently-named file for the same quarter would likely just double up
  the same investor deck (not hash-confirmed identical, but the naming and
  period overlap made filing both more likely to mislead than help) —
  skipped in favor of the more clearly-labeled one.
- One "<Company>_Ltd_DDMMYY.pdf"-pattern file (`L&T_Finance_Holdings_Ltd1_
  230414.pdf`) — same blank/unreadable family found throughout every other
  pass.
- A third-party brokerage stock-picks note (StockPicks_SPA_241114.pdf).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import DOCUMENTS_DIR, to_repo_relative
from companies.registry import get_company
from storage.database import init_db
from storage.repositories import save_company_document

ARCHIVE_ROOT = Path("/Users/radhamurugesan/work/AnnualReports/Finance/L&TFinance Holding")
COMPANY_ID = "LTF"


@dataclass
class DocPlan:
    rel_path: str
    document_type: str
    fiscal_year: str
    quarter: str | None = None


DOCS: list[DocPlan] = [
    DocPlan("2017-18/LTFH Q1FY18.pdf", "financial_result", "FY2018", "Q1"),
    DocPlan("2017-18/Investor_PPT-Q1_FY18.pdf", "investor_presentation", "FY2018", "Q1"),
    DocPlan("2017-18/LTFH Q2FY18.pdf", "financial_result", "FY2018", "Q2"),
    DocPlan("2017-18/Q42018.pdf", "financial_result", "FY2018", "Q4"),
    DocPlan("2016-17/ltfh_q3fy17_investor_presentation.pdf", "investor_presentation", "FY2017", "Q3"),
    DocPlan("2016-17/ltfh_q1fy17_investor_presentation_final.pdf", "investor_presentation", "FY2017", "Q1"),
    DocPlan("2016-17/Annual Report FY 2016-17.pdf", "annual_report", "FY2017"),
    DocPlan("2016-17/LTFH Q4FY17 Investor Presentation V16.pdf", "investor_presentation", "FY2017", "Q4"),
    DocPlan("2016-17/ltfh-unaudited_consolidated_financial_results_-_30.06.2016.pdf", "financial_result", "FY2017", "Q1"),
    DocPlan("2021-22/AR2021-22.pdf", "annual_report", "FY2022"),
    DocPlan("2011-12/LTFH_Q4FY12.pdf", "financial_result", "FY2012", "Q4"),
    DocPlan("2011-12/Annual Report - FY12.pdf", "annual_report", "FY2012"),
    DocPlan("2011-12/L&TFH - Q3FY12 - Earnings Conference Call Transcript.pdf", "transcript", "FY2012", "Q3"),
    DocPlan("2009-10/Annual Report - FY10.pdf", "annual_report", "FY2010"),
    DocPlan("2018-19/Investor Q2 FY19.pdf", "investor_presentation", "FY2019", "Q2"),
    DocPlan("2018-19/LTFH Q1FY19.pdf", "financial_result", "FY2019", "Q1"),
    DocPlan("2018-19/LTFH-Q2 FY19 Earnings Call Transcript-Final.pdf", "transcript", "FY2019", "Q2"),
    DocPlan("2018-19/LTFH-Q1 FY19 Earnings Call Transcript.pdf", "transcript", "FY2019", "Q1"),
    DocPlan("2014-15/ltfh_q3fy15_analyst_presentation.pdf", "investor_presentation", "FY2015", "Q3"),
    DocPlan("2014-15/ltfh_q1fy15_analyst_presentation_final.pdf", "investor_presentation", "FY2015", "Q1"),
    DocPlan("2014-15/ltfh_-_annual_report_-_2014-15.pdf", "annual_report", "FY2015"),
    DocPlan("2014-15/ltfh_unaudited_consolidated_31.12.2014.pdf", "financial_result", "FY2015", "Q3"),
    DocPlan("2014-15/ltfh_unaudited_consolidated_30.06.2014.pdf", "financial_result", "FY2015", "Q1"),
    DocPlan("2014-15/ltfh_q2fy15_analyst_presentation.pdf", "investor_presentation", "FY2015", "Q2"),
    DocPlan("2008-09/Annual Report - FY09.pdf", "annual_report", "FY2009"),
    DocPlan("2013-14/ltfh_audited_consolidated_31.03.2014.pdf", "financial_result", "FY2014", "Q4"),
    DocPlan("2013-14/analyst_call_transcript_q3fy14.pdf", "transcript", "FY2014", "Q3"),
    DocPlan("2013-14/ltfh_analyst_presentation_q1fy14.pdf", "investor_presentation", "FY2014", "Q1"),
    DocPlan("2013-14/ltfh_q3fy14_analyst_presentation.pdf", "investor_presentation", "FY2014", "Q3"),
    DocPlan("2013-14/annual_report_2013-14.pdf", "annual_report", "FY2014"),
    DocPlan("2012-13/presentat - FY13.pdf", "investor_presentation", "FY2013"),
    DocPlan("2012-13/LTFH_FY13_Results.pdf", "financial_result", "FY2013"),
    DocPlan("2012-13/ltfh_investor_presentation_q4fy13.pdf", "investor_presentation", "FY2013", "Q4"),
    DocPlan("2012-13/Annual Report - FY13.pdf", "annual_report", "FY2013"),
    DocPlan("2015-16/ltfh_q3fy16_investor_presentaton.pdf", "investor_presentation", "FY2016", "Q3"),
    DocPlan("2015-16/ltfh_q1fy16_investor_presentaton.pdf", "investor_presentation", "FY2016", "Q1"),
    DocPlan("2015-16/ltfh_q4fy16_investor_presentation_final.pdf", "investor_presentation", "FY2016", "Q4"),
    DocPlan("2015-16/ltfh_-_annual_report_-_fy_2015-16.pdf", "annual_report", "FY2016"),
    DocPlan("2010-11/Annual Report - FY11.pdf", "annual_report", "FY2011"),
    DocPlan("2020-21/AR-2021.pdf", "annual_report", "FY2021"),
    # Pre-holding-company subsidiary reports — same underlying lending
    # business LTF represents today.
    DocPlan("Subsidary Reports/L&T Finance/L&T Finance FY10.pdf", "annual_report", "FY2010"),
    DocPlan("Subsidary Reports/L&T Finance/L&T Finance FY11.pdf", "annual_report", "FY2011"),
    DocPlan("Subsidary Reports/L&T Finance/L&T Finance FY07.pdf", "annual_report", "FY2007"),
    DocPlan("Subsidary Reports/L&T Finance/L&T Finance FY08.pdf", "annual_report", "FY2008"),
    DocPlan("Subsidary Reports/L&T Finance/L&T Finance FY09.pdf", "annual_report", "FY2009"),
]

ADDED_BY = "proprietary-import:AnnualReports/Finance/LTFinanceHolding"


def _copy_pdf(rel_path: str) -> Path:
    src = ARCHIVE_ROOT / rel_path
    dest_dir = DOCUMENTS_DIR / COMPANY_ID
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    dest = dest_dir / f"{stamp}__{Path(rel_path).name}"
    shutil.copy2(src, dest)
    return dest


def main() -> None:
    conn = init_db()
    if get_company(conn, COMPANY_ID) is None:
        print(f"{COMPANY_ID} not registered — aborting")
        return

    for doc in DOCS:
        src = ARCHIVE_ROOT / doc.rel_path
        if not src.exists():
            print(f"MISSING SOURCE  {doc.rel_path}")
            continue
        dest = _copy_pdf(doc.rel_path)
        row = save_company_document(
            conn,
            COMPANY_ID,
            document_type=doc.document_type,
            fiscal_year=doc.fiscal_year,
            quarter=doc.quarter,
            added_by_user=ADDED_BY,
            raw_file_path=to_repo_relative(dest),
        )
        print(
            f"OK  document_id={row['document_id']:4d} {doc.document_type:22s} "
            f"{doc.fiscal_year}{('/' + doc.quarter) if doc.quarter else '':5s}  <- {Path(doc.rel_path).name}"
        )

    conn.close()


if __name__ == "__main__":
    main()
