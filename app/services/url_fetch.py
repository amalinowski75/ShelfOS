"""Fetch a public URL's bytes server-side for storage as an attachment (§10).

SSRF is the risk here — the server makes an HTTP request to a user-supplied URL —
so this is deliberately restrictive: only ``http``/``https``; the resolved host
must be a *globally routable* address (``ip.is_global`` — which rejects loopback,
RFC1918, link-local incl. the cloud metadata endpoint, CGNAT, reserved,
multicast and unspecified); redirects are followed manually so every hop is
re-validated; the body is streamed with a hard size cap; and there are per-read
and total-fetch timeouts.

Residual risk: DNS rebinding (the host could resolve differently between our
guard and httpx's own connection). Accepted for authenticated writers; a stricter
build would pin the connection to the validated IP.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from email.message import Message
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app import config
from app.services.errors import ValidationError

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_FILENAME = "download"
_MAX_FILENAME_LEN = 255
# Process-wide cap on concurrent fetches (sync route runs on the shared threadpool).
_FETCH_SLOTS = threading.BoundedSemaphore(config.ATTACHMENT_URL_MAX_CONCURRENCY)


def _request_headers() -> dict[str, str]:
    # Browser-like headers so WAFs that tarpit non-browser clients let us through.
    return {
        "User-Agent": config.ATTACHMENT_URL_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _reject(reason: str) -> ValidationError:
    return ValidationError(f"could not fetch the URL: {reason}")


def _guard_host(host: str | None) -> None:
    """Reject a host that resolves to any non-public address (SSRF guard)."""
    if not host:
        raise _reject("missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, ValueError):
        raise _reject("host could not be resolved") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before classifying.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        # `is_global` is False for private/loopback/link-local/reserved/multicast/
        # unspecified AND for CGNAT (100.64.0.0/10, which `is_private` misses), so
        # "public only" is the single canonical, future-proof check.
        if not ip.is_global:
            raise _reject("host resolves to a non-public address")


def _guard_url(url: str) -> str:
    """Validate a URL's scheme and host; return it unchanged, or raise."""
    try:
        parts = urlsplit(url)  # e.g. an unbalanced-bracket IPv6 URL raises ValueError
    except ValueError:
        raise _reject("malformed URL") from None
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise _reject("only http and https URLs are allowed")
    _guard_host(parts.hostname)
    return url


def _filename_from(url: str, content_disposition: str | None) -> str:
    """A safe download filename from Content-Disposition or the URL path."""
    name = ""
    if content_disposition:
        # Let the stdlib parse filename / filename* (RFC 5987) for us.
        msg = Message()
        msg["content-disposition"] = content_disposition
        name = msg.get_filename() or ""
    if not name:
        name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    # Strip any path components and control characters (CR/LF/NUL could otherwise
    # ride a Content-Disposition header into the stored filename); keep it bounded.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if not name or name in {".", ".."}:
        name = _DEFAULT_FILENAME
    return name[:_MAX_FILENAME_LEN]


def fetch_url(
    url: str, *, transport: httpx.BaseTransport | None = None
) -> tuple[bytes, str]:
    """Download ``url`` and return ``(data, filename)``.

    Raises :class:`ValidationError` for a bad scheme, a non-public host, a
    non-2xx response, too many redirects, an over-size body, or a network error.
    ``transport`` is an injection point for tests.
    """
    cap = config.MAX_ATTACHMENT_BYTES
    # Bound concurrent fetches so a burst of slow downloads can't exhaust the sync
    # worker-thread pool and stall every other endpoint.
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise _reject("too many downloads in progress — try again shortly")
    try:
        # A hard wall-clock deadline across ALL hops (connect + read): each hop's
        # timeout is clamped to the remaining budget, so a server that stalls on
        # connect or trickles bytes can't hold a worker thread past this ceiling.
        deadline = time.monotonic() + config.ATTACHMENT_URL_TOTAL_TIMEOUT
        current = _guard_url(url)
        return _fetch(current, cap, deadline, transport)
    finally:
        _FETCH_SLOTS.release()


def _fetch(
    current: str,
    cap: int,
    deadline: float,
    transport: httpx.BaseTransport | None,
) -> tuple[bytes, str]:
    with httpx.Client(
        transport=transport,
        follow_redirects=False,
        headers=_request_headers(),
    ) as client:
        for _ in range(config.ATTACHMENT_URL_MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _reject("timed out")
            hop_timeout = httpx.Timeout(min(config.ATTACHMENT_URL_TIMEOUT, remaining))
            try:
                with client.stream("GET", current, timeout=hop_timeout) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise _reject("redirect without a location")
                        current = _guard_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise _reject("the server returned an error")
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > cap:
                        raise _reject(
                            f"file exceeds the {config.MAX_ATTACHMENT_MB} MB limit"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        if time.monotonic() > deadline:
                            raise _reject("timed out")
                        total += len(chunk)
                        if total > cap:
                            raise _reject(
                                f"file exceeds the {config.MAX_ATTACHMENT_MB} MB limit"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        raise _reject("the URL returned no data")
                    filename = _filename_from(
                        current, resp.headers.get("content-disposition")
                    )
                    return data, filename
            except httpx.HTTPError:
                raise _reject("could not reach the server") from None
        raise _reject("too many redirects")
