"""Tests for scanning a packaging barcode/QR to prefill the dialog.

The payloads are the real ones the user's scanner produced, with the ISO 15434
field separators restored (that scanner emits them as key presses, so they never
reach an ``<input>`` — hence the explicit "no separators" case below).
"""

from __future__ import annotations

import pytest
from app import config
from app.services import shops
from app.services.errors import ValidationError
from app.services.shops.base import ProductData
from app.services.shops.scan import parse_scan

GS = "\x1d"

_TME_QR = "QTY:5 PN:MIC334 PO:16200130/6 RoHS https://www.tme.eu/details/MIC334"

_MOUSER_FIELDS = [
    "[)>\x1e06",
    "K37887399",
    "14K005",
    "1P5277",
    "Q25",
    "11K088130615",
    "4LCN",
    "1VKeystone",
]
_DIGIKEY_FIELDS = [
    "[)>\x1e06",
    "PSAM11086-ND",
    "1PESQ-106-33-T-S",
    "30PSAM11086-ND",
    "K",
    "1K97898569",
    "10K122106329",
    "9D2121",
    "1T309150120016",
    "11K1",
    "4LVN",
    "Q20",
    "11ZPICK",
    "12Z6691968",
    "13Z999999",
    "20Z",
]


def _label(fields: list[str], separator: str = GS) -> str:
    return separator.join(fields)


# --- parse_scan ------------------------------------------------------------


def test_tme_qr_yields_the_product_url() -> None:
    scan = parse_scan(_TME_QR)
    assert scan.url == "https://www.tme.eu/details/MIC334"
    # PN: is kept as the fallback MPN for when the TME API can't be reached.
    assert scan.mpn == "MIC334"
    assert scan.shop is None


def test_a_plain_pasted_url_still_parses() -> None:
    scan = parse_scan("  https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL  ")
    assert scan.url == "https://www.mouser.pl/pl/ProductDetail/Walsin/MR04X1201FTL"
    assert scan.mpn is None


def test_mouser_datamatrix_reads_mpn_and_manufacturer() -> None:
    scan = parse_scan(_label(_MOUSER_FIELDS))
    assert scan.url is None
    assert scan.mpn == "5277"
    assert scan.manufacturer == "Keystone"
    assert scan.shop == "mouser"


def test_digikey_datamatrix_reads_the_manufacturer_part_number() -> None:
    scan = parse_scan(_label(_DIGIKEY_FIELDS))
    # 1P is the MANUFACTURER part number; 30P is Digi-Key's own SKU and is what
    # identifies the shop (it ends -ND), not what we look the part up by.
    assert scan.mpn == "ESQ-106-33-T-S"
    assert scan.shop == "digikey"


def test_digikey_is_detected_from_its_own_z_identifiers() -> None:
    """Without a 30P …-ND field the Z data identifiers still give it away."""
    fields = [f for f in _DIGIKEY_FIELDS if not f.startswith("30P")]
    assert parse_scan(_label(fields)).shop == "digikey"


def test_record_separator_is_accepted_too() -> None:
    assert parse_scan(_label(_MOUSER_FIELDS, "\x1e")).mpn == "5277"


def test_a_configured_visible_separator_is_accepted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "SCAN_SEPARATOR", "|")
    assert parse_scan(_label(_MOUSER_FIELDS, "|")).mpn == "5277"


def test_a_concatenated_datamatrix_is_refused_not_guessed() -> None:
    """No separators → the field boundaries are ambiguous, so we must not guess."""
    with pytest.raises(ValidationError) as exc:
        parse_scan("".join(_MOUSER_FIELDS).replace("\x1e", ""))
    assert "separators" in str(exc.value)


def test_a_scanner_that_keeps_rs_but_drops_gs_is_refused_too() -> None:
    """GS and RS are separately configurable, so only the header may survive.

    That splits the payload in two without splitting the body, which a field count
    would accept — the refusal has to key on finding a data identifier.
    """
    body = "".join(_MOUSER_FIELDS[1:])
    with pytest.raises(ValidationError) as exc:
        parse_scan(f"[)>\x1e06{body}")
    assert "separators" in str(exc.value)


def test_an_unsafe_configured_separator_is_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """"-" occurs inside part numbers; splitting on it would shred a real label."""
    monkeypatch.setattr(config, "SCAN_SEPARATOR", "-")
    scan = parse_scan(_label(_DIGIKEY_FIELDS))
    assert scan.mpn == "ESQ-106-33-T-S"  # not truncated at the first hyphen


def test_a_separator_flush_against_the_url_is_not_swallowed_into_it() -> None:
    scan = parse_scan("QTY:5 PN:MIC334\x1d https://www.tme.eu/details/MIC334\x1d")
    assert scan.url == "https://www.tme.eu/details/MIC334"
    assert scan.mpn == "MIC334"


def test_trailing_sentence_punctuation_is_not_part_of_the_url() -> None:
    assert parse_scan("see https://www.tme.eu/details/MIC334.").url == (
        "https://www.tme.eu/details/MIC334"
    )


def test_a_pn_without_a_url_still_parses() -> None:
    """Not every TME label carries the URL; the part number alone is still usable."""
    scan = parse_scan("QTY:5 PN:MIC334 RoHS")
    assert (scan.url, scan.mpn) == (None, "MIC334")


def test_empty_and_unrecognised_codes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_scan("   ")
    with pytest.raises(ValidationError):
        parse_scan("just some text")


# --- fetch_by_mpn ----------------------------------------------------------


def test_mouser_fetch_by_mpn_sends_the_scanned_part_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json

    import httpx
    from app.services.shops.mouser import MouserProvider

    monkeypatch.setattr(config, "MOUSER_API_KEY", "key")
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "Errors": [],
                "SearchResults": {"Parts": [{"ManufacturerPartNumber": "5277"}]},
            },
        )

    product = MouserProvider().fetch_by_mpn(
        "5277", transport=httpx.MockTransport(handler)
    )
    assert seen["body"] == {"SearchByPartRequest": {"mouserPartNumber": "5277"}}
    assert product.mpn == "5277"


def test_digikey_fetch_by_mpn_sends_the_scanned_part_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx
    from app.services.shops import digikey as digikey_module
    from app.services.shops.digikey import DigiKeyProvider

    monkeypatch.setattr(config, "DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(digikey_module, "_token_cache", None)
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 600})
        seen["path"] = req.url.path
        return httpx.Response(
            200, json={"Product": {"ManufacturerProductNumber": "ESQ-106-33-T-S"}}
        )

    DigiKeyProvider().fetch_by_mpn(
        "ESQ-106-33-T-S", transport=httpx.MockTransport(handler)
    )
    assert seen["path"] == "/products/v4/search/ESQ-106-33-T-S/productdetails"


# --- import_code routing ---------------------------------------------------


class _Recorder:
    """A stand-in provider that records how it was called."""

    name = "Rec"

    def __init__(self, product: ProductData | None = None) -> None:
        self.product = product
        self.url: str | None = None
        self.mpn: str | None = None

    def matches(self, url: str) -> bool:
        return True

    def fetch(self, url: str) -> ProductData:
        self.url = url
        if self.product is None:
            raise ValidationError("boom")
        return self.product

    def fetch_by_mpn(self, mpn: str) -> ProductData:
        self.mpn = mpn
        if self.product is None:
            raise ValidationError("boom")
        return self.product


def test_import_code_routes_a_url_through_fetch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = _Recorder(ProductData(mpn="MIC334"))
    monkeypatch.setattr(shops, "_PROVIDERS", [provider])
    product = shops.import_code(_TME_QR)
    assert product.mpn == "MIC334"
    assert provider.url == "https://www.tme.eu/details/MIC334"
    # The URL is echoed back: it's buried in the QR, so the client can't re-derive
    # it from what it sent, and it is what gets saved as the component's shop link.
    assert product.source_url == "https://www.tme.eu/details/MIC334"
    assert product.from_label_only is False


def test_import_code_routes_a_datamatrix_through_fetch_by_mpn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = _Recorder(ProductData(mpn="5277", manufacturer="Keystone Electronics"))
    monkeypatch.setitem(shops._BY_MPN, "mouser", provider)
    product = shops.import_code(_label(_MOUSER_FIELDS))
    assert provider.mpn == "5277"  # the API call, not the URL path
    assert product.manufacturer == "Keystone Electronics"


def test_import_code_falls_back_to_the_label_when_the_api_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A failing/unconfigured shop must still leave the dialog usefully filled."""
    monkeypatch.setitem(shops._BY_MPN, "mouser", _Recorder(None))
    product = shops.import_code(_label(_MOUSER_FIELDS))
    assert (product.mpn, product.manufacturer) == ("5277", "Keystone")
    # Flagged, so the dialog doesn't pass a label read-off as a full shop import.
    assert product.from_label_only is True
    assert product.source_url is None


def test_a_missing_api_key_is_logged_not_silently_degraded(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """An unconfigured shop looks like a success to the user; it must reach the log."""
    monkeypatch.setattr(config, "MOUSER_API_KEY", "")
    with caplog.at_level("WARNING", logger="shelfos"):
        product = shops.import_code(_label(_MOUSER_FIELDS))
    assert product.from_label_only is True
    assert "not configured" in caplog.text


def test_import_code_falls_back_for_a_failing_url_lookup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(shops, "_PROVIDERS", [_Recorder(None)])
    # The TME QR carries PN:, so a dead TME API still yields the MPN.
    product = shops.import_code(_TME_QR)
    assert product.mpn == "MIC334"
    assert product.from_label_only is True


def test_import_code_reraises_when_there_is_nothing_to_fall_back_on(  # noqa: D103
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(shops, "_PROVIDERS", [_Recorder(None)])
    with pytest.raises(ValidationError) as exc:
        shops.import_code("https://www.mouser.com/ProductDetail/x")
    assert "boom" in str(exc.value)


def test_import_code_rejects_an_unsupported_shop_url() -> None:
    with pytest.raises(ValidationError):
        shops.import_code("https://example.com/part/1")


def test_a_part_number_survives_an_unsupported_shop_url() -> None:
    """A TME QR pointing at a site we have no provider for still fills the MPN."""
    product = shops.import_code("PN:MIC334 https://example.com/part/1")
    assert (product.mpn, product.from_label_only) == ("MIC334", True)


def test_an_unencodable_part_number_is_a_clean_error_not_a_crash(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A mangled scan can carry a lone surrogate; quoting it must not 500."""
    from app.services.shops.digikey import DigiKeyProvider

    monkeypatch.setattr(config, "DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "DIGIKEY_CLIENT_SECRET", "secret")
    with pytest.raises(ValidationError):
        DigiKeyProvider().fetch_by_mpn("\ud800")
