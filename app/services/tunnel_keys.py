"""The keys that may bring a label printer to this server (spec §7).

A machine with a printer reaches ShelfOS over an ssh tunnel, which needs its key
authorised here. The person holding that printer has a browser and nothing else
— no account on this machine, and often no idea what an account on this machine
would be — so the setup script registers its own key through the web app.

That is possible because sshd does not read the tunnel account's keys from a
file in its home. A deploy points ``AuthorizedKeysCommand`` at a small root-owned
script that prints this file, which ShelfOS owns and writes like any other data.
Nothing here needs privileges, and nothing here can gain any:

* the ``Match User`` block a deploy installs is the ceiling. It permits remote
  forwarding of one loopback port and nothing else — no shell, no other port, no
  connections out — whatever ends up in this file. The options written onto each
  line say the same thing a second time, for a server set up by hand;
* keys are validated before they are written. This file is configuration to
  sshd, so a line carrying its own options, or a second key smuggled onto a
  second line, is refused rather than escaped.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app import config
from app.services.errors import PrinterError, ValidationError

# The key types worth accepting. Ed25519 is what the setup script makes; the
# others are here because somebody's existing key may be one of them.
_KEY_TYPES: Final = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_BODY = re.compile(r"^[A-Za-z0-9+/]{32,}={0,3}$")
_COMMENT = re.compile(r"^[A-Za-z0-9._@:+-]+([ \t][A-Za-z0-9._@:+-]+)*$")
_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")

_MAX_LINE = 4096
_MAX_KEYS = 50


@dataclass(frozen=True)
class TunnelKey:
    """One authorised machine, as a person would recognise it."""

    comment: str
    key_type: str
    fingerprint: str


def configured() -> bool:
    """Whether this server can register a printer's key at all."""
    return bool(config.TUNNEL_KEYS_FILE)


def _store() -> Path:
    if not configured():
        raise PrinterError(
            "this ShelfOS was not set up to register label printers, so the key "
            "has to be authorised on the server by hand "
            "(./shelfos.sh tunnel-key add)"
        )
    return Path(config.TUNNEL_KEYS_FILE)


def options_for(port: int) -> str:
    """The restrictions written onto a key's line.

    Belt and braces: the sshd ``Match`` block already caps every one of these,
    and an install done by hand from the README may not have it. ``permitopen``
    is not decoration — ``port-forwarding`` re-enables forwarding in *both*
    directions, so without it the same key could open connections from this
    server to anything it can reach.
    """
    return (
        'restrict,port-forwarding,permitopen="127.0.0.1:1",'
        f'permitlisten="127.0.0.1:{port}"'
    )


def split_key(line: str) -> tuple[str, str, str]:
    """``(type, body, comment)`` of a plain public key, or raise.

    The one shape accepted is ``type base64 [comment]``. Anything else — options
    in front, a second line, a comment with a newline in it — is refused, since
    what a key may do is decided here and not by the person sending it.
    """
    line = line.strip()
    if not line or len(line) > _MAX_LINE or not _PRINTABLE.match(line):
        raise ValidationError(
            "that is not a public key: expected one line of 'type base64 comment'"
        )
    parts = line.split(" ", 2)
    if len(parts) < 2:
        raise ValidationError("that is not a public key: it has no key in it")
    key_type, body = parts[0], parts[1]
    comment = parts[2].strip() if len(parts) == 3 else ""
    if key_type not in _KEY_TYPES:
        raise ValidationError(
            f"{key_type!r} is not a key type this accepts; ed25519 is what the "
            "setup script makes"
        )
    if not _BODY.match(body):
        raise ValidationError("the key itself is not base64")
    if comment and (len(comment) > 128 or not _COMMENT.match(comment)):
        raise ValidationError(
            "the key's comment must be plain text — letters, digits and ._@:+-"
        )
    return key_type, body, comment


def fingerprint_of(body: str) -> str:
    """The SHA256 fingerprint ssh itself would print for this key."""
    import base64
    import hashlib

    try:
        raw = base64.b64decode(body, validate=True)
    except Exception:  # noqa: BLE001 - malformed base64 is a validation problem
        raise ValidationError("the key itself is not base64") from None
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def _read_lines() -> list[str]:
    try:
        return [
            line.strip()
            for line in _store().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except FileNotFoundError:
        return []
    except OSError as error:
        raise PrinterError(f"cannot read the authorised keys: {error}") from None


def _write_lines(lines: list[str]) -> None:
    """Replace the file atomically, so sshd never reads a half-written one.

    sshd may run the command that prints this at any moment, including while it
    is being changed — a truncate-and-write would show it an empty key list and
    refuse a tunnel that is perfectly entitled to connect.
    """
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".keys-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")
        # Readable by the command sshd runs, writable only by this service.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except OSError as error:
        os.unlink(temporary)
        raise PrinterError(f"cannot write the authorised keys: {error}") from None


def _parse_line(line: str) -> TunnelKey | None:
    """One stored line as a machine, or ``None`` if it is not a key at all.

    Lines are ``options type body [comment]``, but one written by hand may carry
    no options — so the key is found by its type rather than by counting fields.
    """
    fields = line.split(" ")
    index = next((i for i, field in enumerate(fields) if field in _KEY_TYPES), None)
    if index is None or index + 1 >= len(fields):
        return None
    comment = " ".join(fields[index + 2 :]).strip()
    try:
        return TunnelKey(
            comment=comment or "(no comment)",
            key_type=fields[index],
            fingerprint=fingerprint_of(fields[index + 1]),
        )
    except ValidationError:  # pragma: no cover - a line we did not write
        return None


def list_keys() -> list[TunnelKey]:
    """The machines authorised now, oldest first."""
    return [key for key in map(_parse_line, _read_lines()) if key is not None]


def enroll(public_key: str, *, port: int) -> TunnelKey:
    """Authorise one machine to bring its printer here.

    Registering the same key twice replaces its line rather than adding one:
    running the setup script again after changing the port must not leave the
    old port permitted for ever, with nothing to say so.
    """
    key_type, body, comment = split_key(public_key)
    line = f"{options_for(port)} {key_type} {body}" + (f" {comment}" if comment else "")

    kept = [existing for existing in _read_lines() if f" {body}" not in existing]
    if len(kept) >= _MAX_KEYS:
        raise ValidationError(
            f"{_MAX_KEYS} machines are already registered; remove one first"
        )
    _write_lines([*kept, line])
    registered = _parse_line(line)
    assert registered is not None  # we just built it from validated parts
    return registered


def remove(comment: str) -> int:
    """Withdraw every key registered under ``comment``; how many went.

    Each line is matched by re-reading it, not by pairing the file with a list
    built from it: an unparseable line is skipped by one and kept by the other,
    and from then on the two disagree about which line is which.
    """
    wanted = comment.strip()
    if not wanted:
        raise ValidationError("say which machine to remove")
    kept: list[str] = []
    removed = 0
    for line in _read_lines():
        key = _parse_line(line)
        if key is not None and wanted in (key.comment, key.fingerprint):
            removed += 1
            continue
        kept.append(line)
    if removed:
        _write_lines(kept)
    return removed
