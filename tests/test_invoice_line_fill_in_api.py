"""A photo for an invoice line's component: the invoice's shop first, then others.

An invoice keeps no image URL, so after finalize the invoice page asks the shop
again for each line — by the line's own catalogue number, the key the import
looked it up by.
"""

from __future__ import annotations

import pytest
from app.services import shop_photo_service, shops, url_fetch
from app.services.errors import ValidationError
from app.services.shops.base import ProductData
from fastapi.testclient import TestClient

TME = "https://ce8dc832c.cloudimg.io/v7/_cdn_/45/50/00/00/0/1364_1.jpg"
MOUSER = "https://www.mouser.com/images/ti/images/ITP_TI_SOT-23-5_DBV_t.jpg"
DIGIKEY = "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/x.jpg"


class Shops:
    """The invoice's shop, the fallbacks, and what was downloaded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.own: dict[str, ProductData | Exception] = {}
        self.fallback: dict[str, ProductData | Exception] = {}
        self.asked_own: list[tuple[str, list[str]]] = []
        self.asked_fallback: list[str] = []
        self.blocked: set[str] = {MOUSER}
        self.fetched: list[str] = []
        monkeypatch.setattr(shops, "import_by_index", self._by_index)
        monkeypatch.setattr(shops, "import_by_mpn", self._by_mpn)
        monkeypatch.setattr(url_fetch, "fetch_url", self._fetch)

    @staticmethod
    def _answer(answer: ProductData | Exception | None) -> ProductData:
        if answer is None:
            raise ValidationError("no product found")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _by_index(
        self, shop_key: str, numbers: list[str], *, manufacturer: str | None = None
    ) -> ProductData:
        self.asked_own.append((shop_key, numbers))
        return self._answer(self.own.get(shop_key))

    def _by_mpn(
        self, shop_key: str, mpn: str, *, manufacturer: str | None = None
    ) -> ProductData:
        self.asked_fallback.append(shop_key)
        return self._answer(self.fallback.get(shop_key))

    def _fetch(self, url: str, **_kwargs: object) -> tuple[bytes, str]:
        self.fetched.append(url)
        if url in self.blocked:
            raise ValidationError("that URL returned a web page, not a file")
        return b"\xff\xd8\xff\xe0 jpeg", url.rsplit("/", 1)[-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Shops:
    # A shop found down stays down for a while across requests; not across tests.
    monkeypatch.setattr(shop_photo_service, "_down", {})
    return Shops(monkeypatch)


def _line(
    client: TestClient,
    *,
    supplier: str = "TME",
    spn: str | None = "0603B104K500CT",
    mpn: str | None = "0603B104K500CT-W",
    maker: str | None = "Walsin",
    finalize: bool = True,
) -> tuple[int, int, int]:
    """``(invoice id, line id, component id)`` for a one-line invoice, finalized
    unless ``finalize`` is False."""
    ctype = client.post("/api/types", json={"name": f"t-{supplier}-{mpn}"}).json()
    component = client.post(
        "/api/components",
        json={"type_id": ctype["id"], "mpn": mpn, "manufacturer": maker},
    ).json()
    invoice = client.post(
        "/api/invoices",
        json={
            "supplier": supplier,
            "invoice_number": f"FV-{supplier}-{mpn}",
            "invoice_date": "2026-10-11",
            "currency": "PLN",
        },
    ).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": f"D-{supplier}-{mpn}"}
    ).json()
    line = client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 10,
            "unit_price": "0.05",
            "supplier_part_number": spn,
            "location_id": location["id"],
        },
    )
    assert line.status_code == 201, line.text
    if finalize:
        done = client.post(f"/api/invoices/{invoice['id']}/finalize", json={})
        assert done.status_code == 200, done.text
    return invoice["id"], line.json()["id"], component["id"]


def _photo(client: TestClient, invoice_id: int, line_id: int):  # type: ignore[no-untyped-def]
    return client.post(f"/api/invoices/{invoice_id}/lines/{line_id}/photo")


def test_the_invoice_s_shop_is_asked_by_the_line_s_own_number(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ProductData(
        mpn="0603B104K500CT-W", manufacturer="Walsin", image_url=TME
    )
    invoice_id, line_id, _ = _line(client)

    resp = _photo(client, invoice_id, line_id)

    assert resp.status_code == 201, resp.text
    # The line's own number alone — never the MPN standing in for it.
    assert fake.asked_own == [("tme", ["0603B104K500CT"])]
    assert fake.asked_fallback == []
    assert fake.fetched == [TME]


def test_a_refused_image_falls_back_to_another_shop(
    client: TestClient, fake: Shops
) -> None:
    fake.own["mouser"] = ProductData(
        mpn="LM358", manufacturer="Texas Instruments", image_url=MOUSER
    )
    fake.fallback["digikey"] = ProductData(
        mpn="LM358", manufacturer="Texas Instruments", image_url=DIGIKEY
    )
    invoice_id, line_id, _ = _line(
        client,
        supplier="Mouser",
        spn="595-LM358",
        mpn="LM358",
        maker="Texas Instruments",
    )

    resp = _photo(client, invoice_id, line_id)

    assert resp.status_code == 201, resp.text
    assert fake.fetched == [MOUSER, DIGIKEY]
    # The invoice's own shop is not asked a second time as a fallback.
    assert fake.asked_fallback == ["digikey"]


def test_an_answer_naming_no_mpn_is_taken_on_the_index(
    client: TestClient, fake: Shops
) -> None:
    """TME's answer may carry no manufacturer symbol; its own index found it."""
    fake.own["tme"] = ProductData(mpn=None, manufacturer="Walsin", image_url=TME)
    invoice_id, line_id, _ = _line(client)

    assert _photo(client, invoice_id, line_id).status_code == 201


def test_another_part_or_maker_from_the_own_shop_is_refused(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ProductData(
        mpn="0603B104K500CT-W", manufacturer="ABB", image_url=TME
    )
    invoice_id, line_id, _ = _line(client)
    resp = _photo(client, invoice_id, line_id)
    assert resp.status_code == 422
    assert "tme: answered with ABB's part" in resp.json()["detail"]

    fake.own["tme"] = ProductData(
        mpn="0805B104K500CT", manufacturer="Walsin", image_url=TME
    )
    resp = _photo(client, invoice_id, line_id)
    assert resp.status_code == 422
    assert "tme: answered with 0805B104K500CT" in resp.json()["detail"]
    assert fake.fetched == []


def test_a_supplier_no_shop_serves_goes_straight_to_the_fallbacks(
    client: TestClient, fake: Shops
) -> None:
    fake.fallback["digikey"] = ProductData(
        mpn="0603B104K500CT-W", manufacturer="Walsin", image_url=DIGIKEY
    )
    invoice_id, line_id, _ = _line(client, supplier="Conrad")

    resp = _photo(client, invoice_id, line_id)

    assert resp.status_code == 201, resp.text
    assert fake.asked_own == []
    assert fake.fetched == [DIGIKEY]


def test_a_photo_already_there_asks_no_shop(client: TestClient, fake: Shops) -> None:
    fake.own["tme"] = ProductData(
        mpn="0603B104K500CT-W", manufacturer="Walsin", image_url=TME
    )
    invoice_id, line_id, _ = _line(client)
    first = _photo(client, invoice_id, line_id)

    again = _photo(client, invoice_id, line_id)

    assert (first.status_code, again.status_code) == (201, 200)
    assert len(fake.asked_own) == 1


def test_a_line_of_another_invoice_is_404(client: TestClient, fake: Shops) -> None:
    invoice_id, _, _ = _line(client)
    _, other_line, _ = _line(client, supplier="Mouser", mpn="LM358")

    assert _photo(client, invoice_id, other_line).status_code == 404
    assert _photo(client, invoice_id, 99999).status_code == 404


def test_read_only_accounts_cannot_fetch(
    client: TestClient, anon_client: TestClient, fake: Shops
) -> None:
    invoice_id, line_id, _ = _line(client)
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]

    resp = anon_client.post(
        f"/api/invoices/{invoice_id}/lines/{line_id}/photo",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert fake.asked_own == []


def test_a_digikey_invoice_s_fallback_is_element14_alone(
    client: TestClient, fake: Shops
) -> None:
    """Digi-Key was just asked as the invoice's shop; once is enough."""
    invoice_id, line_id, _ = _line(
        client, supplier="Digi-Key", spn="296-1395-5-ND", mpn="LM358", maker="TI"
    )

    _photo(client, invoice_id, line_id)

    assert [shop for shop, _ in fake.asked_own] == ["digikey"]
    assert fake.asked_fallback == ["farnell"]


def test_a_line_with_no_shop_number_is_checked_in_full(
    client: TestClient, fake: Shops
) -> None:
    """Its MPN, asked as a TME symbol, could name another product outright."""
    fake.fallback["digikey"] = ProductData(
        mpn=None, manufacturer=None, image_url=DIGIKEY
    )
    invoice_id, line_id, _ = _line(client, spn=None)

    resp = _photo(client, invoice_id, line_id)

    assert resp.status_code == 422
    assert fake.asked_own == []  # the invoice's shop is not asked by MPN-as-index
    assert DIGIKEY not in fake.fetched  # an answer naming no part is not taken


def test_a_draft_invoice_s_line_is_refused(client: TestClient, fake: Shops) -> None:
    """Its line can still be pointed at another component."""
    invoice_id, line_id, _ = _line(client, finalize=False)

    resp = _photo(client, invoice_id, line_id)

    assert resp.status_code == 422
    assert "finalize the invoice first" in resp.json()["detail"]
    assert fake.asked_own == []


def test_a_shop_found_down_is_not_asked_again_for_a_while(
    client: TestClient, fake: Shops
) -> None:
    """Each line is its own request; a down shop would cost every one a timeout."""
    fake.own["tme"] = ValidationError("could not reach TME")
    first = _line(client, mpn="A1")
    second = _line(client, mpn="B2")

    _photo(client, *first[:2])
    resp = _photo(client, *second[:2])

    assert [shop for shop, _ in fake.asked_own] == ["tme"]
    assert "not asked again for a while" in resp.json()["detail"]


def test_a_miss_or_a_one_part_refusal_trips_nothing(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ValidationError("no product found")
    fake.fallback["digikey"] = ValidationError(
        "Digi-Key rejected the request: Duplicate Products found for 5270"
    )
    first = _line(client, mpn="A1")
    second = _line(client, mpn="B2")

    _photo(client, *first[:2])
    _photo(client, *second[:2])

    assert len(fake.asked_own) == 2
    assert fake.asked_fallback.count("digikey") == 2


@pytest.mark.parametrize(
    ("supplier", "shop"),
    [
        ("TME", "tme"),
        ("Digi-Key", "digikey"),
        ("DigiKey", "digikey"),
        ("Digi Key Electronics", "digikey"),
        ("Mouser Electronics", "mouser"),
        ("Farnell element14", "farnell"),
        ("element14", "farnell"),
        ("Newark", "farnell"),
        ("Utmel", None),  # contains the letters "tme", not the word
        ("Conrad", None),
        (None, None),
    ],
)
def test_a_hand_typed_supplier_still_names_its_shop(
    supplier: str | None, shop: str | None
) -> None:
    assert shops.shop_key_by_name(supplier) == shop
