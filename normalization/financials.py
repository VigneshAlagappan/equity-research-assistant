"""Metric vocabulary and the wide-row -> NormalizedObservation transform.

metrics_dictionary is a lookup table, not hardcoded columns (README: Financial
Observations) — DEFAULT_METRICS/DEFAULT_METRIC_ALIASES below are seed data,
not a schema. Adding a metric or an alias for a row label the vendor phrases
differently is a data edit (INSERT into these tables), never a code change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from normalization.periods import PeriodParseError, parse_period_header
from normalization.units import NumericParseError, infer_unit, parse_numeric
from sources.base import NormalizedObservation
from storage.db_types import DBConnection
from storage.repositories import get_metric_dictionary_entry, get_metric_key_for_alias, seed_metric_vocabulary

logger = logging.getLogger(__name__)

# (metric_key, display_name, category, applicable_sectors_json, default_unit)
#
# applicable_sectors was originally bank-only for several of these. Verified
# against a real Jio Financial Services (an NBFC, not a bank) export: NBFCs
# report interest income/expense, advances, provisions, and NPA ratios too —
# only deposit-taking (CASA) is genuinely bank-exclusive. Broadened below.
DEFAULT_METRICS: list[tuple[str, str, str, str | None, str]] = [
    # Income statement
    ("interest_earned", "Interest Earned", "income_statement", '["bank","nbfc"]', "INR_CRORE"),
    ("total_revenue", "Total Revenue", "income_statement", None, "INR_CRORE"),
    ("other_income", "Other Income", "income_statement", None, "INR_CRORE"),
    ("interest_expended", "Interest Expended", "income_statement", None, "INR_CRORE"),
    ("operating_expenses", "Operating Expenses", "income_statement", None, "INR_CRORE"),
    ("operating_profit", "Operating Profit", "income_statement", None, "INR_CRORE"),
    ("depreciation", "Depreciation", "income_statement", None, "INR_CRORE"),
    ("provisions_and_contingencies", "Provisions & Contingencies", "income_statement", '["bank","nbfc"]', "INR_CRORE"),
    ("profit_before_tax", "Profit before Tax", "income_statement", None, "INR_CRORE"),
    ("tax", "Tax", "income_statement", None, "INR_CRORE"),
    ("net_profit", "Net Profit", "income_statement", None, "INR_CRORE"),
    ("eps", "EPS", "income_statement", None, "INR"),
    # Balance sheet
    ("equity_share_capital", "Equity Share Capital", "balance_sheet", None, "INR_CRORE"),
    ("reserves", "Reserves", "balance_sheet", None, "INR_CRORE"),
    ("total_shareholders_funds", "Total Shareholders Funds", "balance_sheet", None, "INR_CRORE"),
    ("deposits", "Deposits", "balance_sheet", '["bank"]', "INR_CRORE"),
    ("borrowings", "Borrowings", "balance_sheet", None, "INR_CRORE"),
    ("other_liabilities", "Other Liabilities", "balance_sheet", None, "INR_CRORE"),
    ("total_liabilities", "Total Liabilities", "balance_sheet", None, "INR_CRORE"),
    ("net_block", "Net Block", "balance_sheet", None, "INR_CRORE"),
    ("capital_work_in_progress", "Capital Work in Progress", "balance_sheet", None, "INR_CRORE"),
    ("advances", "Advances", "balance_sheet", '["bank","nbfc"]', "INR_CRORE"),
    ("investments", "Investments", "balance_sheet", None, "INR_CRORE"),
    ("other_assets", "Other Assets", "balance_sheet", None, "INR_CRORE"),
    ("cash_and_bank", "Cash & Bank", "balance_sheet", None, "INR_CRORE"),
    ("total_assets", "Total Assets", "balance_sheet", None, "INR_CRORE"),
    # Non-financial-company balance sheet lines — added for
    # sources/proprietary.py's "3 - Forecast" layout (a manufacturing
    # company's balance sheet, unlike Screener's bank/NBFC-leaning template
    # above), universally applicable rather than sector-tagged.
    ("current_liabilities", "Current Liabilities", "balance_sheet", None, "INR_CRORE"),
    ("inventories", "Inventories", "balance_sheet", None, "INR_CRORE"),
    ("total_current_assets", "Total Current Assets", "balance_sheet", None, "INR_CRORE"),
    ("net_current_assets", "Net Current Assets", "balance_sheet", None, "INR_CRORE"),
    ("net_fixed_assets", "Net Fixed Assets", "balance_sheet", None, "INR_CRORE"),
    # Per-share and share-count figures — also for sources/proprietary.py;
    # verified against a real bank file (ICICI Bank) that these are directly
    # reported rows, not something this app needs to derive itself.
    ("dividend_per_share", "Dividend per Share", "income_statement", None, "INR"),
    ("sales_per_share", "Sales (Revenue) per Share", "income_statement", None, "INR"),
    ("shares_outstanding", "Shares Outstanding (Cr)", "balance_sheet", None, "NUMBER"),
    # Ratios (vendor-reported; financials/ratios.py computes ROA/ROE independently)
    ("gross_npa_percent", "Gross NPA %", "ratio", '["bank","nbfc"]', "PERCENT"),
    ("net_npa_percent", "Net NPA %", "ratio", '["bank","nbfc"]', "PERCENT"),
    ("casa_percent", "CASA %", "ratio", '["bank"]', "PERCENT"),
    ("net_interest_margin", "Net Interest Margin", "ratio", '["bank","nbfc"]', "PERCENT"),
    ("return_on_assets_percent", "Return on Assets %", "ratio", '["bank","nbfc"]', "PERCENT"),
    ("return_on_equity_percent", "Return on Equity %", "ratio", None, "PERCENT"),
    ("book_value", "Book Value", "ratio", None, "INR"),
    # Cash flow
    ("net_cash_from_operating_activities", "Net Cash from Operating Activities", "cash_flow", None, "INR_CRORE"),
    ("net_cash_from_investing_activities", "Net Cash from Investing Activities", "cash_flow", None, "INR_CRORE"),
    ("net_cash_from_financing_activities", "Net Cash from Financing Activities", "cash_flow", None, "INR_CRORE"),
]

# (source, raw_label, metric_key) — raw_label is the exact vendor row label.
# Multiple aliases per metric are expected (vendors phrase the same line
# item differently across exports); add rows here, not code, to extend.
DEFAULT_METRIC_ALIASES: list[tuple[str, str, str]] = [
    ("screener", "Interest Earned", "interest_earned"),
    ("screener", "Sales", "total_revenue"),  # Screener's generic (non-bank-template) top-line label
    ("screener", "Other Income", "other_income"),
    ("screener", "Interest Expended", "interest_expended"),
    ("screener", "Interest", "interest_expended"),  # generic template's label for interest expense
    ("screener", "Expenses", "operating_expenses"),
    ("screener", "Operating Expenses", "operating_expenses"),
    ("screener", "Operating Profit", "operating_profit"),
    ("screener", "Depreciation", "depreciation"),
    ("screener", "Provisions & Contingencies", "provisions_and_contingencies"),
    ("screener", "Provisions and Contingencies", "provisions_and_contingencies"),
    ("screener", "Profit before tax", "profit_before_tax"),
    ("screener", "Profit Before Tax", "profit_before_tax"),
    ("screener", "Tax", "tax"),
    ("screener", "Net Profit", "net_profit"),
    ("screener", "Net profit", "net_profit"),  # real Data Sheet casing (only first word capitalized)
    ("screener", "EPS in Rs", "eps"),
    ("screener", "EPS", "eps"),
    ("screener", "Equity Share Capital", "equity_share_capital"),
    ("screener", "Reserves", "reserves"),
    ("screener", "Total Shareholders Funds", "total_shareholders_funds"),
    ("screener", "Shareholders Funds", "total_shareholders_funds"),
    ("screener", "Net Worth", "total_shareholders_funds"),
    ("screener", "Deposits", "deposits"),
    ("screener", "Borrowings", "borrowings"),
    ("screener", "Other Liabilities", "other_liabilities"),
    ("screener", "Total Liabilities", "total_liabilities"),
    ("screener", "Net Block", "net_block"),
    ("screener", "Capital Work in Progress", "capital_work_in_progress"),
    ("screener", "Advances", "advances"),
    ("screener", "Investments", "investments"),
    ("screener", "Other Assets", "other_assets"),
    ("screener", "Cash & Bank", "cash_and_bank"),
    # "Total" appears twice in the real Balance Sheet section (liabilities-side
    # and assets-side subtotal) — by the balance-sheet identity they're always
    # equal, so aliasing both instances to total_assets is harmless.
    ("screener", "Total", "total_assets"),
    ("screener", "Total Assets", "total_assets"),
    ("screener", "Gross NPA %", "gross_npa_percent"),
    ("screener", "GNPA %", "gross_npa_percent"),
    ("screener", "Net NPA %", "net_npa_percent"),
    ("screener", "NNPA %", "net_npa_percent"),
    ("screener", "CASA %", "casa_percent"),
    ("screener", "Net Interest Margin", "net_interest_margin"),
    ("screener", "NIM %", "net_interest_margin"),
    ("screener", "Return on Assets %", "return_on_assets_percent"),
    ("screener", "ROA %", "return_on_assets_percent"),
    ("screener", "Return on Equity", "return_on_equity_percent"),
    ("screener", "Return on Equity %", "return_on_equity_percent"),
    ("screener", "ROE %", "return_on_equity_percent"),
    ("screener", "Book Value", "book_value"),
    ("screener", "Adjusted Equity Shares in Cr", "shares_outstanding"),
    ("screener", "Cash from Operating Activity", "net_cash_from_operating_activities"),
    ("screener", "Cash from Investing Activity", "net_cash_from_investing_activities"),
    ("screener", "Cash from Financing Activity", "net_cash_from_financing_activities"),
]

# "proprietary" (sources/proprietary.py) parses a hand-built "Equity
# Analysis" workbook's own "3 - Forecast" sheet — a different row-label
# convention than Screener's Data Sheet, verified against a real
# non-financial file (data/raw/ARE&M/proprietary/..._Amara_Raja_Battries.xlsx)
# and a real bank file (data/raw/ICICIBANK/proprietary/..._Bank_-_ICICI_Bank.xlsx).
# The template's "For Banks only" rows (Gross Deposits/Borrowings, Cash &
# Balances with RBI, Advances, ...) are aliased too, not skipped — genuinely
# populated for a bank, genuinely blank (and therefore silently skipped as
# "no data for period", not an error) for a company like Amara Raja. EPS,
# Book Value, Dividend/share, and Sales/share are aliased directly as
# reported rows rather than derived in code, same as every other raw line
# item here — the sheet already computed them once, no need to recompute.
# Deliberately still NOT aliased: "Earnings" (row above "Net Earnings (Net
# Profit or PAT)" — a different, larger-magnitude figure that doesn't behave
# like annual net profit; likely cumulative, left unmapped rather than
# guessed), "Net Cash & Cash Equivalent" (redundant with Cash and
# equivalents, computed differently — mapping both risks two conflicting
# values reconciling under one metric/period), "Balance with Banks, Money at
# Call" (no distinct existing metric_key fits it without overloading
# cash_and_bank, which "Cash & Balances with RBI" already covers), and every
# ratio section (those are calculations financials/ratios.py derives itself,
# not raw facts to ingest). Unaliased rows are skipped with a logged warning by
# build_observations_from_periods, not silently dropped or guessed.
DEFAULT_METRIC_ALIASES = DEFAULT_METRIC_ALIASES + [
    ("proprietary", "Networth (reserves only)", "reserves"),
    ("proprietary", "SHE (includes reserves & Share capital)", "total_shareholders_funds"),
    ("proprietary", "Current Liabilities", "current_liabilities"),
    ("proprietary", "Total Liabilities", "total_liabilities"),
    ("proprietary", "Cash and equivalents", "cash_and_bank"),
    ("proprietary", "Investments", "investments"),
    ("proprietary", "Inventories", "inventories"),
    ("proprietary", "Total Current Assets", "total_current_assets"),
    ("proprietary", "Net Current Assets", "net_current_assets"),
    ("proprietary", "Net Fixed Assets", "net_fixed_assets"),
    ("proprietary", "Net Cash Flow from Operations", "net_cash_from_operating_activities"),
    ("proprietary", "Net Cash Flow from Investments", "net_cash_from_investing_activities"),
    ("proprietary", "Net Cash Flow from Financing", "net_cash_from_financing_activities"),
    ("proprietary", "Expenses", "operating_expenses"),
    ("proprietary", "Other Income", "other_income"),
    ("proprietary", "Total Assets/Liabilities", "total_assets"),
    ("proprietary", "Operating Profit (EBITDA)", "operating_profit"),
    ("proprietary", "Profit before Tax (PBT)", "profit_before_tax"),
    ("proprietary", "Depreciation", "depreciation"),
    ("proprietary", "Net Earnings (Net Profit or PAT)", "net_profit"),
    ("proprietary", "Gross Deposits", "deposits"),
    ("proprietary", "Gross Borrowings", "borrowings"),
    ("proprietary", "Cash & Balances with RBI", "cash_and_bank"),
    ("proprietary", "Advances", "advances"),
    ("proprietary", "Other Liabilities & provisions", "other_liabilities"),
    ("proprietary", "EPS (Net Profit per share)", "eps"),
    ("proprietary", "Book Value (Networth based) per share", "book_value"),
    ("proprietary", "Dividend (per share)", "dividend_per_share"),
    ("proprietary", "Sales (Rev per share)", "sales_per_share"),
    ("proprietary", "Total Number of outstanding shares (in Cr)", "shares_outstanding"),
]

# "yfinance" (sources/yfinance_financials.py) reads yfinance's own
# Ticker.financials/.balance_sheet/.cashflow row labels directly (a fixed,
# documented vocabulary yfinance itself uses across tickers/exchanges, not a
# vendor template that varies file to file the way Screener's does) —
# verified against a real AAPL pull. Values arrive as raw currency units and
# are pre-divided to match this app's "already in the metric's native scale"
# convention (README/schema: canonical_financials stores figures already in
# crore for INR_CRORE, so a USD_MILLION value is stored already-in-millions
# too — see sources/yfinance_financials.py's own division by 1e6). Only the
# lines that feed an existing metric_key are aliased; yfinance's much larger
# vocabulary (EBITDA, Free Cash Flow, dozens of sub-lines, ...) is left
# unmapped rather than growing metrics_dictionary for a single source.
DEFAULT_METRIC_ALIASES = DEFAULT_METRIC_ALIASES + [
    ("yfinance", "Total Revenue", "total_revenue"),
    ("yfinance", "Operating Expense", "operating_expenses"),
    ("yfinance", "Operating Income", "operating_profit"),
    ("yfinance", "Reconciled Depreciation", "depreciation"),
    ("yfinance", "Pretax Income", "profit_before_tax"),
    ("yfinance", "Tax Provision", "tax"),
    ("yfinance", "Net Income", "net_profit"),
    ("yfinance", "Diluted EPS", "eps"),
    ("yfinance", "Stockholders Equity", "total_shareholders_funds"),
    ("yfinance", "Total Liabilities Net Minority Interest", "total_liabilities"),
    ("yfinance", "Total Assets", "total_assets"),
    ("yfinance", "Cash And Cash Equivalents", "cash_and_bank"),
    ("yfinance", "Investmentin Financial Assets", "investments"),
    ("yfinance", "Operating Cash Flow", "net_cash_from_operating_activities"),
    ("yfinance", "Investing Cash Flow", "net_cash_from_investing_activities"),
    ("yfinance", "Financing Cash Flow", "net_cash_from_financing_activities"),
    ("yfinance", "Ordinary Shares Number", "shares_outstanding"),
    # Deliberately no "book_value" alias: yfinance's own per-share book value
    # isn't a raw statement line (it's a computed field on Ticker.info, not
    # in .balance_sheet), and "Tangible Book Value" there is a total-company
    # dollar aggregate, not per-share — aliasing it to book_value (a
    # per-share metric everywhere else in this app, see DEFAULT_METRICS)
    # would silently store the wrong shape of number under that metric_key.
    # Left unmapped rather than guessed.
]

# "sec_edgar" (sources/sec_edgar.py) reads US-GAAP XBRL concept names
# straight from SEC's own data.sec.gov/api/xbrl/companyfacts endpoint —
# raw_label here is the bare us-gaap concept name (e.g. "NetIncomeLoss"),
# same "XBRL tag as raw_label" convention the "nse" aliases below use, not
# a spreadsheet row label. Ordered fallback lists per metric_key (which
# concept a filer actually uses varies by industry) live in sources/
# sec_edgar.py's own _CONCEPT_MAP -- add a new fallback concept there, and
# its alias here, together; this table alone doesn't decide which concept
# wins when a company reports more than one for the same metric_key.
# Bank-specific coverage (interest_earned/deposits/advances) is thinner
# than the general set -- verified against a real JPMorgan Chase filing
# that bank holding companies often skip the generic Revenues concept
# entirely (interest income/expense nets differently in a bank's income
# statement), so total_revenue in particular will be absent for most banks
# rather than wrong.
DEFAULT_METRIC_ALIASES = DEFAULT_METRIC_ALIASES + [
    ("sec_edgar", "Revenues", "total_revenue"),
    ("sec_edgar", "RevenueFromContractWithCustomerExcludingAssessedTax", "total_revenue"),
    ("sec_edgar", "RevenueFromContractWithCustomerIncludingAssessedTax", "total_revenue"),
    ("sec_edgar", "SalesRevenueNet", "total_revenue"),
    ("sec_edgar", "RevenuesNetOfInterestExpense", "total_revenue"),
    ("sec_edgar", "OperatingIncomeLoss", "operating_profit"),
    ("sec_edgar", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "profit_before_tax"),
    ("sec_edgar", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments", "profit_before_tax"),
    ("sec_edgar", "IncomeTaxExpenseBenefit", "tax"),
    ("sec_edgar", "NetIncomeLoss", "net_profit"),
    ("sec_edgar", "ProfitLoss", "net_profit"),
    ("sec_edgar", "EarningsPerShareDiluted", "eps"),
    ("sec_edgar", "EarningsPerShareBasic", "eps"),
    ("sec_edgar", "InterestAndDividendIncomeOperating", "interest_earned"),
    ("sec_edgar", "InterestIncomeOperating", "interest_earned"),
    ("sec_edgar", "InterestAndFeeIncomeLoansAndLeases", "interest_earned"),
    ("sec_edgar", "InterestExpense", "interest_expended"),
    ("sec_edgar", "InterestExpenseOperating", "interest_expended"),
    ("sec_edgar", "Assets", "total_assets"),
    ("sec_edgar", "StockholdersEquity", "total_shareholders_funds"),
    ("sec_edgar", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "total_shareholders_funds"),
    ("sec_edgar", "CommonStockSharesOutstanding", "shares_outstanding"),
    ("sec_edgar", "CommonStockSharesIssued", "shares_outstanding"),
    ("sec_edgar", "Deposits", "deposits"),
    ("sec_edgar", "LoansAndLeasesReceivableNetReportedAmount", "advances"),
    ("sec_edgar", "LoansAndLeasesReceivableNetOfDeferredIncome", "advances"),
    ("sec_edgar", "LoansReceivableNet", "advances"),
    ("sec_edgar", "NetCashProvidedByUsedInOperatingActivities", "net_cash_from_operating_activities"),
    ("sec_edgar", "NetCashProvidedByUsedInInvestingActivities", "net_cash_from_investing_activities"),
    ("sec_edgar", "NetCashProvidedByUsedInFinancingActivities", "net_cash_from_financing_activities"),
]

# "nse" (sources/nse_xbrl.py) reads a quarterly-results XBRL filing pulled
# live from NSE's corporates-financial-results / Integrated Filing listings
# — raw_label here is the XBRL tag's local name (e.g. "InterestEarned"), not
# a spreadsheet row label, but the same alias mechanism applies unchanged.
# Two taxonomies verified against real filings so far (guardrail: add
# support taxonomy-by-taxonomy) — banking ("IFBanking"/"in-bse-fin", IDFC
# First Bank) below, and the general Ind-AS corporate one ("IFIndAs",
# Infosys) further down. They use different tag names for similar concepts
# (e.g. "EmployeeBenefitExpense" vs "EmployeesCost", "ProfitBeforeTax" vs
# "ProfitLossFromOrdinaryActivitiesBeforeTax") but a few tags are genuinely
# shared verbatim across both (OtherIncome, TaxExpense,
# PaidUpValueOfEquityShareCapital/FaceValueOfEquityShareCapital) — aliased
# once, not duplicated per taxonomy. "ProfitLossForPeriod" is the one that
# looks shared but isn't: banking's own tag is "ProfitLossForThePeriod"
# (with "The") — a real, easy-to-miss difference, verified against both a
# real IDFC First Bank and a real Infosys filing side by side — so it gets
# its own separate alias row per taxonomy below rather than one shared row.
# Only the adapter's own "One*" context-ID
# convention (this filing's single reported quarter, not a year-to-date or
# prior-period comparative also present in the same file) feeds these — see
# sources/nse_xbrl.py's module docstring.
DEFAULT_METRIC_ALIASES = DEFAULT_METRIC_ALIASES + [
    # Banking ("IFBanking")
    ("nse", "InterestEarned", "interest_earned"),
    ("nse", "InterestExpended", "interest_expended"),
    ("nse", "OtherIncome", "other_income"),
    ("nse", "OperatingExpenses", "operating_expenses"),
    ("nse", "OperatingProfitBeforeProvisionAndContingencies", "operating_profit"),
    ("nse", "ProvisionsOtherThanTaxAndContingencies", "provisions_and_contingencies"),
    ("nse", "ProfitLossFromOrdinaryActivitiesBeforeTax", "profit_before_tax"),
    ("nse", "TaxExpense", "tax"),
    ("nse", "ProfitLossForThePeriod", "net_profit"),
    ("nse", "BasicEarningsPerShareBeforeExtraordinaryItems", "eps"),
    ("nse", "PercentageOfGrossNpa", "gross_npa_percent"),
    ("nse", "PercentageOfNpa", "net_npa_percent"),
    ("nse", "ReturnOnAssets", "return_on_assets_percent"),
    # General Ind-AS corporate ("IFIndAs") — verified against a real Infosys
    # filing (Q1 FY27, both consolidated and standalone). "RevenueFromOperations"
    # (not "Income", which is Revenue + Other Income combined — this app's
    # total_revenue convention is core revenue only, matching Screener's own
    # "Sales", with other_income aliased separately) and "Expenses" (this
    # taxonomy's one combined total-expenses line, no separate operating-vs-
    # non-operating split) are the two where the natural-sounding tag name
    # isn't the right pick. No "operating_profit"-equivalent tag exists in
    # this taxonomy at all (the P&L runs straight from Income to Expenses to
    # ProfitBeforeExceptionalItemsAndTax) — left unmapped rather than
    # subtracting two lines to approximate one, same reasoning already
    # documented above for "proprietary"'s deliberately-unmapped rows.
    ("nse", "RevenueFromOperations", "total_revenue"),
    ("nse", "Expenses", "operating_expenses"),
    ("nse", "DepreciationDepletionAndAmortisationExpense", "depreciation"),
    ("nse", "ProfitBeforeTax", "profit_before_tax"),
    ("nse", "ProfitLossForPeriod", "net_profit"),
    ("nse", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "eps"),
    # Not a real XBRL tag — sources/nse_xbrl.py derives this row_label itself
    # (PaidUpValueOfEquityShareCapital / FaceValueOfEquityShareCapital,
    # verified against real filings on both taxonomies) since neither has a
    # direct shares-outstanding tag at all.
    ("nse", "DerivedSharesOutstanding", "shares_outstanding"),
    # Balance-sheet facts — reported under the "OneI" INSTANT context (a
    # point-in-time snapshot as of the filing's own period end), not "OneD"/
    # "FourD" (durations) like every tag above. Verified against real
    # Infosys (general Ind-AS) and IDFC First Bank (banking) Q4 FY26
    # filings. "Assets" and "CashAndCashEquivalentsCashFlowStatement" are
    # the two tags genuinely shared verbatim across both taxonomy families
    # (verified: same local name, same meaning, in both real filings) —
    # aliased once. Banking has no standalone "Liabilities" tag (it reports
    # "CapitalAndLiabilities", the same total as "Assets" — the balance
    # sheet's other side, not liabilities-excluding-equity) and no split
    # current/noncurrent Investments (Ind-AS's own "NoncurrentInvestments"/
    # "CurrentInvestments" are similarly left unmapped here rather than
    # guessed at as a sum) — both genuinely unmapped, not an oversight.
    ("nse", "Assets", "total_assets"),
    ("nse", "Liabilities", "total_liabilities"),  # Ind-AS only — no banking equivalent
    ("nse", "Equity", "total_shareholders_funds"),  # Ind-AS only — includes non-controlling interest
    ("nse", "EquityShareCapital", "equity_share_capital"),  # Ind-AS
    ("nse", "OtherEquity", "reserves"),  # Ind-AS
    ("nse", "CashAndCashEquivalentsCashFlowStatement", "cash_and_bank"),
    ("nse", "Deposits", "deposits"),  # banking
    ("nse", "Advances", "advances"),  # banking
    ("nse", "Investments", "investments"),  # banking's one combined line
    ("nse", "Borrowings", "borrowings"),  # banking
    ("nse", "Capital", "equity_share_capital"),  # banking
    ("nse", "ReservesAndSurplus", "reserves"),  # banking
]


def ensure_metric_vocabulary(conn: DBConnection) -> None:
    """Seed metrics_dictionary and metric_aliases, leaving existing rows untouched."""
    seed_metric_vocabulary(conn, DEFAULT_METRICS, DEFAULT_METRIC_ALIASES)


def resolve_metric_key(conn: DBConnection, source: str, raw_label: str) -> str | None:
    """Look up the metric_key for a vendor's raw row label, or None if there's no alias yet."""
    row = get_metric_key_for_alias(conn, source, raw_label.strip())
    return row["metric_key"] if row else None


# metrics_dictionary's own default_unit is always expressed in INR terms
# (every metric was defined before multi-country support existed — see
# DEFAULT_METRICS above) — this localizes it for a non-INR company rather
# than adding a second currency-keyed column to the dictionary table, since
# only the currency/scale word changes, never the metric's shape. Units with
# no INR-specific scale word (PERCENT, RATIO, NUMBER, "x") pass through
# unchanged for every currency.
_UNIT_CURRENCY_LOCALIZATIONS: dict[str, dict[str, str]] = {
    "INR_CRORE": {"USD": "USD_MILLION"},
    "INR_LAKH": {"USD": "USD_THOUSAND"},
    "INR": {"USD": "USD"},
}


def _localize_unit(unit: str, currency: str) -> str:
    if currency == "INR":
        return unit
    return _UNIT_CURRENCY_LOCALIZATIONS.get(unit, {}).get(currency, unit)


def _default_unit_for_metric(conn: DBConnection, metric_key: str, currency: str = "INR") -> str:
    row = get_metric_dictionary_entry(conn, metric_key)
    unit = row["default_unit"] if row and row["default_unit"] else "NUMBER"
    return _localize_unit(unit, currency)


def build_observations_from_periods(
    conn: DBConnection,
    *,
    company_id: str,
    source: str,
    source_file: str,
    parser_version: str,
    period_type: str,
    statement_type: str,
    row_label: str,
    period_values: dict[tuple[str, str | None], object],
    currency: str = "INR",
) -> list[NormalizedObservation]:
    """Turn one row (label + {(fiscal_year, quarter): raw_value}) into observations.

    This is the core transform — period keys are already resolved, so it works
    equally for a "Mar-24" text header (build_observations, below) or a real
    date object (sources/screener.py's Data Sheet parser, which is what
    Screener's actual export uses — the pretty per-topic sheets are formula
    views and don't carry values when read programmatically).

    Unrecognized row labels and unparseable values are skipped with a logged
    warning, not raised — one bad row shouldn't abort an entire section
    (README: malformed data is rejected with a warning, never silently accepted).
    The metric's unit comes from metrics_dictionary, localized for `currency`
    (_localize_unit) — a "%" suffix on the raw cell overrides it (a vendor
    sometimes inlines the sign even on a metric whose canonical unit isn't
    PERCENT). currency defaults to INR since every existing adapter
    (Screener, Proprietary) is India-only; sources/yfinance_financials.py is
    the one caller that passes something else.
    """
    metric_key = resolve_metric_key(conn, source, row_label)
    if metric_key is None:
        logger.warning("No metric_alias for source=%s raw_label=%r — skipping row", source, row_label)
        return []

    default_unit = _default_unit_for_metric(conn, metric_key, currency)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    observations: list[NormalizedObservation] = []

    for (fiscal_year, quarter), raw_value in period_values.items():
        try:
            value = parse_numeric(raw_value)
        except NumericParseError as exc:
            logger.warning(
                "Unparseable value for %s %s %s%s: %s — skipping",
                company_id, metric_key, fiscal_year, quarter or "", exc,
            )
            continue
        if value is None:
            continue  # blank cell — no data for this period, not an error

        unit = infer_unit(raw_value, default_unit)
        observations.append(
            NormalizedObservation(
                company_id=company_id,
                metric_key=metric_key,
                period_type=period_type,
                fiscal_year=fiscal_year,
                quarter=quarter,
                statement_type=statement_type,
                value=value,
                unit=unit,
                currency=currency,
                source=source,
                source_file=source_file,
                parser_version=parser_version,
                retrieved_at=retrieved_at,
            )
        )

    return observations


def build_observations(
    conn: DBConnection,
    *,
    company_id: str,
    source: str,
    source_file: str,
    parser_version: str,
    period_type: str,
    statement_type: str,
    row_label: str,
    header_values: dict[str, object],
) -> list[NormalizedObservation]:
    """Turn one wide-format row (label + {period_header: raw_value}) into observations.

    Thin wrapper over build_observations_from_periods() for text period
    headers ("Mar-24") — parses each header, then delegates. Unparseable
    headers (e.g. "TTM") are skipped silently, same as an unrecognized row label.
    """
    period_values: dict[tuple[str, str | None], object] = {}
    for header, raw_value in header_values.items():
        try:
            fiscal_year, quarter = parse_period_header(str(header), period_type)
        except PeriodParseError:
            continue  # e.g. "TTM" — not a period column, skip silently
        period_values[(fiscal_year, quarter)] = raw_value

    return build_observations_from_periods(
        conn,
        company_id=company_id,
        source=source,
        source_file=source_file,
        parser_version=parser_version,
        period_type=period_type,
        statement_type=statement_type,
        row_label=row_label,
        period_values=period_values,
    )
