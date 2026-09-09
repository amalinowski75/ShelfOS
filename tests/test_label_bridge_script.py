"""The bridge that puts a locally-attached printer on a TCP port.

What it has to get right is the thing socat gets wrong: a Brother QL answers
when it is ready, and a read taken before then returns zero bytes. Treating that
as end of stream tears the connection down before the answer arrives, which is
why the printer "sometimes says what it holds". Everything here is about that.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.services import label_printer as lp

from tests.fake_printer import IDLE_FRAME, FakePrinter, PrinterBridge, frame

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "label_bridge.py"


def _run(*args: str, timeout: float = 10):  # type: ignore[no-untyped-def]
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def test_help_works_without_a_printer() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "--device" in result.stdout


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_a_bad_port_is_refused(port: str) -> None:
    result = _run("--port", port)
    assert result.returncode != 0
    assert port in result.stdout + result.stderr


def test_a_status_question_is_answered_every_time() -> None:
    """The whole point. Reading the device directly answers every time; socat
    answered 2 times in 8 on a real QL-800, because it takes the printer's
    silence for the end of the conversation.
    """
    with (
        FakePrinter([IDLE_FRAME] * 12) as printer,
        PrinterBridge(printer) as bridge,
    ):
        answers = [lp.read_printer_status(bridge.device) for _ in range(12)]
    assert all(answer is not None for answer in answers)
    assert {lp.detect_tape(a) for a in answers if a} == {"62"}


def test_a_whole_raster_job_arrives() -> None:
    """os.write may accept less than it is given, and on a printer behind a
    small kernel buffer it routinely does. Ignoring that loses the tail of every
    label while a three-byte status request still works perfectly — so this
    asserts the byte count, not merely that something arrived.
    """
    from app.services.label_service import LabelData

    labels = [LabelData(id=1, name="D1", path="Lab / D1", qr_svg="")]
    with (
        FakePrinter([IDLE_FRAME, frame(b18=lp._STATUS_COMPLETED)]) as printer,
        PrinterBridge(printer) as bridge,
    ):
        outcome = lp.print_labels(labels, device=bridge.device)
        printer.wait_for(b"\x1a")

    assert outcome.confirmed
    assert printer.received.endswith(b"\x1a")
    assert len(printer.received) > 30_000  # the raster, not just the question


def test_the_device_is_released_between_callers() -> None:
    """usblp allows one opener, so a bridge that held it would let the first
    caller lock everybody out."""
    with (
        FakePrinter([IDLE_FRAME] * 4) as printer,
        PrinterBridge(printer) as bridge,
    ):
        first = lp.read_printer_status(bridge.device)
        second = lp.read_printer_status(bridge.device)
    assert first is not None and second is not None


def test_a_missing_device_does_not_kill_the_bridge() -> None:
    """A printer that is unplugged should make one connection fail, not end the
    service that is waiting for it to come back."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    process = subprocess.Popen(
        [sys.executable, str(_SCRIPT), "--device", "/nonexistent", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                break
            except OSError:
                time.sleep(0.02)
        else:  # pragma: no cover - the bridge never listened
            raise AssertionError("the bridge did not start")
        with connection:
            # Nothing useful comes back: a clean end of stream, or a reset if
            # the bridge dropped the connection first. Either is fine — what
            # must not happen is the service dying with it.
            try:
                connection.sendall(b"\x1b\x69\x53")
                assert connection.recv(64) == b""
            except ConnectionResetError:
                pass
        assert process.poll() is None  # the bridge is still waiting
    finally:
        process.terminate()
        process.wait(timeout=5)
