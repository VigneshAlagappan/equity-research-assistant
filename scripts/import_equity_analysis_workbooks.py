"""One-off importer: registers a company (if not already registered) for
every "Equity Analysis" workbook in data/templates/, parses its Forecast
sheet (scripts/parse_equity_analysis_workbook.py), writes the dashboard JSON
to web/static/data/, and points companies.valuation_model_file at it.

Identity (company_id, display_name) is derived from the filename only — no
NSE/BSE code, sector, or website lookup, per the scope of this pass. Not
idempotent-proof against renaming a file and re-running (each run keys off
whatever's in FILENAME_TO_COMPANY below); safe to re-run as-is since
register_company() and the JSON write are both overwrites, not appends.
"""

from __future__ import annotations

import json
from pathlib import Path

from companies.registry import get_company, register_company
from storage.company_repository import update_company_valuation_model_file
from storage.database import init_db
from scripts.parse_equity_analysis_workbook import parse_workbook

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "data" / "templates"
STATIC_DATA_DIR = REPO_ROOT / "web" / "static" / "data"

# filename (in data/templates/) -> (company_id, display_name). HDFCBANK is
# omitted deliberately — it's already registered with a hand-verified feed.
FILENAME_TO_COMPANY: dict[str, tuple[str, str]] = {
    "Equity Analysis - Bank - Equitas SFB.xlsx": ("EQUITASSFB", "Equitas SFB"),
    "Equity Analysis - Bank - IDFC Bank.xlsx": ("IDFCBANK", "IDFC Bank"),
    "Equity Analysis - Bank - IndusInd.xlsx": ("INDUSIND", "IndusInd"),
    "Equity Analysis - Bank - Kotak Bank.xlsx": ("KOTAKBANK", "Kotak Bank"),
    "Equity Analysis - Bank - Ujjivan Financial.xlsx": ("UJJIVANFINANCIAL", "Ujjivan Financial"),
    "Equity Analysis - Bank - Ujjivan SFB.xlsx": ("UJJIVANSFB", "Ujjivan SFB"),
    "Equity Analysis - Bank - Yes Bank.xlsx": ("YESBANK", "Yes Bank"),
    "Equity Analysis - CAN fin Homes.xlsx": ("CANFINHOMES", "CAN Fin Homes"),
    "Equity Analysis - Capital Trust.xlsx": ("CAPITALTRUST", "Capital Trust"),
    "Equity Analysis - DHFL.xlsx": ("DHFL", "DHFL"),
    "Equity Analysis - Edelweiss.xlsx": ("EDELWEISS", "Edelweiss"),
    "Equity Analysis - IndoStar Capital.xlsx": ("INDOSTARCAPITAL", "IndoStar Capital"),
    "Equity Analysis - JM Financial.xlsx": ("JMFINANCIAL", "JM Financial"),
    "Equity Analysis - L&T Finance Holding.xlsx": ("LTFINANCEHOLDING", "L&T Finance Holding"),
    "Equity Analysis - LIC Housing Finance.xlsx": ("LICHOUSINGFINANCE", "LIC Housing Finance"),
    "Equity Analysis - MAS Financial Services.xlsx": ("MASFINANCIALSERVICES", "MAS Financial Services"),
    "Equity Analysis - Muthoot Finance.xlsx": ("MUTHOOTFINANCE", "Muthoot Finance"),
    "Equity Analysis - PNB housing finance.xlsx": ("PNBHOUSINGFINANCE", "PNB Housing Finance"),
    "Equity Analysis - Repco Finance.xlsx": ("REPCOFINANCE", "Repco Finance"),
    "Equity Analysis - Shriram City Union Finance.xlsx": ("SHRIRAMCITYUNIONFINANCE", "Shriram City Union Finance"),
    "Equity Analysis - Shriram Transport Finance.xlsx": ("SHRIRAMTRANSPORTFINANCE", "Shriram Transport Finance"),
    "Equity Analysis - SRG housing finance.xlsx": ("SRGHOUSINGFINANCE", "SRG Housing Finance"),
    "Equity Analysis - Tata Investment.xlsx": ("TATAINVESTMENT", "Tata Investment"),
}


def main() -> None:
    conn = init_db()
    results = []
    for filename, (company_id, display_name) in FILENAME_TO_COMPANY.items():
        path = TEMPLATES_DIR / filename
        if not path.exists():
            results.append((company_id, "SKIPPED (file not found)"))
            continue

        register_company(conn, company_id, legal_name=display_name, display_name=display_name)

        feed = parse_workbook(path)
        json_filename = f"{company_id.lower()}-analysis.json"
        (STATIC_DATA_DIR / json_filename).write_text(json.dumps(feed))

        update_company_valuation_model_file(conn, company_id, json_filename)

        years = feed["YEARS"]
        results.append((company_id, f"OK  years {years[0]}-{years[-1]}" if years else "OK  no years found"))

    conn.close()

    for company_id, status in results:
        print(f"{company_id:28s} {status}")


if __name__ == "__main__":
    main()
