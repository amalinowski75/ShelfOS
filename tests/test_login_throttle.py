"""The sign-in throttle: unit tests for the counter, and both endpoints under it."""

from __future__ import annotations

import logging
import re
import threading
import time

import pytest
from app import config
from app.auth.throttle import LoginThrottle, log_name, throttle_key
from app.services import user_service as us
from fastapi.testclient import TestClient

from tests.conftest import web_login


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _fail(throttle: LoginThrottle, key: str) -> None:
    """Spend one attempt and leave it counted (i.e. the password was wrong)."""
    assert throttle.reserve(key).retry_after is None


def test_throttle_refuses_after_limit_and_reopens_after_window() -> None:
    clock = _Clock()
    throttle = LoginThrottle(limit=3, window=60, clock=clock)
    for _ in range(3):
        _fail(throttle, "1.2.3.4")
    assert throttle.reserve("1.2.3.4").retry_after == 60
    clock.now += 45
    assert throttle.reserve("1.2.3.4").retry_after == 15
    clock.now += 15
    # The oldest failure has aged out exactly now, so the address may try again.
    assert throttle.reserve("1.2.3.4").retry_after is None


def test_throttle_is_per_address() -> None:
    throttle = LoginThrottle(limit=1, window=60, clock=_Clock())
    _fail(throttle, "1.2.3.4")
    assert throttle.reserve("1.2.3.4").retry_after == 60
    assert throttle.reserve("5.6.7.8").retry_after is None


def test_throttle_disabled_by_zero_limit() -> None:
    throttle = LoginThrottle(limit=0, window=60, clock=_Clock())
    for _ in range(5):
        _fail(throttle, "1.2.3.4")
    assert throttle.reserve("1.2.3.4").retry_after is None


def test_missing_key_is_never_throttled() -> None:
    """No client address means no allowance to share — see client_address."""
    throttle = LoginThrottle(limit=1, window=60, clock=_Clock())
    for _ in range(5):
        assert throttle.reserve(None).retry_after is None


def test_a_reservation_counts_until_it_is_forgotten() -> None:
    """An attempt in flight counts, so concurrent guesses cannot all pass."""
    throttle = LoginThrottle(limit=2, window=60, clock=_Clock())
    first = throttle.reserve("1.2.3.4")
    second = throttle.reserve("1.2.3.4")
    assert first.retry_after is None and second.retry_after is None
    # Both are still in flight and already counted.
    assert throttle.reserve("1.2.3.4").retry_after == 60
    # One turns out to be a correct password and gives its slot back.
    second.forget()
    assert throttle.reserve("1.2.3.4").retry_after is None


def test_forgetting_a_reservation_twice_is_harmless() -> None:
    throttle = LoginThrottle(limit=2, window=60, clock=_Clock())
    reservation = throttle.reserve("1.2.3.4")
    reservation.forget()
    reservation.forget()
    assert throttle._failures == {}


def test_forgetting_a_refused_reservation_does_not_free_a_slot() -> None:
    """A refusal counted nothing, so taking it back must not uncount a failure."""
    throttle = LoginThrottle(limit=1, window=60, clock=_Clock())
    _fail(throttle, "1.2.3.4")
    refused = throttle.reserve("1.2.3.4")
    assert refused.retry_after == 60
    refused.forget()
    assert throttle.reserve("1.2.3.4").retry_after == 60


def test_sweep_is_amortised_not_run_on_every_failure() -> None:
    """The O(n) scan must not run per attempt: that is the attacker's own workload."""
    clock = _Clock()
    throttle = LoginThrottle(limit=3, window=60, clock=clock)
    sweeps = 0
    original = throttle._maybe_sweep

    def counting() -> None:
        nonlocal sweeps
        before = len(throttle._failures)
        original()
        if len(throttle._failures) != before:
            sweeps += 1

    throttle._maybe_sweep = counting  # type: ignore[method-assign]
    for i in range(2000):
        _fail(throttle, f"10.0.{i // 256}.{i % 256}")
    # Every key is live, so nothing has been swept away yet however often the
    # threshold was reached.
    assert sweeps == 0
    assert len(throttle._failures) == 2000
    clock.now += 61
    for i in range(2000):
        _fail(throttle, f"172.16.{i // 256}.{i % 256}")
    # The aged-out keys are gone, and the table tracks only what is live.
    assert len(throttle._failures) == 2000


def test_throttle_retry_after_is_at_least_one_second() -> None:
    clock = _Clock()
    throttle = LoginThrottle(limit=1, window=0.2, clock=clock)
    _fail(throttle, "1.2.3.4")
    assert throttle.reserve("1.2.3.4").retry_after == 1


def test_ipv6_addresses_share_an_allowance_per_64() -> None:
    """A host owns a whole /64, so rotating inside it must not buy new guesses."""
    throttle = LoginThrottle(limit=1, window=60, clock=_Clock())
    _fail(throttle, throttle_key("2001:db8::1"))
    assert throttle.reserve(throttle_key("2001:db8::dead:beef")).retry_after == 60
    # A different /64 is a different allocation and keeps its own allowance.
    assert throttle.reserve(throttle_key("2001:db8:0:1::1")).retry_after is None


def test_throttle_key_keeps_ipv4_addresses_apart() -> None:
    """Including v4-mapped ones, whose /64 would be a single shared ::/64 bucket."""
    assert throttle_key("203.0.113.9") == "203.0.113.9"
    assert throttle_key("::ffff:1.2.3.4") != throttle_key("::ffff:5.6.7.8")
    assert throttle_key("::ffff:1.2.3.4") == "1.2.3.4"
    assert throttle_key("not-an-address") == "not-an-address"


def test_log_name_strips_control_characters_and_bounds_length() -> None:
    assert log_name("bob\nFailed login for admin") == "bobFailed login for admin"
    assert len(log_name("x" * 500)) == 64


def test_log_name_strips_quotes_so_the_line_shape_is_fixed() -> None:
    """A fail2ban filter anchors on the quotes, so a name must not carry one."""
    assert log_name("ad'min") == "admin"
    assert log_name('ad"min') == "admin"


def _seed_admin(session) -> None:  # type: ignore[no-untyped-def]
    from app.models.enums import UserRole

    us.create_user(
        session, username="admin", password="admin-password", role=UserRole.ADMIN
    )


def _tighten(client: TestClient, limit: int = 3) -> LoginThrottle:  # noqa: D401
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
    for _ in range(2):
        assert web_login(anon_client, "admin", "wrong-password").status_code == 401
    resp = web_login(anon_client, "admin", "wrong-password")
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
    assert any(
        m.startswith("Failed login for 'admin' from ") for m in messages
    ), messages
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


def test_successful_login_gives_its_slot_back(
    session,  # type: ignore[no-untyped-def]
    anon_client: TestClient,
) -> None:
    """A right password must not spend an allowance a wrong one is saving up."""
    _seed_admin(session)
    _tighten(anon_client, limit=2)
    good = {"username": "admin", "password": "admin-password"}
    for _ in range(5):
        assert anon_client.post("/api/auth/token", json=good).status_code == 200
    bad = {"username": "admin", "password": "wrong-password"}
    assert anon_client.post("/api/auth/token", json=bad).status_code == 401


def test_a_quote_in_the_username_cannot_change_the_log_line(
    session,  # type: ignore[no-untyped-def]
    anon_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shape the README's fail2ban filter anchors on holds for any name."""
    _seed_admin(session)
    with caplog.at_level(logging.WARNING, logger="shelfos"):
        anon_client.post(
            "/api/auth/token",
            json={"username": "ad'min\"x", "password": "wrong-password"},
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        re.fullmatch(r"Failed login for 'adminx' from [^ ]+", m) for m in messages
    ), messages


def test_no_client_address_disables_the_throttle_rather_than_sharing_one(
    session,  # type: ignore[no-untyped-def]
    anon_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared bucket would let ten failures lock every account out at once."""
    from starlette.requests import Request

    _seed_admin(session)
    _tighten(anon_client, limit=2)
    monkeypatch.setattr(Request, "client", property(lambda self: None))
    bad = {"username": "admin", "password": "wrong-password"}
    for _ in range(5):
        assert anon_client.post("/api/auth/token", json=bad).status_code == 401
    # Nobody is locked out, because nobody could be told apart in the first place.
    good = {"username": "admin", "password": "admin-password"}
    assert anon_client.post("/api/auth/token", json=good).status_code == 200


def test_concurrent_attempts_cannot_all_pass_the_same_check() -> None:
    """The check and the count are one step, so N threads do not get N guesses.

    The sleep stands in for bcrypt, and is the whole point: a check that only
    counted *after* verifying the password would let every thread read a count
    none of them had written yet, and the limit would buy as many bcrypt rounds
    as the attacker can hold connections open. (Without it the threads serialise
    on the lock and even a non-atomic implementation passes this test.)
    """
    throttle = LoginThrottle(limit=3, window=60)
    threads = 50
    ready = threading.Barrier(threads)
    allowed: list[bool] = []
    guard = threading.Lock()

    def attempt() -> None:
        ready.wait()
        reservation = throttle.reserve("1.2.3.4")
        if reservation.retry_after is None:
            time.sleep(0.05)  # the password check this attempt now owns
        with guard:
            allowed.append(reservation.retry_after is None)

    workers = [threading.Thread(target=attempt) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sum(allowed) == 3
