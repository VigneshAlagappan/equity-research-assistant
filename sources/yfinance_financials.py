"""YFinanceAdapter — pulls a company's annual financial statements directly
from Yahoo Finance (via yfinance, already a dependency for
web/live_quote.py's price lookups) instead of parsing an uploaded file.

The pilot path for non-Indian companies: screener.in (sources/screener.py)
only covers Indian listings, so there's no file-based source for a US
ticker like AAPL. yfinance's Ticker.financials/.balance_sheet/.cashflow give
real reported figures directly, no scraping needed.

Not a SourceAdapter subclass — SourceAdapter.parse(file_path, ...) assumes a
raw file to read, and there isn't one here, the API itself is the source.
See ingestion/pipeline.py::ingest_yfinance_company for the pipeline entry
point (a sibling to ingest_file(), same reasoning ingest_macro_file() is its
own function rather than a branch of ingest_file()).

Row-label -> metric_key mapping goes through metric_aliases like every other
adapter (normalization/financials.py's DEFAULT_METRIC_ALIASES,
source="yfinance") — yfinance's row-label vocabulary is fixed across tickers
(the same ~15 line items regardless of company), verified against a real
AAPL pull, unlike a vendor export's varying template.

Annual statements only for this pilot — yfinance's quarterly frames use
calendar-quarter boundaries that don't line up with a company's actual
fiscal quarters (Apple's fiscal Q1 ends in December, not a calendar
quarter), so mapping them to this app's Q1..Q4 convention would silently
mislabel periods. Left for a follow-up once that mapping is worked out
per-company, not guessed at here.
"""

from __future__ import annotations

import logging
import math
from storage.db_types import DBConnection

import yfinance as yf

from normalization.financials import build_observations_from_periods
from sources.base import NormalizedObservation

logger = logging.getLogger(__name__)

PARSER_VERSION = "yfinance-v1-annual"

# yfinance returns raw currency units for aggregate lines (e.g. 391035000000
# for Apple's FY2024 revenue) -- dividing those matches this app's "value is
# already in the metric's native scale" convention (an INR_CRORE observation
# is already in crore, not raw rupees, so a USD_MILLION observation needs to
# already be in millions). EPS is already dollars-per-share and must NOT be
# divided. Shares outstanding, despite being a plain count, IS divided
# anyway: a per-share ratio elsewhere (book value, EPS-derived-from-net-
# profit, see web/valuation_feed.py) is computed downstream as an aggregate
# divided by shares_outstanding, and that only comes out in real
# dollars-per-share if both sides of the division share the same scale --
# the same reason Indian sources already express shares "in Cr" to match
# reserves/etc. also being in Cr, even though the metric's own unit label is
# plain NUMBER either way.
_UNIT_DIVISOR = 1_000_000
_PER_UNIT_ROW_LABELS = {"Diluted EPS"}


class YFinanceAdapter:
    source_id = "yfinance"

    def __init__(self, conn: DBConnection):
        self._conn = conn

    def fetch(
        self, company_id: str, ticker: str, *, currency: str = "USD", statement_type: str = "consolidated"
    ) -> list[NormalizedObservation]:
        """Fetch and normalize this ticker's annual income statement, balance
        sheet, and cash flow. Returns [] (not an error) if yfinance has
        nothing for this ticker — same "absence isn't an error" rule
        sources/screener.py's blank-cell handling follows."""
        t = yf.Ticker(ticker)
        source_file = f"yfinance:{ticker}"
        observations: list[NormalizedObservation] = []
        for frame in (t.financials, t.balance_sheet, t.cashflow):
            if frame is None or frame.empty:
                continue
            for row_label, series in frame.iterrows():
                divisor = 1 if str(row_label) in _PER_UNIT_ROW_LABELS else _UNIT_DIVISOR
                period_values = {
                    (f"FY{period_end.year}", None): value / divisor
                    for period_end, value in series.items()
                    if value is not None and not (isinstance(value, float) and math.isnan(value))
                }
                if not period_values:
                    continue
                observations.extend(
                    build_observations_from_periods(
                        self._conn,
                        company_id=company_id,
                        source=self.source_id,
                        source_file=source_file,
                        parser_version=PARSER_VERSION,
                        period_type="annual",
                        statement_type=statement_type,
                        row_label=str(row_label),
                        period_values=period_values,
                        currency=currency,
                    )
                )
        if not observations:
            logger.warning("yfinance returned no usable financial-statement data for ticker=%s", ticker)
        return observations
