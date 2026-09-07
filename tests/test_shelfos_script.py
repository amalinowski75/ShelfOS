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
    assert numbers == [str(n) for n in range(1, 13)], seen


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
