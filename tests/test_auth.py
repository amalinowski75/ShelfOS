"""Integration tests for authentication and role enforcement (D11)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(session) -> None:  # type: ignore[no-untyped-def]
    from app.models.enums import UserRole
    from app.services import user_service as us

    us.create_user(session, username="admin", password="admin", role=UserRole.ADMIN)


def _csrf_from_page(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert match and match.group(1), "page did not expose a CSRF token"
    return match.group(1)


def test_cookie_write_requires_csrf_token(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    """Session-cookie writes need a matching CSRF token; bearer writes don't (M4)."""
    _seed_admin(session)
    anon_client.post("/login", data={"username": "admin", "password": "admin"})

    # Cookie-authenticated write without the token is rejected.
    resp = anon_client.post("/api/types", json={"name": "resistor"})
    assert resp.status_code == 403

    # A stale/wrong token is also rejected.
    resp = anon_client.post(
        "/api/types", json={"name": "resistor"}, headers={"X-CSRF-Token": "nope"}
    )
    assert resp.status_code == 403

    # The token rendered into the page authorizes the write.
    token = _csrf_from_page(anon_client.get("/").text)
    resp = anon_client.post(
        "/api/types", json={"name": "resistor"}, headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 201


def test_bearer_write_is_exempt_from_csrf(client: TestClient) -> None:
    """API clients authenticate per-request with a bearer token, so no CSRF."""
    resp = client.post("/api/types", json={"name": "resistor"})
    assert resp.status_code == 201


def test_bearer_with_non_numeric_subject_is_unauthenticated(
    anon_client: TestClient,
) -> None:
    """A validly-signed token with a non-numeric subject is a 401, not a 500 (L4)."""
    import jwt
    from app import config

    token = jwt.encode({"sub": "not-a-number"}, config.SECRET_KEY, algorithm="HS256")
    resp = anon_client.get("/api/locations", headers=_bearer(token))
    assert resp.status_code == 401


def test_malformed_bearer_token_is_unauthenticated(
    anon_client: TestClient,
) -> None:
    """A token that isn't a valid JWT decodes to ``None`` and yields a 401."""
    resp = anon_client.get("/api/locations", headers=_bearer("not.a.jwt"))
    assert resp.status_code == 401


def test_production_refuses_default_secret(monkeypatch) -> None:
    """Startup must abort on the public default secret in production (D11)."""
    from app import config
    from app.main import _check_insecure_defaults

    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "SECRET_KEY", config._DEFAULT_SECRET)
    with pytest.raises(RuntimeError, match="SHELFOS_SECRET_KEY"):
        _check_insecure_defaults()


def test_production_refuses_default_admin_password(monkeypatch) -> None:
    """A real secret but the default admin password is still fatal in prod."""
    from app import config
    from app.main import _check_insecure_defaults

    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-production-secret-value-32b")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    with pytest.raises(RuntimeError, match="SHELFOS_ADMIN_PASSWORD"):
        _check_insecure_defaults()


def test_development_tolerates_defaults(monkeypatch) -> None:
    """Insecure defaults are only warnings outside production."""
    from app import config
    from app.main import _check_insecure_defaults

    monkeypatch.setattr(config, "ENV", "development")
    monkeypatch.setattr(config, "SECRET_KEY", config._DEFAULT_SECRET)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    _check_insecure_defaults()  # does not raise


def _login(client: TestClient, username: str, password: str) -> str:
    resp = client.post(
        "/api/auth/token", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def test_unauthenticated_requests_are_401(anon_client: TestClient) -> None:
    assert anon_client.get("/api/locations").status_code == 401
    assert anon_client.post("/api/types", json={"name": "x"}).status_code == 401
    # Public endpoints stay open.
    assert anon_client.get("/health").status_code == 200


def test_token_login_and_me(client: TestClient) -> None:
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"


def test_login_with_wrong_password_is_401(anon_client: TestClient) -> None:
    resp = anon_client.post(
        "/api/auth/token", json={"username": "admin", "password": "nope"}
    )
    assert resp.status_code == 401


def test_read_only_can_read_but_not_write(
    client: TestClient, anon_client: TestClient
) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "viewer", "password": "pw", "role": "read-only"},
    )
    token = _login(anon_client, "viewer", "pw")

    # GET is allowed for read-only accounts.
    assert anon_client.get("/api/locations", headers=_bearer(token)).status_code == 200
    # Writes are rejected with 403.
    resp = anon_client.post(
        "/api/types", json={"name": "resistor"}, headers=_bearer(token)
    )
    assert resp.status_code == 403


def test_regular_user_can_write_but_not_admin(
    client: TestClient, anon_client: TestClient
) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "worker", "password": "pw", "role": "user"},
    )
    token = _login(anon_client, "worker", "pw")

    # A normal user can create catalog data.
    created = anon_client.post(
        "/api/types", json={"name": "resistor"}, headers=_bearer(token)
    )
    assert created.status_code == 201

    # But admin-only endpoints are forbidden.
    assert (
        anon_client.get("/api/admin/users", headers=_bearer(token)).status_code == 403
    )
    assert (
        anon_client.delete(
            "/api/admin/components/1", headers=_bearer(token)
        ).status_code
        == 403
    )


def test_admin_user_management_flow(client: TestClient) -> None:
    created = client.post(
        "/api/admin/users",
        json={"username": "sam", "password": "pw", "role": "user"},
    )
    assert created.status_code == 201
    body = created.json()
    # The password hash must never be exposed.
    assert "password_hash" not in body
    user_id = body["id"]

    users = client.get("/api/admin/users").json()
    assert any(u["name"] == "sam" for u in users)

    # Promote, disable, and reset password.
    assert (
        client.put(f"/api/admin/users/{user_id}/role", json={"role": "admin"}).json()[
            "role"
        ]
        == "admin"
    )
    assert (
        client.put(
            f"/api/admin/users/{user_id}/active", json={"is_active": False}
        ).json()["is_active"]
        is False
    )
    client.put(f"/api/admin/users/{user_id}/password", json={"password": "new"})


def test_disabled_user_cannot_log_in(
    client: TestClient, anon_client: TestClient
) -> None:
    created = client.post(
        "/api/admin/users",
        json={"username": "gone", "password": "pw", "role": "user"},
    ).json()
    client.put(f"/api/admin/users/{created['id']}/active", json={"is_active": False})

    resp = anon_client.post(
        "/api/auth/token", json={"username": "gone", "password": "pw"}
    )
    assert resp.status_code == 401
