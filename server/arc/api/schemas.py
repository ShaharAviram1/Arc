"""Response models shared by more than one router.

``UserOut`` is the shape the client's auth context is built on, so it is
defined once here rather than in whichever router happened to need it first.
It has no ``password_hash`` field, and cannot grow one by accident: Pydantic
serialises the fields it declares, not the attributes of the object it was
given.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from arc.models import UserRole


class UserOut(BaseModel):
    """A user as the API renders them to themselves and to an admin."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    timezone: str
    created_at: datetime
    #: The demo account (M16, owner 2026-09-18). On ``UserOut`` rather than on
    #: the admin view below because the account that needs to know is the one
    #: reading its own row: the client shows it a "How Arc works" entry and a
    #: strip on Watch Now, and everybody else a false it never renders.
    is_demo: bool


class UserAdminOut(UserOut):
    """The admin view: everything above plus whether the account is enabled."""

    is_active: bool


__all__ = ["UserAdminOut", "UserOut"]
