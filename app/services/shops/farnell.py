"""element14 (Farnell / Newark / CPC) provider (spec: create a component from a
shop URL).

The closest sibling to ``mouser``: one API key as a query parameter, one GET, a flat
list of attributes. Two things are its own.

**It is keyed by an order code, not only by an MPN.** Every element14 product URL
ends ``/dp/<order code>``, and ``term=id:<order code>`` resolves that to exactly one
product — no MPN ambiguity at all. The MPN path is still there for a scan or an
invoice line that has nothing else.

**One MPN routinely returns several products**, because element14 lists each
packaging variant separately (3367839 Cut Tape and 3367839RL Re-Reel are the same
part). They are distinguished by the ``isCanonical`` attribute, which is the last
tiebreaker below.

Responses are asked for as JSON. The API speaks XML too — and the documented
examples are XML — but nothing in this repo parses XML, and a parser dependency for
one distributor is not worth carrying.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError
from app.services.shops.base import (
    ProductData,
    ShopLookupMiss,
    fetch_first_match,
    infer_category,
    manufacturer_matches,
)

_API_URL = "https://api.element14.com/catalog/products"

# Fixed API fields that ride along in `attributes` beside the real per-category
# specs: customs/export codes, compliance flags, and element14's own bookkeeping.
# Named explicitly rather than caught by a rule like "labels starting lower-case are
# internal" — that rule is inferred from one part, and when it is wrong it drops a
# real specification without a trace. MSL and SVHC are deliberately NOT here: they
# are facts about the part that someone may well want as a parameter.
_INTERNAL_ATTRIBUTES = frozenset(
    {
        "tariffcode",
        "eueccn",
        "useccn",
        "rohscompliant",
        "rohsphthalatescompliant",
        "hazardous",
        "iscanonical",
        "producttraceability",
    }
)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unbalanced-bracket IPv6 literal
        return ""


def _redact(text: str) -> str:
    """Never let the API key ride out in a message, however unlikely."""
    key = config.FARNELL_API_KEY
    return text.replace(key, "***") if key else text


def _merge_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object hook that keeps EVERY value of a repeated key, as a list.

    element14's JSON is an XML document run through a converter that never learned
    about arrays, so a repeated element becomes a repeated KEY:

        "categories": {"name": "LDO Voltage Regulators",
                       "path": "Semiconductors - ICs",
                       "path": "Power Management ICs - PMIC", …}

    A plain ``json.loads`` keeps only the last of those and drops the rest without a
    word — and the ancestors are the half that carries the useful words ("Resistors"
    for a resistor, where the leaf is a marketing name). So collect them instead.

    Only keys that actually repeat become lists; everything else is untouched, and a
    key whose single value is already an array stays that one array.
    """
    merged: dict[str, Any] = {}
    repeated: set[str] = set()
    for key, value in pairs:
        if key not in merged:
            merged[key] = value
        elif key in repeated:
            merged[key].append(value)
        else:
            repeated.add(key)
            merged[key] = [merged[key], value]
    return merged


def _as_list(value: object) -> list[Any]:
    """A repeated node as a list, whatever the encoder did with a single one.

    Live, ``products``/``attributes``/``datasheets`` are proper arrays even with one
    entry, while a repeated key rescued by ``_merge_duplicate_keys`` is a list only
    when it really did repeat — a category with one path leaves a bare string.
    Normalising here keeps that difference out of every call site.
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: object) -> str:
    """A scalar field as clean text ("" for absent); numbers arrive unquoted."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _attributes(product: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(label, value, unit)`` for every attribute that has a label and a value."""
    rows: list[tuple[str, str, str]] = []
    for attr in _as_list(product.get("attributes")):
        if not isinstance(attr, dict):
            continue
        label = _text(attr.get("attributeLabel"))
        value = _text(attr.get("attributeValue"))
        if label and value:
            rows.append((label, value, _text(attr.get("attributeUnit"))))
    return rows


def _attribute(product: dict[str, Any], label: str) -> str:
    wanted = label.casefold()
    for name, value, _unit in _attributes(product):
        if name.casefold() == wanted:
            return value
    return ""


def _specifications(
    attributes: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """The attributes that describe the PART, with the API's bookkeeping removed.

    Everything that reads attributes goes through here, so the parameters and the
    package can never disagree about which of them are real.
    """
    return [row for row in attributes if row[0].casefold() not in _INTERNAL_ATTRIBUTES]


def _parameters(attributes: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Attributes as ``(label, value)`` for the matching engine.

    The unit is folded back INTO the value where element14 sends it separately:
    "Output Current Max" arrives as value 150 with unit mA, and a bare "150" handed
    to a definition measured in amps is wrong by a factor of a thousand. element14
    also emits a display twin of some attributes ("Output Voltage - Nom" = "3.3V"
    beside "Output Voltage Nom" = 3.3 V); keeping both is harmless, since the engine
    fills a definition from the first label that names it and skips it thereafter.
    """
    return [
        (label, f"{value} {unit}" if unit else value)
        for label, value, unit in attributes
    ]


def _package(attributes: list[tuple[str, str, str]]) -> str | None:
    """The component's case, which element14 states as an attribute.

    Worth pulling out into ``ProductData.package`` rather than leaving among the
    parameters: the matching engine resolves a package from that field or from the
    description, and never from the attributes — so "IC Case / Package: WDFN-EP"
    would otherwise be dropped for a part whose description doesn't repeat it.

    element14 labels it per category, and the label always ENDS in the thing it
    names: "IC Case / Package", "Resistor Case Style", "Transistor Case". So the
    match is anchored at the end rather than looking for the word anywhere, which
    would also take "Package Type", "Package Quantity" and "Base Package Number" —
    a quantity or a base number silently becoming the component's case, and
    whichever came first in element14's own attribute order winning.

    "Packaging" — the reel or tape the part ships on, not its body — needs no guard
    of its own either way: "package" is not a substring of "packaging" (they part
    company at the seventh letter). "Case Height" is a dimension of the case and
    ends in neither shape.
    """
    for label, value, _unit in attributes:
        low = label.casefold()
        if low.endswith(("package", "case")) or "case style" in low:
            return value
    return None


def _description(display_name: str, brand: str, mpn: str) -> str | None:
    """``"ONSEMI - NCP730BMT330TBG - LDO, FIXED, 3.3V…"`` → the part after the MPN.

    Only when those first two segments really are the brand and the part number —
    otherwise the whole string stands, since a description of its own may well
    contain " - ".
    """
    parts = [p.strip() for p in display_name.split(" - ")]
    if (
        len(parts) >= 3
        and brand
        and mpn
        and parts[0].casefold() == brand.casefold()
        and parts[1].casefold() == mpn.casefold()
    ):
        return " - ".join(parts[2:]) or None
    return display_name or None


def _shop_category(product: dict[str, Any]) -> str | None:
    """The category name and the path that leads to it, as one string.

    The path is included because the engine matches TYPE rules over a blob of
    category + description: "LDO Voltage Regulators" alone names no ShelfOS type,
    while the path above it ("Semiconductors - ICs") is where a rule can bite.
    """
    parts: list[str] = []
    for category in _as_list(product.get("categories")):
        if not isinstance(category, dict):
            continue
        parts.extend(_text(p) for p in _as_list(category.get("path")))
        parts.append(_text(category.get("name")))
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " / ".join(seen) or None


def _datasheet_url(product: dict[str, Any]) -> str | None:
    for sheet in _as_list(product.get("datasheets")):
        if isinstance(sheet, dict) and _text(sheet.get("url")):
            return _text(sheet.get("url"))
    return None


class FarnellProvider:
    name = "Farnell"

    def matches(self, url: str) -> bool:
        # element14 sells under three brands and a country site per market
        # (uk.farnell.com, pl.farnell.com, cpc.farnell.com, www.element14.com,
        # www.element14.cn, canada.newark.com …), so match the brand as a domain
        # LABEL rather than listing hosts. A false match is harmless: the lookup
        # only ever calls the fixed api.element14.com and would report no product.
        labels = _host(url).split(".")
        if len(labels) <= 1:
            return False
        return any(brand in labels[:-1] for brand in ("farnell", "element14", "newark"))

    def product_url(self, part_number: str) -> str:
        # No stable direct-product path from an order code alone, so link the search
        # — an exact order code lands on the single product. The host comes from
        # config rather than being a literal like the other providers': it is still
        # not user input, and sending someone to a store they have no account with
        # is the worse failure.
        return f"https://{config.FARNELL_STORE}/search?st={quote(part_number)}"

    def fetch(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> ProductData:
        """Look up the product an element14 URL points at.

        Every product URL ends ``/dp/<order code>``, and the order code is
        element14's own unique key — so a pasted URL never has to go through the
        MPN and can't land on the wrong maker's part. Anything else (a search page,
        a shortened link) falls back to reading the last path segment as an MPN, the
        way the Mouser provider does.
        """
        try:
            path = urlsplit(url).path
        except ValueError:
            raise ValidationError("malformed URL") from None
        segments = [unquote(s) for s in path.split("/") if s]
        if "dp" in segments:
            order_code = segments[segments.index("dp") + 1 :]
            if order_code and order_code[0]:
                return self._fetch_one(
                    order_code[0], by_order_code=True, transport=transport
                )
        if not segments:
            raise ValidationError("could not read a part number from the URL")
        return self.fetch_by_mpn(segments[-1], transport=transport)

    def fetch_by_mpn(
        self,
        mpn: str,
        *,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        """Look a part up by its manufacturer part number (a scan, an invoice line).

        ``manufacturer`` breaks the tie when a bare MPN is sold under several makers
        — the same reason the other providers take it.
        """
        return self._fetch_one(
            mpn, by_order_code=False, manufacturer=manufacturer, transport=transport
        )

    def fetch_by_index(
        self,
        candidates: list[str],
        *,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        """Try each candidate number in order; first hit wins (invoice import).

        Each candidate is tried as an order code and then as an MPN, because an
        invoice carries both and only the row's position says which is which. See
        ``fetch_first_match`` for the miss-only fallthrough and error aggregation.
        """

        def attempt(number: str) -> ProductData:
            try:
                return self._fetch_one(
                    number,
                    by_order_code=True,
                    manufacturer=manufacturer,
                    transport=transport,
                )
            except ShopLookupMiss:
                return self._fetch_one(
                    number,
                    by_order_code=False,
                    manufacturer=manufacturer,
                    transport=transport,
                )

        return fetch_first_match(attempt, candidates)

    def _fetch_one(
        self,
        number: str,
        *,
        by_order_code: bool,
        manufacturer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> ProductData:
        prefix = "id" if by_order_code else "manuPartNum"
        products = self._search(f"{prefix}:{number}", transport=transport)
        if not products:
            raise ShopLookupMiss("no product found")
        product = _pick(
            products,
            number,
            by_order_code=by_order_code,
            manufacturer=manufacturer,
        )
        return _product_data(product)

    def _search(
        self, term: str, *, transport: httpx.BaseTransport | None = None
    ) -> list[dict[str, Any]]:
        if not config.FARNELL_API_KEY:
            raise ValidationError("element14 integration is not configured")
        params = {
            "versionNumber": "1.5",
            "term": term,
            "storeInfo.id": config.FARNELL_STORE,
            "resultsSettings.offset": "0",
            # Not 1: one MPN legitimately returns several packaging variants, and
            # they all have to be on the table for _pick to choose between them.
            # (element14 seems to return the variants regardless — asking for 1 still
            # answered with both — but relying on that would be relying on a bug.)
            "resultsSettings.numberOfResults": "10",
            # The response group that carries `attributes` — the whole point here.
            "resultsSettings.responseGroup": "large",
            "callInfo.responseDataFormat": "json",
            # Lower-case "i", which is what element14 actually accepts.
            "callinfo.apiKey": config.FARNELL_API_KEY,
        }
        # Deliberately NO resultsSettings.refinements.filters. Filtering to inStock
        # would make a part already sitting on the shelf unlookupable the moment
        # Farnell runs out of it, and RoHS is a fact to record, not a filter.
        #
        # A network error, a non-2xx, a non-JSON body (JSONDecodeError → ValueError)
        # or an unexpected shape (AttributeError) all become a clean 422 — never a
        # 500, and the exception (which embeds the api-key query string) never
        # reaches the client.
        try:
            with httpx.Client(
                timeout=config.SHOP_API_TIMEOUT, transport=transport
            ) as client:
                resp = client.get(_API_URL, params=params)
                # The status is worth its own message: element14 answers a bad or
                # unregistered key with 401, and "could not read the response" would
                # send someone looking for a parsing bug that isn't there.
                if resp.status_code >= 400:
                    raise ValidationError(
                        _redact(
                            f"element14 rejected the request (HTTP {resp.status_code})"
                        )
                    )
                # Not resp.json(): the default decoder silently keeps only the last
                # of a repeated key, and this response repeats them (see
                # _merge_duplicate_keys).
                payload = json.loads(resp.text, object_pairs_hook=_merge_duplicate_keys)
            if not isinstance(payload, dict):
                raise ValueError("unexpected response shape")
            # The envelope is named after the SEARCH that was run, not after the
            # endpoint: `manufacturerPartNumberSearchReturn` for manuPartNum: and
            # `premierFarnellPartNumberReturn` for id: (both confirmed against the
            # live API). So find it by SHAPE rather than by a pair of hard-coded
            # keys that the next search type would break.
            #
            # By shape and not just "the first value", because the difference
            # matters: a body that is not a search result at all — what a rejected
            # key looks like when it arrives with HTTP 200 — would otherwise pass
            # for an envelope holding no products, and come back as a
            # ShopLookupMiss. That is the one exception fetch_first_match retries
            # on, so a configuration problem would burn a round trip per candidate
            # and then report "no product found".
            envelope = next(
                (
                    value
                    for value in payload.values()
                    if isinstance(value, dict)
                    and ("products" in value or "numberOfResults" in value)
                ),
                None,
            )
            if envelope is None:
                raise ValidationError(
                    "element14 did not answer with a product search "
                    "(a rejected API key can arrive this way, with HTTP 200)"
                )
            products = _as_list(envelope.get("products"))
        except (httpx.HTTPError, ValueError, AttributeError):
            raise ValidationError("could not read the element14 response") from None
        return [p for p in products if isinstance(p, dict)]


def _pick(
    products: list[dict[str, Any]],
    number: str,
    *,
    by_order_code: bool,
    manufacturer: str | None,
) -> dict[str, Any]:
    """Choose one product from what the search returned.

    Never ``products[0]`` on faith: a term search matches more than the number asked
    for, and element14 lists every packaging variant of a part separately.
    """
    queried = number.strip().casefold()
    key = "sku" if by_order_code else "translatedManufacturerPartNumber"
    exact = [p for p in products if _text(p.get(key)).casefold() == queried]
    if not exact:
        raise ShopLookupMiss(f"no exact match for {number!r}")

    # The caller's manufacturer, when it knows one and any candidate bears it.
    named = [
        p
        for p in exact
        if manufacturer_matches(manufacturer, _text(p.get("brandName")))
    ]
    candidates = named or exact

    # The packaging variants left over are the same part twice; element14 marks the
    # primary one itself. Cut Tape carries isCanonical Y, Re-Reel carries N.
    canonical = [p for p in candidates if _attribute(p, "isCanonical").upper() == "Y"]
    return (canonical or candidates)[0]


def _product_data(product: dict[str, Any]) -> ProductData:
    attributes = _specifications(_attributes(product))
    brand = _text(product.get("brandName")) or _text(product.get("vendorName"))
    mpn = _text(product.get("translatedManufacturerPartNumber"))
    description = _description(_text(product.get("displayName")), brand, mpn)
    shop_category = _shop_category(product)
    return ProductData(
        # The product page from the RESPONSE, not from the input: a lookup by number
        # has no URL of its own, and this is what gets kept as the component's shop
        # link.
        source_url=_text(product.get("productURL")) or None,
        mpn=mpn or None,
        manufacturer=brand or None,
        description=description,
        package=_package(attributes),
        datasheet_url=_datasheet_url(product),
        category=infer_category(shop_category, description),
        shop_category=shop_category,
        parameters=_parameters(attributes),
    )
