"""Calling a trip off (FR-A12, owner 2026-10-05).

Everything a trip started is undone **now**, not at the next tick, and by the
paths that already exist for each kind of thing:

* the trip goes ``cancelled`` and every row not yet delivered with it (a
  delivered episode is on the device and stays the device's);
* the user's wants are **not** written here: the ``compute_wants`` queued at
  the end ends the rows the trip brought in by the reconciler's own rules
  (deleted on a followed, admitted show, shelved with ``trip ended``
  elsewhere) and leaves every other row exactly as it was — a held show's
  window row in particular (FR-A10). The episode decisions below simply do
  not count this user's trip rows as wanting anything;
* an episode nothing else wants and nothing has fetched yet goes back to
  ``not_wanted`` (:func:`~arc.services.acquisition.wants.release_if_unwanted`);
  one downloading is cancelled
  (:func:`~arc.services.acquisition.wants.cancel_if_unwanted`) — a single
  removed from the client with its partial files by ``qbit_cancel``, a pack's
  file given back through its claim and ``qbit_reselect`` applying FR-A11's
  disposition;
* landed bytes and copies of a trip-only episode are deleted without FR-T1's
  grace, by a :data:`~arc.services.trips.names.TRIP_RELEASE` job queued at
  once (the deletion can talk to qBittorrent, which a request must not wait
  on), and a copy queued but not started is not made at all;
* a ``ready`` episode is left to ordinary retention.

An episode another user still wants — for streaming or for a trip of their
own — is untouched by any of this (FR-A2). Nothing here touches a list entry
or MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.acquisition.wants import (
    cancel_if_unwanted,
    enqueue_cancel,
    release_if_unwanted,
)
from arc.services.media.names import EPISODE_KEY, OFFLINE_ENCODE
from arc.services.trips.create import TRIP_NOT_ACTIVE, TRIP_NOT_FOUND, TripConflict, TripNotFound
from arc.services.trips.names import enqueue_trip_release
from arc.services.trips.rules import pending_episode_ids

log = logging.getLogger(__name__)

#: States whose landed bytes a cancelled trip has the release job delete.
_LANDED: frozenset[EpisodeState] = frozenset(
    {
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.MATCHED,
        EpisodeState.FAILED,
        EpisodeState.NOT_WANTED,
    }
)


async def _still_wanted(session: AsyncSession, user_id: int, episode_ids: list[int]) -> set[int]:
    """Episodes with a live want **other than** this user's trip rows.

    The trip's own rows are still live at this point — the reconciliation the
    cancel queues is what removes them — so they are left out by hand.
    """
    rows = await session.scalars(
        select(Want.episode_id)
        .where(
            Want.episode_id.in_(episode_ids),
            Want.dropped_at.is_(None),
            ~((Want.user_id == user_id) & Want.trip.is_(True)),
        )
        .distinct()
    )
    return set(rows.all())


async def drop_queued_copies(
    session: AsyncSession,
    episode_ids: list[int],
    *,
    reason: str = "the trip that wanted it was cancelled",
) -> int:
    """Cancel the not-yet-started trip copies of these episodes.

    Only ``pending`` jobs: a running encode is not stopped mid-flight (the
    queue's own rule), and the release job deletes its result afterwards.
    """
    if not episode_ids:
        return 0
    key = cast(ColumnElement[str], Job.payload[EPISODE_KEY].astext)
    result = await session.execute(
        update(Job)
        .where(
            Job.type == OFFLINE_ENCODE,
            Job.status == JobStatus.PENDING,
            key.in_({str(episode_id) for episode_id in episode_ids}),
        )
        .values(status=JobStatus.CANCELLED, last_error=reason)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def cancel_trip(session: AsyncSession, *, user: User, trip_id: int, now: datetime) -> Trip:
    """Cancel the caller's trip. Flushed, not committed.

    :class:`TripNotFound` for a trip that is not the caller's (another user's
    trip is not acknowledged to exist). Cancelling a cancelled trip is a no-op;
    a trip that finished or expired is a :class:`TripConflict`.
    """
    # ``FOR UPDATE``: a reconciliation reads active trips ``FOR SHARE``
    # (:func:`~arc.services.acquisition.wants._trip_wants`), so the two cannot
    # interleave — it either finishes with the trip still active, or reads it
    # cancelled.
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id).with_for_update())
    if trip is None or trip.user_id != user.id:
        raise TripNotFound(TRIP_NOT_FOUND)
    if trip.state is TripState.CANCELLED:
        return trip
    if trip.state is not TripState.ACTIVE:
        raise TripConflict(TRIP_NOT_ACTIVE)

    trip.state = TripState.CANCELLED
    trip.ended_at = now
    rows = list(
        (await session.scalars(select(TripEpisode).where(TripEpisode.trip_id == trip.id))).all()
    )
    for row in rows:
        if row.state is TripEpisodeState.PENDING:
            row.state = TripEpisodeState.CANCELLED
    episode_ids = [row.episode_id for row in rows]
    # No want row is written here: the reconciliation queued below ends the
    # trip's rows by its own rules (deleted on a followed show, shelved
    # elsewhere) and leaves every row the trip did not bring in as it was.
    await session.flush()

    elsewhere = await pending_episode_ids(session, episode_ids)
    wanted = await _still_wanted(session, user.id, episode_ids)
    released = cancelled = 0
    orphaned: list[int] = []
    episodes = (
        await session.scalars(select(Episode).where(Episode.id.in_(episode_ids)))
        if episode_ids
        else None
    )
    for episode in episodes.all() if episodes is not None else []:
        if episode.id in wanted or episode.id in elsewhere:
            continue
        if await release_if_unwanted(session, episode, wanted_ids=wanted):
            released += 1
        elif await cancel_if_unwanted(session, episode, wanted_ids=wanted):
            cancelled += 1
            await session.flush()
            await enqueue_cancel(session, episode.id)
        elif episode.state in _LANDED:
            orphaned.append(episode.id)

    await drop_queued_copies(session, orphaned)
    for episode_id in orphaned:
        await enqueue_trip_release(session, episode_id)
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "trip cancelled",
        extra={
            "user_id": user.id,
            "trip_id": trip.id,
            "episodes": len(episode_ids),
            "released": released,
            "cancelled": cancelled,
            "bytes_to_delete": orphaned,
        },
    )
    return trip


__all__ = ["cancel_trip", "drop_queued_copies"]
