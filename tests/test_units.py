from __future__ import annotations

import pytest

from normalization.units import NumericParseError, infer_unit, parse_numeric


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1234, 1234.0),
        (1234.5, 1234.5),
        ("1,234", 1234.0),
        ("1,234.56", 1234.56),
        ("18.5%", 18.5),
        ("(12.3)", -12.3),
        ("-", None),
        ("--", None),
        ("", None),
        (None, None),
        ("N/A", None),
        ("  42  ", 42.0),
    ],
)
def test_parse_numeric(raw: object, expected: float | None) -> None:
    assert parse_numeric(raw) == expected


def test_parse_numeric_rejects_garbage() -> None:
    with pytest.raises(NumericParseError):
        parse_numeric("not-a-number")


def test_infer_unit_percent_override() -> None:
    assert infer_unit("18.5%", "INR_CRORE") == "PERCENT"


def test_infer_unit_default_when_no_percent_sign() -> None:
    assert infer_unit("1234", "INR_CRORE") == "INR_CRORE"


def test_infer_unit_non_string_value_uses_default() -> None:
    assert infer_unit(1234, "INR_CRORE") == "INR_CRORE"
