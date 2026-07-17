"""Shop-provider mechanism: normalise a distributor's product page into ShelfOS
component fields (spec: create a component from a shop URL).

Each shop is a small module implementing :class:`ShopProvider`; the registry in
``__init__`` dispatches by URL host. The fuzzy bits (category, parameter values)
are best-effort and are pre-filled into the New Component dialog for the user to
review before creating — so imperfect guesses are corrected, not committed blind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ProductData:
    """A distributor product normalised toward ShelfOS component fields."""

    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    package: str | None = None
    category: str | None = None  # a ShelfOS type name if we could infer one
    datasheet_url: str | None = None
    parameters: list[tuple[str, str]] = field(default_factory=list)  # (label, value)


@runtime_checkable
class ShopProvider(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def fetch(self, url: str) -> ProductData: ...


# Keyword → ShelfOS type name. Order matters: the more specific child types
# (led, mosfet) come before their parents so "LED" doesn't resolve to "diode".
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("resistor", "resistor"),
    ("capacitor", "capacitor"),
    ("inductor", "inductor"),
    ("ferrite", "inductor"),
    ("led", "led"),
    ("diode", "diode"),
    ("mosfet", "mosfet"),
    ("transistor", "transistor"),
    ("connector", "connector"),
    ("crystal", "crystal"),
    ("oscillator", "crystal"),
]


def infer_category(*texts: str | None) -> str | None:
    """Guess a ShelfOS type name from a shop category/description, or None."""
    blob = " ".join(t for t in texts if t).lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in blob:
            return category
    return None


_VALUE_RE = re.compile(r"^[±\s]*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zµΩ]*)")
_MULTIPLIERS = {"p", "n", "u", "k", "M", "G", "m"}


def clean_param_value(raw: str) -> str:
    """Turn a shop value like "10 kOhms" into "10k" so a NUMBER field parses it.

    Keeps the leading number and, if the trailing unit starts with an engineering
    multiplier, that letter (normalising µ→u, K→k). A value it can't confidently
    read (no leading number) is returned stripped, for the user to fix.
    """
    raw = (raw or "").strip()
    match = _VALUE_RE.match(raw)
    if not match:
        return raw
    number, unit = match.group(1), match.group(2)
    mult = ""
    if unit:
        first = {"µ": "u", "K": "k"}.get(unit[0], unit[0])
        if first in _MULTIPLIERS:
            mult = first
    return f"{number}{mult}"
