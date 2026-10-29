"""Redoing a trip's copies from a freshly fetched source (FR-A12, FR-P6).

Production 2026-10-29 (trip 5): copies were encoded from half-downloaded
season-pack files. One came out 9.9 MB and was delivered; the others were
refused or failed, their episodes ``matched`` to files that no longer
existed, their trip rows ``pending``, and nothing retried — a failed copy is
not retried automatically, and the reconciler leaves an episode with any copy
row to that row's own path. This is the lever (owner, 2026-10-29):
``python -m arc.cli redo-copies --trip ID (--episodes 1,3 | --failed)``, and
the same code path is what the encode runs **once** by itself when its
source turns out broken (:mod:`arc.services.media.offline`).

**Which episodes.** The named trip's rows: those whose copy is ``failed``
(``failed=True``), and/or the ``episode_ids`` named. Skipped, with the reason
in the report: an episode the trip does not hold; a ``cancelled`` or
``expired`` row (``ended``); a ``delivered`` row the device let go
(``released``); an episode the trip's user has finished (``watched``); an
episode a job is reading the source of right now — a copy, a transcode or a
remux (``encoding``), except the one encode redoing its own episode; and one
whose source the deleter refused to remove (``busy``: it is searching,
downloading, matching or preparing) — its copy row is left untouched.

**What each one gets**, in this order — the source first, so that a refusal
changes nothing:

a. The **source** goes through retention's own deleter
   (:func:`~arc.services.retention.delete.delete_episode_files`, the copy
   kept out of its targets): a single's torrent removed with its files, a
   pack's file given back through its claim, the episode reset to
   ``not_wanted``. A ``ready`` episode keeps its source — it streams from it —
   and its copy is queued again from it at once. An episode with no source
   (nothing linked, no file claimed) has nothing to remove and keeps its
   state. A refusal skips the episode (``busy``).
b. The copy's **file** is removed (the path is derived from the id) and, if
   it has a row, the row goes back to ``queued`` with nothing of the old copy
   on it (no source, size, ETag, error or ready time) and ``redone_at = now``.
   With no row at all (the settle took it) none is made: the link path, or
   :func:`~arc.services.acquisition.wants._requeue_trip_copies`, queues the
   encode when the new source arrives.
c. Every **delivered**, unreleased, unwatched row on the episode in an active
   trip goes back to ``pending`` (:func:`~arc.services.trips.deliver.repend`)
   and every pending one has its clock restarted: ``available_at = now``, as
   the remake does, so a trip near its deadline does not expire before the new
   copy exists. The device, seeing a new ETag, replaces its copy.

Then one ``compute_wants``: the trip's pending row wants the episode again,
``not_wanted`` → ``wanted`` → searched and fetched, and the linker queues the
copy (``offline_encode`` picks the new source for a ``queued`` row).

Locks are the remake's: every active trip holding a selected episode ``FOR
UPDATE`` in id order, then their rows, then the episodes. Flushed, not
committed: the caller commits. Nothing here touches a list entry or
MyAnimeList.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.media.copies import encoding_episode_ids
from arc.services.media.names import (
    OFFLINE_WHY_TRIP,
    enqueue_offline_encode,
    offline_path_for,
)
from arc.services.playback.progress import completed_episode_ids
from arc.services.retention.delete import delete_episode_files
from arc.services.retention.sweep import safe_path, targets_for_episode
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict
from arc.services.trips.deliver import repend
from arc.services.trips.rules import trip_copy_priority

log = logging.getLogger(__name__)

#: What a removed source is logged as.
REASON_REDO = "trip copy redone from a freshly fetched source"

#: Why an episode was not redone (``RedoReport.skipped``).
SKIP_NOT_IN_TRIP = "not in this trip"
SKIP_ENDED = "ended"
SKIP_RELEASED = "released"
SKIP_WATCHED = "watched"
SKIP_ENCODING = "encoding"
SKIP_BUSY = "busy"

_ENDED: frozenset[TripEpisodeState] = frozenset(
    {TripEpisodeState.CANCELLED, TripEpisodeState.EXPIRED}
)


@dataclass(frozen=True, slots=True)
class RedoReport:
    """What :func:`redo_copies` did for one trip.

    ``redone`` are the episode ids redone; ``skipped`` pairs each episode left
    alone with why (``SKIP_*``). Of the redone: ``sources_removed`` had their
    source deleted, ``repended`` named-trip rows went back from ``delivered``
    to ``pending``.
    """

    trip_id: int
    redone: tuple[int, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()
    sources_removed: int = 0
    repended: int = 0


def skip_reason(row: TripEpisode, *, watched: bool, encoding: bool) -> str | None:
    """Why the named trip's ``row`` is not redone, or ``None``. Pure."""
    if row.state in _ENDED:
        return SKIP_ENDED
    if row.state is TripEpisodeState.DELIVERED and row.released_at is not None:
        return SKIP_RELEASED
    if watched:
        return SKIP_WATCHED
    if encoding:
        return SKIP_ENCODING
    return None


async def _failed_copies(session: AsyncSession, episode_ids: Iterable[int]) -> set[int]:
    ids = list(episode_ids)
    if not ids:
        return set()
    return set(
        (
            await session.scalars(
                select(OfflineCopy.episode_id).where(
                    OfflineCopy.episode_id.in_(ids),
                    OfflineCopy.state == OfflineCopyState.FAILED,
                )
            )
        ).all()
    )


def _remove_copy_file(settings: Settings, episode_id: int) -> bool:
    """Unlink ``offline/<id>.mp4`` when it is there, re-checked against the roots."""
    checked = safe_path(offline_path_for(settings, episode_id), settings)
    if checked is None:
        return False
    try:
        checked.unlink()
    except FileNotFoundError:
        return False
    return True


def reset_copy(copy: OfflineCopy, *, now: datetime) -> None:
    """Put a copy row back to ``queued`` with nothing of the old copy on it. Pure."""
    copy.state = OfflineCopyState.QUEUED
    copy.error = None
    copy.media_file_id = None
    copy.size = None
    copy.etag = None
    copy.ready_at = None
    copy.redone_at = now


async def redo_copies(
    session: AsyncSession,
    settings: Settings,
    *,
    trip: Trip,
    now: datetime,
    episode_ids: list[int] | None = None,
    failed: bool = False,
    exclude_job_id: int | None = None,
) -> RedoReport:
    """Redo ``trip``'s chosen copies (module docstring). Flushed, not committed.

    ``exclude_job_id`` is the ``offline_encode`` calling on its own episode
    (the automatic redo after a broken source): it is not counted as an encode
    in flight. :class:`~arc.services.trips.create.TripConflict`
    (``trip_not_active``) unless the trip is active.
    """
    own = {
        row.episode_id: row
        for row in (
            await session.scalars(select(TripEpisode).where(TripEpisode.trip_id == trip.id))
        ).all()
    }
    skipped: list[tuple[int, str]] = []
    chosen: set[int] = set()
    for episode_id in episode_ids or []:
        if episode_id in own:
            chosen.add(episode_id)
        elif (episode_id, SKIP_NOT_IN_TRIP) not in skipped:
            skipped.append((episode_id, SKIP_NOT_IN_TRIP))
    if failed:
        chosen |= await _failed_copies(session, own)

    trip_ids = {trip.id}
    if chosen:
        trip_ids |= set(
            (
                await session.scalars(
                    select(TripEpisode.trip_id)
                    .join(Trip, Trip.id == TripEpisode.trip_id)
                    .where(
                        TripEpisode.episode_id.in_(sorted(chosen)),
                        Trip.state == TripState.ACTIVE,
                    )
                )
            ).all()
        )

    # Locks: every trip in id order, then their rows, then the episodes.
    trips = {
        locked.id: locked
        for locked in (
            await session.scalars(
                select(Trip)
                .where(Trip.id.in_(trip_ids))
                .order_by(Trip.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    named = trips.get(trip.id)
    if named is None or named.state is not TripState.ACTIVE:
        raise TripConflict(TRIP_NOT_ACTIVE)
    active = [locked.id for locked in trips.values() if locked.state is TripState.ACTIVE]
    rows = list(
        (
            await session.scalars(
                select(TripEpisode)
                .where(TripEpisode.trip_id.in_(active), TripEpisode.episode_id.in_(sorted(chosen)))
                .order_by(TripEpisode.trip_id, TripEpisode.episode_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    episodes = {
        episode.id: episode
        for episode in (
            await session.scalars(
                select(Episode)
                .where(Episode.id.in_(sorted(chosen)))
                .order_by(Episode.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    }

    watched: dict[int, frozenset[int]] = {}
    for trip_id in active:
        user_id = trips[trip_id].user_id
        if user_id not in watched:
            watched[user_id] = await completed_episode_ids(
                session, user_id=user_id, episode_ids=sorted(chosen)
            )
    # Anything reading the source: a copy, a transcode, a remux (§5.3c).
    encoding = await encoding_episode_ids(session, sorted(chosen), exclude_job_id=exclude_job_id)

    redone: list[int] = []
    sources_removed = 0
    repended = 0
    for episode_id in sorted(chosen):
        mine = next(
            (row for row in rows if row.trip_id == named.id and row.episode_id == episode_id),
            None,
        )
        episode = episodes.get(episode_id)
        if mine is None or episode is None:  # pragma: no cover - a row deleted meanwhile
            skipped.append((episode_id, SKIP_NOT_IN_TRIP))
            continue
        reason = skip_reason(
            mine,
            watched=episode_id in watched[named.user_id],
            encoding=episode_id in encoding,
        )
        if reason is not None:
            skipped.append((episode_id, reason))
            continue

        # a. The source first: if it cannot go now, nothing else changes.
        source_removed = False
        if episode.state is not EpisodeState.READY:
            targets = await targets_for_episode(session, settings, episode_id)
            targets = replace(targets, offline_file=None, offline_copy=False)
            if not targets.empty or targets.torrent_file_ids:
                outcome = await delete_episode_files(
                    session,
                    settings,
                    episode,
                    targets,
                    reason=REASON_REDO,
                    keep_copy=True,
                    exclude_job_id=exclude_job_id,
                )
                if not outcome.acted:
                    # In flight (searching, downloading, matching, preparing)
                    # or a reader holds it: the copy row is left as it was, so
                    # the one automatic retry is not spent on nothing.
                    skipped.append((episode_id, SKIP_BUSY))
                    continue
                source_removed = True
                sources_removed += 1

        # b. The copy: file gone, row back to queued and stamped.
        removed_file = _remove_copy_file(settings, episode_id)
        copy = await session.get(OfflineCopy, episode_id, populate_existing=True)
        if copy is not None:
            reset_copy(copy, now=now)
        if episode.state is EpisodeState.READY:
            # It streams from that source; the copy is made from it again.
            await enqueue_offline_encode(
                session,
                episode_id,
                why=OFFLINE_WHY_TRIP,
                priority=await trip_copy_priority(session, episode_id),
            )

        # c. Every live row on it waits for the new copy.
        for row in rows:
            if row.episode_id != episode_id:
                continue
            user_watched = episode_id in watched[trips[row.trip_id].user_id]
            if row.state is TripEpisodeState.DELIVERED:
                if row.released_at is not None or user_watched:
                    continue
                repend(row, available_at=now)
                if row.trip_id == named.id:
                    repended += 1
            elif row.state is TripEpisodeState.PENDING:
                row.available_at = now
        redone.append(episode_id)
        log.info(
            "trip copy redone",
            extra={
                "trip_id": named.id,
                "episode_id": episode_id,
                "copy_file_removed": removed_file,
                "copy_row": copy is not None,
                "source_removed": source_removed,
                "episode_state": episode.state.value,
            },
        )

    if redone:
        await enqueue_compute_wants(session)
    await session.flush()
    return RedoReport(
        trip_id=named.id,
        redone=tuple(redone),
        skipped=tuple(skipped),
        sources_removed=sources_removed,
        repended=repended,
    )


async def redo_after_broken_source(
    session: AsyncSession,
    settings: Settings,
    *,
    episode_id: int,
    now: datetime,
    exclude_job_id: int | None = None,
) -> RedoReport | None:
    """The encode's one automatic redo (FR-P6, owner 2026-10-29). Flushed, not committed.

    Only when some **active** trip has a ``pending`` row on the episode and
    the copy has not been redone before (``redone_at`` is null), and the
    episode is not ``ready`` (it streams from that source); ``None``
    otherwise, with nothing written. The redo is :func:`redo_copies` for that
    one episode under the first such trip.
    """
    copy = await session.get(OfflineCopy, episode_id, populate_existing=True)
    if copy is None or copy.redone_at is not None:
        return None
    episode = await session.get(Episode, episode_id, populate_existing=True)
    if episode is None or episode.state is EpisodeState.READY:
        # A ready episode streams from that source; it is not refetched for a copy.
        return None
    trip = await session.scalar(
        select(Trip)
        .join(TripEpisode, TripEpisode.trip_id == Trip.id)
        .where(
            TripEpisode.episode_id == episode_id,
            TripEpisode.state == TripEpisodeState.PENDING,
            Trip.state == TripState.ACTIVE,
        )
        .order_by(Trip.id)
        .limit(1)
    )
    if trip is None:
        return None
    return await redo_copies(
        session,
        settings,
        trip=trip,
        now=now,
        episode_ids=[episode_id],
        exclude_job_id=exclude_job_id,
    )


__all__ = [
    "REASON_REDO",
    "SKIP_BUSY",
    "SKIP_ENCODING",
    "SKIP_ENDED",
    "SKIP_NOT_IN_TRIP",
    "SKIP_RELEASED",
    "SKIP_WATCHED",
    "RedoReport",
    "redo_after_broken_source",
    "redo_copies",
    "reset_copy",
    "skip_reason",
]
