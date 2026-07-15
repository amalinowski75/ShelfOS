"""Baseline security response headers set on every response (create_app)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_baseline_security_headers_on_every_response(anon_client: TestClient) -> None:
    resp = anon_client.get("/health")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # HSTS is production-only (see below).
    assert "strict-transport-security" not in resp.headers


def test_security_headers_also_on_error_responses(anon_client: TestClient) -> None:
    # A rejected (401) request still carries the framing/sniffing protections.
    resp = anon_client.get(
        "/api/attachments",
        params={"entity_type": "component", "entity_id": 1},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers["x-frame-options"] == "DENY"


def test_hsts_is_sent_only_in_production(
    anon_client: TestClient, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from app import config

    monkeypatch.setattr(config, "is_production", lambda: True)
    resp = anon_client.get("/health")
    assert resp.headers["strict-transport-security"].startswith("max-age=")


def test_route_set_header_is_preserved_not_duplicated(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # The attachment download sets its own nosniff; the middleware uses setdefault,
    # so it stays exactly once and the framing headers are still added alongside.
    from app import config

    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    cid = client.post("/api/components", json={"type_id": ctype["id"]}).json()["id"]
    att = client.post(
        "/api/attachments",
        files={"file": ("d.pdf", b"%PDF-1.4", "application/pdf")},
        data={"entity_type": "component", "entity_id": cid, "kind": "datasheet"},
    ).json()

    resp = client.get(f"/api/attachments/{att['id']}/download")
    assert resp.headers["x-content-type-options"] == "nosniff"  # not "nosniff, nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
