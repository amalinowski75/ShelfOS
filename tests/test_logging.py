"""What the application says out loud, under the logging uvicorn sets up.

Most of these run in a subprocess on purpose. pytest installs a root handler of
its own, which is precisely why the bug they cover survived: in the test suite
every ``_logger.info`` appeared exactly as intended, and in production none of
them existed. Only a process configured the way `uvicorn` configures one can
tell the difference.

They also drive the real modules' own ``_logger`` objects rather than naming a
logger. A first version of this file named one — and named it wrongly, so all
of it passed against a fabricated logger while the production line stayed
silent. Nothing here may depend on a name being retyped correctly.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_UVICORN_THEN = """
import logging, logging.config
from uvicorn.config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)
{body}
"""


def _under_uvicorn_logging(body: str, **env: str) -> str:
    """Run `body` in a process whose logging is uvicorn's, and return its stderr."""
    result = subprocess.run(
        [sys.executable, "-c", _UVICORN_THEN.format(body=textwrap.dedent(body))],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        # From the repo root, so the import works whether or not the package is
        # installed into the interpreter running the suite.
        cwd=str(_REPO_ROOT),
        env={"PATH": "/usr/bin:/bin", **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def test_every_module_logs_to_the_logger_that_is_configured() -> None:
    """The assumption `_configure_logging` rests on, checked rather than stated.

    It sets the level of one logger by name. A module that logs to a different
    one is a module whose INFO lines go nowhere — and that is invisible at the
    call site, in the configuration, and in any test that names a logger itself.
    """
    from app.main import _APP_LOGGER

    named = {
        match
        for path in (_REPO_ROOT / "app").rglob("*.py")
        for match in re.findall(r'getLogger\("([^"]+)"\)', path.read_text())
    }
    assert named == {_APP_LOGGER}, f"configured {_APP_LOGGER!r}, modules use {named!r}"


def test_a_successful_sign_in_reaches_the_log() -> None:
    """The line an operator needs to answer "who was signed in, and when".

    Failed sign-ins are WARNING and always came through; successful ones are
    INFO and did not, which is backwards — a journal that records only the
    attempts that failed cannot settle a question about a session that worked.
    """
    stderr = _under_uvicorn_logging("""
        from app.main import create_app
        from app.auth import throttle
        create_app(create_tables=False)
        throttle._logger.info("Login for '%s' from %s", "kowalski", "10.0.0.5")
    """)
    assert "Login for 'kowalski' from 10.0.0.5" in stderr


def test_without_the_app_uvicorn_alone_swallows_it() -> None:
    """The other half of the pair, so the test above cannot pass by accident.

    uvicorn configures its own three loggers and leaves the root one with only
    logging's last-resort handler, which starts at WARNING.
    """
    stderr = _under_uvicorn_logging("""
        from app.auth import throttle
        throttle._logger.info("Login for '%s' from %s", "kowalski", "10.0.0.5")
        throttle._logger.warning("Failed login for '%s'", "kowalski")
    """)
    assert "Login for 'kowalski' from 10.0.0.5" not in stderr
    assert "Failed login" in stderr


def test_the_level_can_be_turned_down() -> None:
    """An operator who finds sign-ins noisy has somewhere to say so, and the
    warnings that stop a bad start still come through."""
    stderr = _under_uvicorn_logging(
        """
        from app.main import create_app
        from app.auth import throttle
        create_app(create_tables=False)
        throttle._logger.info("Login for '%s' from %s", "kowalski", "10.0.0.5")
        throttle._logger.warning("Failed login for '%s'", "kowalski")
        """,
        SHELFOS_LOG_LEVEL="WARNING",
    )
    assert "Login for 'kowalski' from 10.0.0.5" not in stderr
    assert "Failed login" in stderr


def test_a_level_that_is_not_one_says_so_rather_than_going_quiet() -> None:
    """A typo in the setting must not be the way logging turns itself off."""
    stderr = _under_uvicorn_logging(
        """
        from app.main import create_app
        from app.auth import throttle
        create_app(create_tables=False)
        throttle._logger.info("Login for '%s' from %s", "kowalski", "10.0.0.5")
        """,
        SHELFOS_LOG_LEVEL="verbose",
    )
    assert "not a logging level" in stderr
    assert "Login for 'kowalski' from 10.0.0.5" in stderr


def test_notset_is_not_a_way_to_switch_logging_off() -> None:
    """It is in `getLevelNamesMapping()`, and `setLevel(0)` means "inherit" —
    which lands on the root logger `basicConfig` leaves at WARNING. Silently
    doing the one thing the fallback above exists to prevent."""
    stderr = _under_uvicorn_logging(
        """
        from app.main import create_app
        from app.auth import throttle
        create_app(create_tables=False)
        throttle._logger.info("Login for '%s' from %s", "kowalski", "10.0.0.5")
        """,
        SHELFOS_LOG_LEVEL="NOTSET",
    )
    assert "not a logging level" in stderr
    assert "Login for 'kowalski' from 10.0.0.5" in stderr
