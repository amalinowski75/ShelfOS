"""Tests for the SSRF-guarded URL fetch behind attach-from-URL (spec §10)."""

from __future__ import annotations

import socket
import threading

import httpx
import pytest
from app import config
from app.services import url_fetch
from app.services.errors import ValidationError


def _info(ip: str) -> list:  # type: ignore[type-arg]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def _resolve_to(monkeypatch, ip: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _info(ip))


def _ok(content: bytes = b"data", **headers: str):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(
        lambda req: httpx.Response(200, content=content, headers=headers)
    )


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("ftp://example.com/x")


def test_rejects_malformed_url() -> None:
    # Unbalanced IPv6 brackets make urlsplit raise ValueError → must be a 422, not 500.
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("http://[::1/x")


def test_rejects_when_no_free_slots(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sem = threading.BoundedSemaphore(1)
    sem.acquire()  # occupy the only slot
    monkeypatch.setattr(url_fetch, "_FETCH_SLOTS", sem)
    _resolve_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/x", transport=_ok())


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # link-local / cloud metadata
        "100.64.1.1",  # CGNAT 100.64.0.0/10 (is_private misses it)
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 link-local
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
    ],
)
def test_rejects_non_public_hosts(monkeypatch, ip: str) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, ip)
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("http://target.example/secret", transport=_ok())


def test_rejects_when_any_resolved_record_is_non_public(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    infos = _info("93.184.216.34") + _info("10.0.0.5")  # one public, one private
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("http://mixed.example/x", transport=_ok())


def test_filename_strips_control_characters() -> None:
    name = url_fetch._filename_from(
        "https://x/y", 'attachment; filename="a\x01b\x7f.pdf"'
    )
    assert name == "ab.pdf"


def test_fetches_and_names_from_content_disposition(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=b"PDFDATA",
            headers={"content-disposition": 'attachment; filename="datasheet.pdf"'},
        )
    )
    data, name = url_fetch.fetch_url("https://example.com/dl?id=1", transport=transport)
    assert data == b"PDFDATA"
    assert name == "datasheet.pdf"


def test_names_from_url_path_when_no_disposition(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    _data, name = url_fetch.fetch_url(
        "https://example.com/files/part%20A.pdf", transport=_ok()
    )
    assert name == "part A.pdf"  # basename, percent-decoded


def test_rejects_oversize_via_content_length(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = _ok(
        content=b"x", **{"content-length": str(config.MAX_ATTACHMENT_BYTES + 1)}
    )
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/big", transport=transport)


def test_rejects_oversize_when_streamed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(config, "MAX_ATTACHMENT_BYTES", 8)
    # An iterable body streams with no content-length, so the byte-count cap fires.
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=iter([b"123456789"]))
    )
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/x", transport=transport)


def test_sends_a_browser_user_agent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["ua"] = req.headers.get("user-agent", "")
        return httpx.Response(200, content=b"ok")

    url_fetch.fetch_url("https://example.com/x", transport=httpx.MockTransport(handler))
    assert seen["ua"].startswith("Mozilla/")  # not the default python-httpx UA


def test_non_2xx_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/missing", transport=transport)


def test_follows_and_revalidates_redirects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "https://example.com/b"})
        return httpx.Response(200, content=b"final")

    data, _name = url_fetch.fetch_url(
        "https://example.com/a", transport=httpx.MockTransport(handler)
    )
    assert data == b"final"


def test_follows_a_relative_redirect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})  # relative
        return httpx.Response(200, content=b"ok")

    data, _name = url_fetch.fetch_url(
        "https://example.com/a", transport=httpx.MockTransport(handler)
    )
    assert data == b"ok"


def test_redirect_to_a_non_http_scheme_is_blocked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/a", transport=transport)


def test_redirect_to_a_private_host_is_blocked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def resolve(host, *a, **k):  # type: ignore[no-untyped-def]
        if host == "internal.example":
            return _info("10.0.0.9")
        return _info("93.184.216.34")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "http://internal.example/x"})
    )
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://public.example/a", transport=transport)


def test_too_many_redirects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "https://example.com/loop"})
    )
    with pytest.raises(ValidationError):
        url_fetch.fetch_url("https://example.com/loop", transport=transport)
