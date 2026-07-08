"""Database seeding helpers.

In v1.0 there is no authentication, so a single "system user" owns every
recorded action (decision D2). :func:`ensure_system_user` is idempotent and safe
to call on every startup.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.enums import UserRole
from app.models.user import User

SYSTEM_USER_NAME = "system"


def ensure_system_user(session: Session) -> User:
    """Return the system user, creating it on first call."""
    user = session.exec(select(User).where(User.name == SYSTEM_USER_NAME)).first()
    if user is None:
        user = User(name=SYSTEM_USER_NAME, role=UserRole.ADMIN)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user
