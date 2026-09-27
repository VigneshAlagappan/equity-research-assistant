"""Alpha Vantage commodity price history -- gold/silver spot prices, the
first non-FRED live-fetched macro source (parallel to sources/fred.py's
role for US rates/inflation/employment/etc., see that module's own
docstring for the house style this mirrors: fetch()-style method emitting
MacroNormalizedObservation directly, no SourceAdapter.parse(file_path, ...)
-- there's no raw file, the API response itself is the source, same
reasoning docs/playbook.md's "Adding a macro data source" section (Live-
fetched, pattern B) gives for fred.py).

Chosen after checking two more "obvious" sources first and finding neither
usable live: World Bank's Global Economic Monitor API (the only World Bank
dataset with a real per-series REST interface) has no gold/silver
indicator at all, and the actual World Bank Pink Sheet commodity price
file is a periodically-republished Excel workbook at a URL that changes
every release -- not something to hardcode. Alpha Vantage's
GOLD_SILVER_HISTORY endpoint is a real, stable, keyed API that returns the
same XAUUSD/XAGUSD weekly series back to 2011, so `source_id="alpha_
vantage"` here names the actual data provider, not a nominal upstream.

API shape: GET https://www.alphavantage.co/query?function=GOLD_SILVER_HISTORY
    &symbol=GOLD|SILVER&interval=weekly&apikey=...
Response: {"nominal": "XAUUSD"|"XAGUSD", "data": [{"date": "YYYY-MM-DD",
"price": "1234.56"}, ...]}, newest first. No metadata endpoint (unlike
FRED's separate /series call) -- title/unit are supplied by the caller,
same contract sources/fred.py's fetch_fred_series(unit=...) already uses
for its own CSV export (which also has no unit column).

See ingestion/pipeline.py::ingest_alpha_vantage_commodity_series() for the
pipeline entry point (sibling to ingest_fred_series()).
"""

from __future__ import annotations

import json
import logging
import urllib.request

from normalization.units import NumericParseError, parse_numeric
from sources.macro import MacroNormalizedObservation, MacroPeriodError, infer_period_type

logger = logging.getLogger(__name__)

PARSER_VERSION = "alpha-vantage-commodities-v1-json"
SOURCE_ID = "alpha_vantage"

_FETCH_TIMEOUT_SECONDS = 20.0
_BASE_URL = "https://www.alphavantage.co/query"

#: user-facing asset id (e.g. "GOLD_USD_OZ") -> Alpha Vantage's own `symbol`
#: query param. Only the two assets this app currently tracks -- add an
#: entry here (and nowhere else) for a third commodity Alpha Vantage's
#: GOLD_SILVER_HISTORY endpoint supports.
ASSET_SYMBOLS = {"GOLD_USD_OZ": "GOLD", "SILVER_USD_OZ": "SILVER"}


def commodity_history_url(symbol: str, *, interval: str = "weekly") -> str:
    """Public wrapper, no api_key -- ingestion/pipeline.py's
    ingest_alpha_vantage_commodity_series() records this (without the key)
    as the raw object's source_url (ADR-022), same reasoning
    sources/fred.py::fred_csv_url() gives for its own public wrapper."""
    return f"{_BASE_URL}?function=GOLD_SILVER_HISTORY&symbol={symbol}&interval={interval}"


def fetch_commodity_series_raw(symbol: str, *, api_key: str, interval: str = "weekly") -> bytes:
    """Just the network fetch -- split out so a caller that needs the raw
    bytes themselves (ADR-022's raw/macro/ persistence) doesn't have to
    issue a second HTTP request, same split sources/fred.py's
    fetch_fred_series_raw()/fetch_fred_series() already establishes."""
    url = f"{commodity_history_url(symbol, interval=interval)}&apikey={api_key}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def parse_commodity_json(
    raw_bytes: bytes, asset_id: str, *, unit: str, series_key: str | None = None, region: str | None = None,
) -> list[MacroNormalizedObservation]:
    """The parsing half, extracted so a caller replaying a cataloged
    raw/macro/ object (per ADR-022) can parse without a network call --
    same split as sources/fred.py::parse_fred_csv()."""
    series_key = series_key or asset_id.lower()
    source_file = f"alpha_vantage:{asset_id}"
    payload = json.loads(raw_bytes.decode("utf-8"))

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Alpha Vantage GOLD_SILVER_HISTORY shape for asset_id={asset_id!r}: {payload!r}")

    observations: list[MacroNormalizedObservation] = []
    for row_index, row in enumerate(rows, start=1):
        period = (row.get("date") or "").strip()
        if not period:
            logger.warning("%s row %d: blank date -- skipping", source_file, row_index)
            continue

        try:
            period_type = infer_period_type(period)
        except MacroPeriodError as exc:
            logger.warning("%s row %d: %s -- skipping", source_file, row_index, exc)
            continue

        try:
            value = parse_numeric(row.get("price"))
        except NumericParseError as exc:
            logger.warning("%s row %d: %s -- skipping", source_file, row_index, exc)
            continue
        if value is None:
            continue

        observations.append(
            MacroNormalizedObservation(
                series_key=series_key,
                period_type=period_type,
                period=period,
                value=value,
                unit=unit,
                region=region,
                source=SOURCE_ID,
                source_file=source_file,
                parser_version=PARSER_VERSION,
            )
        )

    if not observations:
        logger.warning("Alpha Vantage returned no usable observations for asset_id=%s", asset_id)
    return observations


def fetch_commodity_series(
    asset_id: str, *, api_key: str, unit: str, series_key: str | None = None, region: str | None = None,
) -> list[MacroNormalizedObservation]:
    """Fetch one commodity's weekly price history and normalize it --
    unchanged public behavior/signature shape as sources/fred.py::
    fetch_fred_series(), composed of fetch_commodity_series_raw() +
    parse_commodity_json(). `asset_id` is this app's own id (e.g.
    "GOLD_USD_OZ"), resolved to Alpha Vantage's `symbol` via ASSET_SYMBOLS.

    Returns [] (not an error) if Alpha Vantage has nothing for this
    asset_id -- same "absence isn't an error" rule sources/fred.py and
    sources/yfinance_financials.py both follow."""
    symbol = ASSET_SYMBOLS.get(asset_id)
    if symbol is None:
        raise ValueError(f"asset_id must be one of {sorted(ASSET_SYMBOLS)}, got {asset_id!r}")
    raw_bytes = fetch_commodity_series_raw(symbol, api_key=api_key)
    return parse_commodity_json(raw_bytes, asset_id, unit=unit, series_key=series_key, region=region)
