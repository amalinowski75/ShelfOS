"""A new component's photo and datasheet: the import's own, else another shop's."""

from __future__ import annotations

import pytest
from app.services import shop_files_service, shops, url_fetch
from app.services.errors import ValidationError
from app.services.shops.base import ProductData
from fastapi.testclient import TestClient

MOUSER = "https://www.mouser.com/images/ti/images/ITP_TI_SOT-23-5_DBV_t.jpg"
DIGIKEY = "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/x.jpg"
FARNELL = "https://uk.farnell.com/productimages/standard/en_GB/3124743-40.jpg"


class Shops:
    """What each fallback shop answers, and what was downloaded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers: dict[str, ProductData | Exception] = {}
        self.blocked: set[str] = {MOUSER}  # Akamai's block page, refused
        self.fetched: list[str] = []
        self.asked: list[str] = []
        self.empty: set[str] = set()  # a 200 with nothing in it
        monkeypatch.setattr(shops, "import_by_mpn", self._lookup)
        monkeypatch.setattr(url_fetch, "fetch_url", self._fetch)

    def _lookup(
        self, shop_key: str, mpn: str, *, manufacturer: str | None = None
    ) -> ProductData:
        self.asked.append(shop_key)
        answer = self.answers.get(shop_key, ValidationError("no product found"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _fetch(self, url: str, **_kwargs: object) -> tuple[bytes, str]:
        self.fetched.append(url)
        if url in self.blocked:
            raise ValidationError("that URL returned a web page, not a file")
        data = b"" if url in self.empty else b"\xff\xd8\xff\xe0 jpeg"
        return data, url.rsplit("/", 1)[-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Shops:
    # A shop found down stays down for a while across requests; not across tests.
    monkeypatch.setattr(shop_files_service, "_down", {})
    return Shops(monkeypatch)


def _component(
    client: TestClient, mpn: str | None = "PCF8574ADWR", maker: str | None = "TI"
) -> int:
    ctype = client.post("/api/types", json={"name": "ic"}).json()
    resp = client.post(
        "/api/components",
        json={"type_id": ctype["id"], "mpn": mpn, "manufacturer": maker},
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _photo(  # type: ignore[no-untyped-def]
    client: TestClient,
    component_id: int,
    image_url: str | None,
    source_url: str | None = None,
):
    return client.post(
        f"/api/components/{component_id}/photo-from-shops",
        json={"image_url": image_url, "source_url": source_url},
    )


def _photos(client: TestClient, component_id: int) -> list[str]:
    listed = client.get(
        "/api/attachments",
        params={"entity_type": "component", "entity_id": component_id},
    ).json()
    return [a["filename"] for a in listed if a["kind"] == "photo"]


def test_the_import_s_own_image_needs_no_other_shop(
    client: TestClient, fake: Shops
) -> None:
    cid = _component(client)

    resp = _photo(client, cid, DIGIKEY)

    assert resp.status_code == 201, resp.text
    assert _photos(client, cid) == ["x.jpg"]
    assert fake.asked == []  # no other shop's quota spent


def test_a_refused_image_falls_back_to_another_shop(
    client: TestClient, fake: Shops
) -> None:
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="Texas Instruments", image_url=DIGIKEY
    )
    cid = _component(client, maker="Texas Instruments")

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 201, resp.text
    assert fake.fetched == [MOUSER, DIGIKEY]
    assert _photos(client, cid) == ["x.jpg"]


def test_no_image_at_the_import_s_shop_is_not_the_end_either(
    client: TestClient, fake: Shops
) -> None:
    fake.answers["farnell"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="TI", image_url=FARNELL
    )
    cid = _component(client)

    resp = _photo(client, cid, None)

    assert resp.status_code == 201, resp.text
    assert fake.asked == ["digikey", "farnell"]
    assert _photos(client, cid) == ["3124743-40.jpg"]


def test_another_maker_s_part_is_refused(client: TestClient, fake: Shops) -> None:
    """One number, two companies: Farnell answers a Keystone 5270 with ABB's."""
    fake.answers["digikey"] = ProductData(
        mpn="5270", manufacturer="ABB", image_url=DIGIKEY
    )
    fake.answers["farnell"] = ProductData(
        mpn="5270", manufacturer="Keystone", image_url=FARNELL
    )
    cid = _component(client, mpn="5270", maker="Keystone Electronics")

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 201, resp.text
    assert DIGIKEY not in fake.fetched  # ABB's picture never even downloaded
    assert _photos(client, cid) == ["3124743-40.jpg"]


def test_a_shop_s_longer_spelling_of_the_maker_is_accepted(
    client: TestClient, fake: Shops
) -> None:
    fake.answers["digikey"] = ProductData(
        mpn="NTS0102GT,115", manufacturer="NXP USA Inc.", image_url=DIGIKEY
    )
    cid = _component(client, mpn="NTS0102GT,115", maker="NXP Semiconductors")

    assert _photo(client, cid, MOUSER).status_code == 201


def test_another_part_number_is_refused(client: TestClient, fake: Shops) -> None:
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574DWR", manufacturer="TI", image_url=DIGIKEY
    )
    cid = _component(client)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert "answered with PCF8574DWR" in resp.json()["detail"]
    assert _photos(client, cid) == []


def test_the_same_refused_picture_is_not_fetched_twice(
    client: TestClient, fake: Shops
) -> None:
    fake.blocked.add(DIGIKEY)
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="TI", image_url=DIGIKEY
    )

    cid = _component(client)
    resp = _photo(client, cid, DIGIKEY)

    assert resp.status_code == 422
    assert fake.fetched == [DIGIKEY]


def test_nothing_anywhere_says_what_each_source_said(
    client: TestClient, fake: Shops
) -> None:
    fake.answers["digikey"] = ValidationError("Digi-Key integration is not configured")
    cid = _component(client)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "the shop's image: that URL returned a web page" in detail
    assert "digikey: Digi-Key integration is not configured" in detail
    assert "farnell: no product found" in detail
    assert _photos(client, cid) == []


def test_without_a_part_number_only_the_given_image_is_tried(
    client: TestClient, fake: Shops
) -> None:
    cid = _component(client, mpn=None)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert fake.asked == []  # nothing to check another shop's answer against
    assert "no part number" in resp.json()["detail"]


def test_without_a_maker_only_the_given_image_is_tried(
    client: TestClient, fake: Shops
) -> None:
    """Nothing tells Keystone's 5270 from ABB's when the component names no maker."""
    fake.answers["farnell"] = ProductData(
        mpn="5270", manufacturer="ABB", image_url=FARNELL
    )
    cid = _component(client, mpn="5270", maker=None)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert fake.asked == []
    assert "no manufacturer" in resp.json()["detail"]


def test_a_shop_answer_naming_no_maker_is_refused(
    client: TestClient, fake: Shops
) -> None:
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer=None, image_url=DIGIKEY
    )
    cid = _component(client)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert DIGIKEY not in fake.fetched


def test_the_shop_imported_from_is_not_asked_again(
    client: TestClient, fake: Shops
) -> None:
    """It would only offer the picture that was just refused, for a paid call."""
    fake.blocked.add(DIGIKEY)
    cid = _component(client)

    _photo(client, cid, DIGIKEY, source_url="https://www.digikey.pl/en/products/x")

    assert fake.asked == ["farnell"]


def test_a_photo_already_there_is_returned_not_fetched_again(
    client: TestClient, fake: Shops
) -> None:
    cid = _component(client)
    first = _photo(client, cid, DIGIKEY)

    again = _photo(client, cid, FARNELL)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert fake.fetched == [DIGIKEY]
    assert _photos(client, cid) == ["x.jpg"]


def test_a_deleted_component_spends_no_shop_s_quota(
    client: TestClient, fake: Shops
) -> None:
    cid = _component(client)
    assert client.delete(f"/api/admin/components/{cid}").status_code == 204

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert "deleted" in resp.json()["detail"]
    assert (fake.fetched, fake.asked) == ([], [])


def test_a_file_that_cannot_be_stored_moves_on_to_the_next_source(
    client: TestClient, fake: Shops
) -> None:
    """A 200 with an empty body downloads fine and is still no photo."""
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="TI", image_url=DIGIKEY
    )
    fake.empty.add(FARNELL)
    cid = _component(client)

    resp = _photo(client, cid, FARNELL)

    assert resp.status_code == 201, resp.text
    assert _photos(client, cid) == ["x.jpg"]


def test_the_chain_shares_one_time_budget(
    client: TestClient, fake: Shops, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each step used to get the full timeout, so three stalls tripled the wait."""
    from app import config
    from app.services import shop_files_service

    now = [0.0]
    budgets: list[float] = []

    def stalling_fetch(url: str, *, deadline: float, **_k: object) -> tuple[bytes, str]:
        budgets.append(deadline - now[0])
        now[0] = deadline  # a host that stalls to the very end
        raise ValidationError("timed out")

    monkeypatch.setattr(url_fetch, "fetch_url", stalling_fetch)
    real = shop_files_service.attach_from_shops
    monkeypatch.setattr(
        shop_files_service,
        "attach_from_shops",
        lambda *a, **k: real(*a, **k, clock=lambda: now[0]),
    )
    cid = _component(client)

    resp = _photo(client, cid, MOUSER)

    assert resp.status_code == 422
    assert budgets == [config.ATTACHMENT_URL_TOTAL_TIMEOUT]  # one budget, used up
    assert fake.asked == []  # no lookup started that could not finish in time
    assert "not asked, out of time" in resp.json()["detail"]


def test_a_missing_component_is_404(client: TestClient, fake: Shops) -> None:
    assert _photo(client, 99999, DIGIKEY).status_code == 404


def test_read_only_accounts_cannot_attach(
    client: TestClient, anon_client: TestClient, fake: Shops
) -> None:
    cid = _component(client)
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]

    resp = anon_client.post(
        f"/api/components/{cid}/photo-from-shops",
        json={"image_url": DIGIKEY},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert fake.fetched == []


def test_a_shop_page_names_its_shop() -> None:
    assert shops.shop_key("https://www.mouser.pl/ProductDetail/X") == "mouser"
    assert shops.shop_key("https://uk.farnell.com/dp/1") == "farnell"
    assert shops.shop_key("https://www.tme.eu/details/abc/") == "tme"
    assert shops.shop_key("https://example.com/x") is None
    assert shops.shop_key(None) is None


def test_only_a_shop_searchable_by_part_number_is_asked_by_one() -> None:
    with pytest.raises(ValidationError, match="cannot be searched by part number"):
        shops.import_by_mpn("tme", "PCF8574ADWR")


def test_two_requests_for_one_component_attach_one_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invoice finalized in two tabs, or a retry while the first still loads.

    Both used to find no photo and both attach one. Now the second waits for the
    first, then finds its photo.
    """
    import threading
    import time
    from types import SimpleNamespace

    from app.models.enums import AttachmentKind
    from app.services import attachment_service

    component = SimpleNamespace(id=1, mpn="X", manufacturer="Acme", deleted_at=None)
    stored: list[SimpleNamespace] = []
    fetching = threading.Event()
    release = threading.Event()

    def slow_fetch(url: str, **_k: object) -> tuple[bytes, str]:
        fetching.set()
        release.wait(5)
        return b"jpeg", "x.jpg"

    def create(*_a: object, **_k: object) -> SimpleNamespace:
        stored.append(SimpleNamespace(id=len(stored) + 1, kind=AttachmentKind.PHOTO))
        return stored[-1]

    monkeypatch.setattr(shop_files_service, "require_entity", lambda *a: component)
    monkeypatch.setattr(attachment_service, "list_attachments", lambda *a, **k: stored)
    monkeypatch.setattr(attachment_service, "create_attachment", create)
    monkeypatch.setattr(url_fetch, "fetch_url", slow_fetch)
    results: list[tuple[object, bool]] = []

    def request() -> None:
        results.append(
            shop_files_service.attach_from_shops(
                None,  # type: ignore[arg-type]
                1,
                AttachmentKind.PHOTO,
                DIGIKEY,
            )
        )

    first = threading.Thread(target=request)
    first.start()
    assert fetching.wait(5)
    second = threading.Thread(target=request)
    second.start()
    time.sleep(0.2)  # long enough for an unguarded second request to get going
    release.set()
    first.join(5)
    second.join(5)

    assert len(stored) == 1
    assert sorted(created for _, created in results) == [False, True]


# --- datasheets ------------------------------------------------------------

MOUSER_SHEET = "https://www.mouser.com/datasheet/2/405/pcf8574a.pdf"
TI_SHEET = "https://www.ti.com/lit/ds/symlink/pcf8574a.pdf"


def _datasheet(  # type: ignore[no-untyped-def]
    client: TestClient,
    component_id: int,
    datasheet_url: str | None,
    source_url: str | None = None,
):
    return client.post(
        f"/api/components/{component_id}/datasheet-from-shops",
        json={"datasheet_url": datasheet_url, "source_url": source_url},
    )


def _datasheets(client: TestClient, component_id: int) -> list[str]:
    listed = client.get(
        "/api/attachments",
        params={"entity_type": "component", "entity_id": component_id},
    ).json()
    return [a["filename"] for a in listed if a["kind"] == "datasheet"]


def test_a_refused_datasheet_comes_from_another_shop(
    client: TestClient, fake: Shops
) -> None:
    """Digi-Key's datasheet is often the maker's own PDF, which downloads."""
    fake.blocked.add(MOUSER_SHEET)
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="TI", image_url=DIGIKEY, datasheet_url=TI_SHEET
    )
    cid = _component(client)

    resp = _datasheet(client, cid, MOUSER_SHEET)

    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "datasheet"
    assert fake.fetched == [MOUSER_SHEET, TI_SHEET]
    assert _datasheets(client, cid) == ["pcf8574a.pdf"]
    assert _photos(client, cid) == []  # a datasheet search attaches no photo


def test_a_datasheet_already_there_is_returned_not_fetched_again(
    client: TestClient, fake: Shops
) -> None:
    cid = _component(client)
    first = _datasheet(client, cid, TI_SHEET)

    again = _datasheet(client, cid, MOUSER_SHEET)

    assert (first.status_code, again.status_code) == (201, 200)
    assert fake.fetched == [TI_SHEET]


def test_no_datasheet_anywhere_says_what_each_source_said(
    client: TestClient, fake: Shops
) -> None:
    fake.blocked.add(MOUSER_SHEET)
    fake.answers["digikey"] = ProductData(
        mpn="PCF8574ADWR", manufacturer="TI", image_url=DIGIKEY
    )
    cid = _component(client)

    resp = _datasheet(client, cid, MOUSER_SHEET)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("no datasheet could be downloaded")
    assert "digikey: no datasheet for this part" in detail
    assert DIGIKEY not in fake.fetched  # its photo is no datasheet


def test_the_photo_and_the_datasheet_do_not_wait_for_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import dialog fetches both at once; one stalling must not hold both.

    The turn is per component AND kind: a second photo request waits, a
    datasheet request does not.
    """
    import threading
    from types import SimpleNamespace

    from app.models.enums import AttachmentKind
    from app.services import attachment_service

    component = SimpleNamespace(id=1, mpn="X", manufacturer="Acme", deleted_at=None)
    stored: list[SimpleNamespace] = []
    photo_started = threading.Event()
    release = threading.Event()

    def fetch(url: str, **_k: object) -> tuple[bytes, str]:
        if url == DIGIKEY:  # the photo stalls until released
            photo_started.set()
            release.wait(5)
        return b"data", url.rsplit("/", 1)[-1]

    def create(*_a: object, kind: AttachmentKind, **_k: object) -> SimpleNamespace:
        stored.append(SimpleNamespace(id=len(stored) + 1, kind=kind))
        return stored[-1]

    monkeypatch.setattr(shop_files_service, "require_entity", lambda *a: component)
    monkeypatch.setattr(attachment_service, "list_attachments", lambda *a, **k: stored)
    monkeypatch.setattr(attachment_service, "create_attachment", create)
    monkeypatch.setattr(url_fetch, "fetch_url", fetch)

    def photo() -> None:
        shop_files_service.attach_from_shops(
            None,  # type: ignore[arg-type]
            1,
            AttachmentKind.PHOTO,
            DIGIKEY,
        )

    stalled = threading.Thread(target=photo)
    stalled.start()
    try:
        assert photo_started.wait(5)
        done = threading.Event()

        def datasheet() -> None:
            shop_files_service.attach_from_shops(
                None,  # type: ignore[arg-type]
                1,
                AttachmentKind.DATASHEET,
                TI_SHEET,
            )
            done.set()

        threading.Thread(target=datasheet).start()
        assert done.wait(2), "the datasheet waited for the stalled photo"
    finally:
        release.set()
        stalled.join(5)
    assert sorted(a.kind.value for a in stored) == ["datasheet", "photo"]
