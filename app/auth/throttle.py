"""Brute-force guard for the two sign-in endpoints (D11).

Counts failed sign-ins per client address over a sliding window and refuses
further attempts from an address that has spent its allowance. In memory and
per process — enough for one uvicorn worker, which is how ShelfOS runs; a
multi-worker deployment would need the counters in the database or a cache.

Keyed by address, not by account, so an attacker cannot lock a real user out
by hammering their name. The trade-off is that an attacker with many addresses
is throttled per address only; bcrypt's cost per attempt is what remains
against that, and the failed-attempt log line lets fail2ban act on the source.

An attempt is claimed *before* the password is verified and released again only
if the credentials were right (:meth:`LoginThrottle.reserve`), so a refused
address spends no bcrypt round, and attempts still in flight count against the
limit rather than all passing a check none of them has yet recorded.
"""

from __future__ import annotations

import contextlib
import ipaddress
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

# How much of a sign-in name goes into the log line, and what is taken out of
# it. A name is user input and the log line is a fixed shape a fail2ban filter
# matches ("Failed login for 'name' from <address>"), so the name must not be
# able to change that shape: control characters would forge a whole entry, and
# a quote would break the delimiter the filter anchors on. Note that %r cannot
# do this job — repr() switches to double quotes around a string containing an
# apostrophe, so a username with one in it would be logged in a shape the
# documented filter does not match, and the attacker choosing that username
# would never be banned.
_LOG_NAME_CHARS = 64
_UNLOGGABLE = re.compile(r"""[\x00-\x1f\x7f'"]""")

# A v6 client is normally given a whole /64, so a fresh address per request
# costs the attacker nothing; the allocation, not the address, is the thing
# worth counting.
_IPV6_PREFIX = 64

# Keys the failure table may hold before a sweep is worth its O(n) scan. The
# threshold then tracks the surviving size, so sweeping costs O(1) amortised
# per failure instead of a full scan under the lock on every one of them.
_SWEEP_MIN_KEYS = 1024


@dataclass(frozen=True)
class Reservation:
    """One claimed attempt: refused, or counted and revocable.

    ``retry_after`` set means the attempt was refused and nothing was counted.
    Otherwise the attempt has already been counted as a failure, and
    :meth:`forget` takes it back — which is what a successful sign-in does.
    """

    retry_after: int | None = None
    throttle: LoginThrottle | None = None
    key: str | None = None
    stamp: float | None = None

    def forget(self) -> None:
        """Take back the counted attempt (the credentials turned out right)."""
        if self.throttle is not None and self.key is not None:
            assert self.stamp is not None
            self.throttle._forget(self.key, self.stamp)


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
        self._sweep_at = _SWEEP_MIN_KEYS

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0

    def reserve(self, key: str | None) -> Reservation:
        """Claim one attempt for ``key``, or refuse it.

        Pruning, the limit check and the count all happen under one acquisition
        of the lock, so concurrent attempts cannot each read a count none of
        them has written yet and all proceed to the (deliberately slow) password
        check. A ``key`` of ``None`` is un-throttleable and always allowed; see
        :func:`client_address`.
        """
        if not self.enabled or key is None:
            return Reservation()
        with self._lock:
            failures = self._prune(key)
            if failures is not None and len(failures) >= self.limit:
                # The window reopens when the oldest failure still counted
                # expires. Whole seconds, rounded up, because this is sent as
                # ``Retry-After`` and "0 seconds" would invite an immediate
                # retry into another refusal.
                wait = failures[0] + self.window - self._clock()
                return Reservation(retry_after=max(1, math.ceil(wait)))
            if failures is None:
                failures = self._failures[key] = deque()
            stamp = self._clock()
            failures.append(stamp)
            self._maybe_sweep()
            return Reservation(throttle=self, key=key, stamp=stamp)

    def _forget(self, key: str, stamp: float) -> None:
        """Drop one counted attempt again (see :meth:`Reservation.forget`)."""
        with self._lock:
            failures = self._failures.get(key)
            if failures is None:
                return
            # Suppressed: the stamp may already have been pruned by age.
            with contextlib.suppress(ValueError):
                failures.remove(stamp)
            if not failures:
                del self._failures[key]

    def _prune(self, key: str) -> deque[float] | None:
        """Drop ``key``'s failures older than the window; None if none remain.

        Caller holds the lock.
        """
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

    def _maybe_sweep(self) -> None:
        """Forget aged-out keys, but only once the table is big enough to bother.

        The table holds one key per distinct address seen within the window, so
        a distributed attack can push it to many thousands of keys. Scanning all
        of them on every failure would make each attempt O(n) under the lock —
        the throttle paying the attacker's costs for them. Sweeping only when
        the table has grown past a threshold that then tracks what survived
        makes it O(1) amortised. Caller holds the lock.
        """
        if len(self._failures) < self._sweep_at:
            return
        cutoff = self._clock() - self.window
        stale = [k for k, f in self._failures.items() if not f or f[-1] <= cutoff]
        for key in stale:
            del self._failures[key]
        self._sweep_at = max(_SWEEP_MIN_KEYS, 2 * len(self._failures))


# Said once per process, not per request: a transport that reports no client is
# a property of the deployment, and one warning names it without turning an
# attack into a flood of identical lines.
_warned_about_missing_client = False


def client_address(request: Request) -> str | None:
    """The address a sign-in came from, or ``None`` if the transport gives none.

    ``request.client`` is what uvicorn reports, which behind a reverse proxy on
    the same host is the proxy's address — unless uvicorn is told to trust the
    proxy's ``X-Forwarded-For`` (``--proxy-headers``, on by default for
    ``--forwarded-allow-ips`` 127.0.0.1), in which case it is the real client.
    The README says so; without it every visitor would share one allowance.

    ``None`` when there is no client to name (some ASGI transports, e.g. a unix
    socket, do not populate it). Deliberately not a placeholder key: a shared
    one would put every visitor in a single bucket, where ten failed attempts
    from anyone would lock out everybody, the admin included, and nothing could
    tell that apart from a real attack. Not throttling is the safer failure
    here, and the warning says the guard is off.
    """
    global _warned_about_missing_client
    client = request.client
    if client and client.host:
        return client.host
    if not _warned_about_missing_client:
        _warned_about_missing_client = True
        _logger.warning(
            "The server reports no client address, so failed sign-ins cannot be "
            "counted per client and the sign-in throttle is inactive. Serve over "
            "TCP, or put a reverse proxy in front that uvicorn is told to trust."
        )
    return None


def throttle_key(address: str) -> str:
    """The allowance an address shares: itself, or its /64 for IPv6.

    A single IPv6 host normally owns a /64, so keying on the full address would
    let it rotate through 2**64 of them and never spend an allowance. IPv4 is
    keyed as-is, including an IPv4-mapped v6 address (``::ffff:1.2.3.4``), whose
    /64 would otherwise be ``::/64`` — one bucket shared by every v4 client
    reaching a dual-stack socket, which is the shared-allowance trap above.
    A host that does not parse as an address is used verbatim.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/{_IPV6_PREFIX}", strict=False))
    return str(ip)


def log_name(username: str) -> str:
    """A sign-in name as it is safe to write into a log line (see _UNLOGGABLE)."""
    return _UNLOGGABLE.sub("", username)[:_LOG_NAME_CHARS]


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
    where = address or "an unknown address"
    reservation = throttle.reserve(throttle_key(address) if address else None)
    if reservation.retry_after is not None:
        _logger.warning(
            "Login refused for '%s' from %s: too many failed attempts, retry in %ds",
            name,
            where,
            reservation.retry_after,
        )
        return LoginAttempt(retry_after=reservation.retry_after)
    user = authenticate()
    if user is None:
        # The attempt is already counted; the one line a fail2ban filter needs
        # is what remains. Its shape is fixed — see _UNLOGGABLE.
        _logger.warning("Failed login for '%s' from %s", name, where)
        return LoginAttempt()
    reservation.forget()
    _logger.info("Login for '%s' from %s", name, where)
    return LoginAttempt(user=user)
