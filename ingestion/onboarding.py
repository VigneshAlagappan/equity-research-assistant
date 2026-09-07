"""New-company onboarding: register + one-shot data population.

Wraps the exact manual sequence performed by hand, one company at a time,
throughout this project's build-out (e.g. for BlackRock: register_company()
-> ingest_sec_edgar_company() -> ingest_yfinance_company() -> a price
history backfill) into a single call, so Settings > Companies > Add Company
can do in one submit what previously took several CLI/script commands.

Country decides the financials source, same branch NSE (India) vs
SEC EDGAR + yfinance (US) already follow everywhere else in this codebase:
  - country="IN": sources.nse_fetch.refresh_company_filings() against the
    given nse_symbol, then ingest_file() per downloaded filing -- the same
    two-step web/app.py's admin_refresh_company() already runs for an
    existing company.
  - country="US": sources.sec_edgar.get_cik_for_ticker() +
    ingest_sec_edgar_company(), plus ingest_yfinance_company() for the
    metrics SEC's XBRL facts don't carry -- the same pairing
    scripts/batch_fetch_sec_edgar.py's own docstring and this session's
    real onboarding of every US company on file already used.

Price history (both countries) is a fresh fetch_daily_bars() pull at
PRICE_BACKFILL_PERIOD, not the daily jobs' trailing-5-day top-up --
scripts/backfill_price_history.py's own reasoning: a brand-new company has
no rows at all yet, so "All-Time Range" needs real depth from the start.

Every step is best-effort and independently recorded (OnboardStep) rather
than all-or-nothing: a company can be legitimately registered even if, say,
SEC EDGAR has nothing filed yet, or yfinance has no price history for a
recent IPO -- the caller (web/app.py's admin_add_company route) surfaces
each step's own outcome rather than failing the whole request over one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from companies.registry import register_company
from ingestion.pipeline import ingest_file, ingest_sec_edgar_company, ingest_yfinance_company
from normalization.companies import normalize_company_id
from sources.nse_fetch import NSEFetchError, refresh_company_filings
from sources.sec_edgar import SECFetchError, get_cik_for_ticker
from sources.yfinance_company_lookup import CompanyLookupError, lookup_company_profile
from sources.yfinance_prices import fetch_daily_bars
from storage.db_types import DBConnection
from storage.price_repository import upsert_daily_bars

# A first pull needs real depth (see module docstring) -- matches
# scripts/backfill_price_history.py's own default choice for a fresh
# backfill rather than the daily jobs' "5d" top-up window.
PRICE_BACKFILL_PERIOD = "10y"


@dataclass
class OnboardStep:
    label: str
    ok: bool
    detail: str


@dataclass
class OnboardResult:
    company_id: str
    steps: list[OnboardStep] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(step.ok for step in self.steps)


def onboard_company(
    main_conn: DBConnection,
    price_conn: DBConnection,
    raw_dir: Path,
    *,
    company_id: str,
    legal_name: str,
    display_name: str,
    country: str = "IN",
    currency: str = "INR",
    fiscal_year_end_month: int | None = None,
    nse_symbol: str | None = None,
    bse_code: str | None = None,
    isin: str | None = None,
    website: str | None = None,
    macro_economic_sector: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    basic_industry: str | None = None,
    listed_date: str | None = None,
) -> OnboardResult:
    # Same "unset means the country's usual default" rule as main.py's
    # cmd_add_company: March close for India, calendar year for the US.
    if fiscal_year_end_month is None:
        fiscal_year_end_month = 12 if country == "US" else 3

    company_id = register_company(
        main_conn,
        company_id,
        legal_name,
        display_name,
        nse_symbol=nse_symbol,
        bse_code=bse_code,
        isin=isin,
        country=country,
        currency=currency,
        fiscal_year_end_month=fiscal_year_end_month,
        website=website,
        macro_economic_sector=macro_economic_sector,
        sector=sector,
        industry=industry,
        basic_industry=basic_industry,
        listed_date=listed_date,
    )
    result = OnboardResult(company_id=company_id)
    result.steps.append(OnboardStep("Register", True, f"Registered {company_id} ({country})."))

    price_ticker: str | None
    if country == "US":
        price_ticker = company_id
        try:
            cik = get_cik_for_ticker(company_id)
            if cik is None:
                raise SECFetchError(f"could not resolve a SEC CIK for ticker {company_id!r}")
            r = ingest_sec_edgar_company(main_conn, company_id, cik, currency=currency)
            result.steps.append(OnboardStep(
                "SEC EDGAR financials", True,
                f"cik={cik} parsed={r.parsed_count} inserted={r.inserted_count} reconciled={r.reconciled_count}",
            ))
        except Exception as exc:  # noqa: BLE001 -- record and keep going, other steps are independent
            result.steps.append(OnboardStep("SEC EDGAR financials", False, str(exc)))

        try:
            r = ingest_yfinance_company(main_conn, company_id, company_id, currency=currency)
            result.steps.append(OnboardStep(
                "Yahoo Finance financials", True,
                f"parsed={r.parsed_count} inserted={r.inserted_count} reconciled={r.reconciled_count}",
            ))
        except Exception as exc:  # noqa: BLE001
            result.steps.append(OnboardStep("Yahoo Finance financials", False, str(exc)))
    else:
        price_ticker = nse_symbol
        if not nse_symbol:
            result.steps.append(OnboardStep("NSE financials", False, "skipped -- no NSE symbol given"))
        else:
            dest_dir = raw_dir / company_id / "nse"
            try:
                fetch_result = refresh_company_filings(nse_symbol, dest_dir)
                reconciled = 0
                for path in fetch_result.downloaded_files:
                    # statement_type is encoded in the filename -- see
                    # admin_refresh_company's identical logic in web/app.py.
                    statement_type = path.stem.split("_")[1]
                    ingest_result = ingest_file(
                        main_conn, path, company_id=company_id, source_id="nse", statement_type=statement_type,
                    )
                    reconciled += ingest_result.reconciled_count
                if fetch_result.downloaded_files:
                    detail = f"downloaded {len(fetch_result.downloaded_files)} filing(s), {reconciled} metric/period reconciled"
                else:
                    detail = "no filings found yet on NSE for this symbol"
                result.steps.append(OnboardStep("NSE financials", True, detail))
            except NSEFetchError as exc:
                result.steps.append(OnboardStep("NSE financials", False, str(exc)))

    if not price_ticker:
        result.steps.append(OnboardStep("Price history", False, "skipped -- no ticker available"))
    else:
        try:
            bars = fetch_daily_bars(price_ticker, period=PRICE_BACKFILL_PERIOD, country=country)
            if not bars:
                result.steps.append(OnboardStep("Price history", False, "no price data returned"))
            else:
                upsert_daily_bars(
                    price_conn,
                    (
                        {
                            "company_id": company_id,
                            "trade_date": bar.trade_date,
                            "open_": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }
                        for bar in bars
                    ),
                )
                result.steps.append(OnboardStep(
                    "Price history", True, f"{len(bars)} bars, {bars[0].trade_date}..{bars[-1].trade_date}",
                ))
        except Exception as exc:  # noqa: BLE001
            result.steps.append(OnboardStep("Price history", False, str(exc)))

    return result


def onboard_new_company(
    main_conn: DBConnection,
    price_conn: DBConnection,
    raw_dir: Path,
    *,
    company_id: str,
    country: str = "IN",
) -> OnboardResult:
    """The simple path: given just a ticker + country, look up the
    company's name/currency/sector/industry/website from Yahoo Finance
    (sources/yfinance_company_lookup.py) and hand everything to
    onboard_company() -- Settings > Companies > Add Company's whole form,
    end to end. Raises CompanyLookupError if Yahoo Finance doesn't
    recognize the ticker/country pairing at all, before anything is written
    to the database -- registering a company under a name nobody can
    verify would be worse than just failing up front."""
    company_id = normalize_company_id(company_id)
    profile = lookup_company_profile(company_id, country)

    result = onboard_company(
        main_conn,
        price_conn,
        raw_dir,
        company_id=company_id,
        legal_name=profile.legal_name,
        display_name=profile.display_name,
        country=country,
        currency=profile.currency,
        nse_symbol=company_id if country == "IN" else None,
        website=profile.website,
        sector=profile.sector,
        industry=profile.industry,
    )
    result.steps.insert(0, OnboardStep(
        "Lookup", True,
        f"Yahoo Finance: {profile.legal_name!r} ({profile.currency}, sector={profile.sector or '—'})",
    ))
    return result
