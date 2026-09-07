"""Changing a password retires the sign-ins issued against the old one (D11).

Tokens are stateless and session cookies are signed, so neither is revocable by
deleting a server-side record — there isn't one. Both instead carry a
fingerprint of the password they were issued against, checked on every request.
"""

from __future__ import annotations

import re

from app.auth.tokens import CREDENTIAL_CLAIM, credential_fingerprint, decode_token
from app.models.enums import UserRole
from app.services import user_service as us
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import web_login


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(session: Session) -> None:
    us.create_user(
        session, username="admin", password="admin-password", role=UserRole.ADMIN
    )


def _csrf_from_page(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert match and match.group(1), "page did not expose a CSRF token"
    return match.group(1)


def _token(client: TestClient, username: str, password: str) -> str:
    resp = client.post(
        "/api/auth/token", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def test_fingerprint_changes_with_the_password(session: Session) -> None:
    user = us.create_user(session, username="alice", password="first-password")
    before = credential_fingerprint(user)
    assert before is not None
    us.set_password(session, user.id, "second-password", actor_id=1)
    assert credential_fingerprint(user) != before


def test_fingerprint_is_not_the_hash_itself(session: Session) -> None:
    """It travels in a readable JWT payload, so it must not carry the hash."""
    user = us.create_user(session, username="alice", password="first-password")
    fingerprint = credential_fingerprint(user)
    assert user.password_hash is not None
    assert fingerprint is not None
    assert user.password_hash not in fingerprint


def test_an_account_that_cannot_sign_in_has_no_fingerprint(session: Session) -> None:
    """The seeded system user has no password and must satisfy no check."""
    from app.seed import ensure_demo_user

    assert credential_fingerprint(ensure_demo_user(session)) is None


def test_token_carries_the_fingerprint(
    session: Session, anon_client: TestClient
) -> None:
    _seed_admin(session)
    claims = decode_token(_token(anon_client, "admin", "admin-password"))
    assert claims is not None
    user = us.get_by_username(session, "admin")
    assert user is not None
    assert claims[CREDENTIAL_CLAIM] == credential_fingerprint(user)


def test_admin_password_reset_kills_the_target_s_token(
    session: Session, client: TestClient, anon_client: TestClient
) -> None:
    """The point of resetting someone's password: their sessions stop, now."""
    client.post(
        "/api/admin/users",
        json={"username": "mallory", "password": "first-password", "role": "user"},
    )
    token = _token(anon_client, "mallory", "first-password")
    assert anon_client.get("/api/locations", headers=_bearer(token)).status_code == 200

    victim = us.get_by_username(session, "mallory")
    assert victim is not None
    resp = client.put(
        f"/api/admin/users/{victim.id}/password", json={"password": "second-password"}
    )
    assert resp.status_code == 200
    assert anon_client.get("/api/locations", headers=_bearer(token)).status_code == 401


def test_changing_your_own_password_retires_your_other_tokens(
    session: Session, anon_client: TestClient, client: TestClient
) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "carol", "password": "first-password", "role": "user"},
    )
    stale = _token(anon_client, "carol", "first-password")
    current = _token(anon_client, "carol", "first-password")
    resp = anon_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "first-password",
            "new_password": "second-password",
        },
        headers=_bearer(current),
    )
    assert resp.status_code == 200
    # Both tokens predate the new password, the one that made the change too.
    assert anon_client.get("/api/locations", headers=_bearer(stale)).status_code == 401
    assert (
        anon_client.get("/api/locations", headers=_bearer(current)).status_code == 401
    )
    # A new one works.
    fresh = _token(anon_client, "carol", "second-password")
    assert anon_client.get("/api/locations", headers=_bearer(fresh)).status_code == 200


def test_changing_your_password_in_the_browser_keeps_you_signed_in(
    session: Session, anon_client: TestClient
) -> None:
    """Otherwise the change would sign you out of the request that made it."""
    _seed_admin(session)
    web_login(anon_client, "admin", "admin-password")
    csrf = _csrf_from_page(anon_client.get("/").text)
    resp = anon_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "admin-password",
            "new_password": "second-password",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert anon_client.get("/", follow_redirects=False).status_code == 200


def test_admin_reset_signs_the_target_out_of_the_browser(
    session: Session, anon_client: TestClient, client: TestClient
) -> None:
    client.post(
        "/api/admin/users",
        json={"username": "dave", "password": "first-password", "role": "user"},
    )
    web_login(anon_client, "dave", "first-password")
    assert anon_client.get("/", follow_redirects=False).status_code == 200

    victim = us.get_by_username(session, "dave")
    assert victim is not None
    client.put(
        f"/api/admin/users/{victim.id}/password", json={"password": "second-password"}
    )
    resp = anon_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_a_sign_in_without_a_fingerprint_is_refused(
    session: Session, anon_client: TestClient
) -> None:
    """A cookie from before this check existed must not be treated as valid.

    Accepting a missing fingerprint would make the whole check optional at the
    caller's choosing; a one-off re-login is the cost of not doing that.
    """
    from app.web.routes import require_web_user
    from fastapi import HTTPException
    from starlette.requests import Request

    _seed_admin(session)
    user = us.get_by_username(session, "admin")
    assert user is not None
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [],
        "session": {"user_id": user.id},  # no credential binding
        "state": {},
    }
    try:
        require_web_user(Request(scope), session)
    except HTTPException as error:
        assert error.status_code == 303
    else:  # pragma: no cover - the guard above must fire
        raise AssertionError("a session with no credential binding was accepted")


def test_a_wrong_fingerprint_is_refused(
    session: Session, anon_client: TestClient
) -> None:
    _seed_admin(session)
    user = us.get_by_username(session, "admin")
    assert user is not None
    other = us.create_user(session, username="other", password="other-password")
    token = _token(anon_client, "admin", "admin-password")
    claims = decode_token(token)
    assert claims is not None
    # Re-sign the same subject with someone else's fingerprint.
    import jwt
    from app.config import SECRET_KEY

    forged = jwt.encode(
        {**claims, CREDENTIAL_CLAIM: credential_fingerprint(other)},
        SECRET_KEY,
        algorithm="HS256",
    )
    assert anon_client.get("/api/locations", headers=_bearer(forged)).status_code == 401


def test_admin_cannot_reset_their_own_password_through_the_admin_route(
    session: Session, client: TestClient
) -> None:
    """It would neither ask for the current password nor keep the caller in."""
    admin = us.get_by_username(session, "admin")
    assert admin is not None
    resp = client.put(
        f"/api/admin/users/{admin.id}/password", json={"password": "second-password"}
    )
    assert resp.status_code == 422
    assert "change-password" in resp.json()["detail"]
    # The password is untouched, so the caller is still signed in.
    assert client.get("/api/auth/me").status_code == 200


def test_the_users_feed_marks_your_own_row(
    session: Session, anon_client: TestClient
) -> None:
    """So the table can leave the Password action off the row that refuses it."""
    _seed_admin(session)
    web_login(anon_client, "admin", "admin-password")
    us.create_user(session, username="other", password="other-password")
    rows = anon_client.get("/web/api/users").json()["data"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["admin"]["is_self"] is True
    assert by_name["other"]["is_self"] is False
