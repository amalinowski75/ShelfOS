"""Shop-provider mechanism: normalise a distributor's product page into ShelfOS
component fields (spec: create a component from a shop URL).

Each shop is a small module implementing :class:`ShopProvider`; the registry in
``__init__`` dispatches by URL host. The fuzzy bits (category, parameter values)
are best-effort and are pre-filled into the New Component dialog for the user to
review before creating — so imperfect guesses are corrected, not committed blind.
"""

from __future__ import annotations

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


# Note: parameter values are returned RAW here. Engineering cleaning ("10 kOhms"
# → "10k") happens client-side and ONLY for a NUMBER-typed field, so a matched
# text field (e.g. a marking code starting with digits) isn't silently truncated.
