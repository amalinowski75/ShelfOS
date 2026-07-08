"""User model (spec §18).

In v1.0 there is no real authentication; a single seeded "system user" owns all
recorded actions (decision D2).
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from app.models.enums import UserRole, enum_column


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    role: UserRole = Field(default=UserRole.USER, sa_column=enum_column(UserRole))
