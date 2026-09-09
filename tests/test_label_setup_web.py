"""The setup page, the download, and the connection test.

The page exists because the deployment is a server and a browser: nobody has a
clone of the repository to take the bridge script from. So the interesting
assertions are about who may see it (everyone signed in, read-only included,
because it is about the machine in front of them) and what it must not say
(anything about the server, which is the administrator's business).
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path

import pytest
from app import config
from app.services import label_setup as setup
from fastapi.testclient import TestClient

from tests.fake_printer import IDLE_FRAME, FakePrinter, PrinterBridge, frame


def _token(client: TestClient, role: str, username: str) -> str:
    client.post(
        "/api/admin/users",
        json={"username": username, "password": "password123", "role": role},
    )
    return client.post(
        "/api/auth/token", json={"username": username, "password": "password123"}
    ).json()["access_token"]


def _headers(client: TestClient, role: str, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, role, username)}"}


# ------------------------------------------------------------------- the page


@pytest.mark.parametrize("role", ["admin", "user", "read-only"])
def test_the_page_opens_for_every_signed_in_account(
    client: TestClient, anon_client: TestClient, role: str
) -> None:
    """Including read-only, and that is the decision, not an oversight.

    It concerns the printer on someone's desk and the machine it is plugged
    into, not what they may change in ShelfOS — and the page changes nothing
    here at all.
    """
    if role == "admin":
        response = client.get("/label-printer")
    else:
        response = anon_client.get(
            "/label-printer", headers=_headers(client, role, f"viewer-{role}")
        )
    assert response.status_code == 200
    assert "Label printer setup" in response.text


def test_the_page_is_closed_to_strangers(anon_client: TestClient) -> None:
    response = anon_client.get("/label-printer", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_ssh_target_is_proposed_from_the_host_header(client: TestClient) -> None:
    """A suggestion in an editable field, and validated again on the way back.

    The port is dropped: it is the web port, and has nothing to do with ssh.
    """
    html = client.get("/label-printer", headers={"Host": "shelf.example:8080"}).text
    assert 'value="shelf.example"' in html
    assert "shelf.example:8080" not in html


def test_the_page_says_nothing_about_the_server(client: TestClient) -> None:
    """A negative assertion because it is a requirement, not an accident.

    Whatever the server needs is set by its administrator, elsewhere. The person
    with the printer has no business being shown it, and no way to act on it.
    """
    html = client.get("/label-printer").text
    assert "SHELFOS_LABEL_DEVICE" not in html
    assert "/etc/shelfos/env" not in html
    assert "systemctl restart shelfos" not in html


def test_the_port_offered_is_the_one_the_server_listens_for(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invisible consistency: no talk of the server, and no unusable default."""
    monkeypatch.setattr(config, "LABEL_DEVICE", "tcp://127.0.0.1:9241")
    html = client.get("/label-printer").text
    assert 'value="9241"' in html
    assert "tcp://127.0.0.1:9241" in html  # the test field agrees with it


def test_the_locations_page_points_here_when_there_is_no_printer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the absence of printing is noticed is where the way out belongs."""
    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    html = client.get("/locations").text
    assert 'href="/label-printer"' in html


# --------------------------------------------------------------- the download


def _download(client: TestClient, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return client.get(
        "/api/labels/setup/installer.sh",
        params={"ssh_user": "adam", "ssh_host": "shelf.example", "bridge_port": 9100},
        headers=headers or {},
    )


def test_the_download_is_a_script_the_browser_saves(
    client: TestClient, tmp_path: Path
) -> None:
    response = _download(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert setup.INSTALLER_FILENAME in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    path = tmp_path / "installer.sh"
    path.write_text(response.text)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


def test_a_read_only_account_may_download_it(
    client: TestClient, anon_client: TestClient
) -> None:
    response = _download(anon_client, _headers(client, "read-only", "viewer-dl"))
    assert response.status_code == 200
    assert "shelfos-label" in response.text


def test_a_stranger_may_not(anon_client: TestClient) -> None:
    assert _download(anon_client).status_code == 401


def test_a_hostile_answer_comes_back_as_a_refusal(client: TestClient) -> None:
    """422, not a script with the value escaped into it somewhere."""
    response = client.get(
        "/api/labels/setup/installer.sh",
        params={"ssh_user": "-oProxyCommand=touch /tmp/x", "ssh_host": "h"},
    )
    assert response.status_code == 422
    assert "ssh user" in response.json()["detail"]


# ------------------------------------------------------------------ the probe


def test_the_probe_says_which_tape_it_found(client: TestClient) -> None:
    with FakePrinter([IDLE_FRAME] * 4) as printer, PrinterBridge(printer) as bridge:
        response = client.get(
            "/api/labels/setup/probe", params={"device": bridge.device}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["answered"] is True
    assert body["tape"] == "62"
    assert "62" in body["detail"]


def test_the_probe_repeats_a_fault_the_printer_reports(client: TestClient) -> None:
    """b8 is error information 1; 0x01 is "no media"."""
    with (
        FakePrinter([frame(b8=0x01)] * 4) as printer,
        PrinterBridge(printer) as bridge,
    ):
        response = client.get(
            "/api/labels/setup/probe", params={"device": bridge.device}
        )
    body = response.json()
    assert response.status_code == 200
    assert body["answered"] is True
    assert body["errors"]


def test_nobody_listening_says_so(client: TestClient) -> None:
    """The tunnel is down, or the bridge is not running on the other machine."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    response = client.get(
        "/api/labels/setup/probe", params={"device": f"tcp://127.0.0.1:{port}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answered"] is False
    assert "listening" in body["detail"]


def test_accepted_and_then_silent_is_its_own_answer(client: TestClient) -> None:
    """The failure this button exists for.

    A connection taken and then nothing said means the bridge is up and the
    printer behind it is not — unplugged, in Editor Lite mode, or held by CUPS.
    Everywhere else in the interface this collapses into "the printer is not
    saying what it holds", which sends people looking at the wrong machine.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    held: list[socket.socket] = []

    def accept_and_say_nothing() -> None:
        connection, _ = listener.accept()
        held.append(connection)  # kept open, and deliberately mute

    thread = threading.Thread(target=accept_and_say_nothing, daemon=True)
    thread.start()
    try:
        response = client.get(
            "/api/labels/setup/probe", params={"device": f"tcp://127.0.0.1:{port}"}
        )
    finally:
        thread.join(timeout=5)
        for connection in held:
            connection.close()
        listener.close()

    assert response.status_code == 200
    body = response.json()
    assert body["answered"] is False
    assert "not answering" in body["detail"]


@pytest.mark.parametrize(
    "device",
    [
        "tcp://example.com:9100",
        "tcp://169.254.169.254:80",
        "tcp://127.0.0.1.nip.io:9100",
    ],
)
def test_the_probe_will_not_be_pointed_anywhere_else(
    client: TestClient, device: str
) -> None:
    """It makes the SERVER open a connection, so it is an SSRF surface."""
    response = client.get("/api/labels/setup/probe", params={"device": device})
    assert response.status_code == 422


def test_a_read_only_account_may_test_the_connection(
    client: TestClient, anon_client: TestClient
) -> None:
    with FakePrinter([IDLE_FRAME] * 4) as printer, PrinterBridge(printer) as bridge:
        response = anon_client.get(
            "/api/labels/setup/probe",
            params={"device": bridge.device},
            headers=_headers(client, "read-only", "viewer-probe"),
        )
    assert response.status_code == 200
    assert response.json()["tape"] == "62"
