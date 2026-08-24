"""Numeric parsing and unit inference for raw vendor cell values.

Validates numeric parsing per README (Ingestion Approach by Source ->
Screener): strip commas, handle blanks/dashes as "no value", never guess a
number out of malformed text.
"""

from __future__ import annotations

import re

_BLANK_VALUES = {"", "-", "--", "na", "n/a", "nan", "nm"}
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


class NumericParseError(ValueError):
    """Raised when a raw cell value looks non-blank but isn't parseable as a number."""


def parse_numeric(raw_value: object) -> float | None:
    """Parse a raw cell value into a float, or None if it represents "no data".

    Raises NumericParseError if the value is non-blank but not a valid number
    (per README: malformed data is rejected with a warning, never silently
    accepted) — the caller decides whether that's a skip-and-log or a hard stop.
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)

    text = str(raw_value).strip()
    if text.lower() in _BLANK_VALUES:
        return None

    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()

    text = text.replace(",", "")

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()

    if not _NUMERIC_RE.match(text):
        raise NumericParseError(f"Cannot parse numeric value from: {raw_value!r}")

    value = float(text)
    return -value if negative else value


def infer_unit(raw_value: object, default_unit: str) -> str:
    """Return PERCENT if the raw text carries a "%" sign, else the caller's default."""
    if isinstance(raw_value, str) and raw_value.strip().endswith("%"):
        return "PERCENT"
    return default_unit
