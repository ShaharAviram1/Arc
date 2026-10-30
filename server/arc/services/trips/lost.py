"""A device that lost a delivered trip copy gets it again (FR-A12, owner 2026-10-30).

Episode 15 of trip 4 was confirmed by the iPad and later lost there; the
server had already deleted its copy, so the device had no way back but a
re-encode. Since then the server keeps a trip's copy until the episode is
watched or the trip ends (:mod:`arc.services.trips.settle`), and the device's
holdings report (``PUT /api/devices/{id}/holdings``, FR-A15) carries what it
actually holds. This module reads that report against the trip rows:

a ``delivered``, unreleased row of an **active** trip that records **this**
device, whose episode the report leaves out, is put back to ``pending``
(:func:`~arc.services.trips.deliver.repend`), so the device fetches the copy
again — ``available_at`` now when the copy is still on disk, else null and the
trip's want fetches and copies the episode again by the ordinary paths
(the reconciliation queued here). Left alone:

* a trip that records no device (made before 2026-10-30) or another device:
  only the trip's own device can say it lost the trip's copy;
* a released row (the device said it deleted it) and an ended trip's rows;
* an episode the trip's user has watched — the device drops a watched copy
  from its report, and that is not a loss;
* a row confirmed less than :data:`REPEND_GRACE` ago: a report the device
  composed just before the confirmation may arrive just after it;
* every row, when the report is at the cap
  (:data:`~arc.services.acquisition.holdings.MAX_HOLDINGS`): the device
  dropped ids to fit, so an absence there proves nothing.

Locks in the order every trip writer takes them: the trips, then the rows,
then the episode. Nothing here touches a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.media.copies import file_matches
from arc.services.media.names import offline_path_for
from arc.services.playback.progress import completed_episode_ids
from arc.services.trips.deliver import repend

log = logging.getLogger(__name__)

#: How long after a confirmation a report that leaves the episode out is not
#: believed (a small call, 2026-10-30): the device's report and its
#: confirmation are sent independently, and one composed a moment before the
#: download finished may land a moment after it was confirmed.
REPEND_GRACE: Final[timedelta] = timedelta(minutes=5)


async def repend_lost_copies(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    device_id: str,
    held: Collection[int],
    now: datetime,
    truncated: bool = False,
) -> list[tuple[int, int]]:
    """Re-pend this device's delivered rows its report leaves out. Flushed, not committed.

    Returns the ``(trip_id, episode_id)`` pairs put back.
    """
    if truncated:
        return []
    trips = (
        await session.scalars(
            select(Trip)
            .where(
                Trip.user_id == user_id,
                Trip.state == TripState.ACTIVE,
                Trip.device_id == device_id,
            )
            .order_by(Trip.id)
            .with_for_update()
        )
    ).all()
    if not trips:
        return []
    holding = set(held)
    rows = (
        await session.scalars(
            select(TripEpisode)
            .where(
                TripEpisode.trip_id.in_([trip.id for trip in trips]),
                TripEpisode.state == TripEpisodeState.DELIVERED,
                TripEpisode.released_at.is_(None),
                TripEpisode.delivered_at.is_not(None),
                TripEpisode.delivered_at <= now - REPEND_GRACE,
            )
            .order_by(TripEpisode.trip_id, TripEpisode.episode_id)
            .with_for_update()
        )
    ).all()
    missing = [row for row in rows if row.episode_id not in holding]
    if not missing:
        return []
    watched = await completed_episode_ids(
        session, user_id=user_id, episode_ids=sorted({row.episode_id for row in missing})
    )
    lost = [row for row in missing if row.episode_id not in watched]
    if not lost:
        return []
    # The episodes last, in one statement and in id order, as every trip
    # writer takes them.
    await session.execute(
        select(Episode.id)
        .where(Episode.id.in_({row.episode_id for row in lost}))
        .order_by(Episode.id)
        .with_for_update()
    )
    repended: list[tuple[int, int]] = []
    for row in lost:
        copy = await session.get(OfflineCopy, row.episode_id)
        on_disk = (
            copy is not None
            and copy.state is OfflineCopyState.READY
            and file_matches(offline_path_for(settings, row.episode_id), copy)
        )
        repend(row, available_at=now if on_disk else None)
        repended.append((row.trip_id, row.episode_id))
        log.warning(
            "device lost a delivered copy; re-pended",
            extra={
                "trip_id": row.trip_id,
                "episode_id": row.episode_id,
                "user_id": user_id,
                "device_id": device_id,
                "copy_on_disk": on_disk,
            },
        )
    if repended:
        # The trip want comes back for a copy that is gone (fetched and copied
        # again), and the device skip lets the episode go.
        await enqueue_compute_wants(session)
    await session.flush()
    return repended


__all__ = ["REPEND_GRACE", "repend_lost_copies"]
