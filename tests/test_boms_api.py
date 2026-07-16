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
    # ...but reading the list, the detail and the report works.
    assert anon_client.get("/api/boms", headers=headers).status_code == 200
    assert anon_client.get(f"/api/boms/{bom_id}", headers=headers).status_code == 200
    report = anon_client.get(f"/api/boms/{bom_id}/report", headers=headers)
    assert report.status_code == 200


def test_anonymous_access_requires_auth(anon_client: TestClient) -> None:
    assert _upload(anon_client).status_code == 401
    assert anon_client.get("/api/boms").status_code == 401
