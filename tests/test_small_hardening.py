"""The three low-consequence gaps left over from the security review.

The API docs behind the admin session, CSRF on the two plain HTML form posts,
and a sign-in that takes the same time whether or not the username exists.
"""

from __future__ import annotations

import time

from app.models.enums import UserRole
from app.services import user_service as us
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import web_login, web_logout


def _seed_admin(session: Session) -> None:
    us.create_user(
        session, username="admin", password="admin-password", role=UserRole.ADMIN
    )


# --- API documentation -------------------------------------------------------


def test_docs_and_schema_are_not_public(anon_client: TestClient) -> None:
    """An inventory of every endpoint is a map for whoever is probing."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        resp = anon_client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_docs_are_served_to_an_admin(session: Session, anon_client: TestClient) -> None:
    _seed_admin(session)
    web_login(anon_client, "admin", "admin-password")
    assert anon_client.get("/docs").status_code == 200
    assert anon_client.get("/redoc").status_code == 200
    schema = anon_client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "ShelfOS"


def test_docs_are_refused_to_a_non_admin(
    session: Session, anon_client: TestClient
) -> None:
    us.create_user(session, username="bob", password="bob-password")
    web_login(anon_client, "bob", "bob-password")
    resp = anon_client.get("/docs", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_the_docs_routes_are_not_in_the_schema(
    session: Session, anon_client: TestClient
) -> None:
    """They are ours, not part of the API being documented."""
    _seed_admin(session)
    web_login(anon_client, "admin", "admin-password")
    paths = anon_client.get("/openapi.json").json()["paths"]
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert path not in paths


# --- CSRF on the login and logout forms --------------------------------------


def test_login_without_the_form_token_is_refused(
    session: Session, anon_client: TestClient
) -> None:
    _seed_admin(session)
    resp = anon_client.post(
        "/login",
        data={"username": "admin", "password": "admin-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "expired" in resp.text
    # And it did not sign anyone in.
    assert anon_client.get("/", follow_redirects=False).status_code == 303


def test_a_refused_login_form_costs_no_allowance(
    session: Session, anon_client: TestClient
) -> None:
    """A post with no token is not a wrong guess, so it must not be counted."""
    from app.auth.throttle import LoginThrottle

    _seed_admin(session)
    throttle = LoginThrottle(limit=2, window=600)
    anon_client.app.state.login_throttle = throttle  # type: ignore[attr-defined]
    for _ in range(5):
        anon_client.post(
            "/login", data={"username": "admin", "password": "wrong-password"}
        )
    # Nothing was counted, so a real sign-in still works.
    assert web_login(anon_client, "admin", "admin-password").status_code == 303


def test_login_page_serves_a_token_the_form_posts_back(
    anon_client: TestClient,
) -> None:
    page = anon_client.get("/login")
    assert 'name="csrf_token"' in page.text


def test_logout_without_the_form_token_is_refused(
    session: Session, anon_client: TestClient
) -> None:
    _seed_admin(session)
    web_login(anon_client, "admin", "admin-password")
    assert anon_client.post("/logout", follow_redirects=False).status_code == 403
    # Still signed in.
    assert anon_client.get("/", follow_redirects=False).status_code == 200
    # And the real form still works.
    assert web_logout(anon_client).status_code == 303


def test_signing_in_replaces_the_pre_login_session(
    session: Session, anon_client: TestClient
) -> None:
    """Session fixation: the cookie handed to the browser before it signed in
    must not be the one that is signed in afterwards."""
    _seed_admin(session)
    anon_client.get("/login")
    before = anon_client.cookies.get("session")
    assert before  # the form token lives in it
    web_login(anon_client, "admin", "admin-password")
    assert anon_client.cookies.get("session") != before


# --- Login enumeration by timing ---------------------------------------------


def test_a_missing_account_costs_a_bcrypt_round_too(
    session: Session, anon_client: TestClient
) -> None:
    """Otherwise the response time answers "does this username exist?".

    Timed rather than asserted on a call count because the cost is the point;
    the bound is loose enough not to be flaky, and the unfixed code is not
    close to it — an unknown username used to return before hashing anything.
    """
    _seed_admin(session)
    us.get_by_username(session, "admin")

    def elapsed(username: str) -> float:
        start = time.perf_counter()
        anon_client.post(
            "/api/auth/token", json={"username": username, "password": "wrong-password"}
        )
        return time.perf_counter() - start

    elapsed("admin")  # warm the cached absent-account hash and any import cost
    known = min(elapsed("admin") for _ in range(3))
    unknown = min(elapsed("nobody-with-this-name") for _ in range(3))
    assert unknown > known / 2, (known, unknown)


def test_authenticate_rejects_an_unknown_name_without_leaking_it(
    session: Session,
) -> None:
    assert us.authenticate(session, "nobody", "some-password") is None


def test_the_absent_account_hash_is_stable_and_unmatchable(session: Session) -> None:
    from app.services.user_service import _absent_password_hash

    assert _absent_password_hash() == _absent_password_hash()  # cached
    assert not us.verify_password("", _absent_password_hash())
