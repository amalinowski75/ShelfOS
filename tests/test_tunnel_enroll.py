"""Registering a printer's machine from the machine itself.

The point of the whole arrangement: somebody with a browser and no account on
the server can set up a printer without touching the server. What has to hold
for that to be safe is what is tested here — the token that makes it possible
may do this one thing, the key it registers may do one thing, and everything
else is refused rather than escaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app import config
from app.auth.tokens import create_access_token, create_enroll_token
from app.models.enums import UserRole
from app.models.user import User
from app.services import tunnel_keys
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tunnel-keys"
    monkeypatch.setattr(config, "TUNNEL_KEYS_FILE", str(path))
    return path


def _user(engine: Engine, name: str, role: UserRole = UserRole.USER) -> User:
    from app.services import user_service as us

    with Session(engine) as session:
        return us.create_user(
            session, username=name, password="password123", role=role, actor_id=None
        )


def _token(engine: Engine, name: str, role: UserRole = UserRole.USER) -> str:
    return create_enroll_token(_user(engine, name, role), hours=1)


def _enroll(client: TestClient, token: str, key: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/labels/setup/enroll",
        json={"public_key": key},
        headers={"Authorization": f"Bearer {token}"},
    )


KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIBFEnyJspiC2cerXzPdgVQqUTIhpyNepCIuX1OjaG+2U "
    "shelfos-label@goofy"
)
OTHER = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAILnOfb7/wBuOuQdspeVD5kXnFFjVNOVffFtDtE1XU0ih "
    "shelfos-label@dopey"
)


def test_a_machine_registers_itself(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    response = _enroll(anon_client, _token(engine, "printer-owner"), KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["comment"] == "shelfos-label@goofy"
    assert body["fingerprint"].startswith("SHA256:")
    assert body["registered"] == 1
    line = keys_file.read_text().strip()
    assert line.endswith(KEY)
    # What it may do is decided here, and written down with it.
    assert line.startswith("restrict,port-forwarding,")
    assert 'permitlisten="127.0.0.1:9100"' in line
    assert 'permitopen="127.0.0.1:1"' in line


def test_the_same_machine_twice_is_one_entry(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """Running the setup script again must not leave an older port permitted."""
    token = _token(engine, "twice")
    _enroll(anon_client, token, KEY)
    second = _enroll(anon_client, token, KEY)
    assert second.json()["registered"] == 1
    assert keys_file.read_text().count("ssh-ed25519") == 1


def test_two_machines_are_two_entries(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    token = _token(engine, "two")
    _enroll(anon_client, token, KEY)
    assert _enroll(anon_client, token, OTHER).json()["registered"] == 2
    assert [k.comment for k in tunnel_keys.list_keys()] == [
        "shelfos-label@goofy",
        "shelfos-label@dopey",
    ]


@pytest.mark.parametrize(
    "key",
    [
        'command="/bin/sh" ' + KEY,
        "no-pty," + KEY,
        KEY + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBFEnyJspiC2 second-key",
        KEY.replace("ssh-ed25519", "ssh-dss"),
        KEY.replace("AAAAC3", "not base64!"),
        "",
    ],
)
def test_a_key_that_is_more_than_a_key_is_refused(
    anon_client: TestClient, engine: Engine, keys_file: Path, key: str
) -> None:
    """This file is configuration to sshd: a line bringing its own permissions,
    or a second key on a second line, would be authorising something nobody
    looked at."""
    response = _enroll(anon_client, _token(engine, f"bad{len(key)}"), key)
    assert response.status_code == 422, response.text
    assert not keys_file.exists() or "ssh-" not in keys_file.read_text()


def test_a_read_only_account_may_not_register_a_machine(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """It changes what this server accepts, and that is a write."""
    token = _token(engine, "viewer", UserRole.READ_ONLY)
    response = _enroll(anon_client, token, KEY)
    assert response.status_code == 403
    assert not keys_file.exists()


def test_an_ordinary_api_token_is_not_a_registration_token(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """Scope is checked, not merely carried: this endpoint takes one kind of
    token, so a stolen session cannot be pointed at it either."""
    full = create_access_token(_user(engine, "with-full-token"))
    assert _enroll(anon_client, full, KEY).status_code == 401


def test_a_registration_token_is_not_a_sign_in(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """The other direction, and the one that matters more.

    This token is written into a file that lands in somebody's Downloads. If it
    authenticated anywhere else, a shell script would be a full credential for
    the account that downloaded it.
    """
    token = _token(engine, "narrow", UserRole.ADMIN)
    headers = {"Authorization": f"Bearer {token}"}
    assert anon_client.get("/api/locations", headers=headers).status_code == 401
    assert anon_client.get("/api/admin/users", headers=headers).status_code == 401
    assert anon_client.get("/api/labels/tapes", headers=headers).status_code == 401


def test_an_expired_token_says_to_download_the_script_again(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    token = create_enroll_token(_user(engine, "stale"), hours=-1)
    response = _enroll(anon_client, token, KEY)
    assert response.status_code == 401
    assert "download the script again" in response.json()["detail"]


def test_a_changed_password_retires_the_token(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """Same rule as every other sign-in here: a new password ends the old ones."""
    from app.services import user_service as us

    user = _user(engine, "changes-password")
    token = create_enroll_token(user, hours=1)
    with Session(engine) as session:
        assert user.id is not None
        us.set_password(session, user.id, "a-different-one", actor_id=user.id)
    response = _enroll(anon_client, token, KEY)
    assert response.status_code == 401
    assert "password has changed" in response.json()["detail"]


def test_a_deactivated_account_cannot_register(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    from app.services import user_service as us

    user = _user(engine, "gone")
    token = create_enroll_token(user, hours=1)
    with Session(engine) as session:
        assert user.id is not None
        us.set_active(session, user.id, False, actor_id=user.id)
    assert _enroll(anon_client, token, KEY).status_code == 401


def test_who_let_which_machine_in_is_recorded(
    anon_client: TestClient, engine: Engine, keys_file: Path
) -> None:
    """A key that can reach this server is worth a line in the audit log."""
    from app.models.audit import AuditLog

    user = _user(engine, "auditor")
    _enroll(anon_client, create_enroll_token(user, hours=1), KEY)
    with Session(engine) as session:
        entries = session.exec(
            select(AuditLog).where(AuditLog.entity_type == "label_printer")
        ).all()
    assert len(entries) == 1
    assert entries[0].user_id == user.id
    assert "shelfos-label@goofy" in (entries[0].new_value or "")


def test_a_server_without_the_key_store_says_so(
    anon_client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 with what to do instead, not a stack trace about a missing path."""
    monkeypatch.setattr(config, "TUNNEL_KEYS_FILE", "")
    response = _enroll(anon_client, _token(engine, "nowhere"), KEY)
    assert response.status_code == 503
    assert "tunnel-key add" in response.json()["detail"]


def test_a_key_can_be_withdrawn(keys_file: Path) -> None:
    tunnel_keys.enroll(KEY, port=9100)
    tunnel_keys.enroll(OTHER, port=9100)
    assert tunnel_keys.remove("shelfos-label@goofy") == 1
    assert [k.comment for k in tunnel_keys.list_keys()] == ["shelfos-label@dopey"]
    assert tunnel_keys.remove("shelfos-label@goofy") == 0


def test_the_file_is_replaced_whole_never_truncated(keys_file: Path) -> None:
    """sshd may run the command that reads this at any moment, including in the
    middle of a change: a truncate-and-write would answer "no keys" and refuse a
    tunnel that is perfectly entitled to connect."""
    tunnel_keys.enroll(KEY, port=9100)
    first = keys_file.stat().st_ino
    tunnel_keys.enroll(OTHER, port=9100)
    assert keys_file.stat().st_ino != first  # replaced by rename, not rewritten
    assert keys_file.stat().st_mode & 0o777 == 0o644  # readable by sshd's command


# --------------------------------- a registered printer IS a configured one


def test_registering_a_machine_is_what_makes_printing_available(
    anon_client: TestClient, engine: Engine, keys_file: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the page's promise stops one step short.

    Test connection goes green, and then an administrator still has to edit a
    settings file and restart the service before anything can be printed —
    with nothing left to decide, since the address and the port were settled
    when the key was authorised.
    """
    from app.services import label_printer as lp

    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    assert lp.printing_configured() is False
    assert lp.configured_device() == ""

    _enroll(anon_client, _token(engine, "printer-owner-2"), KEY)

    assert lp.printing_configured() is True
    assert lp.configured_device() == "tcp://127.0.0.1:9100"


def test_a_device_named_in_the_settings_always_wins(
    anon_client: TestClient, engine: Engine, keys_file: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """An administrator who pointed this at something meant it."""
    from app.services import label_printer as lp

    monkeypatch.setattr(config, "LABEL_DEVICE", "/dev/shelfos-label")
    _enroll(anon_client, _token(engine, "printer-owner-3"), KEY)
    assert lp.configured_device() == "/dev/shelfos-label"


def test_withdrawing_the_last_machine_takes_the_buttons_away(
    keys_file: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The cure for a laptop that has gone for good."""
    from app.services import label_printer as lp

    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    tunnel_keys.enroll(KEY, port=9100)
    assert lp.printing_configured() is True
    tunnel_keys.remove("shelfos-label@goofy")
    assert lp.printing_configured() is False


def test_a_server_with_no_key_store_is_unaffected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No store, no registrations, and nothing to infer from them."""
    from app.services import label_printer as lp

    monkeypatch.setattr(config, "LABEL_DEVICE", "")
    monkeypatch.setattr(config, "TUNNEL_KEYS_FILE", "")
    assert lp.printing_configured() is False
