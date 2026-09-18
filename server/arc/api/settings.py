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

The two **override** routes (M16, owner 2026-09-18) are the editor behind that
list and behind the show page's own control. They take a declared body rather
than the plain object above, because an override has exactly two fields and
they are not a copy of ``DEFAULT_SETTINGS`` — ``extra="forbid"`` so a misspelt
field is refused instead of silently dropped, and pydantic's own 422 is already
the shape the client renders. A ``PUT`` that names neither field **removes**
the override and answers ``null``: "no rules of its own" is a state the row
cannot represent, so it is the absence of the row (see
:func:`arc.services.settings.write_override`).

There is no ``GET`` for a single override. The show page reads its own from
``GET /api/anime/{id}``, which carries an ``override`` field for admins — one
more ``settings`` lookup on a request that page already makes, against a
second round trip and a second thing to keep in step with the first.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from arc.api.anime import ANIME_NOT_FOUND
from arc.api.deps import AdminUser, AnimeId, SessionDep, get_admin_user
from arc.api.schemas import OverrideOut
from arc.services import settings as rules

# Admin only, at the router: these values decide what Arc downloads and what
# it deletes, and a route added here later must not be able to forget it.
router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_admin_user)]
)


class OverrideIn(BaseModel):
    """The body of ``PUT /api/settings/overrides/{anime_id}`` (FR-A3).

    Both fields are optional, and ``null`` means "take this show back to the
    global rule" — which, when it is said about both at once, is a deletion.
    """

    model_config = ConfigDict(extra="forbid")

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
        overrides=[OverrideOut.build(override) for override in await rules.read_overrides(session)],
    )


def _refused(errors: dict[str, str]) -> HTTPException:
    """One :class:`~arc.services.settings.SettingsInvalid` as FastAPI's 422.

    The same ``detail`` shape the rules editor above answers with, so the
    client has one renderer for a refused value wherever it sent it.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[
            {"loc": ["body", key], "msg": message, "type": "value_error"}
            for key, message in sorted(errors.items())
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
        raise _refused(exc.errors) from exc

    await rules.write_values(session, values, admin_id=admin.id)
    await session.commit()
    return await _current(session)


@router.put(
    "/overrides/{anime_id}",
    response_model=OverrideOut | None,
    summary="Set one show's group/resolution override (admin, FR-A3)",
    responses={
        404: {"description": ANIME_NOT_FOUND},
        422: {"description": "the groups or the resolution were refused"},
    },
)
async def put_override(
    anime_id: AnimeId, body: OverrideIn, session: SessionDep, admin: AdminUser
) -> OverrideOut | None:
    """Answers the override as stored, or ``null`` when it named nothing."""
    try:
        override = await rules.write_override(
            session,
            anime_id=anime_id,
            preferred_groups=body.preferred_groups,
            resolution=body.resolution,
            admin_id=admin.id,
        )
    except rules.SettingsInvalid as exc:
        raise _refused(exc.errors) from exc
    except rules.NoSuchAnime as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANIME_NOT_FOUND) from exc

    await session.commit()
    return None if override is None else OverrideOut.build(override)


@router.delete(
    "/overrides/{anime_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Put one show back on the global rules (admin, FR-A3)",
)
async def remove_override(anime_id: AnimeId, session: SessionDep, admin: AdminUser) -> Response:
    """204 whether or not there was a row: Remove means "not there any more"."""
    await rules.delete_override(session, anime_id=anime_id, admin_id=admin.id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["OverrideIn", "SettingsOut", "router"]
