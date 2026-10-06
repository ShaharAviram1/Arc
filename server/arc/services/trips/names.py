"""Trip constants and the one job type, importable without the handler (FR-A12).

Handler-free, like :mod:`arc.services.media.names`: the copy hook in
:mod:`arc.services.media.offline` and the cancel path both queue
:data:`TRIP_RELEASE`, and neither may import the retention deleter to do it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job
from arc.services.jobs.queue import enqueue
from arc.services.media.names import OFFLINE_TRIP_PRIORITY, PRIORITY_PER_EPISODE

#: The two admin settings (FR-D2) and the ceiling the first may be set to.
TRIP_MAX_EPISODES_KEY: Final[str] = "trip_max_episodes"
TRIP_COPY_DAYS_KEY: Final[str] = "trip_copy_days"
TRIP_MAX_EPISODES_CEILING: Final[int] = 50

#: A trip's searches sort just behind the window's own (150): a trip is for
#: later, and somebody waiting to stream tonight is not.
TRIP_SEARCH_PRIORITY: Final[int] = 160

#: Fewest of a finished show's trip episodes worth preferring a pack for
#: (FR-A12, FR-A11; owner 2026-10-05). With this many wanted the search asks
#: the batch forms first and takes a pack covering at least
#: ``min(TRIP_BATCH_MIN, remaining attachable trip episodes)`` of them; below
#: it, or for an airing show, the ordinary single search runs unchanged.
TRIP_BATCH_MIN: Final[int] = 4

#: Settle what a trip has left on the disk for one episode (§5.4e): once its
#: copy is made, delete the source of a trip-only episode (owner decision 4,
#: 2026-10-05); once nothing needs it any more (a cancelled trip), delete the
#: landed bytes and the copy without the G grace. A job rather than a call
#: because both can talk to qBittorrent (a single's torrent is removed with its
#: files) and neither may hold a request's or an encode's transaction open
#: across that. M19 T4's settle queues it for a source a trip left behind.
TRIP_RELEASE: Final[str] = "trip_release"

#: Behind acquisition, in front of retention's 200: freeing a source the copy
#: has replaced is worth doing soon, never before somebody's search.
TRIP_RELEASE_PRIORITY: Final[int] = 180

#: How long after a copy is made its release runs. The encode job that queued
#: it is still ``running`` until its handler returns, and the deleter will not
#: touch a source an encode holds; a few seconds is enough for it to finish.
TRIP_RELEASE_DELAY: Final[timedelta] = timedelta(seconds=15)


#: Settle one trip episode's copy once its devices have it, or once its trip
#: has stopped waiting for it (M19 T4, :mod:`arc.services.trips.settle`): the
#: copy of an episode that is not ``ready`` is deleted when no trip row on it
#: is pending any more. A job because it deletes a file and is queued for
#: later (:data:`TRIP_SETTLE_DELAY`).
OFFLINE_SETTLE: Final[str] = "offline_settle"

#: The hourly pass over every active trip (M19 T4, :mod:`arc.services.trips.
#: sweep`): pending rows past their ``trip_copy_days`` clock expire, trips with
#: nothing pending end. Deduplicated on the type, like the retention sweep.
TRIP_SWEEP: Final[str] = "trip_sweep"

#: Next to :data:`TRIP_RELEASE`: a deletion nobody is waiting for, but one
#: worth doing before retention's 200.
OFFLINE_SETTLE_PRIORITY: Final[int] = TRIP_RELEASE_PRIORITY
TRIP_SWEEP_PRIORITY: Final[int] = 190

#: How long a delivered copy stays after the device confirms it (owner's
#: small call, 2026-10-05): long enough for a second device of the same user to
#: finish its own download from the same copy. Measured from the **latest**
#: confirmation on the episode, so two users' devices both get their hour.
TRIP_SETTLE_DELAY: Final[timedelta] = timedelta(hours=1)

#: How long a settle waits before looking again when an encode still holds
#: the episode (rare: a copy asked for again in the meantime).
TRIP_SETTLE_RETRY: Final[timedelta] = timedelta(minutes=15)


def copy_priority(position: int) -> int:
    """A trip copy's queue priority: 600 + 10 × its place in the trip (FR-P3)."""
    return OFFLINE_TRIP_PRIORITY + PRIORITY_PER_EPISODE * max(position, 0)


def release_dedupe_key(episode_id: int) -> str:
    """One release per episode in the queue at a time."""
    return f"{TRIP_RELEASE}:{episode_id}"


async def enqueue_trip_release(
    session: AsyncSession, episode_id: int, *, delay: timedelta = TRIP_RELEASE_DELAY
) -> Job:
    """Queue :data:`TRIP_RELEASE` for one episode, deduplicated. Flushed."""
    return await enqueue(
        session,
        TRIP_RELEASE,
        {"episode_id": episode_id},
        priority=TRIP_RELEASE_PRIORITY,
        run_after=datetime.now(UTC) + delay,
        dedupe_key=release_dedupe_key(episode_id),
    )


def settle_dedupe_key(episode_id: int) -> str:
    """One settle per episode in the queue at a time."""
    return f"{OFFLINE_SETTLE}:{episode_id}"


async def enqueue_offline_settle(
    session: AsyncSession,
    episode_id: int,
    *,
    run_after: datetime | None = None,
    exclude_job_id: int | None = None,
) -> Job:
    """Queue :data:`OFFLINE_SETTLE` for one episode, deduplicated. Flushed.

    ``run_after`` defaults to now. On a dedupe hit the queued job is returned
    as it stands — the settle itself re-reads the latest confirmation and
    puts itself back if it ran too early. ``exclude_job_id`` is for the
    settle that re-queues itself while it is still ``running``.
    """
    return await enqueue(
        session,
        OFFLINE_SETTLE,
        {"episode_id": episode_id},
        priority=OFFLINE_SETTLE_PRIORITY,
        run_after=run_after,
        dedupe_key=settle_dedupe_key(episode_id),
        exclude_job_id=exclude_job_id,
    )


async def enqueue_trip_sweep(session: AsyncSession) -> Job:
    """Queue :data:`TRIP_SWEEP`, deduplicated on the type. Flushed."""
    return await enqueue(session, TRIP_SWEEP, priority=TRIP_SWEEP_PRIORITY, dedupe_key=TRIP_SWEEP)


__all__ = [
    "OFFLINE_SETTLE",
    "OFFLINE_SETTLE_PRIORITY",
    "TRIP_BATCH_MIN",
    "TRIP_COPY_DAYS_KEY",
    "TRIP_MAX_EPISODES_CEILING",
    "TRIP_MAX_EPISODES_KEY",
    "TRIP_RELEASE",
    "TRIP_RELEASE_DELAY",
    "TRIP_RELEASE_PRIORITY",
    "TRIP_SEARCH_PRIORITY",
    "TRIP_SETTLE_DELAY",
    "TRIP_SETTLE_RETRY",
    "TRIP_SWEEP",
    "TRIP_SWEEP_PRIORITY",
    "copy_priority",
    "enqueue_offline_settle",
    "enqueue_trip_release",
    "enqueue_trip_sweep",
    "release_dedupe_key",
    "settle_dedupe_key",
]
