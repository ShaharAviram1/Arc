"""Which device a trip belongs to (FR-A12, owner 2026-10-30).

A trip downloads to the device that started it (``trips.device_id``). Two
pieces live here:

* :func:`canonical_device_id` — the canonical text of a version-4 UUID, or
  :class:`ValueError`. Pure; the API's ``parse_device_id`` turns the error
  into a 422, the operator's command into exit 1.
* :func:`assign_device` — the operator's re-home (``arc.cli trip-device``):
  set or clear the device of an **active** trip of any user. Used when a trip
  must belong to a given device before any client claims it (production's
  trip 6 after the 2026-10-30 deploy), or when a device's storage was reset
  and it came back with a new id, leaving its trips with an id nobody has.

Nothing here touches a want, a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Trip, TripState
from arc.services.trips.create import TRIP_NOT_ACTIVE, TRIP_NOT_FOUND, TripConflict, TripNotFound

log = logging.getLogger(__name__)


def canonical_device_id(raw: str) -> str:
    """The canonical text of a version-4 UUID; :class:`ValueError` for anything else. Pure."""
    parsed = uuid.UUID(raw)
    if parsed.version != 4:
        raise ValueError("not a version 4 UUID")
    return str(parsed)


async def assign_device(session: AsyncSession, *, trip_id: int, device_id: str | None) -> Trip:
    """Set (or, with ``None``, clear) an active trip's device. Flushed, not committed.

    ``device_id`` must already be canonical (:func:`canonical_device_id`).
    :class:`TripNotFound` for an unknown trip, :class:`TripConflict`
    (``trip_not_active``) for one that has ended. Takes the trip ``FOR
    UPDATE``, as every delivery write does, so it cannot cross a confirmation
    claiming the same trip.
    """
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id).with_for_update())
    if trip is None:
        raise TripNotFound(TRIP_NOT_FOUND)
    if trip.state is not TripState.ACTIVE:
        raise TripConflict(TRIP_NOT_ACTIVE)
    previous = trip.device_id
    trip.device_id = device_id
    await session.flush()
    log.info(
        "trip assigned to a device by the operator",
        extra={
            "trip_id": trip.id,
            "user_id": trip.user_id,
            "device_id": device_id,
            "previous_device_id": previous,
        },
    )
    return trip


__all__ = ["assign_device", "canonical_device_id"]
