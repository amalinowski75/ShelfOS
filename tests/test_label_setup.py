"""The installer ShelfOS hands to whoever has the printer on their desk.

Two things are being defended here. One is that the rendered file is a valid
shell script whatever anyone types into the form — and that hostile answers are
*refused* rather than escaped, because the two places these values land (a
systemd ``ExecStart`` and a udev rule) are not shells and quoting means nothing
in either. The other is the order the script does things in: it asks for sudo,
so what it does with root, and what it checks before touching anything, is the
part worth pinning down.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from app.services import label_setup as setup
from app.services.errors import ValidationError

_REPO = Path(__file__).resolve().parents[1]
_BRIDGE = _REPO / "scripts" / "label_bridge.py"

ANSWERS = {
    "ssh_user": "adam",
    "ssh_host": "shelf.example",
    "ssh_port": 22,
    "device": "/dev/shelfos-label",
    "bridge_port": 9100,
    "group": "plugdev",
}


def _render(**overrides: object) -> str:
    return setup.render_installer(**{**ANSWERS, **overrides})  # type: ignore[arg-type]


class _Enrolment:
    """A stand-in for ShelfOS, listening, so the script can register for real.

    The interesting half of this feature is a POST made by a shell script on
    somebody's laptop; asserting on the text of that script would be asserting
    on the wrong thing. This is small enough to be worth having the script talk
    to something that answers.
    """

    def __init__(self, status: int = 200, detail: str = "") -> None:
        import http.server
        import json
        import threading

        received: list[dict[str, str]] = []
        self.received = received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                received.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "public_key": body.get("public_key", ""),
                    }
                )
                answer = (
                    {
                        "comment": "shelfos-label@test",
                        "fingerprint": "SHA256:x",
                        "port": 9100,
                        "registered": 1,
                    }
                    if status == 200
                    else {"detail": detail}
                )
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> _Enrolment:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _parses(script: str, tmp_path: Path) -> None:
    path = tmp_path / "installer.sh"
    path.write_text(script)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------- the template


def test_the_template_itself_is_a_valid_script() -> None:
    """The template is a real .sh file precisely so this is possible.

    A three-hundred-line script inside a Python triple-quoted string cannot be
    parsed, linted or read; keeping it on disk with ``"@TOKEN@"`` in place of
    each value means ``bash -n`` and shellcheck see it in CI like any other.
    """
    result = subprocess.run(
        ["bash", "-n", str(setup.TEMPLATE_PATH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ what is refused


@pytest.mark.parametrize(
    "user",
    [
        "-oProxyCommand=x",  # read as an OPTION by ssh; quoting cannot help
        "$(id)",
        "`id`",
        "a b",
        "adam;rm -rf ~",
        "adam@evil",
        "adam\nroot",
        "",
        "A" * 40,
    ],
)
def test_a_hostile_ssh_user_is_refused_not_escaped(user: str) -> None:
    with pytest.raises(ValidationError):
        _render(ssh_user=user)


@pytest.mark.parametrize(
    "host",
    [
        "-oProxyCommand=x",
        "host evil",
        "user@host@evil",
        "host:22",
        "host/path",
        "$(id)",
        "[not-an-ipv6]",
        "",
        "h" * 300,
    ],
)
def test_a_hostile_host_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        _render(ssh_host=host)


@pytest.mark.parametrize(
    "device",
    ["/dev/../etc/passwd", "~/dev/x", "/dev/lp 0", "/etc/passwd", "/dev/$(id)", ""],
)
def test_a_hostile_device_is_refused(device: str) -> None:
    with pytest.raises(ValidationError):
        _render(device=device)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "9100; id", "", "80.5"])
def test_a_bad_port_is_refused(port: str) -> None:
    with pytest.raises(ValidationError):
        _render(bridge_port=port)


def test_the_group_is_a_closed_set() -> None:
    """It lands in a udev rule, where shell quoting means nothing whatever."""
    for name in setup.ALLOWED_GROUPS:
        assert _render(group=name)
    with pytest.raises(ValidationError):
        _render(group='plugdev", MODE="0777')


@pytest.mark.parametrize(
    "host", ["shelf.example", "192.0.2.10", "[2001:db8::1]", "shelf", "a.b.c.d.e"]
)
def test_ordinary_hosts_are_accepted(host: str, tmp_path: Path) -> None:
    _parses(_render(ssh_host=host), tmp_path)


def test_the_second_wall_holds_on_its_own(tmp_path: Path) -> None:
    """Belt and braces: quoting is declared to be the second wall, so test it.

    This calls the private renderer directly, skipping validation, with values
    no public path would ever pass. The result must still parse — if quoting
    were doing nothing, this is where it would show.
    """
    script = setup._render(
        {
            "SSH_USER": "a'b\"c $(id) `id`",
            "SSH_HOST": "h;rm -rf /",
            "SSH_PORT": "22",
            "DEVICE": "/dev/x y",
            "BRIDGE_PORT": "9100",
            "GROUP": "lp",
            "BRIDGE_SHA256": setup.bridge_sha256(),
            "SHELFOS_URL": "https://shelf.example",
            "ENROLL_TOKEN": "a.b.c",
            "SERVER_PORT": "9100",
        }
    )
    _parses(script, tmp_path)


def test_an_unfilled_token_is_a_failure_not_a_download() -> None:
    """A misspelt token must break here, not appear in somebody's bash."""
    values = dict(ANSWERS)
    with pytest.raises(ValidationError, match="unfilled token"):
        setup._render(
            {
                "SSH_USER": "adam",
                "SSH_HOST": "h",
                "SSH_PORT": "22",
                "DEVICE": "/dev/x",
                "BRIDGE_PORT": "9100",
                "GROUP": "lp",
                "SHELFOS_URL": "",
                "ENROLL_TOKEN": "",
                "SERVER_PORT": "9100",
                # BRIDGE_SHA256 deliberately absent.
            }
        )
    assert values  # the sample answers above are untouched by this test


# -------------------------------------------------------------- what is in it


def test_the_rendered_script_parses(tmp_path: Path) -> None:
    _parses(_render(), tmp_path)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not here")
def test_the_rendered_script_passes_shellcheck(tmp_path: Path) -> None:
    path = tmp_path / "installer.sh"
    path.write_text(_render())
    result = subprocess.run(
        ["shellcheck", "--shell=bash", "--severity=style", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_the_two_ports_are_kept_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """They are different questions, and treating them as one was a bug.

    The port on the machine with the printer is its own — the only reason to
    change it is that something there already uses it. The server's is fixed by
    its ssh configuration (`PermitListen`) and by what ShelfOS connects to, and
    no answer on a form can change either. The tunnel joins them.

    Before this, choosing 9101 on the form produced a script that asked the
    server to bind 9101, which its sshd refuses — after the page had said the
    printer was registered.
    """
    monkeypatch.setattr(setup, "default_bridge_port", lambda: 9100)
    script = _render(bridge_port=9101)

    assert "BRIDGE_PORT=9101" in script  # this machine's
    assert "SERVER_PORT=9100" in script  # the server's, whatever the form said
    # The bridge listens where it was told; the tunnel carries the server's port
    # to it; the printer is asked for on this machine's.
    assert "--device $DEVICE --port $BRIDGE_PORT" in script
    assert "-R 127.0.0.1:$SERVER_PORT:127.0.0.1:$BRIDGE_PORT" in script
    assert '"$PYTHON" - "$BRIDGE_PORT"' in script


def test_the_same_port_on_both_ends_is_still_the_ordinary_case() -> None:
    """Nothing above should make the common setup read differently."""
    script = _render()
    assert "BRIDGE_PORT=9100" in script
    assert "SERVER_PORT=9100" in script


def test_the_bridge_travels_byte_for_byte() -> None:
    """What lands on the laptop is the file from this repository, not a copy of it."""
    script = _render()
    blob = re.search(
        r"<<'SHELFOS_BRIDGE_BASE64'\n(.*?)\nSHELFOS_BRIDGE_BASE64", script, re.S
    )
    assert blob, "the embedded bridge is missing"
    decoded = base64.b64decode(blob.group(1))
    assert decoded == _BRIDGE.read_bytes()
    assert setup.bridge_sha256() in script


def test_the_script_says_nothing_about_the_server_configuration() -> None:
    """The one requirement it would be easiest to break by being helpful.

    Whatever has to happen on the server is the administrator's, and happens
    elsewhere. Somebody adding "and put this in /etc/shelfos/env" would be
    trying to help; this is why they will not.
    """
    script = _render()
    assert "SHELFOS_LABEL_DEVICE" not in script
    assert "/etc/shelfos/env" not in script
    assert "systemctl restart shelfos" not in script


# ------------------------------------------------------------- what it DOES


def _stub_path(tmp_path: Path, ssh_mode: str = "ok") -> Path:
    """A PATH holding fakes for everything privileged, recording every call.

    ``ssh-keygen`` is deliberately NOT stubbed: the script now makes the key
    itself, and a test that faked that away would be testing a flow nobody runs.
    ``ssh`` is, and its behaviour is the interesting variable — the three ways
    the tunnel can answer are the three ways this script has to explain.
    """
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    for name in (
        "sudo",
        "systemctl",
        "udevadm",
        "usermod",
        "loginctl",
        "journalctl",
        "lpstat",
    ):
        stub = stubs / name
        # Every stub records how it was called and succeeds. Succeeding is what
        # lets the script run to the end, which is the only way to see the order
        # it does things in — and the order is what these tests are about.
        stub.write_text(
            "#!/bin/sh\n" f'printf "%s %s\\n" {name} "$*" >> "{log}"\n' "exit 0\n"
        )
        stub.chmod(0o755)

    # The forward test succeeds by NOT returning: `timeout` ends a connection
    # that held, and that is what a working tunnel looks like. The other two
    # modes are the exact words OpenSSH uses, because the script reads them.
    behaviour = {
        "ok": "sleep 30\n",
        "denied": 'echo "Permission denied (publickey)." >&2\nexit 255\n',
        "port-busy": (
            'echo "Warning: remote port forwarding failed for listen port 9100" >&2\n'
            "exit 255\n"
        ),
        "host-key": 'echo "Host key verification failed." >&2\nexit 255\n',
    }[ssh_mode]
    ssh = stubs / "ssh"
    ssh.write_text(f'#!/bin/sh\nprintf "ssh %s\\n" "$*" >> "{log}"\n{behaviour}')
    ssh.chmod(0o755)
    return stubs


def _prepare_home(tmp_path: Path) -> Path:
    """An empty home for the run. The script makes its own key inside it."""
    home = tmp_path / "home"
    home.mkdir()
    return home


def _run_installer(
    tmp_path: Path, *args: str, ssh_mode: str = "ok"
) -> tuple[subprocess.CompletedProcess, str]:
    script = tmp_path / "installer.sh"
    script.write_text(_render())
    stubs = _stub_path(tmp_path, ssh_mode)
    home = _prepare_home(tmp_path)
    result = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "HOME": str(home),
            "USER": "tester",
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )
    calls = (
        (tmp_path / "calls.log").read_text()
        if (tmp_path / "calls.log").exists()
        else ""
    )
    return result, calls


def test_a_dry_run_never_calls_sudo(tmp_path: Path) -> None:
    """The whole promise of --dry-run, in one assertion."""
    result, calls = _run_installer(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo " not in calls
    assert "would run" in result.stdout


def test_ssh_is_checked_before_anything_is_enabled(tmp_path: Path) -> None:
    """Order matters, because a tunnel that cannot come up says nothing at all.

    The check is the real connection the unit will make, and it happens before
    the first privileged thing this script does — so a machine that is not
    allowed in yet is told so while nothing has been changed.
    """
    result, calls = _run_installer(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.splitlines()
    tried = next(i for i, line in enumerate(lines) if line.startswith("ssh "))
    enabled = [i for i, line in enumerate(lines) if "enable" in line]
    touched = [i for i, line in enumerate(lines) if line.startswith("sudo")]
    assert enabled, "nothing was enabled at all"
    assert tried < min(enabled)
    assert touched and tried < min(touched)


def test_the_connection_it_tests_is_the_one_the_unit_makes(tmp_path: Path) -> None:
    """Not `ssh host true`: an authorized_keys entry restricted to forwarding
    refuses a session on purpose, so that check would fail on a setup that
    works. This one asks for the forward itself."""
    _, calls = _run_installer(tmp_path)
    attempt = next(line for line in calls.splitlines() if line.startswith("ssh "))
    # The bind address is spelled out rather than left to the server's default:
    # sshd matches PermitListen against what was ASKED for, and a bare port asks
    # for no address at all.
    assert "-R 127.0.0.1:9100:127.0.0.1:9100" in attempt  # server's:this one's
    assert "ExitOnForwardFailure=yes" in attempt
    assert "shelfos-label" in attempt  # its own key, not whatever the agent has
    assert " true" not in attempt


def test_the_key_is_made_here_and_stays_here(tmp_path: Path) -> None:
    """The whole reason this page hands out a script and not a credential."""
    result, _ = _run_installer(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    private = tmp_path / "home" / ".ssh" / "shelfos-label"
    assert private.is_file()
    assert "PRIVATE KEY" in private.read_text()
    assert private.stat().st_mode & 0o077 == 0
    # Nothing secret is in what came from the server, and nothing secret is
    # printed either — the public half is, and that is the half meant to travel.
    assert "PRIVATE KEY" not in _render()
    assert "PRIVATE KEY" not in result.stdout + result.stderr


def test_a_key_the_server_does_not_know_gets_the_line_that_fixes_it(
    tmp_path: Path,
) -> None:
    """The one failure a person cannot guess their way out of, so it is spelled
    out: the exact command, with their own key already in it."""
    result, calls = _run_installer(tmp_path, ssh_mode="denied")
    assert result.returncode != 0
    public = (tmp_path / "home" / ".ssh" / "shelfos-label.pub").read_text().strip()
    assert f'tunnel-key add "{public}"' in result.stderr
    assert "run this script again" in result.stderr
    # And it stopped there: nothing was changed on the way to finding out.
    assert "sudo" not in calls


def test_a_port_the_server_will_not_hand_over_names_both_reasons(
    tmp_path: Path,
) -> None:
    """ssh says one sentence for two different problems — a port in use, and a
    port the server's configuration will not permit — so the message names both
    rather than sending somebody hunting for a process that is not there."""
    result, _ = _run_installer(tmp_path, ssh_mode="port-busy")
    assert result.returncode != 0
    assert "already using it" in result.stderr
    assert "does not allow this port" in result.stderr
    assert "9100" in result.stderr


def test_an_unaccepted_host_key_is_never_accepted_for_you(tmp_path: Path) -> None:
    """A script cannot look at a fingerprint and recognise it, so it does not
    pretend to — it says which command to run by hand."""
    result, calls = _run_installer(tmp_path, ssh_mode="host-key")
    assert result.returncode != 0
    assert "ssh -p 22 adam@shelf.example" in result.stderr
    assert "StrictHostKeyChecking" not in _render()
    assert "sudo" not in calls


def test_the_tunnel_unit_uses_that_key_and_no_other(tmp_path: Path) -> None:
    """IdentitiesOnly, because an agent with a dozen keys would otherwise offer
    them all and be refused for too many authentication failures."""
    script = _render()
    unit_line = next(
        line for line in script.splitlines() if "ExecStart=$SSH_BIN" in line
    )
    assert "-i $KEY_PATH" in unit_line
    assert "IdentitiesOnly=yes" in unit_line
    assert "BatchMode=yes" in unit_line


def test_the_user_is_added_to_a_group_never_moved_into_one(tmp_path: Path) -> None:
    """-aG appends; -G REPLACES every group, sudo included. People lock
    themselves out of their own machine that way, and it is one character."""
    script = _render()
    assert "usermod -aG" in script
    assert re.search(r"usermod\s+(-\w*\s+)*-G\b", script) is None


def test_it_refuses_to_run_as_root(tmp_path: Path) -> None:
    """Under sudo it would set up root's user services and root's linger — two
    services nobody would think to look for, while the user's never start."""
    script = _render()
    assert 'if [ "$(id -u)" = 0 ]; then' in script
    guard = script.index('"$(id -u)" = 0')
    assert guard < script.index("udevadm"), "the guard must come first"


def test_the_cups_queue_is_reported_never_removed() -> None:
    """Deleting somebody's configured printer is not an installer's decision."""
    script = _render()
    assert "lpadmin -x" in script
    assert "sudo lpadmin -x" not in script.replace("        sudo lpadmin -x QL-800", "")


def test_uninstall_leaves_the_group_and_the_lingering_alone(tmp_path: Path) -> None:
    """Both may predate this script and may be holding something else up."""
    result, calls = _run_installer(tmp_path, "--uninstall", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gpasswd" not in calls
    assert "disable-linger" not in calls
    assert "Left alone on purpose" in result.stdout


def test_show_bridge_prints_the_code_it_would_install(tmp_path: Path) -> None:
    """The file asks to be read, and the most interesting part of it is base64."""
    script = tmp_path / "installer.sh"
    script.write_text(_render())
    result = subprocess.run(
        ["bash", str(script), "--show-bridge"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == _BRIDGE.read_text()


# ------------------------------------------------------------------ defaults


def test_the_default_port_follows_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """So the form comes out consistent without naming the server's settings."""
    from app import config

    monkeypatch.setattr(config, "LABEL_DEVICE", "tcp://127.0.0.1:9200")
    assert setup.default_bridge_port() == 9200
    monkeypatch.setattr(config, "LABEL_DEVICE", "/dev/shelfos-label")
    assert setup.default_bridge_port() == 9100
    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    assert setup.default_bridge_port() == 9100
    monkeypatch.setattr(config, "LABEL_DEVICE", "tcp://nonsense")
    assert setup.default_bridge_port() == 9100


# ------------------------------------------------------- the probe's own guard


@pytest.mark.parametrize(
    "device",
    [
        "tcp://example.com:9100",
        "tcp://169.254.169.254:80",  # the cloud metadata endpoint
        "tcp://127.0.0.1.nip.io:9100",  # resolves to loopback, and is not it
        "tcp://10.0.0.5:9100",
        "http://127.0.0.1:9100",
        "",
    ],
)
def test_only_the_servers_own_loopback_may_be_tested(device: str) -> None:
    with pytest.raises(ValidationError):
        setup.probe_target(device)


@pytest.mark.parametrize(
    "device",
    [
        "tcp://127.0.0.1:9100",
        "tcp://[::1]:9100",
        "tcp://localhost:9100",
        "/dev/usb/lp0",
    ],
)
def test_loopback_and_devices_are_allowed(device: str) -> None:
    assert setup.probe_target(device) == device


def test_a_build_without_the_bridge_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """503 with a sentence, not a 500 with a stack trace."""
    from app.services.errors import PrinterError

    setup.bridge_source.cache_clear()
    monkeypatch.setattr(setup, "BRIDGE_SOURCE_PATH", Path("/nonexistent/bridge.py"))
    try:
        with pytest.raises(PrinterError, match="label_bridge"):
            setup.bridge_source()
    finally:
        setup.bridge_source.cache_clear()


def test_python_is_what_runs_the_bridge_in_the_unit() -> None:
    """ExecStart is not a shell, so the interpreter is named, not inferred."""
    script = _render()
    assert "ExecStart=$PYTHON $BRIDGE_PATH --device $DEVICE" in script
    assert 'PYTHON="$(command -v python3)"' in script


def test_it_uses_this_interpreter_family(tmp_path: Path) -> None:
    """Guards the assumption behind the whole thing: python3 is on the machine."""
    assert sys.version_info >= (3, 10)


# ------------------------------------------------- registering with ShelfOS


TOKEN = "aaaa.bbbb.cccc"


def test_the_script_registers_its_own_key(tmp_path: Path) -> None:
    """The whole point: nobody signs in to the server, and nothing is typed there.

    Only the public half moves, and it moves to ShelfOS — where the person
    running this is already signed in — rather than to an account on the server
    that most people would not have.
    """
    with _Enrolment() as shelfos:
        script = tmp_path / "installer.sh"
        script.write_text(_render(shelfos_url=shelfos.url, enroll_token=TOKEN))
        stubs = _stub_path(tmp_path)
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": f"{stubs}:/usr/bin:/bin",
                "HOME": str(_prepare_home(tmp_path)),
                "USER": "tester",
                "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(shelfos.received) == 1, shelfos.received
        call = shelfos.received[0]

    assert call["path"] == "/api/labels/setup/enroll"
    assert call["authorization"] == f"Bearer {TOKEN}"
    public = (tmp_path / "home" / ".ssh" / "shelfos-label.pub").read_text().strip()
    assert call["public_key"] == public
    # The private half stays where it was made. This is the assertion that says
    # a page handing out a script is not a page handing out a credential.
    assert "PRIVATE KEY" not in str(shelfos.received)
    assert "nothing to do on the server" in result.stdout


def test_a_refused_registration_falls_back_to_the_line_to_paste(
    tmp_path: Path,
) -> None:
    """An expired token must not be a dead end: the manual way still exists,
    and the script prints it with the key already in it."""
    with _Enrolment(status=401, detail="token has expired") as shelfos:
        script = tmp_path / "installer.sh"
        script.write_text(_render(shelfos_url=shelfos.url, enroll_token=TOKEN))
        stubs = _stub_path(tmp_path)
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": f"{stubs}:/usr/bin:/bin",
                "HOME": str(_prepare_home(tmp_path)),
                "USER": "tester",
                "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            },
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "token has expired" in result.stderr
    assert "tunnel-key add" in result.stderr


def test_a_download_that_cannot_register_says_so_once(tmp_path: Path) -> None:
    """No token in the script — a read-only account, or a server not set up for
    it — and the script says what to do instead of failing at a POST it was
    never going to be allowed to make."""
    result, _ = _run_installer(tmp_path)  # rendered without url or token
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot register itself" in result.stderr
    assert "tunnel-key add" in result.stderr


def test_the_address_and_the_token_are_checked_before_they_are_written() -> None:
    """Both are put there by ShelfOS, and both are validated anyway: they end up
    in a shell script, and "we wrote it ourselves" stops being true the first
    time somebody adds a caller."""
    for url in ("javascript:alert(1)", "https://host/path", "https://host;id"):
        with pytest.raises(ValidationError):
            _render(shelfos_url=url, enroll_token=TOKEN)
    for token in ("not a token", "a.b", "a.b.c.d", "a b.c.d"):
        with pytest.raises(ValidationError):
            _render(shelfos_url="https://host", enroll_token=token)


# ------------------------------------------------- which address to propose


@pytest.mark.parametrize(
    "browser_host", ["127.0.0.1", "localhost", "::1", "LOCALHOST", ""]
)
def test_a_loopback_browser_address_is_replaced_by_our_own(
    monkeypatch: pytest.MonkeyPatch, browser_host: str
) -> None:
    """The address in the browser's bar says how the BROWSER got here.

    Through a container's proxy device, a published port or an `ssh -L`, that is
    loopback — and ssh from the machine with the printer, on the far side of it,
    would come back to that machine. This is the case a test container walks
    into, and the answer is the address the server sees itself at.
    """
    monkeypatch.setattr(setup, "own_address", lambda: "10.0.3.42")
    assert setup.propose_ssh_host(browser_host) == "10.0.3.42"


@pytest.mark.parametrize(
    "browser_host", ["shelf.example", "192.168.1.5", "shelf.example.com"]
)
def test_a_real_address_is_never_second_guessed(
    monkeypatch: pytest.MonkeyPatch, browser_host: str
) -> None:
    """A name somebody chose is the best answer available: it resolves the same
    way from the machine with the printer, which an interface address may not."""
    monkeypatch.setattr(setup, "own_address", lambda: "10.0.3.42")
    assert setup.propose_ssh_host(browser_host) == browser_host


def test_with_no_address_of_its_own_it_offers_what_the_browser_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup, "own_address", lambda: "")
    assert setup.propose_ssh_host("127.0.0.1") == "127.0.0.1"
    assert setup.propose_ssh_host("") == ""


def test_the_address_it_finds_is_one_it_could_be_reached_at() -> None:
    """Whatever this machine answers, it must be usable in the field it fills.

    A route lookup can hand back loopback (no route at all) or a link-local
    address (no DHCP), and neither is somewhere ssh can be pointed.
    """
    address = setup.own_address()
    if address:
        assert setup._valid_host(address)
        parsed = ipaddress.ip_address(address)
        assert not parsed.is_loopback and not parsed.is_link_local


def test_nothing_in_the_script_reaches_for_sudo_to_read(tmp_path: Path) -> None:
    """A --dry-run that asks for a sudo password has broken its one promise.

    The udev comparison used `sudo cat` on a world-readable file, so every
    re-run of `--dry-run` prompted — and then printed "sudo was never called".
    """
    assert "sudo cat" not in _render()
