"""The bridge that puts a locally-attached printer on a TCP port.

What it has to get right is the thing socat gets wrong: a Brother QL answers
when it is ready, and a read taken before then returns zero bytes. Treating that
as end of stream tears the connection down before the answer arrives, which is
why the printer "sometimes says what it holds". Everything here is about that.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import tty
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


def _spawn(device: str, *args: str):  # type: ignore[no-untyped-def]
    """The bridge on a port of its own, for the cases with no printer behind it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    process = subprocess.Popen(
        [sys.executable, str(_SCRIPT), "--device", device, "--port", str(port), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:  # pragma: no cover - the port was taken
            raise AssertionError(f"the bridge exited: {process.communicate()[0]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process, port
        except OSError:
            time.sleep(0.02)
    process.terminate()  # pragma: no cover - the bridge never listened
    raise AssertionError("the bridge did not start")


def test_a_regular_file_is_refused_rather_than_relayed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A mistyped --device must not be served.

    `select` calls a regular file readable immediately and always, and `os.read`
    then returns nothing — so "an empty read is the printer being quiet", which
    is right for usblp, becomes a loop with no delay at all: a third of a core
    burned for as long as anyone is connected. The app refuses the same thing
    for the same reason (`_answers_questions`).
    """
    plain = tmp_path / "not-a-printer"
    plain.write_bytes(b"")
    process, port = _spawn(str(plain))
    try:
        started = _cpu_seconds(process.pid)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            assert connection.recv(64) == b""  # refused, not relayed
        time.sleep(0.5)
        # A busy loop would have burned a good fraction of that half-second.
        assert _cpu_seconds(process.pid) - started < 0.1
        assert process.poll() is None  # …and the service is still there
    finally:
        process.terminate()
        assert "not a character device" in process.communicate(timeout=5)[0]


def _cpu_seconds(pid: int) -> float:
    """User + system time this process has burned, from /proc."""
    with open(f"/proc/{pid}/stat") as handle:
        fields = handle.read().rsplit(") ", 1)[1].split()
    # After the comm field: state is [0], so utime is [11] and stime [12].
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def test_a_silent_caller_is_dropped_so_the_next_one_is_served() -> None:
    """One connection at a time only works while a connection can always end.

    A caller that vanishes without a FIN — a suspended host, a tunnel that drops
    without a reset — would otherwise hold the only slot for ever, and every
    later caller would sit unanswered in the listen backlog. Which is the
    symptom this whole PR set out to remove, with a different cause.
    """
    with (
        FakePrinter([IDLE_FRAME] * 4) as printer,
        PrinterBridge(printer, "--idle-timeout", "0.4") as bridge,
    ):
        silent = socket.create_connection(("127.0.0.1", bridge.port), timeout=2)
        with silent:
            assert silent.recv(64) == b""  # dropped, without our saying anything
        bridge.wait_for_log("closing the connection")

        # And the service took the next caller, rather than being wedged by it.
        assert lp.read_printer_status(bridge.device) is not None


def test_a_printer_that_stops_draining_costs_one_job_not_the_service() -> None:
    """The failure the app already guards: a QL with an error raised stops
    reading its endpoint and never resumes. Here it would be worse than a hung
    job — the bridge serves one caller at a time, so a write with no deadline
    takes the whole service with it and only a restart brings it back.
    """
    with (
        FakePrinter([IDLE_FRAME]) as printer,
        PrinterBridge(printer, "--write-timeout", "0.5") as bridge,
    ):
        printer.stop_draining()  # the pty fills, and every write says EAGAIN
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=5) as jammed:
            # More than the pty will hold, so the writes cannot all land.
            jammed.sendall(b"\x00" * 2_000_000)
            bridge.wait_for_log("stopped accepting the job")

        # The connection was given up on, not the service: the next caller is
        # accepted rather than left in the backlog.
        bridge.wait_for_log("connection from")
        assert bridge.is_running


def test_the_ipv6_loopback_actually_listens() -> None:
    """The one IPv6 spelling the loopback check blessed used to be the one that
    could not work: the socket was AF_INET, so `--host ::1` died at bind."""
    process = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPT),
            "--device",
            "/nonexistent",
            "--host",
            "::1",
            "--port",
            "19144",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"the bridge exited: {process.communicate()[0]}")
            try:
                socket.create_connection(("::1", 19144), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.02)
        else:  # pragma: no cover - the bridge never listened
            raise AssertionError("the bridge did not listen on ::1")
    finally:
        process.terminate()
        output = process.communicate(timeout=5)[0]
    assert "::1:19144" in output
    # …and it is loopback, so it does not warn about being on the network.
    assert "reachable from the network" not in output


def test_binding_a_routable_address_says_so() -> None:
    """The warning has to survive the move to getaddrinfo, and it is the only
    thing standing between a typo and an unauthenticated printer on the LAN."""
    process = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPT),
            "--device",
            "/nonexistent",
            "--host",
            "0.0.0.0",
            "--port",
            "19145",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    time.sleep(0.5)
    process.terminate()
    assert "asks nobody for a password" in process.communicate(timeout=5)[0]


def _bridge_module():  # type: ignore[no-untyped-def]
    """The shipped script, imported, for the one case a subprocess cannot show."""
    spec = importlib.util.spec_from_file_location("label_bridge", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_caller_still_sending_keeps_its_connection_open() -> None:
    """The other half of "silence" — a job trickling in over a slow tunnel is
    not an absent caller, even while the printer has nothing to say back."""
    bridge = _bridge_module()
    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    ours, theirs = socket.socketpair()
    worker = threading.Thread(
        target=bridge.relay,
        args=(theirs, os.ttyname(slave)),
        kwargs={"idle_seconds": 0.3},
        daemon=True,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 1.2  # four times the idle timeout
        while time.monotonic() < deadline:
            ours.sendall(b"\x01")
            time.sleep(0.1)
        # Still relaying: the connection was not dropped mid-job.
        assert worker.is_alive()
    finally:
        ours.close()
        worker.join(timeout=5)
        theirs.close()
        os.close(master)
        os.close(slave)


def test_a_talking_printer_keeps_its_connection_open() -> None:
    """The idle timeout measures silence, not the caller's silence.

    A print job is: send the raster, then wait while the printer confirms each
    label — the caller says nothing for the whole of it. If only what the CALLER
    sends counted as activity, a job longer than the timeout would be cut off
    mid-confirmation, which is exactly the "sometimes it works" this replaces.
    """
    bridge = _bridge_module()
    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    ours, theirs = socket.socketpair()
    worker = threading.Thread(
        target=bridge.relay,
        args=(theirs, os.ttyname(slave)),
        kwargs={"idle_seconds": 0.3},
        daemon=True,
    )
    worker.start()
    try:
        heard = b""
        ours.settimeout(0.2)
        deadline = time.monotonic() + 1.2  # four times the idle timeout
        while time.monotonic() < deadline:
            os.write(master, b"\x01")
            time.sleep(0.1)
            try:
                heard += ours.recv(64)
            except (TimeoutError, OSError):
                break
        # Still hearing the printer after four idle timeouts' worth of caller
        # silence: the connection was not dropped out from under the job.
        assert len(heard) >= 8
    finally:
        # Close the caller's end FIRST and let relay finish on its own: pulling
        # the socket out from under a running select leaves it holding a -1 fd,
        # and the thread dies with an exception instead of returning.
        ours.close()
        worker.join(timeout=5)
        theirs.close()
        os.close(master)
        os.close(slave)


def test_a_taken_port_is_refused_at_once() -> None:
    """What the test helper's retry rests on: a bridge that cannot bind says so
    and exits immediately, rather than sitting there looking like a slow start."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        result = _run("--device", "/nonexistent", "--port", str(port), timeout=10)
    finally:
        holder.close()
    assert result.returncode != 0
    assert "cannot listen" in result.stdout + result.stderr


def test_the_bridge_helper_leaves_no_pipes_behind() -> None:
    """Each bridge holds the read end of its child's stdout; a suite that builds
    a few dozen of them would run the test process out of descriptors."""
    before = len(os.listdir("/proc/self/fd"))
    # Held, deliberately: dropping each bridge on the floor would let refcounting
    # close the pipe for us, and the test would pass whether or not close() does.
    closed = []
    for _ in range(3):
        with FakePrinter([IDLE_FRAME]) as printer:
            bridge = PrinterBridge(printer)
            bridge.close()
            closed.append(bridge)
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before  # nothing accumulated
    assert len(closed) == 3
