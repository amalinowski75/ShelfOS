"""Tests for engineering-notation parsing and formatting (app.units)."""

from __future__ import annotations

import math

import pytest
from app.units import UnitParseError, format_engineering, parse_engineering


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4700", 4700.0),
        ("4.7", 4.7),
        ("0.001", 0.001),
        ("2.2e-3", 0.0022),
        ("-5", -5.0),
        # Suffix notation.
        ("10k", 10_000.0),
        ("4.7k", 4700.0),
        ("100n", 1e-7),
        ("2.2M", 2_200_000.0),
        ("10u", 1e-5),
        ("10µ", 1e-5),
        ("1m", 1e-3),
        ("100R", 100.0),
        # RKM / BS 1852 infix notation.
        ("4k7", 4700.0),
        ("2M2", 2_200_000.0),
        ("4R7", 4.7),
        ("1k0", 1000.0),
        # Trailing unit symbol is ignored.
        ("100nF", 1e-7),
        ("10kΩ", 10_000.0),
        ("10kohm", 10_000.0),
    ],
)
def test_parse_engineering(text: str, expected: float) -> None:
    assert math.isclose(parse_engineering(text), expected, rel_tol=1e-12)


@pytest.mark.parametrize("text", ["", "   ", "abc", "1..2", "k", "4x7"])
def test_parse_engineering_rejects_invalid(text: str) -> None:
    with pytest.raises(UnitParseError):
        parse_engineering(text)


def test_milli_and_mega_are_case_sensitive() -> None:
    assert parse_engineering("1m") == 1e-3
    assert parse_engineering("1M") == 1e6


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (4700, "Ω", "4.7 kΩ"),
        (1e-7, "F", "100 nF"),
        (2_200_000, "Ω", "2.2 MΩ"),
        (100, "Ω", "100 Ω"),
        (0.0033, "F", "3.3 mF"),
        (0, "F", "0 F"),
        (1000, "", "1 k"),
        (-4700, "Ω", "-4.7 kΩ"),
    ],
)
def test_format_engineering(value: float, unit: str, expected: str) -> None:
    assert format_engineering(value, unit) == expected


def test_parse_format_roundtrip() -> None:
    for text in ["4k7", "100n", "2.2M", "330"]:
        value = parse_engineering(text)
        formatted = format_engineering(value)
        assert math.isclose(parse_engineering(formatted), value, rel_tol=1e-12)


def test_format_below_smallest_prefix_falls_back_to_femto() -> None:
    """A magnitude smaller than every SI prefix uses the smallest one (femto)."""
    assert format_engineering(1e-18, "F").endswith("fF")
