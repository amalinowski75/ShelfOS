"""The entry-point script: argument handling, and the promises it makes.

Most of `deploy` cannot run here — it installs packages, creates a user and
talks to systemd — so what is tested is everything up to that point plus the
one guarantee that must never break: `--dry-run` changes nothing and never
reaches `sudo`. `deploy/README.md` carries the by-hand checklist for the rest.
"""

from __future__ import annotations

import os
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


def test_devel_leaves_an_existing_database_untouched(tmp_path: Path) -> None:
    """Nothing in this script moves, renames or deletes what is under data/.

    Deletion belongs to scripts/reset_db.py, which asks for its own typed
    confirmation; here even --reset must get no further than naming it.
    """
    database = _SCRIPT.parent / "data" / "shelfos.db"
    if not database.exists():
        pytest.skip("no database in this checkout to guard")
    before = database.read_bytes()
    _run("devel", "--dry-run", "--port", "9099", "--no-seed", "--no-install")
    _run("devel", "--dry-run", "--reset", "--port", "9099", "--no-install")
    assert database.read_bytes() == before


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
