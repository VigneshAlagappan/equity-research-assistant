"""Parses IITM Pune's long-period rainfall text publications
(data/raw/_macro/iitm/) — plain fixed-width tables, not the
period,value,unit CSV shape sources/macro.py's MacroDataAdapter expects, so
(like sources/rbi_indicators.py) this is its own module.

Two distinct IITM compilations live in that folder, kept as separate
series_key namespaces below since they're independently published, cover
overlapping years, and would otherwise collide on (series_key, region,
period) for the all-India row:

  - The 8 "<n>-<code>.txt" files (1-nmi.txt .. 8-all_ind.txt): one 8-zone
    regionalization ("Parthasarathy"-style), 1813/1826-2006, values already
    in MM. series_key prefix "rainfall_regional_".
  - iitm-regionrf.txt: a different 6-group subdivision regionalization,
    1871-2016, multiple region blocks in one file, values in *10th of mm*
    (divided by 10 here so everything in this module is comparable in MM).
    series_key prefix "rainfall_subdivision_".

Three other files under that folder are deliberately NOT handled here and
raise UnsupportedIitmFileError if passed in:
  - iitm-imr-stn.txt: a station reference table (district/WMO no/lat-lon),
    not a period,value series.
  - NEW-TNREGION.TXT: temperature (not rainfall), in a different
    code+year+flag row layout.
  - 2020_pc_MJJASO.dat: undocumented columns (index, two floats, an
    integer) — meaning isn't clear enough to normalize confidently.

Both handled formats are true fixed-width (not whitespace-delimited):
missing values are blank space, not a placeholder token, so splitting on
whitespace would silently misalign columns. Column boundaries are hardcoded
from the underlying Fortran-style layout rather than derived from each
file's header text, because at least one file (5-wpi.txt) has a header
label typo that shifts its printed column labels by a character without
changing the actual (still consistent) field width.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from normalization.units import NumericParseError, parse_numeric
from sources.macro import MacroNormalizedObservation

logger = logging.getLogger(__name__)

PARSER_VERSION = "iitm-rainfall-v1-fixedwidth"

# "<n>-<code>.txt" regional files: YEAR (4 chars) then 17 columns of 7 chars
# each: Jan..Dec, ANN, JF, MAM, JJAS, OND (annual total precedes the
# seasonal splits).
_REGIONAL_SUFFIXES = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "annual", "jf", "mam", "jjas", "ond",
]
_REGIONAL_STARTS = [4 + 7 * i for i in range(17)]
_REGIONAL_ENDS = [4 + 7 * (i + 1) for i in range(17)]
_REGIONAL_TITLE_RE = re.compile(r"^(.*?)\s+MONTHLY,", re.IGNORECASE)

# iitm-regionrf.txt: YEAR (4 chars), JAN 7 chars, then 15 columns of 6
# chars each: Feb..Dec, JF, MAM, JJAS, OND, ANN (annual total comes last
# here, unlike the regional files above).
_SUBDIVISION_SUFFIXES = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "jf", "mam", "jjas", "ond", "annual",
]
_SUBDIVISION_STARTS = [4, 11, 17, 23, 29, 35, 41, 47, 53, 59, 65, 71, 77, 83, 89, 95, 101]
_SUBDIVISION_ENDS = [11, 17, 23, 29, 35, 41, 47, 53, 59, 65, 71, 77, 83, 89, 95, 101, 107]
_SUBDIVISION_BLOCK_RE = re.compile(r"^\d+\s+(.*?)\s+RAINFALL", re.IGNORECASE)
_SUBDIVISION_UNIT_DIVISOR = 10.0  # source values are in 10th-of-mm

_YEAR_RE = re.compile(r"^\d{4}$")
_REGIONAL_FILENAME_RE = re.compile(r"^\d+-[a-z_]+$", re.IGNORECASE)


class UnsupportedIitmFileError(ValueError):
    """Raised for a file under data/raw/_macro/iitm/ this module doesn't parse."""


def _slugify_region(label: str) -> str | None:
    """"WEST CENTRAL INDIA" -> "west_central_india"; "ALL-INDIA" -> None
    (macro_observations.region convention: NULL means all-India)."""
    label = label.strip()
    if label.upper() in ("ALL-INDIA", "ALL INDIA"):
        return None
    label = label.upper().replace("PENINSIULAR", "PENINSULAR")  # source typo (5-wpi.txt, 6-epi.txt)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    return re.sub(r"_+", "_", slug)


def _parse_value(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return parse_numeric(raw)
    except NumericParseError:
        return None


def _parse_fixed_width_table(
    file_path: Path,
    *,
    series_prefix: str,
    suffixes: list[str],
    starts: list[int],
    ends: list[int],
    region: str | None,
    unit_divisor: float = 1.0,
) -> list[MacroNormalizedObservation]:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    observations: list[MacroNormalizedObservation] = []
    for line in text.splitlines():
        year_field = line[0:4] if len(line) >= 4 else ""
        if not _YEAR_RE.match(year_field):
            continue  # title/header/footer/MEAN/SD/CV rows, and blank lines
        for suffix, start, end in zip(suffixes, starts, ends):
            raw = line[start:end] if end <= len(line) else line[start:] if start < len(line) else ""
            value = _parse_value(raw)
            if value is None:
                continue
            observations.append(
                MacroNormalizedObservation(
                    series_key=f"{series_prefix}{suffix}",
                    period_type="annual",
                    period=year_field,
                    value=value / unit_divisor,
                    unit="MILLIMETRES",
                    region=region,
                    source="iitm",
                    source_file=str(file_path),
                    parser_version=PARSER_VERSION,
                )
            )
    return observations


def parse_regional_series_file(file_path: Path) -> list[MacroNormalizedObservation]:
    """Parse one of the 8 "<n>-<code>.txt" long-period zone files."""
    first_line = file_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    match = _REGIONAL_TITLE_RE.match(first_line)
    if not match:
        raise UnsupportedIitmFileError(f"{file_path}: first line doesn't look like a rainfall series title")
    region = _slugify_region(match.group(1))
    return _parse_fixed_width_table(
        file_path,
        series_prefix="rainfall_regional_",
        suffixes=_REGIONAL_SUFFIXES,
        starts=_REGIONAL_STARTS,
        ends=_REGIONAL_ENDS,
        region=region,
    )


def parse_subdivision_rainfall_file(file_path: Path) -> list[MacroNormalizedObservation]:
    """Parse iitm-regionrf.txt: several region blocks in one file, each
    introduced by a "<n> <REGION NAME> RAINFALL (<years>) ..." header line."""
    text = file_path.read_text(encoding="utf-8", errors="replace")
    observations: list[MacroNormalizedObservation] = []
    current_region: str | None = None
    has_seen_block = False
    for line in text.splitlines():
        block_match = _SUBDIVISION_BLOCK_RE.match(line)
        if block_match:
            current_region = _slugify_region(block_match.group(1))
            has_seen_block = True
            continue
        year_field = line[0:4] if len(line) >= 4 else ""
        if not _YEAR_RE.match(year_field) or not has_seen_block:
            continue
        for suffix, start, end in zip(_SUBDIVISION_SUFFIXES, _SUBDIVISION_STARTS, _SUBDIVISION_ENDS):
            raw = line[start:end] if end <= len(line) else line[start:] if start < len(line) else ""
            value = _parse_value(raw)
            if value is None:
                continue
            observations.append(
                MacroNormalizedObservation(
                    series_key=f"rainfall_subdivision_{suffix}",
                    period_type="annual",
                    period=year_field,
                    value=value / _SUBDIVISION_UNIT_DIVISOR,
                    unit="MILLIMETRES",
                    region=current_region,
                    source="iitm",
                    source_file=str(file_path),
                    parser_version=PARSER_VERSION,
                )
            )
    return observations


def parse_iitm_file(file_path: Path) -> list[MacroNormalizedObservation]:
    """Dispatch on filename convention to the right parser above.

    Raises UnsupportedIitmFileError for the three files in this folder that
    aren't a period,value rainfall series (see module docstring) — callers
    should let that surface rather than silently skipping the file.
    """
    name = file_path.name
    if name == "iitm-regionrf.txt":
        return parse_subdivision_rainfall_file(file_path)
    if _REGIONAL_FILENAME_RE.match(file_path.stem):
        return parse_regional_series_file(file_path)
    raise UnsupportedIitmFileError(
        f"{file_path}: not a recognized IITM rainfall series file "
        f"(expected '<n>-<code>.txt' or 'iitm-regionrf.txt')"
    )
