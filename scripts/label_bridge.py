#!/usr/bin/env python3
"""Expose a locally-attached label printer on a TCP port.

Run this on the machine the printer is plugged into. ShelfOS, wherever it runs,
points ``SHELFOS_LABEL_DEVICE`` at ``tcp://host:port`` and prints as if the
printer were its own — the tape is still read off it, a fault still stops the
job before any tape moves, and each label is still confirmed.

Why this exists rather than a line of ``socat``. A Brother QL on ``usblp``
answers a question when it is ready and not before, and a read taken in the
meantime returns **zero bytes** rather than blocking or reporting "not yet".
``socat`` reads that as end of stream and tears the connection down, so a status
question usually beats its own answer: measured on a real QL-800, ``socat`` got
a frame back 2 times in 8, or 7 in 8 with ``ignoreeof``, where reading the device
directly answered 10 times out of 10. ShelfOS's own reader has always known
this — it polls and keeps waiting — and this is that same loop, so the far end
behaves exactly like a local one instead of nearly.

Usage::

    python scripts/label_bridge.py                      # /dev/shelfos-label on 9100
    python scripts/label_bridge.py --device /dev/usb/lp0 --port 9200
    python scripts/label_bridge.py --host 0.0.0.0       # see the warning below

It listens on 127.0.0.1 by default, and is meant to be reached through an SSH
tunnel; binding it to a routable address puts an unauthenticated printer on the
network.

One connection is served at a time. That is not a limitation to work around:
``usblp`` allows a single opener, and ShelfOS serialises its own jobs anyway, so
a second caller waits rather than being handed a device that cannot be opened.
But "one at a time" only holds while a connection can always end: a printer that
stops draining its endpoint, or a caller that vanishes without a FIN, would
otherwise hold the only slot for ever and take the service down with it. So a
write has a deadline and an idle connection is closed — see WRITE_SECONDS and
IDLE_SECONDS.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import select
import socket
import stat
import sys
import time

# The same poll interval the app's own reader uses, for the same reason: the
# printer answers in milliseconds when it answers at all, and a device with
# nothing to say must not spin a CPU while we wait for it.
POLL_SECONDS = 0.05
CHUNK_BYTES = 4096

# How long the printer may go on refusing bytes before this connection is given
# up on. The app's own writer has the same deadline for the same reason (see
# `_write_all` in app/services/label_printer.py): a QL that raises a cover-open
# error mid-raster stops reading its endpoint and never resumes, and a write
# with no deadline hangs with no error of any kind. Here it would be worse than
# a hung job — one connection is served at a time, so a wedged write wedges the
# service, and every later caller sits in the listen backlog unanswered.
WRITE_SECONDS = 30.0

# How long a connection may go completely silent before it is closed. A caller
# that disappears without a FIN — a suspended host, a tunnel whose carrier drops
# without a reset — would otherwise hold the only slot for ever. Generous: a
# whole label job is seconds, and this only ever fires on a peer that is gone.
IDLE_SECONDS = 120.0

DEFAULT_DEVICE = "/dev/shelfos-label"
DEFAULT_PORT = 9100


class DeviceStalled(Exception):
    """The printer stopped accepting the job and did not start again."""


def _log(message: str) -> None:
    """One line per event, unbuffered, for `journalctl -u shelfos-label`."""
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _write_all(device: int, data: bytes, deadline: float) -> None:
    """Put every byte on the device, however many writes that takes.

    ``os.write`` is allowed to accept less than it is given, and on a printer
    behind a small kernel buffer it routinely does — a raster job is tens of
    kilobytes. Ignoring the return value loses the tail of every label while a
    three-byte status request still works perfectly, which is the most
    confusing shape this bug could have.

    Bounded, because a printer with an error raised stops draining its endpoint
    and never resumes. Giving up here ends one connection; not giving up ends
    the service, since the next caller is never accepted.
    """
    sent = 0
    while sent < len(data):
        if time.monotonic() >= deadline:
            raise DeviceStalled(f"{sent} of {len(data)} bytes sent")
        try:
            sent += os.write(device, data[sent : sent + CHUNK_BYTES])
        except BlockingIOError:
            select.select([], [device], [], POLL_SECONDS)


def relay(
    connection: socket.socket,
    device_path: str,
    *,
    idle_seconds: float = IDLE_SECONDS,
    write_seconds: float = WRITE_SECONDS,
) -> None:
    """Carry one connection to the printer and back until the caller hangs up.

    The device end is never treated as finished. A read of zero bytes from
    ``usblp`` means "nothing to say yet", which is exactly what it says while
    the printer is thinking about a status request — ending the connection there
    is the bug this file exists to avoid. Only the socket closing ends the
    conversation, because only the caller knows when it has finished asking.

    "Only the socket closing" has one exception, and it is the one that keeps
    that rule safe: a socket whose peer is gone never closes at all. So a
    connection that goes wholly silent for ``idle_seconds`` is ended here — see
    IDLE_SECONDS for why that cannot be confused with a printer thinking.
    """
    try:
        device = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as error:
        # Most often EBUSY: something else holds the printer, usually CUPS.
        _log(f"cannot open {device_path}: {error.strerror}")
        return
    try:
        if not stat.S_ISCHR(os.fstat(device).st_mode):
            # The app refuses to interrogate anything else for the same reason
            # (`_answers_questions`): a regular file reports itself readable and
            # then returns nothing, for ever. Here the "an empty read is the
            # printer being quiet" rule turns that into a busy loop at 100% of a
            # core — so a mistyped --device must be refused, not relayed.
            _log(f"{device_path} is not a character device; refusing the caller")
            return
        # Only a byte actually moved counts as activity: a device that reports
        # itself readable and then says nothing is precisely what an idle
        # printer does, and treating that as life would never time anything out.
        last_moved = time.monotonic()
        while True:
            readable, _, _ = select.select(
                [connection.fileno(), device], [], [], POLL_SECONDS
            )
            if connection.fileno() in readable:
                data = connection.recv(CHUNK_BYTES)
                if not data:
                    return  # the caller is done; this one really is the end
                last_moved = time.monotonic()
                _write_all(device, data, time.monotonic() + write_seconds)
            if device in readable:
                try:
                    answer = os.read(device, CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if answer:
                    last_moved = time.monotonic()
                    connection.sendall(answer)
                # An empty read is the printer being quiet, not the end of it.
            if time.monotonic() - last_moved >= idle_seconds:
                _log(f"nothing said in {idle_seconds:g}s; closing the connection")
                return
    except DeviceStalled as stalled:
        # The printer is usually waiting for an error to be cleared with the
        # button on the front. Ending the connection frees the slot; the device
        # is closed below, so the next caller gets a fresh open.
        _log(f"{device_path} stopped accepting the job ({stalled}); giving up")
    except (OSError, ConnectionError) as error:
        _log(f"connection ended: {error}")
    finally:
        os.close(device)


def _is_loopback(address: str) -> bool:
    """Whether a bound address is one only this machine can reach."""
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:  # pragma: no cover - getaddrinfo returns literals
        return False


def _keep_alive(connection: socket.socket) -> None:
    """Ask TCP to notice a peer that has gone away.

    Without this the default is to wait for hours, which for a service that
    holds one connection at a time is indistinguishable from being dead. The
    per-socket intervals are Linux-only and optional: the timeout in `relay` is
    the guarantee, this just makes the common case fail faster and lets the
    kernel spot a dead peer even mid-transfer.
    """
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            connection.setsockopt(socket.IPPROTO_TCP, option, value)


def serve(
    host: str,
    port: int,
    device_path: str,
    *,
    idle_seconds: float = IDLE_SECONDS,
    write_seconds: float = WRITE_SECONDS,
) -> None:
    # Through getaddrinfo rather than a hard-coded AF_INET, so `--host ::1`
    # listens instead of dying at bind — the one IPv6 spelling the loopback
    # check appeared to bless was the one that could not work.
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except OSError as error:
        raise SystemExit(
            f"cannot listen on {host}:{port}: {error.strerror or error}"
        ) from None
    family, socktype, proto, _canonical, address = infos[0]
    listener = socket.socket(family, socktype, proto)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(address)
    except OSError as error:
        raise SystemExit(f"cannot listen on {host}:{port}: {error.strerror}") from None
    listener.listen(1)
    _log(f"{device_path} is on {host}:{port}")
    if not _is_loopback(address[0]):
        _log("warning: reachable from the network, and it asks nobody for a password")
    while True:
        connection, peer = listener.accept()
        _log(f"connection from {peer[0]}:{peer[1]}")
        with connection:
            _keep_alive(connection)
            relay(
                connection,
                device_path,
                idle_seconds=idle_seconds,
                write_seconds=write_seconds,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="the printer")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="port to listen on"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default loopback)"
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=IDLE_SECONDS,
        help="seconds of silence before a connection is closed (0 to never)",
    )
    parser.add_argument(
        "--write-timeout",
        type=float,
        default=WRITE_SECONDS,
        help="seconds the printer may refuse bytes before the job is given up",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit(f"port must be between 1 and 65535, not {args.port}")
    if not os.path.exists(args.device):
        # Said once at startup rather than only when a print fails: a printer
        # that is not plugged in is the likeliest reason this ever misbehaves.
        _log(f"warning: {args.device} does not exist yet")
    try:
        serve(
            args.host,
            args.port,
            args.device,
            # 0 means "wait as long as it takes", for whoever finds the timeout
            # in their way; the write deadline still holds either way.
            idle_seconds=args.idle_timeout or float("inf"),
            write_seconds=args.write_timeout or float("inf"),
        )
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
