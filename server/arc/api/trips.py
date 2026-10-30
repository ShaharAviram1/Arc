"""Trips: prepare the next X aired episodes of a show for a device (FR-A12).

Three routes, thin over :mod:`arc.services.trips`:

* ``POST /api/anime/{id}/trip`` ``{count, device_id}`` → **201** :class:`~arc.api.
  trip_schemas.TripOut`. Refusals carry a short code as ``detail``: **403**
  ``demo_account``; **409** ``trip_active`` (this show already has the
  caller's active trip: one per show, any number of shows) or
  ``storage_held`` (FR-T6); **422** ``count_out_of_range`` (1..
  ``trip_max_episodes``) or ``nothing_aired`` (no aired episode after the
  caller's progress) or a ``device_id`` that is not a v4 UUID; **404** for an
  unknown show. The trip is the asking device's to download (owner,
  2026-10-30).
* ``GET /api/trips`` → the caller's active trips, oldest first (``[]`` for
  none).
* ``DELETE /api/trips/{id}`` → **204**. The caller's own trip only; anybody
  else's is a 404, as is an unknown id. A trip that already finished or
  expired is a 409 ``trip_not_active``; cancelling a cancelled one is a 204.

What a device reports about one episode of the caller's trip (M19 T4; an
episode not in the trip, or anybody else's trip, is a 404):

* ``POST /api/trips/{id}/episodes/{eid}/delivered`` ``{etag, device_id,
  sha256, size}`` → **204**: the device holds the copy. Idempotent; a stale
  ETag is accepted and logged; a ``sha256`` or ``size`` that is not the ready
  copy's is **409** ``copy_mismatch`` and changes nothing (owner, 2026-10-30).
* ``DELETE /api/trips/{id}/episodes/{eid}/delivered`` → **204**: the device
  deleted its copy, so the user's window stops skipping the episode.
* ``POST /api/trips/{id}/episodes/{eid}/again`` → **200** :class:`~arc.api.
  trip_schemas.TripOut`: a delivered or expired episode is asked for again;
  409 ``trip_not_active`` once the trip has ended.

Each of the three names the calling device: ``device_id`` in the POST bodies,
the ``device_id`` query parameter on the DELETE (422 when it is not a v4
UUID). A trip that records another device answers **409**
``trip_other_device``; a trip that records none is claimed by the first
device to confirm or re-ask (:mod:`arc.services.trips.deliver`).

Nothing here writes to a list entry or to MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
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
from arc.api.devices import parse_device_id
from arc.api.trip_schemas import AgainIn, DeliveredIn, TripIn, TripOut
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import (
    COPY_MISMATCH,
    TRIP_OTHER_DEVICE,
    TripConflict,
    TripError,
    TripForbidden,
    TripInvalid,
    TripNotFound,
    active_trips,
    create_trip,
)
from arc.services.trips.deliver import ask_again, confirm_delivered, release_delivered
from arc.services.trips.view import trip_facts

log = logging.getLogger(__name__)

router = APIRouter(tags=["trips"])

TripId = Annotated[int, PathParam(ge=MIN_ID, le=MAX_ID, description="A trip row id.")]

#: The calling device on the DELETE, which has no body.
DeviceQuery = Annotated[
    str | None,
    Query(min_length=32, max_length=45, description="The calling device's own UUID (v4)."),
]


def _device(raw: str | None) -> str | None:
    """The canonical device id, ``None`` when the client named none, or a 422."""
    return None if raw is None else parse_device_id(raw)


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
        422: {"description": "count_out_of_range | nothing_aired | bad device_id"},
    },
)
async def start_trip(
    anime_id: AnimeId,
    body: TripIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> TripOut:
    device_id = parse_device_id(body.device_id)
    try:
        created = await create_trip(
            session,
            settings,
            user=user,
            anime_id=anime_id,
            count=body.count,
            now=now(),
            device_id=device_id,
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return TripOut.build(await trip_facts(session, settings, created.trip))


@router.get(
    "/api/trips",
    response_model=list[TripOut],
    summary="The caller's active trips, oldest first (FR-A12)",
)
async def active(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> list[TripOut]:
    return [
        TripOut.build(await trip_facts(session, settings, trip))
        for trip in await active_trips(session, user.id)
    ]


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
    responses={
        404: {"description": "trip or episode not found"},
        409: {"description": f"{TRIP_OTHER_DEVICE} | {COPY_MISMATCH}"},
    },
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
            sha256=body.sha256,
            size=body.size,
            now=now(),
            device_id=_device(body.device_id),
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api/trips/{trip_id}/episodes/{episode_id}/delivered",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="A device deleted its copy of this trip episode (FR-A12)",
    responses={
        404: {"description": "trip or episode not found"},
        409: {"description": TRIP_OTHER_DEVICE},
    },
)
async def released(
    trip_id: TripId,
    episode_id: EpisodeId,
    user: CurrentUser,
    session: SessionDep,
    device_id: DeviceQuery = None,
) -> Response:
    try:
        await release_delivered(
            session,
            user=user,
            trip_id=trip_id,
            episode_id=episode_id,
            now=now(),
            device_id=_device(device_id),
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
        409: {"description": f"trip_not_active | {TRIP_OTHER_DEVICE}"},
    },
)
async def again(
    trip_id: TripId,
    episode_id: EpisodeId,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    body: AgainIn | None = None,
) -> TripOut:
    try:
        trip = await ask_again(
            session,
            settings,
            user=user,
            trip_id=trip_id,
            episode_id=episode_id,
            now=now(),
            device_id=_device(body.device_id if body is not None else None),
        )
    except TripError as exc:
        raise _http(exc) from exc
    await session.commit()
    return TripOut.build(await trip_facts(session, settings, trip))


__all__ = ["router"]
