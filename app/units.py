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

import math
import re
from decimal import Decimal, InvalidOperation

__all__ = ["UnitParseError", "parse_engineering", "format_engineering"]


class UnitParseError(ValueError):
    """Raised when a value cannot be parsed as an engineering-notation number."""


# Multiplier for each accepted prefix letter, plus ``r``/``R`` as the RKM
# ohm-unity marker. Uppercase aliases are added only where the uppercase letter
# is not another *prefix* in this set, so entering values stays forgiving without
# guessing across a real prefix collision:
#   * ``N`` = nano, ``U`` = micro, ``P`` = pico — no other prefix uses these
#     letters (peta is unsupported), so folding is unambiguous.
#   * ``m``/``M`` stay milli/mega — both are real, distinct prefixes.
#   * ``f``/``g``/``t`` are NOT folded: ``F`` is the far more common farad unit,
#     ``G``/``T`` already mean giga/tera and ``g``/``t`` read as gram/tonne.
#     ``k``/``K`` both mean kilo (kept from earlier behaviour).
# The trailing unit symbol is ignored, not checked against the parameter's unit,
# so e.g. ``"100N"`` is taken as 100 nano even though N is also newton — accepted
# in this electronic-component domain where these letters are prefixes in practice.
_PARSE_PREFIXES: dict[str, Decimal] = {
    "f": Decimal("1e-15"),
    "p": Decimal("1e-12"),
    "P": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "N": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "U": Decimal("1e-6"),
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

# Every accepted prefix letter, for the regex character classes below.
_PREFIX_CHARS = "".join(sorted(symbol for symbol in _PARSE_PREFIXES if symbol))

# Characters allowed in a trailing unit symbol (ignored when parsing). Both the
# Greek capital omega (U+03A9) and the legacy ohm sign (U+2126) are accepted.
_UNIT_TAIL = "a-zA-ZΩΩμµ%°/"

# "4k7", "2M2", "4R7", "1k0": digits, prefix letter, more digits, optional unit.
_RKM_RE = re.compile(
    rf"^([+-]?\d*)\.?(\d*)?([{_PREFIX_CHARS}])(\d+)\s*[{_UNIT_TAIL}]*$"
)

# "4.7k", "100n", "4700", "10kΩ": number, optional prefix, optional unit tail.
_SUFFIX_RE = re.compile(
    rf"^([+-]?\d*\.?\d+)\s*([{_PREFIX_CHARS}]?)\s*[{_UNIT_TAIL}]*$"
)


def parse_engineering(text: str) -> float:
    """Parse an engineering-notation string into a base-unit float.

    Accepts suffix (``"4.7k"``) and RKM/BS-1852 infix (``"4k7"``) notation, plain
    or scientific numbers, and a trailing unit symbol in any case — the unit
    itself is ignored, values are returned in base units. The suffix form also
    tolerates a space before the prefix (``"4.7 k"``). Uppercase prefixes are
    folded where unambiguous (``"100NF"`` and ``"100PF"`` read as nano/pico), but
    ``m``/``M`` stay milli/mega.

    Raises:
        UnitParseError: If the string is empty, unrecognized, or out of the
            representable ``float`` range (e.g. ``"1e400"``).
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
            return _finite(text, float(direct))

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
    return _finite(mantissa_text, float(mantissa * _PARSE_PREFIXES[prefix]))


def _finite(text: str, result: float) -> float:
    """Guard against a magnitude that overflows ``float`` to ``inf``/``nan``.

    ``Decimal`` happily holds ``1e400``, but ``float(...)`` of it is ``inf``; a
    non-finite value must never reach storage or JSON.
    """
    if not math.isfinite(result):
        raise UnitParseError(f"value out of range: {text!r}")
    return result


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
