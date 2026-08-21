"""API tests for KiCad BOM import (spec §21/§22) — multipart upload + report."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import config
from fastapi.testclient import TestClient

_FIXTURE = (Path(__file__).parent / "fixtures" / "kicad_bom.csv").read_bytes()


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the attachment store (the saved CSV) at a throwaway directory."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def _upload(http: TestClient, *, name: str = "hiduart", headers=None):  # type: ignore[no-untyped-def]
    return http.post(
        "/api/boms",
        files={"file": ("kicad_bom.csv", _FIXTURE, "text/csv")},
        data={"name": name},
        headers=headers or {},
    )


def test_upload_parses_and_stores_the_bom(client: TestClient) -> None:
    resp = _upload(client)
    assert resp.status_code == 201
    bom = resp.json()
    assert bom["name"] == "hiduart"
    assert bom["source_filename"] == "kicad_bom.csv"

    # The original CSV is kept as a `bom` attachment.
    attachments = client.get(
        "/api/attachments",
        params={"entity_type": "bom", "entity_id": bom["id"]},
    ).json()
    assert len(attachments) == 1


def test_get_bom_returns_parsed_lines(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    detail = client.get(f"/api/boms/{bom_id}").json()
    assert detail["id"] == bom_id
    categories = {line["category"] for line in detail["lines"]}
    assert {"resistor", "capacitor", "transistor"} <= categories
    resistor = next(
        line for line in detail["lines"] if line["references"].startswith("R3")
    )
    assert resistor["quantity"] == 3 and resistor["mpn"] == "RES-1K-0402"


def test_report_has_summary_and_lines(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    report = client.get(f"/api/boms/{bom_id}/report").json()
    assert set(report["summary"]) >= {"lines", "ok", "missing", "no_mpn", "buildable"}
    assert len(report["lines"]) == report["summary"]["lines"]


def test_reimport_rebuilds_the_lines_from_the_stored_csv(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    before = client.get(f"/api/boms/{bom_id}").json()["lines"]

    resp = client.post(f"/api/boms/{bom_id}/reimport")
    assert resp.status_code == 200 and resp.json()["id"] == bom_id

    after = client.get(f"/api/boms/{bom_id}").json()["lines"]
    # Same parse of the same file: the content matches, but they are fresh rows.
    assert [ln["references"] for ln in after] == [ln["references"] for ln in before]
    assert {ln["id"] for ln in after}.isdisjoint({ln["id"] for ln in before})


def test_report_scales_with_the_requested_board_count(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]

    one = client.get(f"/api/boms/{bom_id}/report").json()
    assert one["summary"]["boards"] == 1

    ten = client.get(f"/api/boms/{bom_id}/report", params={"boards": 10}).json()
    assert ten["summary"]["boards"] == 10
    for before, after in zip(one["lines"], ten["lines"], strict=True):
        assert after["total_quantity"] == before["quantity"] * 10
        assert after["quantity"] == before["quantity"]  # per-board figure is kept

    # A nonsensical count is rejected rather than silently treated as one board.
    zero = client.get(f"/api/boms/{bom_id}/report", params={"boards": 0})
    assert zero.status_code == 422


def _stocked_component(client: TestClient, mpn: str = "PICK-ME") -> int:
    """A component with stock, for assigning to a BOM line."""
    type_id = client.post("/api/types", json={"name": f"type-{mpn}"}).json()["id"]
    component_id = client.post(
        "/api/components", json={"type_id": type_id, "mpn": mpn}
    ).json()["id"]
    location_id = client.post(
        "/api/locations", json={"type": "drawer", "name": f"D-{mpn}"}
    ).json()["id"]
    client.post(
        "/api/stock/add",
        json={"component_id": component_id, "location_id": location_id, "quantity": 25},
    )
    return int(component_id)


def test_assign_and_unassign_a_component_to_a_line(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    # A line with no MPN: nothing for the report to match on by itself.
    line = next(
        ln
        for ln in client.get(f"/api/boms/{bom_id}").json()["lines"]
        if ln["mpn"] is None
    )
    component_id = _stocked_component(client)

    resp = client.put(
        f"/api/boms/{bom_id}/lines/{line['id']}/component",
        json={"component_id": component_id},
    )
    assert resp.status_code == 200
    assert resp.json()["component_id"] == component_id

    row = next(
        ln
        for ln in client.get(f"/api/boms/{bom_id}/report").json()["lines"]
        if ln["id"] == line["id"]
    )
    assert row["assigned"]["component_id"] == component_id
    assert row["stock"] == 25  # read from the assigned component

    assert (
        client.delete(
            f"/api/boms/{bom_id}/lines/{line['id']}/component"
        ).status_code
        == 204
    )
    row = next(
        ln
        for ln in client.get(f"/api/boms/{bom_id}/report").json()["lines"]
        if ln["id"] == line["id"]
    )
    assert row["assigned"] is None


def test_marking_a_line_as_ordered_round_trips(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    line_id = client.get(f"/api/boms/{bom_id}").json()["lines"][0]["id"]
    row = lambda: next(  # noqa: E731
        ln
        for ln in client.get(f"/api/boms/{bom_id}/report").json()["lines"]
        if ln["id"] == line_id
    )
    assert row()["ordered"] is False

    resp = client.put(
        f"/api/boms/{bom_id}/lines/{line_id}/ordered", json={"ordered": True}
    )
    assert resp.status_code == 200 and resp.json()["ordered"] is True
    assert row()["ordered"] is True

    client.put(f"/api/boms/{bom_id}/lines/{line_id}/ordered", json={"ordered": False})
    assert row()["ordered"] is False


def test_assigning_across_boms_or_to_nothing_is_404(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    line_id = client.get(f"/api/boms/{bom_id}").json()["lines"][0]["id"]
    component_id = _stocked_component(client, "OTHER-PICK")

    # A line id that belongs to a different BOM must not be assignable through this one.
    other_id = _upload(client, name="second").json()["id"]
    crossed = client.put(
        f"/api/boms/{other_id}/lines/{line_id}/component",
        json={"component_id": component_id},
    )
    assert crossed.status_code == 404

    missing = client.put(
        f"/api/boms/{bom_id}/lines/999999/component",
        json={"component_id": component_id},
    )
    assert missing.status_code == 404
    # Removing an assignment that was never made says so rather than pretending.
    assert (
        client.delete(f"/api/boms/{bom_id}/lines/{line_id}/component").status_code
        == 404
    )


def test_list_and_delete(client: TestClient) -> None:
    bom_id = _upload(client).json()["id"]
    assert bom_id in [b["id"] for b in client.get("/api/boms").json()]

    assert client.delete(f"/api/boms/{bom_id}").status_code == 204
    assert client.get(f"/api/boms/{bom_id}").status_code == 404
    assert client.delete(f"/api/boms/{bom_id}").status_code == 404


def test_upload_of_a_columnless_file_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/boms",
        files={"file": ("bad.csv", b"Foo,Bar\n1,2\n", "text/csv")},
    )
    assert resp.status_code == 422


def test_upload_of_a_header_only_file_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/boms",
        files={"file": ("empty.csv", b"Reference,Value\n", "text/csv")},
    )
    assert resp.status_code == 422


def _read_only_headers(client: TestClient, anon_client: TestClient) -> dict[str, str]:
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "password123", "role": "read-only"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "viewer", "password": "password123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_read_only_can_read_but_not_write(
    client: TestClient, anon_client: TestClient
) -> None:
    bom_id = _upload(client).json()["id"]
    headers = _read_only_headers(client, anon_client)

    assert _upload(anon_client, headers=headers).status_code == 403
    assert anon_client.delete(f"/api/boms/{bom_id}", headers=headers).status_code == 403
    reimport = anon_client.post(f"/api/boms/{bom_id}/reimport", headers=headers)
    assert reimport.status_code == 403
    line_id = client.get(f"/api/boms/{bom_id}").json()["lines"][0]["id"]
    assign = anon_client.put(
        f"/api/boms/{bom_id}/lines/{line_id}/component",
        json={"component_id": 1},
        headers=headers,
    )
    assert assign.status_code == 403
    unassign = anon_client.delete(
        f"/api/boms/{bom_id}/lines/{line_id}/component", headers=headers
    )
    assert unassign.status_code == 403
    ordered = anon_client.put(
        f"/api/boms/{bom_id}/lines/{line_id}/ordered",
        json={"ordered": True},
        headers=headers,
    )
    assert ordered.status_code == 403
    # ...but reading the list, the detail and the report works.
    assert anon_client.get("/api/boms", headers=headers).status_code == 200
    assert anon_client.get(f"/api/boms/{bom_id}", headers=headers).status_code == 200
    report = anon_client.get(f"/api/boms/{bom_id}/report", headers=headers)
    assert report.status_code == 200


def test_anonymous_access_requires_auth(anon_client: TestClient) -> None:
    assert _upload(anon_client).status_code == 401
    assert anon_client.get("/api/boms").status_code == 401
