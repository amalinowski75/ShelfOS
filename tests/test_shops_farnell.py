"""Tests for the element14 (Farnell) provider.

Two fixtures, for two different jobs.

``_REAL_BY_ID`` is the live JSON, as text, trimmed only of branches this provider
never reads. It has to be text: the shape that matters — element14 repeating a KEY
where an array belongs — cannot be written as a Python dict.

``_FARNELL_OK`` and ``_FARNELL_BY_ID`` are built here from the same live fields, so
a test can vary one thing at a time (which variant is canonical, which maker sells
the part). ``_FARNELL_OK`` carries two products because element14 lists every
packaging variant of a part separately — that is the case the picker exists for.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app import config
from app.services import shops
from app.services.errors import ValidationError
from app.services.shops.base import ShopLookupMiss
from app.services.shops.farnell import FarnellProvider

_URL = "https://uk.farnell.com/onsemi/ncp730bmt330tbg/ldo-fixed-3-3v/dp/3367839"


def _product(sku: str, *, canonical: str, package_name: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "displayName": (
            "ONSEMI - NCP730BMT330TBG - LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C"
        ),
        "productURL": f"https://uk.farnell.com/onsemi/ncp730bmt330tbg/dp/{sku}",
        "translatedManufacturerPartNumber": "NCP730BMT330TBG",
        "brandName": "ONSEMI",
        "vendorName": "ONSEMI",
        "packageName": package_name,
        "categories": {
            "name": "LDO Voltage Regulators",
            "path": [
                "Semiconductors - ICs",
                "Power Management ICs - PMIC",
                "Voltage Regulators",
                "LDO Voltage Regulators",
            ],
        },
        "datasheets": [
            {
                "type": "T",
                "description": "Technical Data Sheet (2.62MB) EN",
                "url": "http://www.farnell.com/datasheets/2912887.pdf",
            }
        ],
        "attributes": [
            {"attributeLabel": "tariffCode", "attributeValue": "85429000"},
            {"attributeLabel": "rohsCompliant", "attributeValue": "YES"},
            {"attributeLabel": "isCanonical", "attributeValue": canonical},
            {"attributeLabel": "hazardous", "attributeValue": "false"},
            {
                "attributeLabel": "Output Voltage Nom",
                "attributeUnit": "V",
                "attributeValue": "3.3",
            },
            {"attributeLabel": "Output Voltage - Nom", "attributeValue": "3.3V"},
            {
                "attributeLabel": "Output Current Max",
                "attributeUnit": "mA",
                "attributeValue": "150",
            },
            {"attributeLabel": "MSL", "attributeValue": "MSL 1 - Unlimited"},
            {"attributeLabel": "IC Mounting", "attributeValue": "Surface Mount"},
            {"attributeLabel": "IC Case / Package", "attributeValue": "WDFN-EP"},
        ],
    }


_FARNELL_OK = {
    "manufacturerPartNumberSearchReturn": {
        "numberOfResults": 2,
        "products": [
            # Deliberately NOT canonical-first: taking products[0] must not pass.
            # (Live, element14 happens to send Cut Tape first — and sends both even
            # when asked for one result.)
            _product("3367839RL", canonical="N", package_name="Re-Reel"),
            _product("3367839", canonical="Y", package_name="Cut Tape"),
        ],
    }
}

# The same part looked up by its order code. A DIFFERENT root key, because
# element14 names the envelope after the search rather than the endpoint — this is
# the live behaviour, not a guess.
_FARNELL_BY_ID = {
    "premierFarnellPartNumberReturn": {
        "numberOfResults": 1,
        "products": [_product("3367839", canonical="Y", package_name="Cut Tape")],
    }
}


# The live JSON answer to the id: lookup above, verbatim except that the branches
# this provider never reads (prices, stock, image) are cut. It is here as TEXT and
# not as a dict because the shape under test cannot be written as a Python dict:
# `categories` repeats the key "path" four times. That is an XML document run
# through a converter with no notion of arrays, and a plain json.loads keeps only
# the last one.
_REAL_BY_ID = """
{"premierFarnellPartNumberReturn":{"numberOfResults":1,"products":[
{"sku":"3367839",
 "displayName":"ONSEMI - NCP730BMT330TBG - LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C",
 "productURL":"https://uk.farnell.com/onsemi/ncp730bmt330tbg/ldo-fixed/dp/3367839",
 "productStatus":"STOCKED","packSize":1,"id":"pf_UK1_3367839_0",
 "categories":{"name":"LDO Voltage Regulators","path":"Semiconductors - ICs",
   "path":"Power Management ICs - PMIC","path":"Voltage Regulators",
   "path":"LDO Voltage Regulators"},
 "datasheets":[{"type":"T","description":"Technical Data Sheet (2.62MB) EN",
   "url":"http://www.farnell.com/datasheets/2912887.pdf"}],
 "vendorName":"ONSEMI","brandName":"ONSEMI",
 "translatedManufacturerPartNumber":"NCP730BMT330TBG",
 "translatedMinimumOrderQuality":1,
 "attributes":[{"attributeLabel":"tariffCode","attributeValue":"85429000"},
   {"attributeLabel":"rohsCompliant","attributeValue":"YES"},
   {"attributeLabel":"Output Voltage Nom","attributeUnit":"V","attributeValue":"3.3"},
   {"attributeLabel":"isCanonical","attributeValue":"Y"},
   {"attributeLabel":"Operating Temperature Max","attributeUnit":"\\u00b0C",
    "attributeValue":"125"},
   {"attributeLabel":"MSL","attributeValue":"MSL 1 - Unlimited"},
   {"attributeLabel":"Output Current Max","attributeUnit":"mA","attributeValue":"150"},
   {"attributeLabel":"Output Voltage - Nom","attributeValue":"3.3V"},
   {"attributeLabel":"productTraceability","attributeValue":"Yes-Date/Lot Code"},
   {"attributeLabel":"IC Mounting","attributeValue":"Surface Mount"},
   {"attributeLabel":"IC Case / Package","attributeValue":"WDFN-EP"}],
 "nationalClassCode":null,"countryOfOrigin":"MY","inventoryCode":5,
 "reeling":false,"orderMultiples":"1","packageName":"Cut Tape"}]}}
"""


def _transport(
    body: object, seen: list[httpx.URL] | None = None, status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=str(body))

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "FARNELL_API_KEY", "key-123")
    monkeypatch.setattr(config, "FARNELL_STORE", "uk.farnell.com")


# ---- host matching ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://uk.farnell.com/x/dp/1",
        "https://pl.farnell.com/x/dp/1",
        "https://cpc.farnell.com/x/dp/1",
        "https://www.element14.com/x/dp/1",
        "https://www.element14.cn/x/dp/1",  # multi-part-looking TLD
        "https://canada.newark.com/x/dp/1",
        "https://www.newark.com/x/dp/1",
        "https://farnell.com/x/dp/1",  # bare domain, no country prefix
    ],
)
def test_matches_every_element14_brand_and_country_site(url: str) -> None:
    assert FarnellProvider().matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.mouser.com/ProductDetail/x",
        "https://example.com/farnell",  # the brand in the PATH is not this shop
        "http://[::1/x",  # malformed: must not raise
    ],
)
def test_does_not_match_other_urls(url: str) -> None:
    assert not FarnellProvider().matches(url)


def test_a_lookalike_subdomain_matches_and_that_is_deliberate() -> None:
    # Matching the brand as a domain LABEL is what covers uk./pl./cpc. and three
    # brands without listing hosts, and the price is that someone else's
    # "farnell.example.com" matches too. Harmless, and pinned so it reads as a
    # trade-off rather than an oversight: the lookup only ever calls the fixed
    # api.element14.com, which just answers that it has no such product.
    assert FarnellProvider().matches("https://farnell.example.com/x")


# ---- the request -----------------------------------------------------------


def test_a_product_url_is_looked_up_by_its_order_code() -> None:
    # The whole reason a pasted URL beats an MPN: /dp/<code> is element14's own
    # unique key, so the lookup cannot land on another maker's part. Live, id:
    # answers with the one product where manuPartNum: answers with both packaging
    # variants.
    seen: list[httpx.URL] = []
    FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_BY_ID, seen))
    assert seen[0].params["term"] == "id:3367839"


def test_reads_the_envelope_whatever_the_search_named_it() -> None:
    # element14 names the root after the SEARCH, not the endpoint:
    # manufacturerPartNumberSearchReturn for manuPartNum:, and
    # premierFarnellPartNumberReturn for id: (both confirmed against the live API).
    # Reading the single value it wraps is what makes one parser serve both — and
    # what will survive the third search type.
    by_id = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_BY_ID))
    by_mpn = FarnellProvider().fetch_by_mpn(
        "NCP730BMT330TBG", transport=_transport(_FARNELL_OK)
    )
    assert _FARNELL_BY_ID.keys() != _FARNELL_OK.keys()  # the point of the test
    assert by_id.mpn == by_mpn.mpn == "NCP730BMT330TBG"
    assert by_id.package == by_mpn.package == "WDFN-EP"


def test_a_url_without_an_order_code_falls_back_to_the_last_segment() -> None:
    seen: list[httpx.URL] = []
    FarnellProvider().fetch(
        "https://uk.farnell.com/search/NCP730BMT330TBG",
        transport=_transport(_FARNELL_OK, seen),
    )
    assert seen[0].params["term"] == "manuPartNum:NCP730BMT330TBG"


def test_the_search_asks_the_configured_store_and_filters_nothing(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(config, "FARNELL_STORE", "pl.farnell.com")
    seen: list[httpx.URL] = []
    FarnellProvider().fetch_by_mpn(
        "NCP730BMT330TBG", transport=_transport(_FARNELL_OK, seen)
    )
    params = seen[0].params
    assert params["storeInfo.id"] == "pl.farnell.com"
    assert params["callinfo.apiKey"] == "key-123"  # lower-case "i", as element14 wants
    assert params["resultsSettings.responseGroup"] == "large"  # carries `attributes`
    # No inStock/rohsCompliant refinement: a part already on the shelf must stay
    # lookupable when Farnell runs out of it.
    assert "resultsSettings.refinements.filters" not in params
    # More than one, or the packaging variants could never be compared.
    assert int(params["resultsSettings.numberOfResults"]) > 1


# ---- choosing between the results -----------------------------------------


def test_prefers_the_canonical_packaging_variant() -> None:
    # One MPN, two products: Cut Tape and Re-Reel are the same part, and element14
    # marks the primary one itself.
    product = FarnellProvider().fetch_by_mpn(
        "NCP730BMT330TBG", transport=_transport(_FARNELL_OK)
    )
    assert product.source_url is not None
    assert product.source_url.endswith("/dp/3367839")


def test_the_canonical_flag_decides_it_not_the_ordering() -> None:
    # Same list, canonicality swapped: the answer must swap with it. Without this
    # the test above would pass on "take the second one".
    flipped = {
        "manufacturerPartNumberSearchReturn": {
            "products": [
                _product("3367839RL", canonical="Y", package_name="Re-Reel"),
                _product("3367839", canonical="N", package_name="Cut Tape"),
            ]
        }
    }
    product = FarnellProvider().fetch_by_mpn(
        "NCP730BMT330TBG", transport=_transport(flipped)
    )
    assert product.source_url is not None
    assert product.source_url.endswith("/dp/3367839RL")


def test_a_product_that_is_not_the_number_asked_for_is_a_miss() -> None:
    # A term search matches more than what was asked for, so never trust the list.
    with pytest.raises(ShopLookupMiss):
        FarnellProvider().fetch_by_mpn(
            "SOMETHING-ELSE", transport=_transport(_FARNELL_OK)
        )


def test_manufacturer_breaks_a_tie_between_makers_sharing_an_mpn() -> None:
    shared = {
        "manufacturerPartNumberSearchReturn": {
            "products": [
                {
                    "sku": "111",
                    "translatedManufacturerPartNumber": "5120",
                    "brandName": "ABB",
                    "displayName": "ABB - 5120 - Relay",
                    "productURL": "https://uk.farnell.com/abb/5120/dp/111",
                },
                {
                    "sku": "222",
                    "translatedManufacturerPartNumber": "5120",
                    "brandName": "Keystone Electronics",
                    "displayName": "KEYSTONE - 5120 - Test point",
                    "productURL": "https://uk.farnell.com/keystone/5120/dp/222",
                },
            ]
        }
    }
    provider = FarnellProvider()
    # Without a manufacturer, element14's own ordering stands.
    assert provider.fetch_by_mpn("5120", transport=_transport(shared)).manufacturer == (
        "ABB"
    )
    # With one — a scan's 1V, an invoice line's — it decides. Truncated on the
    # label, which is what manufacturer_matches is tolerant of.
    picked = provider.fetch_by_mpn(
        "5120", manufacturer="Keyston", transport=_transport(shared)
    )
    assert picked.manufacturer == "Keystone Electronics"


# ---- normalisation ---------------------------------------------------------


def test_normalises_a_product_into_component_fields() -> None:
    product = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))
    assert product.mpn == "NCP730BMT330TBG"
    assert product.manufacturer == "ONSEMI"
    # The brand and the part number are stripped off the front of displayName —
    # they are already their own fields, and repeating them in the description is
    # noise in the table.
    assert product.description == "LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C"
    assert product.datasheet_url == "http://www.farnell.com/datasheets/2912887.pdf"
    assert product.shop_category is not None
    # The path is kept, not just the leaf: a TYPE match rule bites on
    # "Semiconductors - ICs" where "LDO Voltage Regulators" names no ShelfOS type.
    assert "Semiconductors - ICs" in product.shop_category
    assert "LDO Voltage Regulators" in product.shop_category


def test_the_real_response_end_to_end_including_its_repeated_keys() -> None:
    """The one test driven by the live JSON rather than a dict built here.

    Its point is the category path. element14 repeats the "path" KEY instead of
    sending an array, so the stock decoder keeps only the last of the four and the
    ancestors vanish — and the ancestors are the half that carries words a type rule
    can match ("Resistors" for a resistor, where the leaf is a marketing name).
    """
    product = FarnellProvider().fetch(_URL, transport=_transport(_REAL_BY_ID))

    # Pinned as the whole string, not as four "is it in there" checks: the leaf
    # ("LDO Voltage Regulators") CONTAINS its parent ("Voltage Regulators"), so a
    # containment test stays green while half the path is being dropped. This also
    # pins the order, and the de-duplication of the leaf against `name`.
    assert product.shop_category == (
        "Semiconductors - ICs / Power Management ICs - PMIC"
        " / Voltage Regulators / LDO Voltage Regulators"
    )

    # And the rest of the mapping, against the real thing rather than a fixture
    # written to match the code.
    assert product.mpn == "NCP730BMT330TBG"
    assert product.manufacturer == "ONSEMI"
    assert product.description == "LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C"
    assert product.package == "WDFN-EP"
    assert product.datasheet_url == "http://www.farnell.com/datasheets/2912887.pdf"
    assert ("Output Current Max", "150 mA") in product.parameters
    assert ("Operating Temperature Max", "125 °C") in product.parameters
    assert ("IC Mounting", "Surface Mount") in product.parameters
    assert "tariffCode" not in [label for label, _ in product.parameters]


def test_reads_the_case_out_of_the_attributes_into_package() -> None:
    # The matching engine resolves a package from this field or from the
    # description, never from the attributes — so a part whose description doesn't
    # repeat its case would lose it entirely.
    product = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))
    assert product.package == "WDFN-EP"
    # And it stays among the parameters, so a param_name rule can still route it.
    assert ("IC Case / Package", "WDFN-EP") in product.parameters


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("IC Case / Package", "WDFN-EP"),
        ("Resistor Case Style", "WDFN-EP"),  # element14 names it per category
        ("Capacitor Case Style", "WDFN-EP"),
        # The reel or tape the part SHIPS on, which is not its body.
        ("Packaging", None),
        # A dimension of the case, not the case.
        ("Case Height - Max", None),
        ("Operating Temperature Max", None),
    ],
)
def test_which_attribute_labels_name_the_components_case(
    label: str, expected: str | None
) -> None:
    one = {
        "r": {
            "products": [
                {
                    "sku": "1",
                    "translatedManufacturerPartNumber": "P1",
                    "displayName": "X - P1 - a part",
                    "attributes": [
                        {"attributeLabel": label, "attributeValue": "WDFN-EP"}
                    ],
                }
            ]
        }
    }
    product = FarnellProvider().fetch_by_mpn("P1", transport=_transport(one))
    assert product.package == expected


def test_a_separate_unit_is_folded_back_into_the_value() -> None:
    # "150" for a definition measured in amps is wrong by a factor of a thousand.
    product = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))
    assert ("Output Current Max", "150 mA") in product.parameters
    assert ("Output Voltage Nom", "3.3 V") in product.parameters
    # The display twin element14 sends alongside is kept as-is; the engine fills a
    # definition from the first label that names it and skips it after that.
    assert ("Output Voltage - Nom", "3.3V") in product.parameters


def test_drops_the_apis_own_bookkeeping_but_keeps_facts_about_the_part() -> None:
    product = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))
    labels = [label for label, _ in product.parameters]
    for internal in ("tariffCode", "rohsCompliant", "isCanonical", "hazardous"):
        assert internal not in labels
    assert "MSL" in labels  # a fact about the part, not about the API


def test_mounting_reaches_the_dialog_through_the_attribute_value() -> None:
    # Farnell states how a part mounts, which the Mouser API never does. Nothing in
    # the provider does this: the engine joins attribute VALUES and the seeded
    # ("surface mount" -> SMT) rule matches. Asserted here because that is a chain
    # of three components, and it silently produces "Other" if any link moves.
    product = FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))
    values = [value for _, value in product.parameters]
    assert "Surface Mount" in values


# ---- failure modes ---------------------------------------------------------


def test_without_a_key_the_integration_says_so(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "FARNELL_API_KEY", "")
    with pytest.raises(ValidationError, match="not configured"):
        FarnellProvider().fetch(_URL, transport=_transport(_FARNELL_OK))


def test_an_empty_result_is_a_miss_not_an_error() -> None:
    # The TYPE matters: only a ShopLookupMiss falls through to the next candidate
    # in fetch_first_match — a plain ValidationError would abandon the import.
    empty = {"manufacturerPartNumberSearchReturn": {"numberOfResults": 0}}
    with pytest.raises(ShopLookupMiss):
        FarnellProvider().fetch(_URL, transport=_transport(empty))


def test_a_rejected_request_names_the_status(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # element14 answers an unregistered key with 401, and "could not read the
    # response" would send someone hunting a parsing bug that isn't there.
    with pytest.raises(ValidationError, match="401"):
        FarnellProvider().fetch(_URL, transport=_transport({}, status=401))


def test_an_error_never_carries_the_api_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "FARNELL_API_KEY", "401")  # in the message otherwise
    with pytest.raises(ValidationError) as exc:
        FarnellProvider().fetch(_URL, transport=_transport({}, status=401))
    assert "401" not in str(exc.value).replace("HTTP ***", "")


def test_a_non_json_body_is_a_clean_failure() -> None:
    with pytest.raises(ValidationError, match="could not read"):
        FarnellProvider().fetch(_URL, transport=_transport("<html>nope</html>"))


def test_an_unexpected_envelope_is_a_clean_failure() -> None:
    with pytest.raises(ValidationError, match="could not read"):
        FarnellProvider().fetch(_URL, transport=_transport({"fault": "nope"}))


# ---- registry --------------------------------------------------------------


def test_the_registry_routes_a_farnell_url_to_this_provider(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    # import_code is the single entry point the dialog and the scanner both use.
    monkeypatch.setattr(
        shops._farnell,
        "fetch",
        lambda url, **kw: FarnellProvider().fetch(
            url, transport=_transport(_FARNELL_OK)
        ),
    )
    product = shops.import_code(_URL)
    assert product.mpn == "NCP730BMT330TBG"
    assert product.source_url == _URL  # the page the user was actually looking at


def test_an_invoice_line_falls_back_from_order_code_to_mpn() -> None:
    # A line carries both numbers and only its column says which is which, so each
    # candidate is tried as an order code and then as an MPN.
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.params["term"].startswith("id:"):
            return httpx.Response(200, json={"r": {"numberOfResults": 0}})
        return httpx.Response(200, json=_FARNELL_OK)

    product = FarnellProvider().fetch_by_index(
        ["NCP730BMT330TBG"], transport=httpx.MockTransport(handler)
    )
    assert [u.params["term"] for u in seen] == [
        "id:NCP730BMT330TBG",
        "manuPartNum:NCP730BMT330TBG",
    ]
    assert product.mpn == "NCP730BMT330TBG"
