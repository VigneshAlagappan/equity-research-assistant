"""MacroDataAdapter — parses non-company macro/regulatory series (RBI, IMD, ...).

Deliberately not a SourceAdapter (sources/base.py): that interface's parse()
requires a company_id, and macro series have none — forcing one through
would misrepresent what this data is. MacroNormalizedObservation mirrors
NormalizedObservation's shape (metric/period/value/unit/provenance) minus
company_id, plus an optional region for series that aren't all-India.

Expected CSV shape — one file per series, columns `period,value,unit` and
an optional `region` (blank/omitted = all-India):

    period,value,unit
    2015,1108.9,MILLIMETRES
    2016,1142.3,MILLIMETRES

series_key is inferred from the filename stem (data/raw/_macro/imd/rainfall_index.csv
-> "rainfall_index") unless overridden — same "read the path convention, don't
guess from content" principle detect_from_path() already uses for company_id/source_id.

period must be "YYYY" (annual) or "YYYY-MM" (monthly) — no fiscal-year headers
here, unlike Screener: RBI/IMD publish by calendar year, not India's Apr-Mar
fiscal year, and forcing that mapping onto rainfall/repo-rate data would just
be wrong, not merely inconsistent.

A third period shape, "YYYY-MM-DD", covers weekly/fortnightly/quarterly data
(sources/rbi_indicators.py, sources/rbi_dbie_tables.py) — the exact reported
date, since "YYYY-MM-DD" can't distinguish weekly from fortnightly from
quarterly by shape alone; infer_period_type() only confirms the string looks
like a date ("dated"), the caller is responsible for the actual cadence
(ingestion/validation.py's validate_macro_observation cross-checks against
it accordingly).
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from normalization.units import NumericParseError, parse_numeric

logger = logging.getLogger(__name__)

PARSER_VERSION = "macro-v1-csv"

#: source_id values seeded in config.settings.DEFAULT_SOURCES for macro data
#: — mirrors ADAPTER_CLASSES' role for company sources (ingestion/detector.py),
#: a fixed set validated against rather than trusting whatever folder name
#: shows up under data/raw/_macro/.
#:
#: "mfin" is the one entry here that never actually reaches this module's
#: CSV parsing — its content (industry-body guidance PDFs, not a numeric
#: series) has no period/value/unit shape to parse. It's included in this
#: set only so data/raw/_macro/mfin/ is a recognized path (detect_macro_
#: source_from_path doesn't reject it) for archiving those PDFs alongside
#: the other macro sources; nothing calls ingest_macro_file() on them.
MACRO_SOURCE_IDS = frozenset({"rbi", "imd", "iitm", "mospi", "irda", "mfin"})

_ANNUAL_RE = re.compile(r"^\d{4}$")
_MONTHLY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATED_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class MacroPeriodError(ValueError):
    """Raised when a period column value isn't "YYYY", "YYYY-MM", or "YYYY-MM-DD"."""


def infer_period_type(period: str) -> str:
    """"YYYY" -> "annual", "YYYY-MM" -> "monthly", "YYYY-MM-DD" -> "dated"
    (weekly/fortnightly/quarterly all share this shape — see module
    docstring). Raises MacroPeriodError for anything else."""
    period = period.strip()
    if _ANNUAL_RE.match(period):
        return "annual"
    if _MONTHLY_RE.match(period):
        return "monthly"
    if _DATED_RE.match(period):
        return "dated"
    raise MacroPeriodError(f"period must look like 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD', got {period!r}")


@dataclass(frozen=True)
class MacroNormalizedObservation:
    """One (series, region, period) macro data point from a single source, pre-storage.

    Mirrors financial_observations columns, minus the auto-assigned
    observation_id and the created_at timestamp the pipeline stamps on insert
    — same relationship NormalizedObservation has to financial_observations.
    """

    series_key: str
    period_type: str  # "annual" | "monthly"
    period: str  # "2015" | "2015-06"
    value: float
    unit: str
    source: str  # source_id, e.g. "rbi", "imd"
    source_file: str
    parser_version: str
    region: str | None = None
    source_url: str | None = None
    retrieved_at: str = ""  # ISO-8601; filled by the caller (pipeline stamps if blank)


class MacroDataAdapter:
    """One instance parses for one source_id (rbi/imd/mospi/irda) — unlike
    ScreenerAdapter, source_id isn't fixed per class, since the same simple
    CSV shape serves several distinct providers rather than one vendor
    format needing its own class each."""

    def __init__(self, source_id: str):
        if source_id not in MACRO_SOURCE_IDS:
            raise ValueError(f"source_id must be one of {sorted(MACRO_SOURCE_IDS)}, got {source_id!r}")
        self.source_id = source_id

    def parse(
        self, file_path: Path, series_key: str | None = None, **kwargs: object
    ) -> list[MacroNormalizedObservation]:
        """Parse one CSV file into MacroNormalizedObservations.

        series_key defaults to the filename stem — pass it explicitly to
        override (e.g. two files legitimately sharing a stem)."""
        series_key = series_key or file_path.stem
        source_file = str(file_path)

        with file_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "period" not in reader.fieldnames or "value" not in reader.fieldnames:
                raise ValueError(
                    f"{file_path} must have 'period' and 'value' columns (got {reader.fieldnames})"
                )
            has_unit_column = "unit" in reader.fieldnames
            has_region_column = "region" in reader.fieldnames

            observations: list[MacroNormalizedObservation] = []
            for row_index, row in enumerate(reader, start=2):  # header is row 1
                period = (row.get("period") or "").strip()
                if not period:
                    logger.warning("%s row %d: blank period — skipping", file_path, row_index)
                    continue

                try:
                    period_type = infer_period_type(period)
                except MacroPeriodError as exc:
                    logger.warning("%s row %d: %s — skipping", file_path, row_index, exc)
                    continue

                try:
                    value = parse_numeric(row.get("value"))
                except NumericParseError as exc:
                    logger.warning("%s row %d: %s — skipping", file_path, row_index, exc)
                    continue
                if value is None:
                    logger.warning("%s row %d: blank value — skipping", file_path, row_index)
                    continue

                unit = (row.get("unit") or "").strip() if has_unit_column else ""
                if not unit:
                    logger.warning("%s row %d: missing unit — skipping", file_path, row_index)
                    continue

                region = (row.get("region") or "").strip() if has_region_column else ""

                observations.append(
                    MacroNormalizedObservation(
                        series_key=series_key,
                        period_type=period_type,
                        period=period,
                        value=value,
                        unit=unit,
                        region=region or None,
                        source=self.source_id,
                        source_file=source_file,
                        parser_version=PARSER_VERSION,
                    )
                )

        return observations
