"""The account that owns demo data.

Stock movements and audit entries carry a ``user_id`` that is a foreign key to
``users.id``, so the demo data has to be attributed to somebody. Nobody is
signed in while it is being generated — ``shelfos.sh devel`` seeds an empty
database before it starts the app, and ``scripts/seed_demo.py`` can be run
against a database that has never had an admin — so demo data brings its own
actor.

This used to be seeded on every startup, back when decision D2 said there was no
authentication and one "system" account owned every recorded action. D11 gave
people real accounts and every action has been attributed to the person who took
it since; the account stayed in the startup path anyway, so a fresh production
install got a second admin-role row that nothing would ever use and that showed
up in the users table looking like an account somebody forgot about. It is
created here now, by the only thing that needs it.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.enums import UserRole
from app.models.user import User
from app.services.errors import ValidationError

DEMO_USER_NAME = "demo"

# What this account was called when it was seeded at startup. A database from
# before that change still has it, with audit and stock rows pointing at its id.
_LEGACY_NAME = "system"


def ensure_demo_user(session: Session) -> User:
    """Return the account demo data is attributed to, creating it if needed.

    An older database's ``system`` account is adopted rather than renamed. The
    name is not merely a label here: it is what the audit log shows against
    every action that account took, and "system did this" was true when it was
    written. Renaming it now would make years-old entries claim something that
    was never on screen. New databases get ``demo``, which is what it is.

    Adoption goes by the property, not by the name: only an account that cannot
    sign in is taken. Nothing reserves either name, so an admin can create a
    person's account called ``demo`` through the users page — and matching on
    the name alone would then attribute every demo stock movement and audit
    entry to that person, putting their name in §19 against hundreds of actions
    they never took. When the name is held by a real account and there is no
    seeded actor to adopt, this refuses rather than guessing.
    """
    for name in (DEMO_USER_NAME, _LEGACY_NAME):
        user = session.exec(select(User).where(User.name == name)).first()
        if user is not None and user.password_hash is None:
            return user
    holder = session.exec(select(User).where(User.name == DEMO_USER_NAME)).first()
    if holder is not None:
        raise ValidationError(
            f"cannot attribute demo data: an account named {DEMO_USER_NAME!r} "
            "already exists and can sign in. Rename it first, so the demo data "
            "is not recorded against a real person."
        )
    user = User(name=DEMO_USER_NAME, role=UserRole.ADMIN)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
