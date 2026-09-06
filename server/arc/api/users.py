"""User administration (spec §4.10 FR-D1): list accounts, enable, promote.

Admin-only apart from ``PATCH /api/users/me``, which is every account's own
profile: the timezone the schedule's weekdays are grouped in (FR-C3). It is
here rather than under ``/api/auth`` because it writes a ``users`` row, and
because a user and an admin changing the same table from two different routers
is how the two drift apart.

The rest of the router exists to protect one rule: **Arc must never end up with
no active admin**. Two guards enforce it, and both are needed:

* an admin may not deactivate or demote *themselves*. This is the friendly
  one — it catches the obvious mistake and gives a message that explains it.
  Somebody else's admin can always do it for them.
* the last active admin may not be removed by anyone. This is the one that
  survives concurrency: two admins, each demoting the other at the same
  instant, both pass the self-check and each sees the other still in place.
  The change is therefore made and *then* counted, inside a transaction
  serialised by an advisory lock, and rolled back if the count is zero.

Without them the only way back in is a database console.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from arc.api.deps import AdminUser, CurrentUser, SessionDep
from arc.api.schemas import UserAdminOut, UserOut
from arc.models import User, UserRole
from arc.services.auth import count_active_admins, lock_admin_changes

router = APIRouter(prefix="/api/users", tags=["users"])

USER_NOT_FOUND = "user not found"
NO_SELF_DEACTIVATE = "you cannot deactivate your own account"
NO_SELF_DEMOTE = "you cannot change your own role"
LAST_ADMIN = "at least one active admin is required"
UNKNOWN_TIMEZONE = "not an IANA timezone this server knows"

#: The width of ``users.timezone``. Checked here so an over-long name is a 422
#: rather than a database error on the way out.
MAX_TIMEZONE_LENGTH = 64


class UserPatch(BaseModel):
    """Fields an admin may change. Omitted fields are left alone."""

    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    role: UserRole | None = None


class ProfilePatch(BaseModel):
    """What a user may change about their own account: the timezone (FR-C3)."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=MAX_TIMEZONE_LENGTH)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        """Refuse anything :mod:`zoneinfo` cannot resolve.

        The column is free text and
        :func:`arc.services.catalog.schedule.user_timezone` falls back to UTC
        rather than failing, which is right for a row that is already wrong —
        but a *write* is where the mistake can still be reported. Without this,
        ``"GMT+2"`` would be accepted, stored, and silently render every
        weekday in UTC for ever.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(UNKNOWN_TIMEZONE) from exc
        return value


@router.get("", response_model=list[UserAdminOut], summary="List accounts (admin)")
async def index(admin: AdminUser, session: SessionDep) -> list[User]:
    rows = await session.scalars(select(User).order_by(User.id))
    return list(rows.all())


@router.patch(
    "/me",
    response_model=UserOut,
    summary="Change your own timezone (FR-C3)",
    responses={422: {"description": UNKNOWN_TIMEZONE}},
)
async def update_me(body: ProfilePatch, user: CurrentUser, session: SessionDep) -> User:
    """Any signed-in account, and only their own row.

    Declared before ``/{user_id}`` so that ``me`` is a route and not a user id;
    it would be a 422 either way, but the order is what makes that an accident
    rather than the design.
    """
    user.timezone = body.timezone
    await session.commit()
    return user


@router.patch(
    "/{user_id}",
    response_model=UserAdminOut,
    summary="Enable/disable an account or change its role (admin)",
    responses={
        404: {"description": USER_NOT_FOUND},
        409: {"description": f"{NO_SELF_DEACTIVATE} / {NO_SELF_DEMOTE} / {LAST_ADMIN}"},
    },
)
async def update(user_id: int, body: UserPatch, admin: AdminUser, session: SessionDep) -> User:
    removes_an_admin = body.is_active is False or (
        body.role is not None and body.role is not UserRole.ADMIN
    )
    if removes_an_admin:
        # Before the row is read, so that two of these run one after the
        # other rather than side by side. Released when the transaction ends,
        # either way.
        await lock_admin_changes(session)

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)

    if user.id == admin.id:
        if body.is_active is False:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_SELF_DEACTIVATE)
        if body.role is not None and body.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_SELF_DEMOTE)

    if body.is_active is not None:
        # Takes effect at once: `resolve_session` checks `is_active` on every
        # request, so a deactivated user's open tabs are logged out on their
        # next call rather than when their session eventually expires.
        user.is_active = body.is_active
    if body.role is not None:
        user.role = body.role

    if removes_an_admin:
        # Counted *after* the change and before the commit, so the question
        # asked is "what would this leave behind?" rather than "what is there
        # now?" — the second is what the caller's own admin row answers, and
        # it is exactly the one that is wrong under a concurrent demotion.
        await session.flush()
        if await count_active_admins(session) == 0:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_ADMIN)

    await session.commit()
    return user


__all__ = [
    "LAST_ADMIN",
    "MAX_TIMEZONE_LENGTH",
    "NO_SELF_DEACTIVATE",
    "NO_SELF_DEMOTE",
    "UNKNOWN_TIMEZONE",
    "USER_NOT_FOUND",
    "router",
]
