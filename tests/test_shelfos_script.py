"""The entry-point script: argument handling, and the promises it makes.

Most of `deploy` cannot run here — it installs packages, creates a user and
talks to systemd — so what is tested is everything up to that point plus the
one guarantee that must never break: `--dry-run` changes nothing and never
reaches `sudo`. `deploy/README.md` carries the by-hand checklist for the rest.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "shelfos.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the script needs bash"
)


def _run(*args: str, cwd: Path | None = None, env_extra: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """Run the script with a scrubbed environment.

    ``stdin=DEVNULL`` is load-bearing: the script reads answers from /dev/tty,
    and under ``pytest -s`` an inherited terminal would let a prompt block the
    whole run with nothing on screen to say why.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd or _SCRIPT.parent),
        "TERM": "dumb",
    }
    env.update(env_extra or {})
    return subprocess.run(
        [str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


# --- the shape of the command line -------------------------------------------


def test_the_script_parses() -> None:
    """A syntax error must fail the suite, not only the shellcheck job."""
    assert subprocess.run(["bash", "-n", str(_SCRIPT)]).returncode == 0


def test_help_names_every_command() -> None:
    result = _run("--help")
    assert result.returncode == 0
    for command in ("devel", "deploy", "update", "status", "backup"):
        assert command in result.stdout, command


@pytest.mark.parametrize("command", ["devel", "deploy", "update", "status", "backup"])
def test_each_command_has_its_own_help(command: str) -> None:
    result = _run(command, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_no_arguments_is_not_an_error() -> None:
    """Asking what this is counts as using it correctly."""
    result = _run()
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_an_unknown_command_is_a_usage_error() -> None:
    result = _run("frobnicate")
    assert result.returncode == 2
    assert "frobnicate" in result.stderr


def test_an_unknown_option_is_a_usage_error() -> None:
    assert _run("--nonesuch").returncode == 2
    assert _run("devel", "--nonesuch").returncode == 2


def test_version_matches_the_project() -> None:
    import tomllib

    result = _run("--version")
    assert result.returncode == 0
    pyproject = tomllib.loads((_SCRIPT.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] in result.stdout


@pytest.mark.parametrize("port", ["abc", "0", "65536", "-1", "9_000"])
def test_a_bad_port_is_refused(port: str) -> None:
    result = _run("devel", "--dry-run", "--port", port)
    assert result.returncode == 2, result.stdout
    assert port in result.stderr


# --- dry run ------------------------------------------------------------------


def _sudo_trap(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A `sudo` earlier on PATH that records being called, and fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "sudo-was-called"
    (bin_dir / "sudo").write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    (bin_dir / "sudo").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}, marker


def test_a_dry_run_deploy_never_reaches_sudo(tmp_path: Path) -> None:
    """The guarantee that makes --dry-run worth trusting, and this testable."""
    env, marker = _sudo_trap(tmp_path)
    result = _run(
        "deploy",
        "--dry-run",
        "--domain",
        "example.test",
        "--no-printer",
        "--no-caddy",
        "-y",
        env_extra=env,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "deploy --dry-run invoked sudo"


def test_a_dry_run_deploy_walks_the_steps_in_order(tmp_path: Path) -> None:
    env, _ = _sudo_trap(tmp_path)
    result = _run(
        "deploy",
        "--dry-run",
        "--domain",
        "example.test",
        "--no-printer",
        "-y",
        env_extra=env,
    )
    assert result.returncode == 0, result.stderr
    seen = [line for line in result.stderr.splitlines() if line.strip().startswith("[")]
    numbers = [line.split("/")[0].split("[")[-1].strip() for line in seen]
    assert numbers == [str(n) for n in range(1, 14)], seen


def test_a_dry_run_deploy_never_prints_the_password(tmp_path: Path) -> None:
    env, _ = _sudo_trap(tmp_path)
    secret = "correct-horse-battery-staple"
    result = subprocess.run(
        [
            str(_SCRIPT),
            "deploy",
            "--dry-run",
            "--domain",
            "example.test",
            "--no-printer",
            "--no-caddy",
            "-y",
            "--admin-password-stdin",
        ],
        input=secret + "\n",
        capture_output=True,
        text=True,
        env={"PATH": env["PATH"], "HOME": str(tmp_path), "TERM": "dumb"},
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr


def test_a_dry_run_devel_does_not_start_the_server() -> None:
    result = _run("devel", "--dry-run", "--port", "9099", "--no-seed", "--no-install")
    assert result.returncode == 0, result.stderr
    assert "exec .venv/bin/uvicorn" in result.stderr


# --- the data promise ---------------------------------------------------------


def test_devel_leaves_an_existing_database_untouched() -> None:
    """Nothing in this script moves, renames or deletes what is under data/.

    Deletion belongs to scripts/reset_db.py, which asks for its own typed
    confirmation; here even --reset must get no further than naming it.

    The database is created when the checkout has none, rather than skipping:
    data/shelfos.db is gitignored, so on CI and on any fresh clone a skip would
    mean the promise in this test's name is never actually checked by the suite
    that runs on every change.
    """
    database = _SCRIPT.parent / "data" / "shelfos.db"
    ours = not database.exists()
    if ours:
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(b"not a database, just bytes to count\n")
    try:
        before = database.read_bytes()
        _run("devel", "--dry-run", "--port", "9099", "--no-seed", "--no-install")
        _run("devel", "--dry-run", "--reset", "--port", "9099", "--no-install")
        assert database.read_bytes() == before
    finally:
        if ours:
            database.unlink()


def test_devel_reports_the_database_in_the_clone() -> None:
    result = _run("devel", "--dry-run", "--port", "9099", "--no-seed", "--no-install")
    assert "data/shelfos.db" in result.stderr


# --- status -------------------------------------------------------------------


def test_status_without_an_install_says_so(tmp_path: Path) -> None:
    result = _run("status")
    assert result.returncode == 3
    assert "No installed service" in result.stdout
    assert "Traceback" not in result.stderr


def test_status_never_prints_a_setting_value() -> None:
    """It reports that a placeholder is still there, never what any value is."""
    result = _run("status")
    assert "SHELFOS_SECRET_KEY=" not in result.stdout


# --- what review found -------------------------------------------------------


def test_deploy_refuses_rather_than_inventing_a_password(tmp_path: Path) -> None:
    """A placeholder written into /etc/shelfos/env would clear the app's own
    length floor, so the install would come up healthy on a password printed in
    this repository."""
    env, _ = _sudo_trap(tmp_path)
    result = _run(
        "deploy",
        "--domain",
        "example.test",
        "--no-caddy",
        "--no-printer",
        "-y",
        env_extra=env,
    )
    assert result.returncode == 1
    assert "--admin-password-stdin" in result.stderr
    assert "prompted for" not in result.stderr


def test_a_password_with_sed_metacharacters_survives_intact(tmp_path: Path) -> None:
    """`&` on sed's replacement side expands to the whole matched line, and `|`
    would close the command — neither failure looks anything like a password."""
    script = _SCRIPT.read_text()
    body = script[script.index("render_env_file() {") :]
    body = body[: body.index("\n}\n") + 3]
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f"REPO_ROOT={_SCRIPT.parent}\n"
        "DEPLOY_ADMIN_USER='ze&non|x'\n"
        "DEPLOY_ADMIN_PASSWORD='p&ss|w\\ord'\n"
        "DEPLOY_WANT_PRINTER=0\n"
        f"{body}\n"
        "render_env_file 'a-secret'\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, result.stderr
    assert "SHELFOS_ADMIN_PASSWORD=p&ss|w\\ord" in result.stdout
    assert "SHELFOS_ADMIN_USERNAME=ze&non|x" in result.stdout
    assert "SHELFOS_SECRET_KEY=a-secret" in result.stdout


def test_a_malformed_settings_key_warns_and_carries_on(tmp_path: Path) -> None:
    """`${!key}` on an invalid name is fatal in bash, so a typo in the settings
    file used to end the script naming neither the file nor the line."""
    home = tmp_path / "home"
    (home / ".ShelfOS").mkdir(parents=True)
    (home / ".ShelfOS" / ".env").write_text("FOO-BAR=1\nPORT=9099\n")
    result = _run(
        "devel",
        "--dry-run",
        "--no-seed",
        "--no-install",
        env_extra={"HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert "FOO-BAR" in result.stderr
    assert "invalid variable name" not in result.stderr
    # And the readable settings after it were still applied.
    assert "9099" in result.stderr


def test_the_installed_port_comes_from_the_unit(tmp_path: Path) -> None:
    """deploy templates --port into ExecStart, so the unit is what knows it.

    Assuming the default made `status` call a healthy install unreachable and
    made `update` recommend rolling back a good update.
    """
    script = _SCRIPT.read_text()
    body = script[script.index("installed_port() {") :]
    body = body[: body.index("\n}\n") + 3]
    unit = tmp_path / "shelfos.service"
    unit.write_text(
        (_SCRIPT.parent / "deploy" / "shelfos.service")
        .read_text()
        .replace("--port 9000", "--port 9100")
    )
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f"SERVICE_PATH={unit}\nENV_FILE_SYSTEM=/nonexistent\nDEFAULT_PORT=9000\n"
        "env_file_value() { :; }\n"
        f"{body}\ninstalled_port\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.stdout.strip() == "9100", result.stderr


@pytest.mark.parametrize(
    ("content", "replaceable"),
    [
        ("", True),
        ("# only a comment\n", True),
        ("{MARKER}\nshelfos.x {{\n  reverse_proxy 127.0.0.1:9000\n}}\n", True),
        ("{MARKER}\nshelfos.x {{\n  a\n}}\nother.pl {{\n  b\n}}\n", False),
        ("other.pl {{\n  # shelfos is only mentioned here\n  b\n}}\n", False),
    ],
)
def test_only_a_caddyfile_that_is_ours_alone_is_replaced(
    tmp_path: Path, content: str, replaceable: bool
) -> None:
    """Overwriting one that serves somebody else's site takes it off the air."""
    script = _SCRIPT.read_text()
    marker = script.split('readonly CADDY_MARKER="', 1)[1].split('"', 1)[0]
    body = script[script.index("caddyfile_is_ours_alone() {") :]
    body = body[: body.index("\n}\n") + 3]
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(content.format(MARKER=marker))
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f"CADDYFILE={caddyfile}\nCADDY_MARKER='{marker}'\n{body}\n"
        "if caddyfile_is_ours_alone; then echo replace; else echo separate; fi\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.stdout.strip() == ("replace" if replaceable else "separate")


def test_backup_restore_never_steps_down_to_the_service_user(tmp_path: Path) -> None:
    """Stepping down gives up root's right to traverse directories.

    An archive normally sits in the operator's home, which is 0750 on Ubuntu, so
    the service user cannot enter it whatever the archive's own mode is. That
    surfaced as "Permission denied" on a plainly world-readable file, and no
    amount of chmod on the file helped.
    """
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_backup() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert "sudo -u" not in body, "backup steps down to the service user again"
    # And it puts the ownership back afterwards (in give_data_back), or the
    # service comes up unable to write the database it just restored.
    assert "give_data_back" in body


def _restore_resolver(tmp_path: Path) -> str:
    """The archive-path branch of cmd_backup, on its own, callable from bash."""
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_backup() {") :]
    body = body[: body.index("\n}\n") + 3]
    resolver = body[body.index('if [ "$action" = restore ]; then') :]
    return resolver[: resolver.index("\n    fi\n") + 7]


def _run_resolver(tmp_path: Path, cwd: Path, *args: str):  # type: ignore[no-untyped-def]
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "\n".join(
            [
                "set -euo pipefail",
                'die() { printf "error: %s\\n" "$2" >&2; exit "$1"; }',
                "resolve() {",
                "    local action=restore",
                _restore_resolver(tmp_path),
                '    printf "%s\\n" "$@"',
                "}",
                f"cd {cwd}",
                "resolve " + " ".join(args),
                "",
            ]
        )
    )
    return subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_backup_restore_makes_the_archive_path_absolute(tmp_path: Path) -> None:
    """It is read by a process that need not share this working directory."""
    archive = tmp_path / "sub" / "snap.tar.gz"
    archive.parent.mkdir()
    archive.touch()
    result = _run_resolver(tmp_path, archive.parent, "../sub/snap.tar.gz")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(archive)


def test_a_missing_directory_fails_instead_of_becoming_a_root_path(
    tmp_path: Path,
) -> None:
    """`cd` failing used to leave the substitution empty, so a typo in the
    directory became "/<basename>" — a path the operator never typed, at the
    filesystem root, which might even exist."""
    result = _run_resolver(tmp_path, tmp_path, "./bakups/snap.tar.gz")
    assert result.returncode == 1
    assert "no such directory" in result.stderr
    assert "/snap.tar.gz" not in result.stdout


def test_the_archive_is_found_after_a_leading_flag(tmp_path: Path) -> None:
    """`restore --force snap.tar.gz` is a reasonable thing to type, and looking
    only at $1 left the relative path to be read by a root process elsewhere —
    with the service already stopped."""
    archive = tmp_path / "sub" / "snap.tar.gz"
    archive.parent.mkdir()
    archive.touch()
    result = _run_resolver(tmp_path, archive.parent, "--force", "snap.tar.gz")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["--force", str(archive)]


def test_the_data_is_handed_back_even_when_a_restore_fails(tmp_path: Path) -> None:
    """backup.py swaps the database before the attachments, so a part-way
    failure is exactly when root-owned data is left behind — and the case where
    nobody thinks to check ownership."""
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_backup() {") :]
    body = body[: body.index("\n}\n") + 3]
    tail = body[
        body.index(
            'if [ "$action" = restore ] && is_deployed; then',
            body.index("local status=0"),
        ) :
    ]
    assert "give_data_back" in tail
    assert 'if [ "$status" = 0 ]' not in tail, "the chown is gated on success again"


def test_the_handback_resolves_symlinks(tmp_path: Path) -> None:
    """backup.py supports an attachments directory that is a symlink to external
    storage, and `chown -R` on a symlinked operand changes the link, not the
    tree behind it."""
    script = _SCRIPT.read_text()
    body = script[script.index("give_data_back() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert "readlink -f" in body


def test_the_installed_code_is_not_owned_by_the_service_user() -> None:
    """It is executed by root now (backup, password), and the unit's
    ProtectSystem=strict already made ownership of it worth nothing to the
    service — so a service account able to edit it would be a path from a
    web-app bug to root on the operator's next sudo."""
    script = _SCRIPT.read_text()
    assert 'chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"' not in script
    assert 'chown -R root:root "$INSTALL_DIR"' in script


def test_password_is_a_command() -> None:
    """The way out of a start that refuses over a password.

    Without it the app's own advice — run scripts/set_password.py — does not
    work on a deployed install: the script falls back to a database path
    relative to the working directory, which on a server is not where the
    database is.
    """
    result = _run("password", "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "password" in _run("--help").stdout


def test_password_names_the_database_for_the_helper() -> None:
    """Which is the whole reason this wrapper exists rather than a doc line."""
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_password() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert '"${ENV_ARGS[@]}"' in body
    assert "set_password.py" in body
    assert "give_data_back" in body  # it wrote the database as root


def test_restore_warns_before_starting_into_a_refusal() -> None:
    """An archive carries its own accounts, so one from a laptop brings that
    laptop's admin — and the next start refuses over a password nobody on this
    machine chose, three steps after the cause."""
    script = _SCRIPT.read_text()
    guard = script[script.index("restore_password_guard() {") :]
    guard = guard[: guard.index("\n}\n") + 3]
    assert "admins_on_the_default_password" in guard
    assert "set_password.py" in guard
    body = script[script.index("cmd_backup() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert body.index("restore_password_guard") < body.index(
        'systemctl start "$SERVICE_NAME"'
    )


def test_every_global_the_script_reads_is_one_it_sets() -> None:
    """Under `set -u` an unassigned reference is not a style problem, it is a
    crash — and one shipped this way, in the handback after a restore, where it
    fired after the database had been replaced and before the service started.

    shellcheck finds it only with check-unassigned-uppercase, which is not on by
    default; CI enables it, and this fails the suite even if that job does not
    run.
    """
    script = _SCRIPT.read_text()
    # Every reference, not only the ones with a trailing sigil. The earlier
    # pattern required one, so it saw ${FOO#...} but not "$FOO" — and the crash
    # it was written for had both on the same line, caught by the accident of
    # which half came first. Two-character names count too.
    # A backslash-escaped $ is text the script prints, not a reference it makes.
    read = set(re.findall(r"(?<!\\)\$\{?([A-Z][A-Z0-9_]+)", script))
    # Anywhere on a line, not only at the start: several are set in a run of
    # `A=1; B=2` and a start-anchored pattern would call them unassigned.
    assigned = set(re.findall(r"(?:^|[;&|\s(])([A-Z][A-Z0-9_]+)=", script, re.M))
    # Set by the environment or by bash itself, not by this file.
    external = {
        "HOME",
        "PATH",
        "PORT",
        "PYTHON",
        "TMPDIR",
        "SHELFOS_ENV_FILE",
        "DATABASE_URL",
        "SHELFOS_ATTACHMENTS_DIR",
        "SHELFOS_NEW_PASSWORD",
        "RUNNER_TEMP",
        "IFS",
        # Read out of /etc/os-release inside a subshell; not this file's to set.
        "ID",
        "ID_LIKE",
    }
    missing = sorted(read - assigned - external)
    assert not missing, f"referenced but never assigned: {missing}"


def _guard_probe(tmp_path: Path, *, exposed: str, ok: bool, assume_yes: int) -> str:
    """Drive restore_password_guard with everything around it stubbed."""
    script = _SCRIPT.read_text()
    body = script[script.index("restore_password_guard() {") :]
    body = body[: body.index("\n}\n") + 3]
    lister = (
        f"admins_on_the_default_password() {{ printf '%s' {shlex.quote(exposed)}; }}"
        if ok
        else "admins_on_the_default_password() { return 1; }"
    )
    probe = tmp_path / "guard.sh"
    probe.write_text(
        "\n".join(
            [
                "set -euo pipefail",
                f"ASSUME_YES={assume_yes}",
                "QUIET=0; C_YELLOW=''; C_OFF=''; C_DIM=''",
                'warn() { printf "warning: %s\\n" "$*" >&2; }',
                'info() { printf "%s\\n" "$*" >&2; }',
                "have_tty() { return 1; }",
                "give_data_back() { :; }",
                'ask_yes_no() { echo "PROMPTED" >&2; return 0; }',
                lister,
                body,
                "restore_password_guard",
                "",
            ]
        )
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return result.stderr


def test_the_guard_keeps_a_username_whole(tmp_path: Path) -> None:
    """A username is free text — user_service only refuses an empty one — so
    splitting on whitespace would prompt for half a name and then try to set the
    password of an account that does not exist."""
    out = _guard_probe(tmp_path, exposed="Ala Kowalska\nbob\n", ok=True, assume_yes=1)
    assert "password 'Ala Kowalska'" in out
    assert "password 'Ala'" not in out


def test_the_guard_does_not_prompt_when_nobody_asked(tmp_path: Path) -> None:
    """--yes and no terminal both mean unattended, and set_password.py reads the
    new password from a terminal — so offering would block a scripted restore
    with the service still stopped."""
    out = _guard_probe(tmp_path, exposed="admin\n", ok=True, assume_yes=1)
    assert "PROMPTED" not in out
    assert "shelfos.sh password 'admin'" in out


def test_a_failed_check_is_not_a_clean_bill_of_health(tmp_path: Path) -> None:
    """A broken venv, an unreadable database and a refused sudo all produce no
    output; reading that as "no exposed admins" would let the one safety net
    between a restore and a dead service report all clear."""
    out = _guard_probe(tmp_path, exposed="", ok=False, assume_yes=1)
    assert "could not check" in out


def test_the_guard_runs_whatever_the_restore_returned() -> None:
    """backup.py swaps the database before the attachments, so a part-way
    failure has usually already installed the archive's accounts."""
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_backup() {") :]
    body = body[: body.index("\n}\n") + 3]
    tail = body[body.index("restore_password_guard") - 400 :]
    assert '[ "$status" = 0 ] && restore_password_guard' not in tail


def test_listing_accounts_changes_nothing() -> None:
    """--list exits 0 without writing, and used to fall into the chown and the
    offer to start the service."""
    script = _SCRIPT.read_text()
    body = script[script.index("cmd_password() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert "--list|-l) listing=1" in body
    assert '[ "$listing" = 0 ]' in body


def test_a_clone_names_its_database_absolutely() -> None:
    """The script is meant to run from anywhere — resolve_repo_root follows a
    symlink into ~/bin — and the helpers resolve a relative DATABASE_URL against
    the working directory."""
    script = _SCRIPT.read_text()
    body = script[script.index("resolve_install() {") :]
    body = body[: body.index("\n}\n") + 3]
    assert 'DEPLOY_DB="sqlite:///$REPO_ROOT/data/shelfos.db"' in body
    assert "${DATABASE_URL:-$DEPLOY_DB}" in body  # a caller's value still wins


# --- tunnel-key: who may bring a label printer here ---------------------------


def _tunnel_probe(tmp_path: Path, body: str) -> subprocess.CompletedProcess:  # type: ignore[no-untyped-def]
    """Run the tunnel-key functions against a home of our own.

    The real ones write to /var/lib/shelfos-tunnel through sudo, which a test
    cannot have; everything else about them — validation, the options line, the
    add/list/remove bookkeeping — is exactly what wants testing, so the file
    operations are pointed at tmp_path and sudo is replaced by running the
    command directly.
    """
    script = _SCRIPT.read_text()
    rule = "# " + "-" * 75
    start = script.index("tunnel_key_options() {")
    end = script.index(f"{rule}\n# main")
    # tunnel_port lives up with the deploy steps, and the permitted port comes
    # from it — so take the real one rather than stub the answer being asserted.
    port_fn = script[
        script.index("tunnel_port() {") : script.index("sshd_dropin_body() {")
    ]
    home = tmp_path / "tunnel-home"
    home.mkdir()
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -uo pipefail\n"
        f"TUNNEL_USER=$(id -un)\nSERVICE_USER=$(id -un)\n"
        f"TUNNEL_HOME={home}\nTUNNEL_KEYS={home}/tunnel-keys\nDRY_RUN=0\n"
        'info() { printf "%s\\n" "$*"; }\n'
        'note() { printf "%s\\n" "$*"; }\n'
        'die() { printf "%s\\n" "$2" >&2; exit "$1"; }\n'
        'valid_port() { [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ]; }\n'
        "env_file_value() { :; }\n"
        "ENV_FILE_SYSTEM=/nonexistent\n"
        # Writes land here directly: the point of the test is what gets written.
        'sudo_run() { "$@"; }\n'
        'write_file() { cat > "$1"; }\n'
        "ensure_tunnel_user() { :; }\n"
        # Anything this probe forgot to bring along must fail the test rather
        # than quietly expand to nothing — which is how an empty port reached an
        # authorized_keys line here once.
        'command_not_found_handle() { printf "MISSING: %s\\n" "$1" >&2; exit 127; }\n'
        f"{port_fn}\n{script[start:end]}\n{body}\n"
    )
    return subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def _make_key(directory: Path, comment: str) -> str:
    """A real ed25519 public key, made the way the installer makes one.

    Not a hand-written string that looks like one: `tunnel-key add` asks
    ssh-keygen for a second opinion, and a fake would be refused there for a
    reason that has nothing to do with what each test is about.
    """
    path = directory / comment.replace("@", "-")
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return path.with_suffix(".pub").read_text().strip()


@pytest.fixture(scope="module")
def good_key(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _make_key(tmp_path_factory.mktemp("keys"), "shelfos-label@goofy")


@pytest.mark.parametrize(
    "mangle",
    [
        # Options of its own: this line is appended to a file sshd reads as
        # configuration, so a key that brings its own permissions is the whole
        # attack. Refused, never escaped.
        lambda key: 'command="/bin/sh" ' + key,
        lambda key: "no-pty," + key,
        # A second key smuggled in on a second line, authorised unseen.
        lambda key: key + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB7iVYt x",
        lambda key: key + "\r\nssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7iVYt y",
        lambda key: key.replace("ssh-ed25519", "ssh-dss"),
        lambda key: key.split(" ", 1)[1],  # the type stripped off
        lambda key: key.replace("AAAA", "not-base64!!", 1),
        lambda key: key + " $(id)",
        lambda _key: "",
    ],
)
def test_a_key_that_is_more_than_a_key_is_refused(
    tmp_path: Path, good_key: str, mangle
) -> None:  # type: ignore[no-untyped-def]
    key = mangle(good_key)
    result = _tunnel_probe(
        tmp_path,
        f"if valid_public_key {shlex.quote(key)};"
        " then echo accepted; else echo refused; fi",
    )
    assert result.stdout.strip() == "refused", result.stdout


def test_an_ordinary_public_key_is_accepted(tmp_path: Path, good_key: str) -> None:
    result = _tunnel_probe(
        tmp_path,
        f"if valid_public_key {shlex.quote(good_key)};"
        " then echo accepted; else echo refused; fi",
    )
    assert result.stdout.strip() == "accepted", result.stdout + result.stderr


def test_an_authorized_key_may_only_bind_the_one_port(
    tmp_path: Path, good_key: str
) -> None:
    """The options are the whole security story of this feature.

    `restrict` turns everything off; `port-forwarding` puts back forwarding in
    BOTH directions, which is why permitopen has to close the outgoing half —
    without it the same key would turn the server into a proxy into whatever
    network it sits on.
    """
    result = _tunnel_probe(tmp_path, f"tunnel_key_add {shlex.quote(good_key)}")
    assert result.returncode == 0, result.stderr
    line = (tmp_path / "tunnel-home" / "tunnel-keys").read_text().strip()
    assert line.startswith("restrict,port-forwarding,")
    assert 'permitlisten="127.0.0.1:9100"' in line
    assert 'permitopen="127.0.0.1:1"' in line
    assert line.endswith(good_key)


def test_the_same_machine_coming_back_replaces_its_entry(
    tmp_path: Path, good_key: str
) -> None:
    """Never two lines for one key: the older one would keep permitting an
    older port for ever, and nothing would ever say so."""
    result = _tunnel_probe(
        tmp_path,
        f"tunnel_key_add {shlex.quote(good_key)}\n"
        f"tunnel_key_add {shlex.quote(good_key)}\n"
        "tunnel_key_list",
    )
    assert result.returncode == 0, result.stderr
    keys = (tmp_path / "tunnel-home" / "tunnel-keys").read_text()
    assert keys.count("ssh-ed25519") == 1, keys
    assert result.stdout.count("shelfos-label@goofy") == 1, result.stdout


def test_a_key_can_be_withdrawn_by_the_name_it_was_added_under(
    tmp_path: Path, good_key: str
) -> None:
    other = _make_key(tmp_path, "shelfos-label@dopey")
    result = _tunnel_probe(
        tmp_path,
        f"tunnel_key_add {shlex.quote(good_key)}\n"
        f"tunnel_key_add {shlex.quote(other)}\n"
        "tunnel_key_remove shelfos-label@goofy\n"
        "tunnel_key_list",
    )
    assert result.returncode == 0, result.stderr
    keys = (tmp_path / "tunnel-home" / "tunnel-keys").read_text()
    assert "dopey" in keys and "goofy" not in keys
    assert "shelfos-label@dopey" in result.stdout


def test_removing_something_that_is_not_there_fails_loudly(
    tmp_path: Path, good_key: str
) -> None:
    """Silence here would read as "withdrawn" for a key that still works."""
    result = _tunnel_probe(
        tmp_path,
        f"tunnel_key_add {shlex.quote(good_key)}\ntunnel_key_remove nobody@nowhere",
    )
    assert result.returncode != 0
    assert "nobody@nowhere" in result.stderr


def test_the_permitted_port_follows_the_installed_setting(tmp_path: Path) -> None:
    """One source for the port: a key permitted to bind 9100 while the service
    listens for 9241 is a tunnel that comes up and carries nothing."""
    env = tmp_path / "env"
    env.write_text("SHELFOS_LABEL_DEVICE=tcp://127.0.0.1:9241\n")
    script = _SCRIPT.read_text()
    body = script[
        script.index("tunnel_port() {") : script.index("sshd_dropin_body() {")
    ]
    rule = "# " + "-" * 75
    helpers = script[
        script.index("env_file_value() {") : script.index(f"{rule}\n# Shared helpers")
    ]
    probe = tmp_path / "port.sh"
    probe.write_text(
        f"ENV_FILE_SYSTEM={env}\n"
        'valid_port() { [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ]; }\n'
        f"{helpers}\n{body}\ntunnel_port\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.stdout.strip() == "9241", result.stderr


def test_a_dry_run_tunnel_key_never_reaches_sudo(tmp_path: Path, good_key: str) -> None:
    env, log = _sudo_trap(tmp_path)
    result = _run("tunnel-key", "--dry-run", "add", good_key, env_extra=env)
    assert result.returncode == 0, result.stderr
    assert not log.exists(), log.read_text()
    assert "would authorize" in result.stderr


def test_tunnel_key_refuses_a_key_carrying_its_own_options(
    tmp_path: Path, good_key: str
) -> None:
    """End to end through the real command, not only the helper."""
    result = _run("tunnel-key", "add", 'command="/bin/sh" ' + good_key)
    assert result.returncode == 2
    assert "not a plain public key" in result.stderr


# --- the sshd block, which is the ceiling on every registered key -------------


def _dropin(tmp_path: Path, env: str = "") -> str:
    """Render the sshd drop-in the deploy installs."""
    script = _SCRIPT.read_text()
    rule = "# " + "-" * 75
    body = script[
        script.index("tunnel_port() {") : script.index("deploy_step_tunnel() {")
    ]
    helpers = script[
        script.index("env_file_value() {") : script.index(f"{rule}\n# Shared helpers")
    ]
    env_file = tmp_path / "env"
    env_file.write_text(env)
    probe = tmp_path / "dropin.sh"
    probe.write_text(
        f"ENV_FILE_SYSTEM={env_file}\n"
        "TUNNEL_USER=shelfos-tunnel\n"
        "TUNNEL_KEYS_COMMAND=/usr/local/lib/shelfos/tunnel-keys\n"
        'valid_port() { [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ]; }\n'
        f"{helpers}\n{body}\nsshd_dropin_body\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_sshd_block_closes_itself(tmp_path: Path) -> None:
    """`Match all` at the end, and it is not a nicety.

    Drop-ins are included at the TOP of sshd_config, so a Match block left open
    would swallow every global setting after it — the whole server's ssh
    configuration would silently apply to one account and nothing else.
    """
    lines = [line for line in _dropin(tmp_path).splitlines() if line.strip()]
    matches = [line for line in lines if line.startswith("Match ")]
    assert matches == ["Match User shelfos-tunnel", "Match all"]
    assert lines[-1] == "Match all"


def test_a_registered_key_may_only_carry_the_printer(tmp_path: Path) -> None:
    """The ceiling lives here rather than in the key's own options, so it holds
    however the key got into the file — including if ShelfOS itself were made to
    write one."""
    block = _dropin(tmp_path)
    assert "Match User shelfos-tunnel" in block
    assert "AllowTcpForwarding remote" in block  # -R only; no outbound tunnels
    assert "PermitOpen none" in block
    # Both spellings of the one binding: sshd matches PermitListen against what
    # the client ASKED for, and a request for a bare port carries no address —
    # so an entry naming only the address it resolves to can refuse exactly the
    # forward it was written to allow. Neither reaches past loopback.
    assert "PermitListen 127.0.0.1:9100 localhost:9100" in block
    assert "PermitTTY no" in block
    assert "ForceCommand /usr/sbin/nologin" in block
    assert "AuthorizedKeysCommandUser root" in block


def test_the_permitted_port_follows_the_setting(tmp_path: Path) -> None:
    """A key allowed to bind 9100 while the service listens for 9241 is a tunnel
    that comes up and carries nothing."""
    block = _dropin(tmp_path, env="SHELFOS_LABEL_DEVICE=tcp://127.0.0.1:9241\n")
    assert "PermitListen 127.0.0.1:9241 localhost:9241" in block


def test_the_keys_command_answers_for_one_account_only(tmp_path: Path) -> None:
    """sshd runs it as root, so it does one thing: print one file for one name."""
    command = (_SCRIPT.parent / "deploy" / "tunnel-keys.sh").read_text()
    rendered = command.replace("@TUNNEL_USER@", "shelfos-tunnel").replace(
        "@TUNNEL_KEYS@", str(tmp_path / "keys")
    )
    path = tmp_path / "tunnel-keys.sh"
    path.write_text(rendered)
    (tmp_path / "keys").write_text("ssh-ed25519 AAAA test@machine\n")

    asked_for_ours = subprocess.run(
        ["sh", str(path), "shelfos-tunnel"], capture_output=True, text=True
    )
    assert asked_for_ours.stdout.strip() == "ssh-ed25519 AAAA test@machine"

    for other in ("root", "shelfos", ""):
        answer = subprocess.run(
            ["sh", str(path), other], capture_output=True, text=True
        )
        assert answer.stdout == "", other
        assert answer.returncode == 0


def test_a_missing_key_file_is_not_an_error(tmp_path: Path) -> None:
    """No printer has registered yet. sshd reads empty output as "no keys"; an
    error would be logged as a broken server instead."""
    command = (_SCRIPT.parent / "deploy" / "tunnel-keys.sh").read_text()
    path = tmp_path / "tunnel-keys.sh"
    path.write_text(
        command.replace("@TUNNEL_USER@", "shelfos-tunnel").replace(
            "@TUNNEL_KEYS@", str(tmp_path / "nothing-here")
        )
    )
    answer = subprocess.run(
        ["sh", str(path), "shelfos-tunnel"], capture_output=True, text=True
    )
    assert answer.returncode == 0
    assert answer.stdout == ""


# --- steps that must not fail the whole deploy --------------------------------


def _step_probe(tmp_path: Path, setup: str, call: str) -> subprocess.CompletedProcess:  # type: ignore[no-untyped-def]
    """Run one deploy step with everything privileged replaced.

    `deploy` traps ERR and stops on the first failing command, which is right
    for a step that has actually failed and wrong for one that merely could not
    finish a cosmetic part of its job. That distinction is only visible with the
    trap in place, so the probe installs it too.
    """
    script = _SCRIPT.read_text()
    body = script[
        script.index("deploy_step_printer() {") : script.index(
            'readonly CADDY_MARKER="'
        )
    ]
    probe = tmp_path / "step.sh"
    probe.write_text(
        "set -Eeuo pipefail\n"
        "trap 'echo TRAP-FIRED >&2; exit 9' ERR\n"
        "step() { :; }\nstep_ok() { :; }\nstep_skipped() { :; }\n"
        'warn() { printf "WARNED: %s\\n" "$*" >&2; }\n'
        "write_file() { cat > /dev/null; }\n"
        "DRY_RUN=0\nDEPLOY_WANT_PRINTER=1\nDEPLOY_PRINTER_GROUP=plugdev\n"
        "BROTHER_VENDOR=04f9\n"
        f"UDEV_RULE={tmp_path}/rules\n"
        f"{setup}\n{body}\n{call}\n"
    )
    return subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_udev_refusing_to_reapply_the_rule_does_not_fail_the_deploy(
    tmp_path: Path,
) -> None:
    """What killed a real deploy at step 10 of 12, with everything installed and
    nothing started.

    In a container /sys is not writable even for root, so `udevadm trigger`
    prints "Permission denied" for every device and exits non-zero. The rule is
    written either way and takes effect at the next replug; ending the deploy
    there is much worse than saying so.
    """
    result = _step_probe(
        tmp_path,
        setup='sudo_run() { [ "$1" = udevadm ] && return 1; return 0; }',
        call="deploy_step_printer",
    )
    assert "TRAP-FIRED" not in result.stderr
    assert result.returncode == 0, result.stderr
    assert "WARNED" in result.stderr
    assert "replug" in result.stderr


def test_every_step_runs_after_the_step_that_makes_what_it_writes_to(
    tmp_path: Path,
) -> None:
    """Ordering, asserted rather than remembered.

    The tunnel step went in before the one that creates /var/lib/shelfos and
    died on a real deploy with "cannot create regular file ... No such file or
    directory" — at step 4 of 13, having made an account and nothing else. The
    dependency is invisible when reading either function on its own, so it is
    written down here.
    """
    script = _SCRIPT.read_text()
    order = [
        line.strip()
        for line in script[
            script.index("    deploy_step_packages") : script.index("    trap - ERR")
        ].splitlines()
        if line.strip().startswith("deploy_step_")
    ]
    assert order.index("deploy_step_dirs") < order.index("deploy_step_tunnel")
    assert order.index("deploy_step_user") < order.index("deploy_step_tunnel")
    # And the settings the app reads are written before it is imported or run.
    assert order.index("deploy_step_env") < order.index("deploy_step_import")
    assert order.index("deploy_step_unit") < order.index("deploy_step_verify")


def test_the_key_store_is_written_under_the_data_directory(tmp_path: Path) -> None:
    """The two paths that have to agree: what the deploy creates, and what the
    app is told to write. A mismatch is only visible when a printer registers."""
    script = _SCRIPT.read_text()
    assert 'readonly TUNNEL_KEYS="$DATA_DIR/tunnel-keys"' in script
    made = script[
        script.index("deploy_step_dirs() {") : script.index("deploy_step_code() {")
    ]
    assert '"$DATA_DIR"' in made


def _env_probe(tmp_path: Path, content: str, calls: str) -> tuple[str, str]:
    """Run set_env_setting against a settings file of our own."""
    script = _SCRIPT.read_text()
    body = script[
        script.index("set_env_setting() {") : script.index("deploy_tunnel_account() {")
    ]
    env_file = tmp_path / "env"
    env_file.write_text(content)
    probe = tmp_path / "env-probe.sh"
    probe.write_text(
        "set -Eeuo pipefail\n"
        f"ENV_FILE_SYSTEM={env_file}\nSERVICE_USER=$(id -un)\nDRY_RUN=0\n"
        'info() { printf "INFO: %s\\n" "$*" >&2; }\n'
        'sudo_run() { "$@"; }\n'
        'write_file() { cat > "$1"; }\n'
        f"{body}\n{calls}\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, result.stderr
    return env_file.read_text(), result.stderr


def test_a_settings_file_from_before_learns_the_new_settings(tmp_path: Path) -> None:
    """The upgrade case, and the one a real deploy walked into.

    An existing /etc/shelfos/env is never replaced — it holds the signing secret
    and the shop keys — so a server set up for registering printers would have
    gone on saying it was not.
    """
    text, log = _env_probe(
        tmp_path,
        "SHELFOS_SECRET_KEY=abc\nSHELFOS_LABEL_DEVICE=tcp://127.0.0.1:9100\n",
        "set_env_setting SHELFOS_TUNNEL_USER shelfos-tunnel\n"
        "set_env_setting SHELFOS_TUNNEL_KEYS /var/lib/shelfos/tunnel-keys\n",
    )
    assert "SHELFOS_TUNNEL_USER=shelfos-tunnel" in text
    assert "SHELFOS_TUNNEL_KEYS=/var/lib/shelfos/tunnel-keys" in text
    # Everything that was there is still there, once.
    assert text.count("SHELFOS_SECRET_KEY=abc") == 1
    assert "INFO" in log


def test_an_empty_setting_is_filled_in_where_it_stands(tmp_path: Path) -> None:
    """In place, not appended: a second line for the same key would win, and the
    first would mislead whoever read the file next."""
    text, _ = _env_probe(
        tmp_path,
        "SHELFOS_TUNNEL_USER=\nSHELFOS_SECRET_KEY=abc\n",
        "set_env_setting SHELFOS_TUNNEL_USER shelfos-tunnel\n",
    )
    assert text.splitlines()[0] == "SHELFOS_TUNNEL_USER=shelfos-tunnel"
    assert text.count("SHELFOS_TUNNEL_USER") == 1


def test_an_answer_already_there_is_left_alone(tmp_path: Path) -> None:
    """Including one that disagrees with this deploy: it is somebody's choice,
    and a deploy is not the place to overrule it."""
    text, log = _env_probe(
        tmp_path,
        "SHELFOS_TUNNEL_KEYS=/srv/keys\n",
        "set_env_setting SHELFOS_TUNNEL_KEYS /var/lib/shelfos/tunnel-keys\n",
    )
    assert text == "SHELFOS_TUNNEL_KEYS=/srv/keys\n"
    assert "INFO" not in log


def _sshd_probe(tmp_path: Path, mode: str) -> tuple[str, Path]:
    """Run the sshd half of the tunnel step against a stand-in for sshd.

    ``mode`` is how that stand-in behaves, and the three are the three ways this
    can go: it will not test anything until its run directory exists; it rejects
    what this step wrote; or it rejects the machine's own configuration, ours or
    no ours.
    """
    script = _SCRIPT.read_text()
    # From tunnel_port on: the drop-in's body is built from it, and the step
    # writes what that produces.
    body = script[
        script.index("tunnel_port() {") : script.index("deploy_step_dirs() {")
    ]
    dropin = tmp_path / "60-shelfos-tunnel.conf"
    run_dir = tmp_path / "run-sshd"
    behaviour = {
        "needs-run-dir": (
            f'if [ ! -d "{run_dir}" ]; then\n'
            '  echo "Missing privilege separation directory: /run/sshd" >&2\n'
            "  exit 1\nfi\nexit 0\n"
        ),
        "rejects-ours": (
            f'if [ -f "{dropin}" ]; then\n'
            f'  echo "{dropin}: line 4: Bad configuration option" >&2\n'
            "  exit 1\nfi\nexit 0\n"
        ),
        "broken-anyway": 'echo "/etc/ssh/sshd_config: line 12: bad" >&2\nexit 1\n',
    }[mode]
    fake = tmp_path / "sshd"
    fake.write_text("#!/bin/sh\n" + behaviour)
    fake.chmod(0o755)
    probe = tmp_path / "sshd-probe.sh"
    probe.write_text(
        "set -Eeuo pipefail\n"
        "trap 'echo TRAP-FIRED >&2; exit 9' ERR\n"
        "step_ok() { printf 'OK: %s\\n' \"$*\"; }\n"
        "step_skipped() { printf 'SKIPPED: %s\\n' \"$*\"; }\n"
        'warn() { printf "WARNED: %s\\n" "$*" >&2; }\n'
        'die() { printf "DIED: %s\\n" "$2" >&2; exit 1; }\n'
        'write_file() { cat > "$1"; }\n'
        # Ownership is not what these tests are about, and a test cannot have
        # root; everything else runs for real, including the stand-in sshd.
        "sudo_run() {\n"
        '  case "$1" in\n'
        f'    {fake}) shift; "{fake}" "$@" ;;\n'
        '    install) mkdir -p "${@: -1}" ;;\n'
        "    systemctl) return 1 ;;\n"
        '    *) "$@" ;;\n'
        "  esac\n"
        "}\n"
        "systemctl() { return 1; }\n"
        "DRY_RUN=0\nTUNNEL_USER=shelfos-tunnel\n"
        f"TUNNEL_KEYS={tmp_path}/keys\n"
        f"TUNNEL_KEYS_COMMAND={tmp_path}/bin/tunnel-keys\n"
        f"SSHD_DROPIN={dropin}\nSSHD_BIN={fake}\n"
        f"SSHD_DROPIN_DIR={tmp_path}\nSSHD_RUN_DIR={run_dir}\n"
        f"REPO_ROOT={_SCRIPT.parent}\n"
        "ENV_FILE_SYSTEM=/nonexistent\n"
        'valid_port() { [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ]; }\n'
        "env_file_value() { :; }\n"
        f"{body}\ndeploy_tunnel_sshd\n"
    )
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return result.stdout + result.stderr, dropin


def test_sshd_refusing_to_test_anything_is_not_our_configuration(
    tmp_path: Path,
) -> None:
    """What a fresh container does, and what killed a deploy at step 5 of 13.

    `sshd -t` will not test a configuration at all while /run/sshd is missing,
    and on a machine where ssh has never started it is missing — systemd makes
    it when the service comes up. The check was therefore failing over something
    it had written nothing about, and withdrawing a perfectly good file.
    """
    output, dropin = _sshd_probe(tmp_path, "needs-run-dir")
    assert "DIED" not in output, output
    assert "TRAP-FIRED" not in output
    assert "OK: sshd will take registered printers" in output
    assert "Match User shelfos-tunnel" in dropin.read_text()


def test_a_configuration_sshd_really_rejects_is_withdrawn(tmp_path: Path) -> None:
    """The case the check exists for: our file is the problem, so it goes, and
    nothing is reloaded."""
    output, dropin = _sshd_probe(tmp_path, "rejects-ours")
    assert "DIED" in output
    assert not dropin.exists()


def test_an_already_broken_sshd_config_is_not_blamed_on_this_deploy(
    tmp_path: Path,
) -> None:
    """It fails without our file too, so withdrawing ours fixes nothing. Put it
    back, say the configuration could not be checked, and carry on rather than
    ending a deploy over something that was already there."""
    output, dropin = _sshd_probe(tmp_path, "broken-anyway")
    assert "DIED" not in output
    assert "WARNED" in output
    assert "not verified" in output
    assert "Match User shelfos-tunnel" in dropin.read_text()


# --- which address the service binds ------------------------------------------


def _rendered_unit(tmp_path: Path, *args: str) -> str:
    """The unit a dry-run deploy would install, as it prints it."""
    env, _ = _sudo_trap(tmp_path)
    result = _run("deploy", "--dry-run", "-y", *args, env_extra=env)
    assert result.returncode == 0, result.stderr
    return result.stderr


def test_the_service_binds_loopback_unless_it_is_told_otherwise(
    tmp_path: Path,
) -> None:
    """The default is the safe one: a plain-HTTP port on a network interface
    carries sign-ins in the clear, so it is asked for, never assumed."""
    unit = _rendered_unit(tmp_path, "--no-tls")
    assert "--host 127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit


def test_listen_puts_the_address_in_the_unit(tmp_path: Path) -> None:
    """Reaching the service at the machine's own address, with no proxy device
    or port forward in between — which is the point of the option."""
    unit = _rendered_unit(tmp_path, "--no-tls", "--listen", "0.0.0.0")
    assert "--host 0.0.0.0 \\" in unit
    # The line keeps its continuation, or the unit stops parsing there and
    # every argument after it is silently lost.
    assert "--port 9000" in unit


def test_a_non_loopback_bind_says_what_it_costs(tmp_path: Path) -> None:
    """It is a reasonable thing to want and an unreasonable thing to do by
    accident, so the summary says plainly what is being published."""
    output = _rendered_unit(tmp_path, "--no-tls", "--listen", "0.0.0.0")
    assert "plain HTTP" in output


@pytest.mark.parametrize(
    "address", ["localhost", "0.0.0.0.0", "shelf.example", "", "1.2.3.4:9000"]
)
def test_a_bind_address_that_is_not_an_address_is_refused(
    tmp_path: Path, address: str
) -> None:
    """A name would be resolved by uvicorn at start-up, so a typo becomes a
    service that will not start, for a reason two layers down in the journal."""
    env, _ = _sudo_trap(tmp_path)
    result = _run(
        "deploy", "--dry-run", "-y", "--no-tls", "--listen", address, env_extra=env
    )
    assert result.returncode != 0
    assert "--listen" in result.stderr


def test_the_installed_address_is_read_back_from_the_unit(tmp_path: Path) -> None:
    """`status` asks the service where it actually is: a health check aimed at
    127.0.0.1 reports "no answer" for a perfectly healthy service bound
    somewhere else."""
    script = _SCRIPT.read_text()
    body = script[
        script.index("installed_listen() {") : script.index("installed_port() {")
    ]
    unit = tmp_path / "shelfos.service"
    unit.write_text(
        (_SCRIPT.parent / "deploy" / "shelfos.service")
        .read_text()
        .replace("--host 127.0.0.1", "--host 10.0.3.42")
    )
    probe = tmp_path / "listen.sh"
    probe.write_text(f"SERVICE_PATH={unit}\n{body}\ninstalled_listen\n")
    result = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert result.stdout.strip() == "10.0.3.42", result.stderr

    probe.write_text(f"SERVICE_PATH=/nonexistent\n{body}\ninstalled_listen\n")
    fallback = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert fallback.stdout.strip() == "127.0.0.1"


def test_a_proxy_and_a_direct_port_at_once_is_pointed_out(tmp_path: Path) -> None:
    """Both at once is almost certainly not what anybody meant.

    The app is then reachable past Caddy, so the certificate, the headers it
    adds and the trusted-proxy setting apply to one way in and not to the other
    — and nothing on screen would have said so.
    """
    output = _rendered_unit(tmp_path, "--domain", "example.test", "--listen", "0.0.0.0")
    assert "past it" in output


def test_the_ordinary_deploy_is_unchanged(tmp_path: Path) -> None:
    """Nothing above may alter the path almost everyone takes: Caddy in front,
    the service on loopback behind it."""
    output = _rendered_unit(tmp_path, "--domain", "example.test")
    assert "--host 127.0.0.1" in output
    assert "Caddy on example.test" in output
    assert "past it" not in output
    # The unit's own comments explain what binding 0.0.0.0 would mean, so match
    # the summary's wording rather than the two words it shares with them.
    assert "in plain HTTP, to anything" not in output
