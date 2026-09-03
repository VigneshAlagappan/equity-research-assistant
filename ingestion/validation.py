"""Structural validation of a NormalizedObservation before it's stored.

Every parser validates required columns, types, dates, currency, units, and
company identity; malformed data is rejected with a warning, never silently
accepted (README: Ingestion Approach by Source). Adapters already handle
per-cell parsing; this is the last, adapter-agnostic checkpoint.
"""

from __future__ import annotations

import math

from sources.base import NormalizedObservation
from sources.macro import MacroNormalizedObservation, infer_period_type

_FISCAL_YEAR_RE_PREFIX = "FY"
_VALID_QUARTERS = {"Q1", "Q2", "Q3", "Q4"}
_VALID_PERIOD_TYPES = {"annual", "quarterly"}
_VALID_STATEMENT_TYPES = {"consolidated", "standalone", None}
_VALID_UNITS = {
    "INR_CRORE", "INR_LAKH", "INR",
    "USD_MILLION", "USD_THOUSAND", "USD", "USD_BILLION",  # non-INR companies — normalization/financials.py's _localize_unit
    "PERCENT", "RATIO", "NUMBER",
}
# weekly/fortnightly/quarterly/dated all use the same "YYYY-MM-DD" period
# string shape (infer_period_type() -> "dated"). "dated" is also a real
# period_type in its own right (sources/rbi_dbie_tables.py) for an
# irregular as-on-this-date snapshot with no fixed recurring cadence at
# all — the other three are a specific, regular cadence whose string alone
# can't distinguish it from the others, so validate_macro_observation
# checks all four against infer_period_type()'s "dated" classification
# rather than an exact match to it.
_DATED_MACRO_PERIOD_TYPES = {"weekly", "fortnightly", "quarterly", "dated"}
_VALID_MACRO_PERIOD_TYPES = {"annual", "monthly"} | _DATED_MACRO_PERIOD_TYPES


def validate_observation(obs: NormalizedObservation) -> list[str]:
    """Return a list of problem descriptions; empty list means the observation is well-formed."""
    problems: list[str] = []

    if not obs.company_id:
        problems.append("company_id is empty")
    if not obs.metric_key:
        problems.append("metric_key is empty")

    if obs.period_type not in _VALID_PERIOD_TYPES:
        problems.append(f"period_type must be one of {sorted(_VALID_PERIOD_TYPES)}, got {obs.period_type!r}")
    elif obs.period_type == "quarterly" and obs.quarter not in _VALID_QUARTERS:
        problems.append(f"quarterly observation has invalid quarter: {obs.quarter!r}")
    elif obs.period_type == "annual" and obs.quarter is not None:
        problems.append(f"annual observation must not carry a quarter, got {obs.quarter!r}")

    if not (
        len(obs.fiscal_year) == 6
        and obs.fiscal_year.startswith(_FISCAL_YEAR_RE_PREFIX)
        and obs.fiscal_year[2:].isdigit()
    ):
        problems.append(f"fiscal_year must look like 'FY2025', got {obs.fiscal_year!r}")

    if obs.statement_type not in _VALID_STATEMENT_TYPES:
        problems.append(
            f"statement_type must be one of {sorted(t for t in _VALID_STATEMENT_TYPES if t)}, "
            f"got {obs.statement_type!r}"
        )

    if obs.unit not in _VALID_UNITS:
        problems.append(f"unit must be one of {sorted(_VALID_UNITS)}, got {obs.unit!r}")

    if not isinstance(obs.value, (int, float)) or isinstance(obs.value, bool) or not math.isfinite(obs.value):
        problems.append(f"value must be a finite number, got {obs.value!r}")

    if not obs.source:
        problems.append("source is empty")
    if not obs.source_file:
        problems.append("source_file is empty")
    if not obs.parser_version:
        problems.append("parser_version is empty")

    return problems


def validate_macro_observation(obs: MacroNormalizedObservation) -> list[str]:
    """Same checkpoint as validate_observation(), for the company-less macro shape."""
    problems: list[str] = []

    if not obs.series_key:
        problems.append("series_key is empty")

    if obs.period_type not in _VALID_MACRO_PERIOD_TYPES:
        problems.append(
            f"period_type must be one of {sorted(_VALID_MACRO_PERIOD_TYPES)}, got {obs.period_type!r}"
        )
    else:
        try:
            inferred = infer_period_type(obs.period)
        except ValueError:
            problems.append(f"period is not a recognizable 'YYYY'/'YYYY-MM'/'YYYY-MM-DD' value: {obs.period!r}")
        else:
            if obs.period_type in _DATED_MACRO_PERIOD_TYPES:
                if inferred != "dated":
                    problems.append(f"period {obs.period!r} does not look like a date (YYYY-MM-DD), expected for period_type {obs.period_type!r}")
            elif inferred != obs.period_type:
                problems.append(f"period {obs.period!r} does not match period_type {obs.period_type!r}")

    if not obs.unit:
        problems.append("unit is empty")

    if not isinstance(obs.value, (int, float)) or isinstance(obs.value, bool) or not math.isfinite(obs.value):
        problems.append(f"value must be a finite number, got {obs.value!r}")

    if not obs.source:
        problems.append("source is empty")
    if not obs.source_file:
        problems.append("source_file is empty")
    if not obs.parser_version:
        problems.append("parser_version is empty")

    return problems
