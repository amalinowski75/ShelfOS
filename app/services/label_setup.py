"""Build the installer that sets up a label printer on somebody else's desk (§7).

ShelfOS can print to a printer plugged into another machine: that machine runs
``scripts/label_bridge.py`` and an SSH tunnel, and the server prints to
``tcp://127.0.0.1:<port>``. Standing that up by hand means reading the README,
cloning the repository onto a laptop for one file, and writing two systemd units
and a udev rule without a typo.

Nobody has a clone. The deployment is a server and a browser, so this renders a
single self-contained installer with the answers already filled in, including
the bridge itself, and the browser downloads it.

Two rules govern everything here:

* **Reject, don't escape.** Every value is validated against a narrow pattern
  and refused otherwise. Quoting is applied on top, but it is the second wall,
  never the load-bearing one — and for the two places quoting means nothing (a
  systemd ``ExecStart``, which is not a shell, and a udev rule) it is the only
  wall there is.
* **Nothing about the server.** The person running the installer sets up the
  machine in front of them. What the server needs is the administrator's to do,
  elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import shlex
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Final

from app import config
from app.services import label_printer as lp
from app.services.errors import PrinterError, ValidationError

# The bridge, and the template that installs it. ``scripts/`` is outside the
# wheel's ``app*`` packages, but the deployment path copies the whole repository
# and installs it editable, so the file is there — and when it is not, the route
# says so rather than raising.
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BRIDGE_SOURCE_PATH: Final = _REPO_ROOT / "scripts" / "label_bridge.py"
TEMPLATE_PATH: Final = Path(__file__).with_name("label_setup_installer.sh")

INSTALLER_FILENAME: Final = "shelfos-label-setup.sh"

DEFAULT_DEVICE: Final = "/dev/shelfos-label"
DEFAULT_BRIDGE_PORT: Final = 9100
DEFAULT_SSH_PORT: Final = 22
# A closed set, not a pattern: this lands in a udev rule, where shell quoting
# means nothing at all, and where the only two useful answers are these.
ALLOWED_GROUPS: Final = ("plugdev", "lp")

# A POSIX-ish account name. The leading character matters more than it looks: a
# value starting with "-" is read by ssh as an OPTION, and the target is the
# last argument of a command line systemd runs without a shell, so no amount of
# quoting would help.
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# A hostname label, or an IPv4/IPv6 literal, checked below.
_HOSTNAME = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)
# An absolute device path with no traversal, no whitespace, no tilde.
_DEVICE = re.compile(r"^/dev/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
# What is left of a token after rendering — a typo in a name, which must break
# the build rather than reach somebody's bash.
_LEFTOVER_TOKEN = re.compile(r"@[A-Z0-9_]+@")
_BASE64_ONLY = re.compile(r"^[A-Za-z0-9+/=\n]+$")
# An ordinary absolute http(s) URL, with nothing after the authority: this is
# where the script posts its key, not a path to anything.
_HTTP_URL = re.compile(r"^https?://[A-Za-z0-9.:_\[\]-]{1,255}$")
# Three base64url segments. A JWT and nothing else.
_JWT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

# Only a loopback literal, and literally: the tunnel by design ends on the
# server's own loopback, so there is no reason for this endpoint to open a
# connection anywhere else. Matching text rather than resolving a name is what
# closes the DNS-rebinding window and rejects "127.0.0.1.nip.io", which resolves
# to loopback and is not it.
# ``split_network_device`` takes the brackets off an IPv6 literal, so "::1" is
# what a "tcp://[::1]:9100" arrives as here.
_PROBE_HOSTS: Final = ("127.0.0.1", "::1", "localhost")


def default_bridge_port() -> int:
    """The port the server is already expecting, so the form comes out right.

    The one trace of the server's configuration on the setup page, and an
    invisible one: the person with the printer should not have to think about
    the server's settings, but must not be handed a port nobody is listening on
    either.
    """
    device = config.LABEL_DEVICE
    if device and lp.is_network_device(device):
        try:
            return lp.split_network_device(device)[1]
        except ValidationError:
            return DEFAULT_BRIDGE_PORT
    return DEFAULT_BRIDGE_PORT


def _valid_host(host: str) -> bool:
    """Whether ``host`` is a hostname, an IPv4 literal, or ``[IPv6]``."""
    if len(host) > 255:
        return False
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
        return True
    return bool(_HOSTNAME.match(host))


def _port(value: str | int, what: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise ValidationError(f"{what} must be a number between 1 and 65535")
    return int(text)


@lru_cache(maxsize=1)
def bridge_source() -> str:
    """The bridge script's text, read once.

    Raises :class:`PrinterError` — a 503 with a sentence — when the file is
    missing, which happens only in a build that shipped ``app/`` without
    ``scripts/``. A stack trace would say nothing a reader could act on.
    """
    try:
        return BRIDGE_SOURCE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise PrinterError(
            "this ShelfOS build does not carry scripts/label_bridge.py, so the "
            "installer cannot be assembled; set the printer up from the README"
        ) from None


def bridge_sha256() -> str:
    """The checksum the installer prints and re-checks after decoding."""
    return hashlib.sha256(bridge_source().encode("utf-8")).hexdigest()


def render_installer(
    *,
    ssh_user: str,
    ssh_host: str,
    ssh_port: str | int = DEFAULT_SSH_PORT,
    device: str = DEFAULT_DEVICE,
    bridge_port: str | int = DEFAULT_BRIDGE_PORT,
    group: str = ALLOWED_GROUPS[0],
    shelfos_url: str = "",
    enroll_token: str = "",
) -> str:
    """Validate the answers and return the installer as text.

    Every argument is refused unless it matches its pattern exactly; see the
    module docstring for why rejection, not escaping, is the boundary.
    """
    user = ssh_user.strip()
    if not _SSH_USER.match(user):
        raise ValidationError(
            "the ssh user must be a plain account name: lower-case letters, "
            "digits, '_' and '-', starting with a letter or '_'"
        )
    host = ssh_host.strip()
    if not _valid_host(host):
        raise ValidationError(
            "the server must be a hostname, an IPv4 address, or an IPv6 "
            "address in square brackets — no user@, port or path"
        )
    ssh_port_number = _port(ssh_port, "the ssh port")
    bridge_port_number = _port(bridge_port, "the printer port")
    device = device.strip()
    if not _DEVICE.match(device) or ".." in device:
        raise ValidationError(
            "the device must be an absolute path under /dev, without spaces"
        )
    if group not in ALLOWED_GROUPS:
        raise ValidationError("the group must be one of: " + ", ".join(ALLOWED_GROUPS))
    # Where the script sends its own public key, and what lets it. Both are put
    # there by ShelfOS rather than typed by anyone, and both are checked anyway:
    # they end up in a shell script, and "we wrote it ourselves" is exactly the
    # assumption that stops being true the first time somebody adds a caller.
    if shelfos_url and not _HTTP_URL.match(shelfos_url):
        raise ValidationError("the ShelfOS address must be an http or https URL")
    if enroll_token and not _JWT.match(enroll_token):
        raise ValidationError("the registration token is malformed")

    return _render(
        {
            "SSH_USER": user,
            "SSH_HOST": host,
            "SSH_PORT": str(ssh_port_number),
            "DEVICE": device,
            "BRIDGE_PORT": str(bridge_port_number),
            "GROUP": group,
            "BRIDGE_SHA256": bridge_sha256(),
            "SHELFOS_URL": shelfos_url,
            "ENROLL_TOKEN": enroll_token,
        }
    )


def _render(values: dict[str, str]) -> str:
    """Put validated values into the template. Assumes validation has happened.

    Substitution replaces ``"@TOKEN@"`` *including the surrounding quotes* with
    ``shlex.quote(value)``. Keeping the quotes in the template is what lets the
    file in the repository parse and pass shellcheck on its own, which a string
    of ``@TOKEN@`` fragments could not.

    Deliberately not ``re.sub`` with a replacement string, where a value
    containing ``\\1`` would be re-read as a group reference — the same class of
    bug as ``&`` in sed, which ``render_env_file()`` in ``shelfos.sh`` already
    describes. Not ``str.format`` or an f-string either: the script is full of
    ``${...}`` and ``$(...)``.
    """
    try:
        script = TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise PrinterError(
            "this ShelfOS build is missing the installer template, so the "
            "installer cannot be assembled; set the printer up from the README"
        ) from None
    for name, value in values.items():
        script = script.replace(f'"@{name}@"', shlex.quote(value))

    blob = base64.b64encode(bridge_source().encode("utf-8")).decode("ascii")
    wrapped = "\n".join(textwrap.wrap(blob, 76))
    if not _BASE64_ONLY.match(wrapped):  # pragma: no cover - base64 is base64
        raise ValidationError("the embedded bridge is not base64")
    script = script.replace("@BRIDGE_BASE64@", wrapped)

    leftover = _LEFTOVER_TOKEN.search(script)
    if leftover:
        # A misspelt token in the template. Failing here rather than shipping
        # a script with a literal "@SSH_HSOT@" in the middle of it.
        raise ValidationError(
            f"the installer template has an unfilled token: {leftover.group(0)}"
        )
    return script


def probe_target(device: str) -> str:
    """Return ``device`` if this endpoint may connect to it, else raise.

    The connection test asks the *server* to open a connection, so it is an SSRF
    surface and treated as one. Accepted: a loopback ``tcp://`` address written
    literally, and a device path. Matching text rather than resolving a name is
    the point — a resolved check would accept a name that answers with loopback
    now and something else at connect time.
    """
    device = device.strip()
    if not device:
        raise ValidationError("say which printer to test")
    if not lp.is_network_device(device):
        if not _DEVICE.match(device) or ".." in device:
            raise ValidationError(
                "test either a loopback tcp:// address or a path under /dev"
            )
        return device
    host, port = lp.split_network_device(device)
    if host not in _PROBE_HOSTS:
        raise ValidationError(
            "only the server's own loopback can be tested: the tunnel ends "
            "there by design, so 127.0.0.1, [::1] and localhost are the "
            "addresses this can reach"
        )
    _port(port, "the printer port")
    return device
