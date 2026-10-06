"""Deleting what a trip no longer needs, for one episode (FR-A12, FR-T7).

The body of the :data:`~arc.services.trips.names.TRIP_RELEASE` job. Two
endings, decided under a row lock on the episode so the answer cannot change
between the question and the deletion:

1. **The source, once the copy is made** (owner decision 4, 2026-10-05). The
   episode is *still* trip-only — re-checked here, because a normal want may
   have arrived since the copy hook queued this — and its copy is ready on
   disk. The source goes through retention's own deleter
   (:func:`~arc.services.retention.delete.delete_episode_files`), so a single's
   torrent is removed with its files and a pack's file is given back through
   its claim, ``qbit_reselect`` applying FR-A11's disposition. The deleter is
   handed targets **without** the copy, so the ``offline_copies`` row and file
   stay ``ready`` for the device, and the episode takes the existing edge to
   ``not_wanted``. While its trip want is live the reconciler leaves it there
   (:func:`~arc.services.acquisition.wants._start_searches` skips a trip-only
   episode with a ready copy); a normal want arriving later fetches it again
   by the ordinary path.
2. **Everything, once nothing needs it** (a cancelled trip). The episode has
   no live want (a trip row with no pending trip behind it counts as none —
   a cancel leaves those for the next reconciliation to end), no active trip
   is waiting on it, it was a trip's, and it is not ``ready``: the landed
   bytes and the copy are deleted now, without FR-T1's grace. A dropped want
   of somebody else's still counting its grace keeps landed *source* bytes
   for retention to measure — but not a ``not_wanted`` episode's copy, which
   has no source left to protect and which nothing else would ever delete.
   A ``trip ended`` drop counts no grace at all.

Anything else is left alone: a ``ready`` episode is retention's, an episode
with work in flight (``downloading``, ``matching``, ``preparing``) is not this
job's to interrupt, and an episode somebody wants for streaming is theirs. A
deletion refused because an encode still holds the source — the deleter's own
guard for an offline encode, :func:`_transcode_in_flight` for a queued or
running transcode — raises :class:`ReleaseDeferred`, so the runner's backoff
tries again once it has finished.

A copy some device confirmed less than ``TRIP_SETTLE_DELAY`` ago is kept by
the second ending (M19 T4): a second device of the same user may still be
fetching it, and the settle (:mod:`arc.services.trips.settle`) comes back for
it — deleting the copy and handing any source left behind back to this job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    OfflineCopy,
    OfflineCopyState,
    TripEpisode,
    Want,
)
from arc.services.acquisition.wants import REASON_TRIP_ENDED
from arc.services.jobs.queue import ACTIVE_STATUSES
from arc.services.media.copies import file_matches
from arc.services.media.names import latest_transcode_jobs, offline_path_for
from arc.services.retention.delete import delete_episode_files
from arc.services.retention.sweep import targets_for_episode
from arc.services.trips.hooks import stamp_available
from arc.services.trips.rules import latest_delivery, pending_episode_ids
from arc.services.trips.settle import settle_due

log = logging.getLogger(__name__)

#: What the two deletions are logged as.
REASON_COPY_MADE = "trip copy made; the source is no longer needed"
REASON_NOTHING_NEEDS_IT = "trip over; nothing needs these bytes"

#: States whose source a trip-only episode's copy is made from.
_SOURCE_STATES: frozenset[EpisodeState] = frozenset({EpisodeState.MATCHED, EpisodeState.FAILED})

#: States whose bytes an ended trip may delete at once.
_ORPHAN_STATES: frozenset[EpisodeState] = frozenset(
    {
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHED,
        EpisodeState.FAILED,
        EpisodeState.NOT_WANTED,
    }
)

type Outcome = Literal["source_deleted", "all_deleted", "kept"]


class ReleaseDeferred(RuntimeError):
    """The deleter refused for now (an encode holds the source). Retry later."""


@dataclass(frozen=True, slots=True)
class Release:
    """What :func:`release_episode` did, and why."""

    episode_id: int
    outcome: Outcome
    why: str


async def _wants(session: AsyncSession, episode_id: int) -> tuple[int, int, int]:
    """``(live wants, live trip wants, dropped rows still counting a grace)``.

    A row shelved because its trip ended (:data:`~arc.services.acquisition.
    wants.REASON_TRIP_ENDED`) counts no grace: the only user who wanted those
    bytes has just said so.
    """
    row = (
        await session.execute(
            select(
                func.count().filter(Want.dropped_at.is_(None)),
                func.count().filter(Want.dropped_at.is_(None), Want.trip.is_(True)),
                func.count().filter(
                    Want.dropped_at.is_not(None),
                    Want.drop_reason.is_distinct_from(REASON_TRIP_ENDED),
                ),
            ).where(Want.episode_id == episode_id)
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def _transcode_in_flight(session: AsyncSession, episode_id: int) -> bool:
    """Whether a ``transcode`` is queued or running for the episode.

    The deleter's own guard covers offline encodes only, and a queued
    transcode reads the source when it starts. The release **defers** (the
    runner retries) rather than cancelling it: a transcode queued for an
    episode means somebody — a promotion, an admin — asked for it, and the
    next run of this job sees what that turned into.
    """
    job = (await latest_transcode_jobs(session, [episode_id])).get(episode_id)
    return job is not None and job.status in ACTIVE_STATUSES


async def release_episode(session: AsyncSession, settings: Settings, episode_id: int) -> Release:
    """Apply the module docstring's two rules to one episode. Flushed, not committed."""
    episode = await session.scalar(
        select(Episode).where(Episode.id == episode_id).with_for_update()
    )
    if episode is None:
        return Release(episode_id, "kept", "the episode is gone")
    if episode.state is EpisodeState.READY:
        return Release(episode_id, "kept", "a ready episode is retention's")

    pending = bool(await pending_episode_ids(session, [episode_id]))
    live, trip, counting = await _wants(session, episode_id)
    if not pending:
        # A trip row with no trip waiting behind it is one a cancel has just
        # left for the next reconciliation to end; it wants nothing.
        live -= trip
        trip = 0
    copy = await session.get(OfflineCopy, episode_id)
    copy_ready = (
        copy is not None
        and copy.state is OfflineCopyState.READY
        and file_matches(offline_path_for(settings, episode_id), copy)
    )

    if live and live == trip:
        if not copy_ready:
            return Release(episode_id, "kept", "trip-only, but its copy is not ready")
        if episode.state not in _SOURCE_STATES:
            return Release(episode_id, "kept", f"trip-only and {episode.state.value}")
        if await _transcode_in_flight(session, episode_id):
            raise ReleaseDeferred(f"episode {episode_id}: a transcode still needs the source")
        targets = await targets_for_episode(session, settings, episode_id)
        # Everything but the copy: the device has yet to fetch it.
        targets = replace(targets, offline_file=None, offline_copy=False)
        removed = await delete_episode_files(
            session, settings, episode, targets, reason=REASON_COPY_MADE, keep_copy=True
        )
        if not removed.acted:
            raise ReleaseDeferred(f"episode {episode_id}: the deleter declined for now")
        await stamp_available(session, episode_id)
        await session.flush()
        return Release(episode_id, "source_deleted", REASON_COPY_MADE)

    if live:
        return Release(episode_id, "kept", "somebody wants it for streaming")
    if pending:
        return Release(episode_id, "kept", "a trip is still waiting on it")
    if copy is not None and settle_due(
        await latest_delivery(session, episode_id), now=datetime.now(UTC)
    ):
        # A device confirmed the copy within the hour: a second device of the
        # same user may still be fetching it. The settle that confirmation
        # queued (or the trip sweep's safety net) comes back for it, and hands
        # any source left over back to this job.
        return Release(episode_id, "kept", "a device confirmed its copy within the hour")
    # A ``not_wanted`` episode has no source left to protect: whatever it
    # holds is a trip's copy nobody is waiting for, and a dropped want of
    # somebody else's would otherwise keep it on the disk for ever — nothing
    # else (retention, the idle rule) ever looks at a non-ready episode's copy.
    if counting and episode.state is not EpisodeState.NOT_WANTED:
        return Release(episode_id, "kept", "a dropped want is still counting its grace period")
    if episode.state not in _ORPHAN_STATES:
        return Release(episode_id, "kept", f"work in flight ({episode.state.value})")
    was_trip = await session.scalar(
        select(TripEpisode.trip_id).where(TripEpisode.episode_id == episode_id).limit(1)
    )
    if was_trip is None:
        return Release(episode_id, "kept", "it was never a trip's")
    targets = await targets_for_episode(session, settings, episode_id)
    if targets.empty:
        return Release(episode_id, "kept", "nothing on the disk")
    if await _transcode_in_flight(session, episode_id):
        raise ReleaseDeferred(f"episode {episode_id}: a transcode still needs the source")
    removed = await delete_episode_files(
        session, settings, episode, targets, reason=REASON_NOTHING_NEEDS_IT
    )
    if not removed.acted:
        raise ReleaseDeferred(f"episode {episode_id}: the deleter declined for now")
    return Release(episode_id, "all_deleted", REASON_NOTHING_NEEDS_IT)


__all__ = [
    "REASON_COPY_MADE",
    "REASON_NOTHING_NEEDS_IT",
    "Release",
    "ReleaseDeferred",
    "release_episode",
]
