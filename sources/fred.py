"""FRED (Federal Reserve Economic Data) — the US macro/regulatory data source,
parallel to sources/rbi_indicators.py etc. for India.

Live-fetched from FRED's public CSV endpoint (no API key required, same
"no external services beyond the LLM API" spirit as
sources/yfinance_financials.py's live pull — see
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series_id>), not staged
as a file under data/raw/_macro/fred/ like the CSV convention
sources/macro.py's MacroDataAdapter parses. Mirrors YFinanceAdapter's shape
(a fetch() method, not a SourceAdapter.parse(file_path, ...) — there's no
raw file here, the API itself is the source) rather than the RBI/IITM
bespoke-parser pattern, since FRED's own export is already clean single-
series tabular data, not a multi-sheet workbook or fixed-width text file
needing a bespoke layout parser.

FRED's CSV always reports dates as "YYYY-MM-DD" regardless of the series'
underlying cadence (daily/weekly/monthly/quarterly/annual all use the first
day of the period for anything coarser than daily) — classified as
sources/macro.py's "dated" period_type via infer_period_type(), the same
bucket sources/rbi_dbie_tables.py uses for irregular as-on-this-date
snapshots, rather than guessing a series' cadence from its series_id.
Missing observations are published as "." and skipped, not treated as zero.

See ingestion/pipeline.py::ingest_fred_series for the pipeline entry point
(a sibling to ingest_yfinance_company(), same reasoning ingest_macro_file()
is its own function rather than a branch of ingest_file()).
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request

from normalization.units import NumericParseError, parse_numeric
from sources.macro import MacroNormalizedObservation, MacroPeriodError, infer_period_type

logger = logging.getLogger(__name__)

PARSER_VERSION = "fred-v1-csv"
SOURCE_ID = "fred"

_FETCH_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "Mozilla/5.0 (compatible; GlobalEquityResearchAssistant/1.0)"
_MISSING_VALUE_TOKENS = {"", "."}


def _csv_url(series_id: str) -> str:
    return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def fetch_fred_series(
    series_id: str, *, unit: str, series_key: str | None = None, region: str | None = None
) -> list[MacroNormalizedObservation]:
    """Fetch one FRED series and normalize it into MacroNormalizedObservations.

    unit must be passed explicitly — FRED's CSV export has no unit column
    (e.g. "PERCENT" for FEDFUNDS/DGS10, "INDEX" for CPIAUCSL), same
    "the CSV convention requires a unit column" contract
    sources/macro.py's MacroDataAdapter already enforces for RBI/IMD/etc.
    series_key defaults to the FRED series_id itself (lowercased, to match
    this app's snake_case series_key convention elsewhere) unless overridden.

    Returns [] (not an error) if FRED has nothing for this series_id — same
    "absence isn't an error" rule sources/yfinance_financials.py follows.
    """
    series_key = series_key or series_id.lower()
    source_file = f"fred:{series_id}"

    req = urllib.request.Request(_csv_url(series_id), headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None or "observation_date" not in reader.fieldnames:
        raise ValueError(f"Unexpected FRED CSV shape for series_id={series_id!r}: {reader.fieldnames}")
    value_column = next((name for name in reader.fieldnames if name != "observation_date"), None)
    if value_column is None:
        raise ValueError(f"No value column found in FRED CSV for series_id={series_id!r}")

    observations: list[MacroNormalizedObservation] = []
    for row_index, row in enumerate(reader, start=2):  # header is row 1
        period = (row.get("observation_date") or "").strip()
        if not period:
            logger.warning("%s row %d: blank DATE — skipping", source_file, row_index)
            continue

        try:
            period_type = infer_period_type(period)
        except MacroPeriodError as exc:
            logger.warning("%s row %d: %s — skipping", source_file, row_index, exc)
            continue

        raw_value = row.get(value_column)
        if raw_value is not None and raw_value.strip() in _MISSING_VALUE_TOKENS:
            continue  # FRED's own "no observation this period" marker, not a parse failure
        try:
            value = parse_numeric(raw_value)
        except NumericParseError as exc:
            logger.warning("%s row %d: %s — skipping", source_file, row_index, exc)
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
        logger.warning("FRED returned no usable observations for series_id=%s", series_id)
    return observations
