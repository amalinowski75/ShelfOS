"""Engineering-notation parsing and formatting (decision D4).

Numeric parameter values are always stored in base units (ohm, farad, volt, …).
This module converts between those base-unit numbers and the human-friendly
engineering notation used in the UI, for example:

    parse_engineering("4k7")   -> 4700.0
    parse_engineering("100nF") -> 1e-07      (trailing unit symbol ignored)
    format_engineering(4700, "Ω") -> "4.7 kΩ"

Two input styles are supported:

* Suffix notation: ``"4.7k"``, ``"100n"``, ``"2.2M"``.
* RKM / BS 1852 infix notation where the prefix letter also marks the decimal
  point: ``"4k7"`` == ``4.7k``, ``"4R7"`` == ``4.7``, ``"100R"`` == ``100``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = ["UnitParseError", "parse_engineering", "format_engineering"]


class UnitParseError(ValueError):
    """Raised when a value cannot be parsed as an engineering-notation number."""


# Parsing is lenient: it accepts both ``k``/``K`` for kilo and ``r``/``R`` as the
# unity (ohm) marker. ``m`` is always milli and ``M`` always mega.
_PARSE_PREFIXES: dict[str, Decimal] = {
    "f": Decimal("1e-15"),
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "µ": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "r": Decimal(1),
    "R": Decimal(1),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
}

# Formatting uses canonical SI symbols (lowercase k, µ for micro).
_FORMAT_PREFIXES: list[tuple[Decimal, str]] = [
    (Decimal("1e12"), "T"),
    (Decimal("1e9"), "G"),
    (Decimal("1e6"), "M"),
    (Decimal("1e3"), "k"),
    (Decimal(1), ""),
    (Decimal("1e-3"), "m"),
    (Decimal("1e-6"), "µ"),
    (Decimal("1e-9"), "n"),
    (Decimal("1e-12"), "p"),
    (Decimal("1e-15"), "f"),
]

_PREFIX_CHARS = "fpnuµmrRkKMGT"

# "4k7", "2M2", "4R7", "1k0": digits, prefix letter, more digits.
_RKM_RE = re.compile(rf"^([+-]?\d*)\.?(\d*)?([{_PREFIX_CHARS}])(\d+)$")

# "4.7k", "100n", "4700", "10kΩ": number, optional prefix, optional unit tail.
_SUFFIX_RE = re.compile(rf"^([+-]?\d*\.?\d+)\s*([{_PREFIX_CHARS}]?)\s*[a-zA-ZΩμµ%°/]*$")


def parse_engineering(text: str) -> float:
    """Parse an engineering-notation string into a base-unit float.

    Args:
        text: e.g. ``"10k"``, ``"4k7"``, ``"100nF"``, ``"2.2e-3"``.

    Returns:
        The value expressed in base units.

    Raises:
        UnitParseError: If the string is empty or not recognized.
    """
    raw = text.strip()
    if not raw:
        raise UnitParseError("empty value")

    # Plain decimal or scientific notation ("4700", "-5", "2.2e-3").
    try:
        direct = Decimal(raw)
    except InvalidOperation:
        pass
    else:
        if direct.is_finite():
            return float(direct)

    rkm = _RKM_RE.match(raw)
    if rkm and rkm.group(3) in _PARSE_PREFIXES:
        int_part, frac_before, prefix, frac_after = rkm.groups()
        # The prefix letter stands in for the decimal point: "4k7" -> "4.7".
        mantissa_text = f"{int_part or '0'}{frac_before or ''}.{frac_after}"
        return _apply(mantissa_text, prefix)

    suffix = _SUFFIX_RE.match(raw)
    if suffix:
        number, prefix = suffix.group(1), suffix.group(2)
        return _apply(number, prefix)

    raise UnitParseError(f"cannot parse engineering value: {text!r}")


def _select_prefix(magnitude: Decimal) -> tuple[Decimal, str]:
    """Return the largest ``(factor, symbol)`` whose factor is <= ``magnitude``."""
    for factor, symbol in _FORMAT_PREFIXES:
        if magnitude >= factor:
            return factor, symbol
    return _FORMAT_PREFIXES[-1]


def _apply(mantissa_text: str, prefix: str) -> float:
    try:
        mantissa = Decimal(mantissa_text)
    except InvalidOperation as exc:
        raise UnitParseError(f"invalid number: {mantissa_text!r}") from exc
    return float(mantissa * _PARSE_PREFIXES[prefix])


def format_engineering(
    value: float, unit: str = "", *, precision: int = 3, sep: str = " "
) -> str:
    """Format a base-unit value using engineering prefixes.

    Args:
        value: Value in base units.
        unit: Base-unit symbol appended after the prefix (e.g. ``"Ω"``, ``"F"``).
        precision: Maximum number of fractional digits on the mantissa.
        sep: Separator placed between the number and the (prefixed) unit.

    Returns:
        A display string such as ``"4.7 kΩ"`` or ``"100 nF"``.
    """
    if value == 0:
        return f"0{sep}{unit}".rstrip() if unit else "0"

    magnitude = Decimal(str(abs(value)))
    sign = "-" if value < 0 else ""

    factor, symbol = _select_prefix(magnitude)
    mantissa = magnitude / factor
    number = f"{mantissa:.{precision}f}".rstrip("0").rstrip(".")
    suffix = f"{symbol}{unit}"
    if not suffix:
        return f"{sign}{number}"
    return f"{sign}{number}{sep}{suffix}"
