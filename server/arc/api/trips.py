"""Trips: prepare the next X aired episodes of a show for a device (FR-A12).

Three routes, thin over :mod:`arc.services.trips`:

* ``POST /api/anime/{id}/trip`` ``{count}`` → **201** :class:`~arc.api.
  trip_schemas.TripOut`. Refusals carry a short code as ``detail``: **403**
  ``demo_account``; **409** ``trip_active`` (one active trip per user) or
  ``storage_held`` (FR-T6); **422** ``count_out_of_range`` (1..
  ``trip_max_episodes``) or ``nothing_aired`` (no aired episode after the
  caller's progress); **404** for an unknown show.
* ``GET /api/trips/current`` → the caller's active trip, or ``null``.
* ``DELETE /api/trips/{id}`` → **204**. The caller's own trip only; anybody
  else's is a 404, as is an unknown id. A trip that already finished or
  expired is a 409 ``trip_not_active``; cancelling a cancelled one is a 204.

What a device reports about one episode of the caller's trip (M19 T4; an
episode not in the trip, or anybody else's trip, is a 404):

* ``POST /api/trips/{id}/episodes/{eid}/delivered`` ``{etag}`` → **204**: the
  device holds the copy. Idempotent; a stale ETag is accepted and logged.
* ``DELETE /api/trips/{id}/episodes/{eid}/delivered`` → **204**: the device
  deleted its copy, so the user's window stops skipping the episode.
* ``POST /api/trips/{id}/episodes/{eid}/again`` → **200** :class:`~arc.api.
  trip_schemas.TripOut`: a delivered or expired episode is asked for again;
  409 ``trip_not_active`` once the trip has ended.

Nothing here writes to a list entry or to MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from fastapi import Path as PathParam

from arc.api.deps import (
    MAX_ID,
    MIN_ID,
    AnimeId,
    CurrentUser,
    EpisodeId,
    SessionDep,
    SettingsDep,
)
from arc.api.trip_schemas import DeliveredIn, TripIn, TripOut
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import (
    TripConflict,
    TripError,
    TripForbidden,
    TripInvalid,
    TripNotFound,
    active_trip,
    create_trip,
)
from arc.services.trips.deliver import ask_again, confirm_delivered, release_delivered
from arc.services.trips.view import trip_facts

log = logging.getLogger(__name__)

router = APIRouter(tags=["trips"])

TripId = Annotated[int, PathParam(ge=MIN_ID, le=MAX_ID, description="A trip row id.")]


def now() -> datetime:
    """The clock a trip is made against; a function so a test can pin it."""
    return datetime.now(UTC)


def _http(exc: TripError) -> HTTPException:
    code = status.HTTP_409_CONFLICT
    if isinstance(exc, TripForbidden):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, TripInvalid):
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(exc, TripNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif not isinstance(exc, TripConflict):  # pragma: no cover - every subclass is above
        code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail=str(exc))


@router.post(
    "/api/anime/{anime_id}/trip",
    response_model=TripOut,
    status_code=status.HTTP_201_CREATED,
    summary="Prepare the next X aired episodes for a trip (FR-A12)",
    responses={
        403: {"description": "demo_account"},
        404: {"description": "anime not found"},
        409: {"description": "trip_active | storage_held"},
        422: {"description": "count_out_of_range | nothing_aired"},
    },
)
async def start_trip(
    anime_id: AnimeId,
    body: TripIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> TripOut:
    try:
        created = await create_trip(
            session, settings, user=user, anime_id=anime_id, count=body.count, now=now()
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return TripOut.build(await trip_facts(session, settings, created.trip))


@router.get(
    "/api/trips/current",
    response_model=TripOut | None,
    summary="The caller's active trip, or null (FR-A12)",
)
async def current(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> TripOut | None:
    trip = await active_trip(session, user.id)
    if trip is None:
        return None
    return TripOut.build(await trip_facts(session, settings, trip))


@router.delete(
    "/api/trips/{trip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a trip (FR-A12)",
    responses={404: {"description": "trip not found"}, 409: {"description": "trip_not_active"}},
)
async def cancel(trip_id: TripId, user: CurrentUser, session: SessionDep) -> Response:
    try:
        await cancel_trip(session, user=user, trip_id=trip_id, now=now())
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/trips/{trip_id}/episodes/{episode_id}/delivered",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="A device holds this trip episode's copy (FR-A12)",
    responses={404: {"description": "trip or episode not found"}},
)
async def delivered(
    trip_id: TripId,
    episode_id: EpisodeId,
    body: DeliveredIn,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    try:
        await confirm_delivered(
            session,
            user=user,
            trip_id=trip_id,
            episode_id=episode_id,
            etag=body.etag,
            now=now(),
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api/trips/{trip_id}/episodes/{episode_id}/delivered",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="A device deleted its copy of this trip episode (FR-A12)",
    responses={404: {"description": "trip or episode not found"}},
)
async def released(
    trip_id: TripId, episode_id: EpisodeId, user: CurrentUser, session: SessionDep
) -> Response:
    try:
        await release_delivered(
            session, user=user, trip_id=trip_id, episode_id=episode_id, now=now()
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/trips/{trip_id}/episodes/{episode_id}/again",
    response_model=TripOut,
    summary="Ask for a delivered or expired trip episode again (FR-A12)",
    responses={
        404: {"description": "trip or episode not found"},
        409: {"description": "trip_not_active"},
    },
)
async def again(
    trip_id: TripId,
    episode_id: EpisodeId,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> TripOut:
    try:
        trip = await ask_again(
            session, settings, user=user, trip_id=trip_id, episode_id=episode_id, now=now()
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return TripOut.build(await trip_facts(session, settings, trip))


__all__ = ["router"]
