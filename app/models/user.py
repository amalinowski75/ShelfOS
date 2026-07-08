"""User model (spec §18, decision D11).

Humans authenticate with their own accounts; the seeded "system" user owns
automated actions and cannot log in (no password hash).
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import UserRole, enum_column


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    role: UserRole = Field(default=UserRole.USER, sa_column=enum_column(UserRole))
    # None means the account cannot log in (e.g. the system user).
    password_hash: str | None = Field(default=None)
    is_active: bool = Field(default=True)
