"""Filling an invoice line's component in from the invoice's shop.

A component created at finalize is built from the line's fields alone, so the
invoice page asks the shop again for each line — once, by the line's own
catalogue number — and adds the shop link, datasheet and photo it lacks.
"""

from __future__ import annotations

import pytest
from app.services import shop_files_service, shops, url_fetch
from app.services.errors import ValidationError
from app.services.shops.base import ProductData
from fastapi.testclient import TestClient

TME = "https://ce8dc832c.cloudimg.io/v7/_cdn_/45/50/00/00/0/1364_1.jpg"
TME_PAGE = "https://www.tme.eu/pl/details/0603b104k500ct/"
SHEET = "https://www.tme.eu/Document/0603B104K500CT.pdf"
MOUSER = "https://www.mouser.com/images/ti/images/ITP_TI_SOT-23-5_DBV_t.jpg"
DIGIKEY = "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/x.jpg"


class Shops:
    """The invoice's shop, the fallbacks, and what was downloaded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.own: dict[str, ProductData | Exception] = {}
        self.fallback: dict[str, ProductData | Exception] = {}
        self.asked_own: list[tuple[str, list[str]]] = []
        self.asked_fallback: list[str] = []
        self.blocked: set[str] = {MOUSER, SHEET}
        self.fetched: list[str] = []
        self.deadlines: list[object] = []
        # A failure of one's own for a URL: the message url_fetch would give.
        self.errors: dict[str, str] = {}
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

    def _fetch(self, url: str, **kwargs: object) -> tuple[bytes, str]:
        self.fetched.append(url)
        self.deadlines.append(kwargs.get("deadline"))
        if url in self.errors:
            raise ValidationError(f"could not fetch the URL: {self.errors[url]}")
        if url in self.blocked:
            raise ValidationError("that URL returned a web page, not a file")
        return b"\xff\xd8\xff\xe0 jpeg", url.rsplit("/", 1)[-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Shops:
    # A shop found down stays down for a while across requests; not across tests.
    monkeypatch.setattr(shop_files_service, "_down", {})
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


def _fill(client: TestClient, invoice_id: int, line_id: int):  # type: ignore[no-untyped-def]
    return client.post(f"/api/invoices/{invoice_id}/lines/{line_id}/fill-in")


def _has(client: TestClient, component_id: int) -> dict[str, list[str]]:
    query = {"entity_type": "component", "entity_id": component_id}
    files = client.get("/api/attachments", params=query).json()
    links = client.get("/api/links", params=query).json()
    return {
        "photo": [a["filename"] for a in files if a["kind"] == "photo"],
        "datasheet": [a["filename"] for a in files if a["kind"] == "datasheet"],
        "links": [f"{k['kind']}:{k['url']}" for k in links],
    }


WALSIN = {"mpn": "0603B104K500CT-W", "manufacturer": "Walsin"}


def test_one_lookup_fills_in_link_datasheet_and_photo(
    client: TestClient, fake: Shops
) -> None:
    fake.blocked.discard(SHEET)
    fake.own["tme"] = ProductData(
        **WALSIN, image_url=TME, datasheet_url=SHEET, source_url=TME_PAGE
    )
    invoice_id, line_id, cid = _line(client)

    resp = _fill(client, invoice_id, line_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "photo": "added",
        "datasheet": "added",
        "shop_link": "added",
        "reasons": [],
    }
    # Asked once, by the line's own number — never the MPN standing in for it.
    assert fake.asked_own == [("tme", ["0603B104K500CT"])]
    assert fake.asked_fallback == []
    has = _has(client, cid)
    assert has["photo"] == ["1364_1.jpg"]
    assert has["datasheet"] == ["0603B104K500CT.pdf"]
    assert has["links"] == [f"shop:{TME_PAGE}"]


def test_a_datasheet_the_shop_withholds_is_kept_as_a_link(
    client: TestClient, fake: Shops
) -> None:
    """TME's document host refuses a server; the import dialog's rule applies."""
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, datasheet_url=SHEET)
    invoice_id, line_id, cid = _line(client)

    body = _fill(client, invoice_id, line_id).json()

    assert body["datasheet"] == "linked"
    assert f"datasheet:{SHEET}" in _has(client, cid)["links"]


def test_with_no_page_named_the_line_s_own_page_is_linked(
    client: TestClient, fake: Shops
) -> None:
    """TME's answer names no product page; the one built from the line does."""
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME)
    invoice_id, line_id, cid = _line(client)

    _fill(client, invoice_id, line_id)

    assert _has(client, cid)["links"] == [
        f"shop:{shops.product_url('tme', '0603B104K500CT')}"
    ]


def test_what_the_component_has_is_left_alone(client: TestClient, fake: Shops) -> None:
    fake.blocked.discard(SHEET)
    fake.own["tme"] = ProductData(
        **WALSIN, image_url=TME, datasheet_url=SHEET, source_url=TME_PAGE
    )
    invoice_id, line_id, cid = _line(client)
    client.post(
        "/api/links",
        json={
            "entity_type": "component",
            "entity_id": cid,
            "kind": "shop",
            "url": "https://www.tme.eu/pl/details/mine/",
        },
    )

    body = _fill(client, invoice_id, line_id).json()

    assert body["shop_link"] == "present"
    assert body["datasheet"] == "added"
    assert _has(client, cid)["links"] == ["shop:https://www.tme.eu/pl/details/mine/"]


def test_a_component_with_everything_asks_no_shop(
    client: TestClient, fake: Shops
) -> None:
    fake.blocked.discard(SHEET)
    fake.own["tme"] = ProductData(
        **WALSIN, image_url=TME, datasheet_url=SHEET, source_url=TME_PAGE
    )
    invoice_id, line_id, _ = _line(client)
    _fill(client, invoice_id, line_id)

    again = _fill(client, invoice_id, line_id).json()

    assert again == {
        "photo": "present",
        "datasheet": "present",
        "shop_link": "present",
        "reasons": [],
    }
    assert len(fake.asked_own) == 1


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

    body = _fill(client, invoice_id, line_id).json()

    assert body["photo"] == "added"
    assert fake.fetched == [MOUSER, DIGIKEY]
    # The invoice's own shop is not asked a second time as a fallback, and each
    # fallback once, for the datasheet and the photo alike.
    assert "mouser" not in fake.asked_fallback
    assert sorted(fake.asked_fallback) == ["digikey", "farnell"]


def test_an_answer_naming_no_mpn_is_taken_on_the_number(
    client: TestClient, fake: Shops
) -> None:
    """TME's answer may carry no manufacturer symbol; its own number found it."""
    fake.own["tme"] = ProductData(mpn=None, manufacturer="Walsin", image_url=TME)
    invoice_id, line_id, _ = _line(client)

    assert _fill(client, invoice_id, line_id).json()["photo"] == "added"


def test_another_part_or_maker_from_the_own_shop_is_refused(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ProductData(
        mpn="0603B104K500CT-W", manufacturer="ABB", image_url=TME, source_url=TME_PAGE
    )
    invoice_id, line_id, cid = _line(client)
    body = _fill(client, invoice_id, line_id).json()
    assert "tme: answered with ABB's part" in body["reasons"]
    assert f"shop:{TME_PAGE}" not in _has(client, cid)["links"]  # not its page

    fake.own["tme"] = ProductData(
        mpn="0805B104K500CT", manufacturer="Walsin", image_url=TME
    )
    body = _fill(client, invoice_id, line_id).json()
    assert "tme: answered with 0805B104K500CT" in body["reasons"]
    assert fake.fetched == []


def test_a_supplier_no_shop_serves_gets_a_photo_from_the_fallbacks(
    client: TestClient, fake: Shops
) -> None:
    fake.fallback["digikey"] = ProductData(**WALSIN, image_url=DIGIKEY)
    invoice_id, line_id, cid = _line(client, supplier="Conrad")

    body = _fill(client, invoice_id, line_id).json()

    assert (body["photo"], body["shop_link"], body["datasheet"]) == (
        "added",
        "none",
        "none",
    )
    assert fake.asked_own == []
    assert _has(client, cid)["links"] == []


def test_a_line_of_another_invoice_is_404(client: TestClient, fake: Shops) -> None:
    invoice_id, _, _ = _line(client)
    _, other_line, _ = _line(client, supplier="Mouser", mpn="LM358")

    assert _fill(client, invoice_id, other_line).status_code == 404
    assert _fill(client, invoice_id, 99999).status_code == 404


def test_read_only_accounts_cannot_fill_in(
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
        f"/api/invoices/{invoice_id}/lines/{line_id}/fill-in",
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

    _fill(client, invoice_id, line_id)

    assert [shop for shop, _ in fake.asked_own] == ["digikey"]
    assert fake.asked_fallback == ["farnell"]


def test_a_line_with_no_shop_number_is_checked_in_full(
    client: TestClient, fake: Shops
) -> None:
    """Its MPN, asked as a TME symbol, could name another product outright."""
    fake.fallback["digikey"] = ProductData(
        mpn=None, manufacturer=None, image_url=DIGIKEY
    )
    invoice_id, line_id, cid = _line(client, spn=None)

    body = _fill(client, invoice_id, line_id).json()

    assert body["photo"] == "none"
    assert fake.asked_own == []  # the invoice's shop is not asked by MPN-as-number
    assert DIGIKEY not in fake.fetched  # an answer naming no part is not taken
    assert _has(client, cid)["links"] == []  # nor a page built from nothing


def test_a_draft_invoice_s_line_is_refused(client: TestClient, fake: Shops) -> None:
    """Its line can still be pointed at another component."""
    invoice_id, line_id, _ = _line(client, finalize=False)

    resp = _fill(client, invoice_id, line_id)

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

    _fill(client, *first[:2])
    body = _fill(client, *second[:2]).json()

    assert [shop for shop, _ in fake.asked_own] == ["tme"]
    assert any("not asked again for a while" in r for r in body["reasons"])


def test_a_miss_or_a_one_part_refusal_trips_nothing(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ValidationError("no product found")
    fake.fallback["digikey"] = ValidationError(
        "Digi-Key rejected the request: Duplicate Products found for 5270"
    )
    first = _line(client, mpn="A1")
    second = _line(client, mpn="B2")

    _fill(client, *first[:2])
    _fill(client, *second[:2])

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


def test_a_shop_never_asked_is_still_a_fallback(
    client: TestClient, fake: Shops
) -> None:
    """A Digi-Key invoice line with no number: Digi-Key wasn't asked, so may be."""
    fake.fallback["digikey"] = ProductData(
        mpn="LM358", manufacturer="TI", image_url=DIGIKEY
    )
    invoice_id, line_id, _ = _line(
        client, supplier="Digi-Key", spn=None, mpn="LM358", maker="TI"
    )

    body = _fill(client, invoice_id, line_id).json()

    assert body["photo"] == "added"
    assert fake.asked_own == []
    assert fake.asked_fallback[0] == "digikey"


def test_the_datasheet_and_the_photo_share_one_time_budget(
    client: TestClient, fake: Shops
) -> None:
    """Each used to get its own 30 s, so a line could hold its turn for a minute."""
    fake.blocked.discard(SHEET)
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, datasheet_url=SHEET)
    invoice_id, line_id, _ = _line(client)

    _fill(client, invoice_id, line_id)

    # The photo first: small, and not to be starved by a slow datasheet.
    assert fake.fetched == [TME, SHEET]
    assert len(set(fake.deadlines)) == 1 and fake.deadlines[0] is not None


def test_a_linked_datasheet_still_gets_its_file_once_a_shop_has_one(
    client: TestClient, fake: Shops
) -> None:
    """A link points at the datasheet; it is not the file, and stops nothing.

    The first run finds no file anywhere and keeps TME's as a link. A later run
    — another shop selling the part by then, or one that was down — fetches the
    file, without a second link.
    """
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, datasheet_url=SHEET)
    invoice_id, line_id, cid = _line(client)

    first = _fill(client, invoice_id, line_id).json()
    assert first["datasheet"] == "linked"
    assert f"datasheet:{SHEET}" in _has(client, cid)["links"]

    ti_sheet = "https://www.ti.com/lit/ds/symlink/x.pdf"
    fake.fallback["digikey"] = ProductData(**WALSIN, datasheet_url=ti_sheet)
    second = _fill(client, invoice_id, line_id).json()

    assert second["datasheet"] == "added"
    assert _has(client, cid)["datasheet"] == ["x.pdf"]
    assert _has(client, cid)["links"].count(f"datasheet:{SHEET}") == 1


def test_a_link_already_there_is_not_added_twice(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, datasheet_url=SHEET)
    invoice_id, line_id, cid = _line(client)
    _fill(client, invoice_id, line_id)

    again = _fill(client, invoice_id, line_id).json()

    assert again["datasheet"] == "linked"
    assert _has(client, cid)["links"].count(f"datasheet:{SHEET}") == 1


def test_the_own_shop_having_no_picture_is_said(
    client: TestClient, fake: Shops
) -> None:
    fake.own["tme"] = ProductData(**WALSIN)
    invoice_id, line_id, _ = _line(client)

    body = _fill(client, invoice_id, line_id).json()

    assert body["photo"] == "none"
    assert "tme: no image for this part" in body["reasons"]


def test_the_attachments_are_listed_once(
    client: TestClient, fake: Shops, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fill-in knows the photo is missing; the photo search doesn't re-ask."""
    from app.services import attachment_service

    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, source_url=TME_PAGE)
    invoice_id, line_id, _ = _line(client)
    listed = []
    real = attachment_service.list_attachments
    monkeypatch.setattr(
        attachment_service,
        "list_attachments",
        lambda *a, **k: listed.append(1) or real(*a, **k),
    )

    _fill(client, invoice_id, line_id)

    assert len(listed) == 1


def test_a_refused_datasheet_comes_from_another_shop_not_as_a_link(
    client: TestClient, fake: Shops
) -> None:
    """TME's document host refuses a server; Digi-Key's PDF downloads."""
    ti_sheet = "https://www.ti.com/lit/ds/symlink/x.pdf"
    fake.own["tme"] = ProductData(**WALSIN, image_url=TME, datasheet_url=SHEET)
    fake.fallback["digikey"] = ProductData(
        **WALSIN, image_url=DIGIKEY, datasheet_url=ti_sheet
    )
    invoice_id, line_id, cid = _line(client)

    body = _fill(client, invoice_id, line_id).json()

    assert body["datasheet"] == "added"
    assert _has(client, cid)["datasheet"] == ["x.pdf"]
    assert not any(k.startswith("datasheet:") for k in _has(client, cid)["links"])


def test_one_lookup_per_fallback_shop_serves_both_files(
    client: TestClient, fake: Shops
) -> None:
    """A line wanting a photo and a datasheet asks Digi-Key once, not twice."""
    ti_sheet = "https://www.ti.com/lit/ds/symlink/x.pdf"
    fake.fallback["digikey"] = ProductData(
        **WALSIN, image_url=DIGIKEY, datasheet_url=ti_sheet
    )
    invoice_id, line_id, _ = _line(client, spn=None)  # no own-shop answer

    body = _fill(client, invoice_id, line_id).json()

    assert (body["photo"], body["datasheet"]) == ("added", "added")
    assert fake.asked_fallback == ["digikey"]
