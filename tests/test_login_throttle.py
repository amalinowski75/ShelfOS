"""The sign-in throttle: unit tests for the counter, and both endpoints under it."""

from __future__ import annotations

import logging

import pytest
from app import config
from app.auth.throttle import LoginThrottle, log_name
from app.services import user_service as us
from fastapi.testclient import TestClient


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_throttle_refuses_after_limit_and_reopens_after_window() -> None:
    clock = _Clock()
    throttle = LoginThrottle(limit=3, window=60, clock=clock)
    for _ in range(3):
        assert throttle.retry_after("1.2.3.4") is None
        throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 60
    clock.now += 45
    assert throttle.retry_after("1.2.3.4") == 15
    clock.now += 15
    # The oldest failure has aged out exactly now, so the address may try again.
    assert throttle.retry_after("1.2.3.4") is None


def test_throttle_is_per_address() -> None:
    throttle = LoginThrottle(limit=1, window=60, clock=_Clock())
    throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 60
    assert throttle.retry_after("5.6.7.8") is None


def test_throttle_disabled_by_zero_limit() -> None:
    throttle = LoginThrottle(limit=0, window=60, clock=_Clock())
    for _ in range(5):
        throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") is None


def test_throttle_forgets_addresses_whose_failures_aged_out() -> None:
    """A scan from many addresses must not grow the counter table forever."""
    clock = _Clock()
    throttle = LoginThrottle(limit=3, window=60, clock=clock)
    for i in range(100):
        throttle.record_failure(f"10.0.0.{i}")
    assert len(throttle._failures) == 100
    clock.now += 61
    throttle.record_failure("10.0.1.1")  # any write sweeps the stale ones
    assert list(throttle._failures) == ["10.0.1.1"]


def test_throttle_retry_after_is_at_least_one_second() -> None:
    clock = _Clock()
    throttle = LoginThrottle(limit=1, window=0.2, clock=clock)
    throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 1


def test_log_name_strips_control_characters_and_bounds_length() -> None:
    assert log_name("bob\nFailed login for 'admin'") == "bobFailed login for 'admin'"
    assert len(log_name("x" * 500)) == 64


def _seed_admin(session) -> None:  # type: ignore[no-untyped-def]
    from app.models.enums import UserRole

    us.create_user(
        session, username="admin", password="admin-password", role=UserRole.ADMIN
    )


def _tighten(client: TestClient, limit: int = 3) -> LoginThrottle:
    """Give the client's app a throttle small enough to hit in a test."""
    throttle = LoginThrottle(limit=limit, window=600)
    client.app.state.login_throttle = throttle  # type: ignore[attr-defined]
    return throttle


def test_api_token_endpoint_is_throttled(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    _seed_admin(session)
    _tighten(anon_client, limit=3)
    bad = {"username": "admin", "password": "wrong-password"}
    for _ in range(3):
        assert anon_client.post("/api/auth/token", json=bad).status_code == 401
    resp = anon_client.post("/api/auth/token", json=bad)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    # Refused before the password is looked at: the right one is refused too.
    good = {"username": "admin", "password": "admin-password"}
    assert anon_client.post("/api/auth/token", json=good).status_code == 429


def test_web_login_is_throttled(session, anon_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    _seed_admin(session)
    _tighten(anon_client, limit=2)
    bad = {"username": "admin", "password": "wrong-password"}
    for _ in range(2):
        assert anon_client.post("/login", data=bad).status_code == 401
    resp = anon_client.post("/login", data=bad)
    assert resp.status_code == 429
    assert "Too many failed sign-in attempts" in resp.text
    assert "Retry-After" in resp.headers


def test_successful_login_does_not_reset_the_counter(
    session,
    anon_client: TestClient,  # type: ignore[no-untyped-def]
) -> None:
    """Knowing one valid account must not buy unlimited guesses at another."""
    _seed_admin(session)
    _tighten(anon_client, limit=2)
    bad = {"username": "admin", "password": "wrong-password"}
    good = {"username": "admin", "password": "admin-password"}
    assert anon_client.post("/api/auth/token", json=bad).status_code == 401
    assert anon_client.post("/api/auth/token", json=good).status_code == 200
    assert anon_client.post("/api/auth/token", json=bad).status_code == 401
    assert anon_client.post("/api/auth/token", json=bad).status_code == 429


def test_failed_login_is_logged_with_name_and_address(
    session,
    anon_client: TestClient,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    """The line a fail2ban filter matches: who was tried, from where."""
    _seed_admin(session)
    with caplog.at_level(logging.WARNING, logger="shelfos"):
        anon_client.post(
            "/api/auth/token", json={"username": "admin", "password": "wrong-password"}
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Failed login for 'admin' from ") for m in messages), (
        messages
    )
    # The password never reaches the log.
    assert not any("wrong-password" in m for m in messages)


def test_throttled_login_is_logged(
    session,
    anon_client: TestClient,
    caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    _seed_admin(session)
    _tighten(anon_client, limit=1)
    bad = {"username": "admin", "password": "wrong-password"}
    anon_client.post("/api/auth/token", json=bad)
    with caplog.at_level(logging.WARNING, logger="shelfos"):
        anon_client.post("/api/auth/token", json=bad)
    assert any(
        r.getMessage().startswith("Login refused for 'admin' from ")
        for r in caplog.records
    )


def test_app_throttle_is_built_from_config(anon_client: TestClient) -> None:
    throttle = anon_client.app.state.login_throttle  # type: ignore[attr-defined]
    assert throttle.limit == config.LOGIN_MAX_FAILURES
    assert throttle.window == config.LOGIN_FAILURE_WINDOW_SECONDS
