"""User model (spec §18, decision D11).

Humans authenticate with their own accounts, and every action is recorded
against whoever took it. An account with no password hash cannot sign in at all;
the one the demo data is attributed to is the only such account ShelfOS makes
(see :mod:`app.seed`).
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import UserRole, enum_column


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    role: UserRole = Field(default=UserRole.USER, sa_column=enum_column(UserRole))
    # None means the account cannot sign in at all, and cannot be given a
    # password later either (see user_service.set_password). The demo data's
    # actor is the only account ShelfOS creates this way.
    password_hash: str | None = Field(default=None)
    is_active: bool = Field(default=True)
