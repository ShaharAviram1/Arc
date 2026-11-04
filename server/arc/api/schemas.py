"""Response models shared by more than one router.

``UserOut`` is the shape the client's auth context is built on, so it is
defined once here rather than in whichever router happened to need it first.
It has no ``password_hash`` field, and cannot grow one by accident: Pydantic
serialises the fields it declares, not the attributes of the object it was
given.

``OverrideOut`` is here for the same reason since M16: a per-show rule
override is carried by the rules editor (``/api/settings``) *and* by the show
page (``GET /api/anime/{id}``, admins only), and one shape means the client
has one type and one renderer for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from arc.models import UserRole
from arc.services.settings import Override


class OverrideOut(BaseModel):
    """One per-show rule override (``override:anime:<id>``, FR-A3, FR-D2).

    Edited by an admin on the show page and in Admin → Rules (M16); the
    ranker reads the row it stands for in
    :func:`~arc.services.acquisition.rules.load_rules`. ``title`` is carried
    even where the reader already knows it — the admin table is a list of
    shows — and is ``""`` for an override whose show is gone.
    """

    anime_id: int
    title: str
    preferred_groups: list[str] | None = None
    resolution: str | None = None

    @classmethod
    def build(cls, override: Override) -> OverrideOut:
        return cls(
            anime_id=override.anime_id,
            title=override.title,
            preferred_groups=override.preferred_groups,
            resolution=override.resolution,
        )


class UserOut(BaseModel):
    """A user as the API renders them to themselves and to an admin."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    timezone: str
    created_at: datetime
    #: Deprecated, removed next release: always ``False``. The demo account
    #: and the ``users.is_demo`` column are gone (M21, owner 2026-11-03); the
    #: field stays one release so a cached client that still requires it on
    #: ``/api/auth/me`` keeps working.
    is_demo: Literal[False] = False


class UserAdminOut(UserOut):
    """The admin view: everything above plus whether the account is enabled."""

    is_active: bool


__all__ = ["OverrideOut", "UserAdminOut", "UserOut"]
