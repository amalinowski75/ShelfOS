"""The suite must see the documented defaults, not the shell it was started in.

``app.config`` and ``app.db`` read the environment when they are imported, so a
developer whose shell holds the real deployment's settings would otherwise test
those settings — and, once, print a real label from the one test that deliberately
prints with no device configured. ``tests/conftest.py`` empties them out first;
this is the proof that it still does.

The check needs a polluted environment, which cannot be arranged from inside a
process that has already imported ``app``. So the assertions live in their own
test, and the one above it runs that test again in a child process with the
settings put back.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app import config
from app import db as app_db
from app.services import label_printer

_INNER = "tests/test_env_isolation.py::test_a_test_sees_the_defaults"


def test_a_polluted_shell_does_not_reach_the_suite(tmp_path: Path) -> None:
    """Run the assertions below in a child process that has the settings set."""
    env = {
        **os.environ,
        "SHELFOS_LABEL_DEVICE": str(tmp_path / "fake-ql"),
        "SHELFOS_TUNNEL_KEYS": str(tmp_path / "authorized_keys"),
        "SHELFOS_ENV": "production",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'deployment.db'}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", _INNER, "-q", "-p", "no:cacheprovider"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_test_sees_the_defaults() -> None:
    """Every setting a stray environment could carry in is back to its default."""
    assert config.LABEL_DEVICE == ""
    assert config.TUNNEL_KEYS_FILE == ""
    assert config.ENV == "development"
    # Both roads to a printer, not just the configured device: with no device set,
    # a registered tunnel key would make this the loopback bridge instead.
    assert label_printer.configured_device() == ""
    # The engine the API depends on is the throwaway default, not a real database.
    assert str(app_db.engine.url) == app_db.DEFAULT_DATABASE_URL
