"""What a trip does when a copy becomes ready (FR-A12, owner decision 4).

Called from :func:`arc.services.media.offline.on_copy_ready`, the single place
that reacts to a copy being made, after the ready state is committed. Two
writes and no I/O, so a failure here can never undo the copy:

* every ``pending`` row of an active trip on this episode that has no
  ``available_at`` gets one — the start of the ``trip_copy_days`` clock M19
  T4's sweep measures;
* for an episode that is **not** ``ready``, a :data:`~arc.services.trips.
  names.TRIP_RELEASE` job is queued. A ``ready`` episode's copy is an
  ordinary copy and its source is retention's; any other episode's copy is a
  trip's, and the job deletes the source once it is sure the episode is still
  trip-only (:mod:`arc.services.trips.release` re-checks under a lock — a
  normal want may have arrived in the meantime, and then the source stays for
  the promotion to transcode).

Decided by the episode's state rather than by the job's ``why``: the encode is
deduplicated per episode, so a trip's request can be served by a job queued
for another reason, and the state is the fact that matters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Episode, EpisodeState, Trip, TripEpisode, TripEpisodeState, TripState
from arc.services.trips.names import enqueue_trip_release

log = logging.getLogger(__name__)


async def stamp_available(
    session: AsyncSession, episode_id: int, *, now: datetime | None = None
) -> int:
    """Set ``available_at`` on the pending trip rows of one episode that lack it."""
    active = select(Trip.id).where(Trip.state == TripState.ACTIVE)
    result = await session.execute(
        update(TripEpisode)
        .where(
            TripEpisode.episode_id == episode_id,
            TripEpisode.state == TripEpisodeState.PENDING,
            TripEpisode.available_at.is_(None),
            TripEpisode.trip_id.in_(active),
        )
        .values(available_at=now or datetime.now(UTC))
        .execution_options(synchronize_session="fetch")
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def trip_copy_ready(session: AsyncSession, episode: Episode) -> None:
    """React to ``episode``'s copy becoming ready. Flushed; the caller commits."""
    stamped = await stamp_available(session, episode.id)
    queued = False
    if episode.state is not EpisodeState.READY:
        await enqueue_trip_release(session, episode.id)
        queued = True
    await session.flush()
    if stamped or queued:
        log.info(
            "trip copy ready",
            extra={"episode_id": episode.id, "rows_stamped": stamped, "release_queued": queued},
        )


__all__ = ["stamp_available", "trip_copy_ready"]
