"""A photo for an invoice line's component: the invoice's shop first, then others.

An invoice keeps no image URL, so after finalize the invoice page asks the shop
again for each line — by the line's own catalogue number, the key the import
looked it up by.
"""

from __future__ import annotations

import pytest
from app.services import shops, url_fetch
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
    return Shops(monkeypatch)


def _line(
    client: TestClient,
    *,
    supplier: str = "TME",
    spn: str | None = "0603B104K500CT",
    mpn: str | None = "0603B104K500CT-W",
    maker: str | None = "Walsin",
) -> tuple[int, int, int]:
    """``(invoice id, line id, component id)`` for a one-line draft invoice."""
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
    line = client.post(
        f"/api/invoices/{invoice['id']}/lines",
        json={
            "component_id": component["id"],
            "quantity": 10,
            "unit_price": "0.05",
            "supplier_part_number": spn,
        },
    )
    assert line.status_code == 201, line.text
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
    assert fake.asked_own == [("tme", ["0603B104K500CT", "0603B104K500CT-W"])]
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
