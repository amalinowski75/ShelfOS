"""API-level tests for file attachments (spec §10) — multipart upload + download."""

from __future__ import annotations

import pytest
from app import config
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point every test's attachment store at a throwaway directory."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def _component_id(client: TestClient) -> int:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    return client.post("/api/components", json={"type_id": ctype["id"]}).json()["id"]


def _invoice_id(client: TestClient) -> int:
    return client.post(
        "/api/invoices",
        json={
            "supplier": "Mouser",
            "invoice_number": "INV-1",
            "invoice_date": "2026-07-15",
            "currency": "EUR",
        },
    ).json()["id"]


def _upload(
    http: TestClient,
    entity_id: int,
    *,
    filename: str = "d.pdf",
    content: bytes = b"%PDF-1.4 bytes",
    content_type: str = "application/pdf",
    entity_type: str = "component",
    kind: str = "datasheet",
    notes: str | None = None,
    headers: dict[str, str] | None = None,
):  # type: ignore[no-untyped-def]
    data = {"entity_type": entity_type, "entity_id": entity_id, "kind": kind}
    if notes is not None:
        data["notes"] = notes
    return http.post(
        "/api/attachments",
        files={"file": (filename, content, content_type)},
        data=data,
        headers=headers or {},
    )


def test_upload_returns_metadata_without_file_path(client: TestClient) -> None:
    component_id = _component_id(client)
    resp = _upload(client, component_id)

    assert resp.status_code == 201
    body = resp.json()
    assert body["entity_type"] == "component"
    assert body["entity_id"] == component_id
    assert body["kind"] == "datasheet"
    assert body["filename"] == "d.pdf"
    # The internal on-disk path is never exposed.
    assert "file_path" not in body


def test_download_returns_bytes_and_headers(client: TestClient) -> None:
    component_id = _component_id(client)
    att_id = _upload(client, component_id, content=b"hello-pdf").json()["id"]

    resp = client.get(f"/api/attachments/{att_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"hello-pdf"
    assert resp.headers["content-type"] == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and "d.pdf" in disposition


def test_download_of_extensionless_file_falls_back_to_octet_stream(
    client: TestClient,
) -> None:
    component_id = _component_id(client)
    att_id = _upload(
        client, component_id, filename="readme", content_type="text/plain"
    ).json()["id"]

    resp = client.get(f"/api/attachments/{att_id}/download")
    assert resp.headers["content-type"] == "application/octet-stream"


def test_list_returns_metadata(client: TestClient) -> None:
    component_id = _component_id(client)
    _upload(client, component_id, notes="rev B")

    resp = client.get(
        "/api/attachments",
        params={"entity_type": "component", "entity_id": component_id},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["filename"] == "d.pdf"
    assert rows[0]["notes"] == "rev B"  # notes round-trips through the API
    assert "file_path" not in rows[0]  # and the on-disk path is never exposed


def test_attachments_work_for_invoices_too(client: TestClient) -> None:
    invoice_id = _invoice_id(client)
    resp = _upload(
        client,
        invoice_id,
        entity_type="invoice",
        kind="invoice_pdf",
        filename="inv.pdf",
    )
    assert resp.status_code == 201

    rows = client.get(
        "/api/attachments",
        params={"entity_type": "invoice", "entity_id": invoice_id},
    ).json()
    assert [r["kind"] for r in rows] == ["invoice_pdf"]


def test_download_handles_a_non_ascii_filename(client: TestClient) -> None:
    component_id = _component_id(client)
    att_id = _upload(client, component_id, filename="rezystörs ą.pdf").json()["id"]

    resp = client.get(f"/api/attachments/{att_id}/download")
    assert resp.status_code == 200
    # Starlette RFC 5987-encodes a non-ASCII name; the header stays well-formed.
    assert "attachment" in resp.headers["content-disposition"]


def test_delete_removes_the_attachment(client: TestClient) -> None:
    component_id = _component_id(client)
    att_id = _upload(client, component_id).json()["id"]

    assert client.delete(f"/api/attachments/{att_id}").status_code == 204
    assert client.get(f"/api/attachments/{att_id}/download").status_code == 404
    assert client.delete(f"/api/attachments/{att_id}").status_code == 404


def test_download_unknown_id_404(client: TestClient) -> None:
    assert client.get("/api/attachments/999/download").status_code == 404


def test_upload_unknown_entity_type_422(client: TestClient) -> None:
    resp = _upload(client, 1, entity_type="widget")
    assert resp.status_code == 422


def test_upload_missing_entity_404(client: TestClient) -> None:
    resp = _upload(client, 999)
    assert resp.status_code == 404


def test_upload_empty_file_422(client: TestClient) -> None:
    component_id = _component_id(client)
    resp = _upload(client, component_id, content=b"")
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
    component_id = _component_id(client)
    att_id = _upload(client, component_id).json()["id"]
    headers = _read_only_headers(client, anon_client)

    # Writes are blocked for read-only accounts...
    assert _upload(anon_client, component_id, headers=headers).status_code == 403
    delete = anon_client.delete(f"/api/attachments/{att_id}", headers=headers)
    assert delete.status_code == 403
    # ...but reads (list + download) work.
    listed = anon_client.get(
        "/api/attachments",
        params={"entity_type": "component", "entity_id": component_id},
        headers=headers,
    )
    assert listed.status_code == 200
    download = anon_client.get(f"/api/attachments/{att_id}/download", headers=headers)
    assert download.status_code == 200


def test_anonymous_access_requires_auth(anon_client: TestClient) -> None:
    # Every endpoint — including the read-only GETs — needs an authenticated user.
    assert _upload(anon_client, 1).status_code == 401
    assert (
        anon_client.get(
            "/api/attachments", params={"entity_type": "component", "entity_id": 1}
        ).status_code
        == 401
    )
    assert anon_client.get("/api/attachments/1/download").status_code == 401
