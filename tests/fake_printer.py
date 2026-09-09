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
import socket
import subprocess
import sys
import threading
import time
import tty
from collections.abc import Callable
from pathlib import Path

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

    def __init__(
        self,
        frames: list[bytes],
        on_page: Callable[[int], None] | None = None,
    ) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self.frames = list(frames)
        self.received = bytearray()
        # Called with the running page count each time a job's print-and-eject
        # byte arrives, so a test can act BETWEEN labels — cancelling a run, say
        # — without racing it on a sleep.
        self.on_page = on_page
        self.pages = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop_draining(self) -> None:
        """Stop reading the job, as a QL does when an error is raised mid-raster.

        The pty's buffer then fills and every further write returns EAGAIN for
        ever — the shape that hung the app on its first evening with real
        hardware, and the one a bridge with no write deadline inherits.
        """
        self._stop.set()

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
            if chunk.endswith(b"\x1a"):
                self.pages += 1
                if self.on_page is not None:
                    self.on_page(self.pages)
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


class PrinterBridge:
    """The shipped bridge, run for real in front of a :class:`FakePrinter`.

    ``scripts/label_bridge.py`` is what a person puts on the machine their
    printer is plugged into, so the transport tests drive that rather than an
    imitation of it — an in-process stand-in would have gone on passing while
    the thing we actually ship drifted away from it.
    """

    #: Tries before giving up on finding a port nobody else takes first.
    ATTEMPTS = 3

    def __init__(self, printer: FakePrinter, *args: str) -> None:
        self.printer = printer
        self.log: list[str] = []
        self._extra = list(args)
        for attempt in range(self.ATTEMPTS):
            self._start()
            if self._wait_until_listening():
                return
            # The port was taken between the probe closing and the child binding
            # it — by another bridge in a parallel test, or by anything wanting
            # an ephemeral port. The child says so and exits at once, so this is
            # milliseconds rather than a timeout; try another number.
            failure = "\n".join(self.log)
            self.close()
            if attempt == self.ATTEMPTS - 1 or "cannot listen" not in failure:
                raise AssertionError(
                    f"the bridge did not listen on {self.port}: {failure or '(silent)'}"
                )

    def _start(self) -> None:
        # The port is picked here because the bridge refuses `--port 0`: a
        # service whose callers must know its number has no use for "any free
        # one". That leaves a window before the child binds it, which is what
        # ATTEMPTS is for.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.device = f"tcp://127.0.0.1:{self.port}"
        script = Path(__file__).resolve().parents[1] / "scripts" / "label_bridge.py"
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--device",
                self.printer.path,
                "--port",
                str(self.port),
                *self._extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        # Drained on a thread, not left in the pipe: the bridge logs a line per
        # connection, and a test driving enough of them would fill the 64 KiB
        # buffer and block the bridge inside print() — a hang with no
        # explanation. Keeping the lines also makes them assertable.
        self.log = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.log.append(line.rstrip("\n"))

    @property
    def is_running(self) -> bool:
        """Whether the bridge is still serving — the property most of these
        tests are really about, since one wedged connection used to end it."""
        return self._process.poll() is None

    def wait_for_log(self, fragment: str, timeout: float = 5.0) -> str:
        """Block until the bridge has said something containing ``fragment``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.log):
                if fragment in line:
                    return line
            time.sleep(0.02)
        raise AssertionError(
            f"the bridge never said {fragment!r}; it said: {self.log}"
        )

    def _wait_until_listening(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Checked every round: a child that died at startup (the port was
            # taken) should fail in milliseconds with its own words, rather than
            # after the full timeout with "the bridge did not listen", which
            # points at the bridge instead of at the collision.
            if self._process.poll() is not None:
                return False
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return True
            except OSError:
                time.sleep(0.02)
        return False

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged bridge
            self._process.kill()
            self._process.wait(timeout=5)
        finally:
            self._reader.join(timeout=5)
            if self._process.stdout is not None:
                self._process.stdout.close()

    def __enter__(self) -> PrinterBridge:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
