"""Brute-force guard for the two sign-in endpoints (D11).

Counts failed sign-ins per client address over a sliding window and refuses
further attempts from an address that has spent its allowance. In memory and
per process — enough for one uvicorn worker, which is how ShelfOS runs; a
multi-worker deployment would need the counters in the database or a cache.

Keyed by address, not by account, so an attacker cannot lock a real user out
by hammering their name. The trade-off is that an attacker with many addresses
is throttled per address only; bcrypt's cost per attempt is what remains
against that, and the failed-attempt log line lets fail2ban act on the source.

The limiter is checked *before* the password is verified, so a throttled
address does not spend a bcrypt round either.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

_logger = logging.getLogger("shelfos")

# How much of a sign-in name goes into the log line. A name is user input and
# a log is a file people grep, so it is bounded and rendered with ``%r`` (which
# escapes newlines and other control characters — no forged log entries).
_LOG_NAME_CHARS = 64
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LoginThrottle:
    """Sliding-window failure counter per key (a client address).

    ``limit`` failures within ``window`` seconds refuse the key until the
    oldest of them has aged out. ``limit <= 0`` or ``window <= 0`` disables
    the throttle. ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        *,
        limit: int,
        window: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or ``None`` if it may now.

        Whole seconds, rounded up, because the value is sent as ``Retry-After``
        and "0 seconds" would tell a client to retry into a refusal.
        """
        if not self.enabled:
            return None
        with self._lock:
            failures = self._prune(key)
            if failures is None or len(failures) < self.limit:
                return None
            # The window reopens when the oldest failure still counted expires.
            wait = failures[0] + self.window - self._clock()
        return max(1, math.ceil(wait))

    def record_failure(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            failures = self._prune(key)
            if failures is None:
                failures = self._failures[key] = deque()
            failures.append(self._clock())
            # Bound memory: a key's deque never needs more than ``limit``
            # entries to decide it is over the limit, and stale keys go with
            # the sweep below, so a scan from many addresses cannot grow this
            # without bound past the window.
            while len(failures) > self.limit:
                failures.popleft()
            self._sweep()

    def _prune(self, key: str) -> deque[float] | None:
        """Drop ``key``'s failures older than the window; None if none remain."""
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = self._clock() - self.window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def _sweep(self) -> None:
        """Forget every key whose failures have all aged out."""
        cutoff = self._clock() - self.window
        stale = [k for k, f in self._failures.items() if f[-1] <= cutoff]
        for key in stale:
            del self._failures[key]


def client_address(request: Request) -> str:
    """The address a sign-in came from, as the throttle keys it and the log names it.

    ``request.client`` is what uvicorn reports, which behind a reverse proxy on
    the same host is the proxy's address — unless uvicorn is told to trust the
    proxy's ``X-Forwarded-For`` (``--proxy-headers``, on by default for
    ``--forwarded-allow-ips`` 127.0.0.1), in which case it is the real client.
    The README says so; without it every visitor would share one allowance.
    """
    client = request.client
    return client.host if client and client.host else "unknown"


def log_name(username: str) -> str:
    """A sign-in name as it is safe to write into a log line."""
    return _CONTROL.sub("", username)[:_LOG_NAME_CHARS]


@dataclass(frozen=True)
class LoginAttempt[U]:
    """What a sign-in attempt came to: a user, a refusal, or a wrong guess.

    ``retry_after`` is set when the throttle refused the attempt before the
    password was looked at; ``user`` is set when the credentials were right.
    Both ``None`` is a wrong username or password.
    """

    user: U | None = None
    retry_after: int | None = None


def attempt_login[U](
    request: Request,
    authenticate: Callable[[], U | None],
    *,
    username: str,
) -> LoginAttempt[U]:
    """Run one sign-in attempt through the throttle, logging the outcome.

    ``authenticate`` is the credential check to run if the throttle allows it
    (it is a callable so a refused attempt costs no bcrypt round). The throttle
    lives on ``request.app.state`` so every app instance — each test's included
    — has counters of its own.
    """
    throttle: LoginThrottle = request.app.state.login_throttle
    address = client_address(request)
    name = log_name(username)
    wait = throttle.retry_after(address)
    if wait is not None:
        _logger.warning(
            "Login refused for %r from %s: too many failed attempts, retry in %ds",
            name,
            address,
            wait,
        )
        return LoginAttempt(retry_after=wait)
    user = authenticate()
    if user is None:
        throttle.record_failure(address)
        # The one line a fail2ban filter needs: the outcome and the source.
        _logger.warning("Failed login for %r from %s", name, address)
        return LoginAttempt()
    _logger.info("Login for %r from %s", name, address)
    return LoginAttempt(user=user)
