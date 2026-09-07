#!/usr/bin/env python3
"""Set an account's password from the command line.

The way out of the one situation the app cannot talk you out of: it refuses to
start in production while an admin still has the default password, and changing
that password through the UI needs the app to be running. This does it against
the database directly, with the app stopped.

Also the answer to an admin who has locked themselves out — there is no
password reset by email, and an admin cannot reset their own through the admin
API by design (that route asks for no current password, so it is not a path
your own account should have).

The new password is read from the terminal without echoing, or from
``SHELFOS_NEW_PASSWORD`` when there is no terminal to prompt at. It is subject
to the same policy the app enforces, so this cannot be used to put an account
back below it.

Usage::

    python scripts/set_password.py admin
    python scripts/set_password.py admin --list     # show accounts and exit
    SHELFOS_NEW_PASSWORD=... python scripts/set_password.py admin   # unattended

Targets the same database as the app: ``DATABASE_URL`` (default the *relative*
``data/shelfos.db``), and says which one it opened before it changes anything —
this is the tool reached for on a box that will not boot, where it is unlikely to
be run from the project directory. It refuses a database that does not exist
rather than creating an empty one and then reporting the account missing, which
on a bad day reads as the accounts having been lost.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import app.models  # noqa: F401  (registers every table on SQLModel.metadata)
from app.db import engine
from app.models.user import User
from app.services import user_service as us
from app.services.errors import ValidationError
from sqlmodel import Session, col, select


def _require_existing_database() -> None:
    """Refuse a database that is not there, and say which one was looked for.

    The app creates its schema on first start; this tool only ever edits an
    account that already exists, so a missing file is a wrong path — a wrong
    working directory, most likely — and not an empty install. Creating one
    would answer "No account named 'admin'", which is the wrong answer to a
    question nobody asked, and would leave a stray database behind for whoever
    next runs from that directory.
    """
    url = engine.url
    print(f"Database: {url}", file=sys.stderr)
    if url.drivername.startswith("sqlite") and url.database not in (None, ":memory:"):
        path = Path(url.database)
        if not path.exists():
            raise SystemExit(
                f"No database at {path.resolve()}. Run this from the ShelfOS "
                "directory, or set DATABASE_URL to the one the app uses."
            )


def _accounts(session: Session) -> list[User]:
    """Every account that can sign in, so the listing is the useful one."""
    return list(
        session.exec(
            select(User)
            .where(col(User.password_hash).is_not(None))
            .order_by(col(User.name))
        ).all()
    )


def _read_new_password() -> str:
    """The new password, from the environment or a terminal that does not echo."""
    from_env = os.environ.get("SHELFOS_NEW_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise SystemExit("No terminal to prompt at; set SHELFOS_NEW_PASSWORD instead.")
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Repeat: "):
        raise SystemExit("The two entries do not match.")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", nargs="?", help="the account to change")
    parser.add_argument(
        "--list", action="store_true", help="list the accounts and exit"
    )
    args = parser.parse_args()

    _require_existing_database()
    with Session(engine) as session:
        if args.list or not args.username:
            for account in _accounts(session):
                state = "active" if account.is_active else "disabled"
                print(f"{account.name}\t{account.role.value}\t{state}")
            if not args.username and not args.list:
                raise SystemExit("\nName an account to change its password.")
            return

        user = us.get_by_username(session, args.username)
        if user is None:
            raise SystemExit(f"No account named {args.username!r}.")
        if user.password_hash is None:
            # Checked by the property, not by a name: the account demo data is
            # attributed to is called "demo" in a new database and "system" in
            # an older one, and having no hash is what actually makes an
            # account unable to sign in.
            raise SystemExit(
                f"{user.name!r} cannot sign in and has no password to set."
            )

        try:
            # actor_id is the account itself: nobody is signed in to attribute
            # this to, and the audit entry should not claim otherwise.
            assert user.id is not None
            us.set_password(session, user.id, _read_new_password(), actor_id=user.id)
        except ValidationError as error:
            raise SystemExit(str(error)) from None

    print(
        f"Password set for {args.username!r}. "
        "Every session and API token it had has stopped working."
    )


if __name__ == "__main__":
    main()
