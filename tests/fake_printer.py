"""A pty standing in for a Brother QL, so the whole print path can be tested.

The real transport writes raster bytes to a character device and reads status
frames back from it. A temporary file cannot stand in for that — ``label_printer``
deliberately refuses to interrogate anything that is not a character device,
because a regular file reports itself readable and then says nothing for ever.
A pty is a character device that can answer, which makes the round trip —
ask, refuse or print, confirm — testable in milliseconds with no hardware.

Two things are easy to get wrong here and cost real time:

* both ends must be raw, or the terminal line discipline eats and rewrites the
  binary job (``\\n`` becomes ``\\r\\n``, and a ``0x1a`` looks like EOF);
* the slave fd must stay open for the lifetime of the fake, or the reader gets
  ``EIO`` the moment the printer side closes and the thread dies before it can
  answer anything.
"""

from __future__ import annotations

import os
import threading
import time
import tty

# What a QL sends back, as captured from a real QL-800 on the bench: 62 mm
# continuous tape, no errors, answering a status request.
IDLE_FRAME = bytes.fromhex(
    "80 20 42 34 38 30 00 00 00 00 3e 0a 00 00 23 00"
    "00 00 00 01 00 00 00 00 00 81 00 00 00 00 00 00".replace(" ", "")
)


def frame(**fields: int) -> bytes:
    """A status frame with named bytes changed (``b8`` is error information 1)."""
    raw = bytearray(IDLE_FRAME)
    for name, value in fields.items():
        raw[int(name[1:])] = value
    return bytes(raw)


class FakePrinter:
    """A device node that drains print jobs and answers status questions."""

    def __init__(self, frames: list[bytes]) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self.frames = list(frames)
        self.received = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = os.read(self.master, 8192)
            except OSError:
                return
            if not chunk:
                return
            self.received += chunk
            # A status request is answered directly; the end of a raster job
            # (its print-and-eject byte) draws the frame that follows it.
            asked = b"\x1b\x69\x53" in chunk or chunk.endswith(b"\x1a")
            if asked and self.frames:
                try:
                    os.write(self.master, self.frames.pop(0))
                except OSError:
                    return

    def wait_for(self, suffix: bytes, timeout: float = 2.0) -> None:
        """Block until the device has received something ending in ``suffix``.

        A print job carries its own status request, so the fake answers — and
        the caller returns — while the tail of the job is still on the wire.
        Assertions about what arrived have to wait for it rather than race it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bytes(self.received).endswith(suffix):
                return
            time.sleep(0.01)
        raise AssertionError(f"nothing ending in {suffix!r} arrived in {timeout}s")

    def close(self) -> None:
        self._stop.set()
        os.close(self.master)
        os.close(self._slave)

    def __enter__(self) -> FakePrinter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
