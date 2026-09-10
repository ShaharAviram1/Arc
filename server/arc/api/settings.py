"""The admin rules editor: read and write ``settings`` (FR-D2, FR-T5).

Two routes over :mod:`arc.services.settings`, and the router is as thin as
CLAUDE.md asks: it turns one exception into a 422 and commits.

The body of the ``PUT`` is a **plain object**, not a pydantic model with a
field per key, for one reason: the keys are
:data:`arc.models.DEFAULT_SETTINGS`, and a second declaration of them here
would be a list to keep in step with that one for ever. The cost is that the
422 is raised by hand rather than generated; it is raised in FastAPI's own
shape (``detail`` = a list of ``{loc, msg, type}``) so the client renders one
kind of validation error, not two.

``GET`` answers ``values``, ``defaults`` and ``overrides`` together on
purpose. The panel needs the defaults to offer a "reset" that does not hardcode
7 and 21 in TypeScript, and it needs the overrides because "why is this show
downloading 720p?" is a question about the rules page.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from arc.api.deps import AdminUser, SessionDep, get_admin_user
from arc.services import settings as rules

# Admin only, at the router: these values decide what Arc downloads and what
# it deletes, and a route added here later must not be able to forget it.
router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_admin_user)]
)


class OverrideOut(BaseModel):
    """One per-show rule override (``override:anime:<id>``), read-only.

    Editing them is M16; listing them is what makes the global rules page
    honest about the shows that do not follow it.
    """

    anime_id: int
    title: str
    preferred_groups: list[str] | None = None
    resolution: str | None = None


class SettingsOut(BaseModel):
    """``GET`` and ``PUT`` both answer this."""

    #: Every editable key with the value in force.
    values: dict[str, Any]
    #: The same keys with their first-boot values, so the client can offer
    #: "reset to default" without a copy of them.
    defaults: dict[str, Any]
    overrides: list[OverrideOut]


async def _current(session: SessionDep) -> SettingsOut:
    return SettingsOut(
        values=await rules.read_values(session),
        defaults=rules.defaults(),
        overrides=[
            OverrideOut(
                anime_id=override.anime_id,
                title=override.title,
                preferred_groups=override.preferred_groups,
                resolution=override.resolution,
            )
            for override in await rules.read_overrides(session)
        ],
    )


@router.get("", response_model=SettingsOut, summary="The admin-editable rules (admin)")
async def read(session: SessionDep) -> SettingsOut:
    return await _current(session)


@router.put(
    "",
    response_model=SettingsOut,
    summary="Change some of the rules (admin, FR-D2)",
    responses={422: {"description": "one or more values were refused"}},
)
async def update(
    body: Annotated[dict[str, Any], Body()], session: SessionDep, admin: AdminUser
) -> SettingsOut:
    """Write only the keys the body names; everything else is left alone.

    Validated against the settings *as they stand*, so the one cross-field
    rule (the two resolutions must differ) is applied to what the table will
    hold rather than to the patch on its own.
    """
    try:
        values = rules.validate(body, await rules.read_values(session))
    except rules.SettingsInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {"loc": ["body", key], "msg": message, "type": "value_error"}
                for key, message in sorted(exc.errors.items())
            ],
        ) from exc

    await rules.write_values(session, values, admin_id=admin.id)
    await session.commit()
    return await _current(session)


__all__ = ["OverrideOut", "SettingsOut", "router"]
