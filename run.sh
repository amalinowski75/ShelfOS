#!/bin/sh
#
# Start ShelfOS, setting the project up first if it needs it.
#
# Clone the repo, run ./run.sh, and the app is on http://127.0.0.1:9000 — the
# first run builds the virtualenv and installs the dependencies, later runs go
# straight to serving unless the dependencies have changed since.
#
# Settings come from the environment or from the .env file below, so either
# PORT=8080 ./run.sh or a PORT line in that file works, and likewise for PYTHON
# (which interpreter builds the venv) and SHELFOS_ENV_FILE (where to read).

set -e

# Work from the project, not from wherever the script was called.
cd "$(dirname "$0")"

# Local settings first, so everything below can be configured from the file too:
# shop API keys, the label printer, the session secret. Optional — without it the
# app runs on its development defaults (admin/admin, shop imports disabled), which
# is what a fresh clone should do rather than refuse to start.
ENV_FILE=${SHELFOS_ENV_FILE:-$HOME/.ShelfOS/.env}
if [ -f "$ENV_FILE" ]; then
    # `set -a` exports every assignment the file makes, so a line written as a
    # plain FOO=bar reaches the app too — without it only `export FOO=bar` would,
    # and the difference is invisible until a setting quietly does nothing.
    #
    # `set +e` because this is the user's file, not ours: a command in it that
    # happens to return non-zero should not kill the launcher before it has said
    # anything, which is what the outer `set -e` would otherwise do.
    set +e
    set -a
    . "$ENV_FILE"
    set +a
    set -e
else
    echo "No $ENV_FILE — running with development defaults (admin/admin, no shop keys)."
fi

VENV=.venv
PYTHON=${PYTHON:-python3}
# Written after a successful install, and compared against pyproject.toml: a pull
# that adds a dependency would otherwise leave the venv a version behind and fail
# at import, which reads as a broken app rather than a stale one.
STAMP=$VENV/.shelfos-installed

if [ ! -x "$VENV/bin/uvicorn" ] || [ pyproject.toml -nt "$STAMP" ]; then
    echo "Setting up $VENV (this takes a minute)…"
    # No "does it already exist" guard: `venv` is idempotent, and the state that
    # needs it most is a HALF-BUILT .venv/ — the module makes the directories
    # first and ensurepip is the slow part, so an interrupted first run leaves
    # exactly that. Skipping the rebuild there fails on the missing pip instead.
    if ! "$PYTHON" -m venv "$VENV"; then
        # Debian and Ubuntu ship venv separately, and its own error message
        # ("ensurepip is not available") does not say what to install.
        echo "Could not create $VENV. On Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    fi
    "$VENV/bin/pip" install --quiet --upgrade pip
    # Editable, with the dev extra, so the same venv runs the app and the tests.
    "$VENV/bin/pip" install --editable ".[dev]"
    touch "$STAMP"
    echo "Done."
fi

# exec, so Ctrl-C reaches uvicorn directly and no shell lingers around it.
exec "$VENV/bin/uvicorn" app.main:app --reload --port "${PORT:-9000}"
