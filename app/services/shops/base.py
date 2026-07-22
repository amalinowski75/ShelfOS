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
    # The shop's OWN category text, kept verbatim alongside the inferred type. It
    # often carries facts the description omits — TME files a 100nF part under "MLCC
    # SMD capacitors" while its description never says SMD — so the dialog folds it
    # into the same best-effort inference it runs over the description.
    shop_category: str | None = None
    datasheet_url: str | None = None
    parameters: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    # The product page this came from, when there was one. Set by the registry, not
    # by a provider: with a scan the URL is whatever the code carried (a TME QR wraps
    # it in other tokens), so the client can't re-derive it from what it sent.
    source_url: str | None = None
    # True when the shop's API contributed nothing and the fields were read straight
    # off the scanned label — the dialog says so instead of implying a full import.
    from_label_only: bool = False


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
