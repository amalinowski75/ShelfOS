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


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "1..2", "k", "4x7", "nan", "inf", "-inf", "1e400", "1e400k"],
)
def test_parse_engineering_rejects_invalid(text: str) -> None:
    # "1e400" is a finite Decimal but overflows float to inf -> must be rejected.
    with pytest.raises(UnitParseError):
        parse_engineering(text)


def test_milli_and_mega_are_case_sensitive() -> None:
    assert parse_engineering("1m") == 1e-3
    assert parse_engineering("1M") == 1e6


def test_prefix_case_folding() -> None:
    """Uppercase is accepted for prefixes whose uppercase form is free
    (N=nano, U=micro, P=pico), but m/M stay milli/mega."""
    assert parse_engineering("1N") == 1e-9
    assert parse_engineering("2.2U") == 2.2e-6
    assert parse_engineering("100P") == 100e-12
    assert parse_engineering("10K") == 10_000.0
    # milli vs mega stays case-sensitive (both are real prefixes).
    assert parse_engineering("1m") == 1e-3
    assert parse_engineering("1M") == 1e6


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Both notations, spaces optional, with and without a trailing unit.
        ("4k7", 4700.0),
        ("4.7k", 4700.0),
        ("4.7 k", 4700.0),
        ("4k7Ω", 4700.0),  # RKM infix + trailing unit symbol
        ("4.7kΩ", 4700.0),
        ("100nF", 1e-7),
        ("100 nf", 1e-7),
        ("220uF", 220e-6),
        # Uppercase prefixes folded where the case is free (Variant B).
        ("100NF", 1e-7),
        ("100PF", 1e-10),
        ("2.2U", 2.2e-6),
        ("4N7", 4.7e-9),
        # A common unit letter is not mistaken for a prefix.
        ("100F", 100.0),
        # Multi-character unit.
        ("10kHz", 10_000.0),
        ("10 khz", 10_000.0),
        # Signed RKM and suffix forms.
        ("-4k7", -4700.0),
        ("-2.2k", -2200.0),
    ],
)
def test_parse_forgiving_notation(text: str, expected: float) -> None:
    assert math.isclose(parse_engineering(text), expected, rel_tol=1e-12)


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
