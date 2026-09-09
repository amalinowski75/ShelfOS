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

It listens on 127.0.0.1 only, and is meant to be reached through an SSH tunnel;
binding it to a routable address puts an unauthenticated printer on the network.

One connection is served at a time. That is not a limitation to work around:
``usblp`` allows a single opener, and ShelfOS serialises its own jobs anyway, so
a second caller waits rather than being handed a device that cannot be opened.
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import time

# The same poll interval the app's own reader uses, for the same reason: the
# printer answers in milliseconds when it answers at all, and a device with
# nothing to say must not spin a CPU while we wait for it.
POLL_SECONDS = 0.05
CHUNK_BYTES = 4096

DEFAULT_DEVICE = "/dev/shelfos-label"
DEFAULT_PORT = 9100


def _log(message: str) -> None:
    """One line per event, unbuffered, for `journalctl -u shelfos-label`."""
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _write_all(device: int, data: bytes) -> None:
    """Put every byte on the device, however many writes that takes.

    ``os.write`` is allowed to accept less than it is given, and on a printer
    behind a small kernel buffer it routinely does — a raster job is tens of
    kilobytes. Ignoring the return value loses the tail of every label while a
    three-byte status request still works perfectly, which is the most
    confusing shape this bug could have.
    """
    sent = 0
    while sent < len(data):
        try:
            sent += os.write(device, data[sent : sent + CHUNK_BYTES])
        except BlockingIOError:
            select.select([], [device], [], POLL_SECONDS)


def relay(connection: socket.socket, device_path: str) -> None:
    """Carry one connection to the printer and back until the caller hangs up.

    The device end is never treated as finished. A read of zero bytes from
    ``usblp`` means "nothing to say yet", which is exactly what it says while
    the printer is thinking about a status request — ending the connection there
    is the bug this file exists to avoid. Only the socket closing ends the
    conversation, because only the caller knows when it has finished asking.
    """
    try:
        device = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as error:
        # Most often EBUSY: something else holds the printer, usually CUPS.
        _log(f"cannot open {device_path}: {error.strerror}")
        return
    try:
        while True:
            readable, _, _ = select.select(
                [connection.fileno(), device], [], [], POLL_SECONDS
            )
            if connection.fileno() in readable:
                data = connection.recv(CHUNK_BYTES)
                if not data:
                    return  # the caller is done; this one really is the end
                _write_all(device, data)
            if device in readable:
                try:
                    answer = os.read(device, CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if answer:
                    connection.sendall(answer)
                # An empty read is the printer being quiet, not the end of it.
    except (OSError, ConnectionError) as error:
        _log(f"connection ended: {error}")
    finally:
        os.close(device)


def serve(host: str, port: int, device_path: str) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as error:
        raise SystemExit(f"cannot listen on {host}:{port}: {error.strerror}") from None
    listener.listen(1)
    _log(f"{device_path} is on {host}:{port}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        _log("warning: reachable from the network, and it asks nobody for a password")
    while True:
        connection, peer = listener.accept()
        _log(f"connection from {peer[0]}:{peer[1]}")
        with connection:
            relay(connection, device_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="the printer")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="port to listen on"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default loopback)"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit(f"port must be between 1 and 65535, not {args.port}")
    if not os.path.exists(args.device):
        # Said once at startup rather than only when a print fails: a printer
        # that is not plugged in is the likeliest reason this ever misbehaves.
        _log(f"warning: {args.device} does not exist yet")
    try:
        serve(args.host, args.port, args.device)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
