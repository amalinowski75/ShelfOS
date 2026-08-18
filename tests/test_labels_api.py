"""Label endpoints: the printer-bitmap preview (spec §7)."""

from __future__ import annotations

import io

from app import config
from fastapi.testclient import TestClient
from PIL import Image


def _location(client: TestClient, name: str = "D1") -> int:
    created = client.post("/api/locations", json={"type": "drawer", "name": name})
    return int(created.json()["id"])


def test_preview_returns_the_bitmap_that_would_be_printed(client: TestClient) -> None:
    location_id = _location(client)
    response = client.get(f"/api/labels/locations/{location_id}/preview.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    # The default 62 mm roll at 30 mm per label — the dots the printer gets.
    assert Image.open(io.BytesIO(response.content)).size == (696, 354)


def test_preview_accepts_a_tape_and_length_to_try(client: TestClient) -> None:
    """Trying a roll must not need an env var and a restart.

    Tuning the layout is "look, change one thing, reload" — which is only true
    if the tape and length can be varied per request here.
    """
    location_id = _location(client)

    longer = client.get(f"/api/labels/locations/{location_id}/preview.png?length=50")
    assert Image.open(io.BytesIO(longer.content)).size == (696, round(50 * 300 / 25.4))

    die_cut = client.get(f"/api/labels/locations/{location_id}/preview.png?tape=62x29")
    assert Image.open(io.BytesIO(die_cut.content)).size == (696, 271)

    unknown = client.get(f"/api/labels/locations/{location_id}/preview.png?tape=nope")
    assert unknown.status_code == 422
    assert "unknown label tape" in unknown.json()["detail"]


def test_preview_of_a_missing_or_absurd_location(client: TestClient) -> None:
    assert client.get("/api/labels/locations/999/preview.png").status_code == 404
    # Past SQLite's rowid range the driver raises mid-query; refuse up front so
    # it is a 422 and not an unmapped 500.
    huge = "9" * 26
    assert client.get(f"/api/labels/locations/{huge}/preview.png").status_code == 422


def test_preview_needs_a_login_but_not_a_writer(
    client: TestClient, anon_client: TestClient
) -> None:
    location_id = _location(client)
    assert (
        anon_client.get(f"/api/labels/locations/{location_id}/preview.png").status_code
        == 401
    )
    # Looking at a label changes nothing, so a read-only account may.
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]
    allowed = anon_client.get(
        f"/api/labels/locations/{location_id}/preview.png",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert allowed.status_code == 200


def test_printing_a_branch_sends_it_to_the_device(  # type: ignore[no-untyped-def]
    client: TestClient, tmp_path, monkeypatch
) -> None:
    device = tmp_path / "lp0"
    device.touch()
    monkeypatch.setattr(config, "LABEL_DEVICE", str(device))
    rack = client.post("/api/locations", json={"type": "rack", "name": "Rack A"}).json()
    client.post(
        "/api/locations",
        json={"type": "drawer", "name": "D1", "parent_id": rack["id"]},
    )

    response = client.post("/api/labels/locations/print", json={"root": rack["id"]})

    assert response.status_code == 200
    # A file is not a printer, so nothing confirms the job: the caller has to
    # say "sent" rather than "printed", which is what `confirmed` is for.
    # A file cannot be asked what tape it holds, so the configured one stands.
    assert response.json() == {"sent": 2, "confirmed": False, "tape": "62"}
    assert device.read_bytes().startswith(b"\x1bia\x01")


def test_printing_without_a_printer_says_what_to_set(client: TestClient) -> None:
    location_id = _location(client)
    response = client.post("/api/labels/locations/print", json={"ids": [location_id]})
    assert response.status_code == 422
    assert "SHELFOS_LABEL_DEVICE" in response.json()["detail"]


def test_an_unreachable_printer_is_a_503(  # type: ignore[no-untyped-def]
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """Nothing is wrong with the request, so it is not a 4xx: retrying may work."""
    monkeypatch.setattr(config, "LABEL_DEVICE", str(tmp_path / "unplugged" / "lp0"))
    location_id = _location(client)
    response = client.post("/api/labels/locations/print", json={"ids": [location_id]})
    assert response.status_code == 503
    assert "not there" in response.json()["detail"]


def test_printing_rejects_unknown_and_absurd_ids(  # type: ignore[no-untyped-def]
    client: TestClient, tmp_path, monkeypatch
) -> None:
    (tmp_path / "lp0").touch()
    monkeypatch.setattr(config, "LABEL_DEVICE", str(tmp_path / "lp0"))
    assert (
        client.post("/api/labels/locations/print", json={"ids": [999]}).status_code
        == 404
    )
    huge = int("9" * 26)
    assert (
        client.post("/api/labels/locations/print", json={"ids": [huge]}).status_code
        == 422
    )


def test_printing_is_a_write_so_read_only_accounts_cannot(
    client: TestClient, anon_client: TestClient, tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    (tmp_path / "lp0").touch()
    monkeypatch.setattr(config, "LABEL_DEVICE", str(tmp_path / "lp0"))
    location_id = _location(client)
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]

    refused = anon_client.post(
        "/api/labels/locations/print",
        json={"ids": [location_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert refused.status_code == 403


def test_the_tape_list_says_what_can_be_printed_on(  # type: ignore[no-untyped-def]
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(config, "LABEL_TAPES", "62red,29x90,12,17x54,62x29")
    monkeypatch.setattr(config, "LABEL_DEVICE", "")

    body = client.get("/api/labels/tapes").json()

    assert [tape["id"] for tape in body["tapes"]] == [
        "12",
        "62red",
        "17x54",
        "29x90",
        "62x29",
    ]
    by_id = {tape["id"]: tape for tape in body["tapes"]}
    # Named as they are on the box, since that is what someone matches against
    # the roll in their hand.
    assert by_id["62red"]["name"] == "62 mm continuous, black/red"
    assert by_id["62x29"]["name"] == "62 × 29 mm die-cut"
    assert by_id["62red"]["two_color"] and not by_id["62x29"]["two_color"]
    assert by_id["12"]["length_mm"] is None  # continuous: the length is ours
    # With no printer answering there is nothing to pre-select but the setting.
    assert body["loaded"] is None
    assert body["configured"] == config.LABEL_TAPE


def test_printing_on_a_tape_the_printer_is_not_holding_asks_first(  # type: ignore[no-untyped-def]
    client: TestClient, monkeypatch
) -> None:
    """The refusal carries both tapes, because the client's next move is to ask
    whether to print on what is loaded or wait for the roll to be changed."""
    from tests.fake_printer import IDLE_FRAME, FakePrinter, frame

    location_id = _location(client)
    with FakePrinter([IDLE_FRAME, IDLE_FRAME, frame(b18=0x01)]) as printer:
        monkeypatch.setattr(config, "LABEL_DEVICE", printer.path)

        refused = client.post(
            "/api/labels/locations/print",
            json={"ids": [location_id], "tape": "62x29"},
        )
        assert refused.status_code == 409
        body = refused.json()
        assert (body["requested"], body["loaded"]) == ("62x29", "62")
        assert "62 × 29 mm die-cut" in body["detail"]
        assert len(printer.received) == 3  # the question only; no tape moved

        # Answering the question prints, on the roll that is actually there.
        accepted = client.post(
            "/api/labels/locations/print",
            json={"ids": [location_id], "tape": "62x29", "accept_loaded": True},
        )
        assert accepted.status_code == 200
        assert accepted.json()["tape"] == "62"


def test_the_tape_list_stays_out_of_the_way_of_a_print(  # type: ignore[no-untyped-def]
    client: TestClient, monkeypatch
) -> None:
    """A status question is three bytes on the wire the raster travels, so
    asking during a job splices them into the middle of it — worse than two
    competing jobs, because nothing downstream can tell them from the label."""
    from app.services import label_printer as lp

    monkeypatch.setattr(config, "LABEL_DEVICE", "/dev/null")
    lp._PRINT_LOCK.acquire()  # stand in for a print in flight
    try:
        body = client.get("/api/labels/tapes").json()
    finally:
        lp._PRINT_LOCK.release()

    # Answered anyway, just without the printer's own answer — which is exactly
    # the case the dialog already handles ("pick the roll you loaded").
    assert body["loaded"] is None
    assert body["tapes"]
