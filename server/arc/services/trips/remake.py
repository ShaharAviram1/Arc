"""Re-making a trip's copies with the current encode settings (FR-A12, FR-P6).

An admin's ``python -m arc.cli remake-copies`` (owner, 2026-10-28: the copies
went from 720p H.264 CRF 26 to 1080p HEVC CRF 28, and the trip under way was
to follow). A copy is **stale** when what its row recorded — codec, CRF,
audio bitrate, the picture's height — is not what an encode now would make
(:func:`~arc.services.media.copies.made_otherwise`; not the language-bearing
``settings_key``, which could read stale on every run for an episode with no
rendition).

**Which rows.** The stale episodes of the named trip are found first; then
every *active* trip holding one of them — the named one and any other
user's — is locked (trips in id order, then their rows, then the episodes,
before anything is deleted: the order every trip writer takes,
:func:`~arc.services.trips.deliver._row`), and each live row of a stale
episode in any of them follows the same rules:

* **skipped**, untouched: a ``cancelled`` or ``expired`` row; a
  ``delivered`` one whose device let it go (``released_at``, e.g. deleted
  once watched, FR-S8); a ``delivered``, unreleased one whose trip's user has
  completed the episode (it would be fetched again for nobody);
* **held**: a device fetched the copy within the settle's quiet window
  (:func:`~arc.services.trips.settle.fetch_hold`): row and copy are left
  alone, and the operator runs the command again later;
* otherwise the stale copy is removed the way the settle removes one — a
  *pending* ``offline_encode`` dropped
  (:func:`~arc.services.trips.cancel.drop_queued_copies`), then file and row
  (:func:`~arc.services.retention.delete.delete_copy`; the file is unlinked
  before the caller commits, as the settle's is). A *running* encode makes
  the deleter refuse: the rows are **skipped**, unchanged;
* then a ``delivered`` row goes back to ``pending``
  (:func:`~arc.services.trips.deliver.repend`, "again"'s shape) —
  **repended** — and a ``pending`` row stays pending — **cleared**. Both get
  ``available_at = now``: the ``trip_copy_days`` clock restarts, as "again"
  restarts it, so a trip near its deadline is not expired by the next sweep
  before the new copy exists (the phase never reads ``available_at``; the
  copy hook stamps only rows that have none).

**Copies the settle already took** (``delivered_before``, owner 2026-10-28).
An hour after a confirmation the settle deletes a trip-only episode's copy,
row and file, so nothing on the server records how the copy on the device
was made. Given the time the settings changed, a ``delivered``, unreleased,
unwatched row delivered before it whose episode has **no** copy row is
re-pended the same way (``repend``, ``available_at = now``, **repended**);
the same skips and the other-trips rule apply. A row whose episode has a
current copy row is left alone whenever it was delivered: the device may
hold that copy, and the ETag rule decides. Without the time, nothing
copyless is touched.

One ``compute_wants`` is queued when anything was removed or re-pended. With no copy row
left, the reconciler queues the copy again from a source still on disk
(:func:`~arc.services.acquisition.wants._requeue_trip_copies`), or wants and
fetches a trip-only episode again — its source went when the first copy was
made (owner decision 4) — after which the linker queues the copy. The
device, seeing the new copy's ETag on an episode it holds, replaces its own
(``client/src/offline/downloads.ts`` ``adoptTrip``); a late confirmation of
the old copy leaves the row pending (:func:`~arc.services.trips.deliver.
confirm_delivered`).

Flushed, not committed: the caller commits. Nothing here touches a want, a
list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    OfflineCopy,
    Rendition,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.media.copies import made_otherwise, offline_options
from arc.services.playback.progress import completed_episode_ids
from arc.services.retention.delete import delete_copy
from arc.services.trips.cancel import drop_queued_copies
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict
from arc.services.trips.deliver import repend
from arc.services.trips.settle import fetch_hold

log = logging.getLogger(__name__)

#: What a removed stale copy is logged as, and the reason on a dropped job.
REASON_REMAKE = "trip copy re-made with the current encode settings"

#: The rows that never come back.
_ENDED: frozenset[TripEpisodeState] = frozenset(
    {TripEpisodeState.CANCELLED, TripEpisodeState.EXPIRED}
)


@dataclass(frozen=True, slots=True)
class RemakeReport:
    """What :func:`remake_stale` found and did for one trip's rows.

    ``stale`` counts the trip's episodes whose copy was made with other
    settings; each is then exactly one of ``repended`` (a delivered row back
    to pending), ``cleared`` (a pending row whose copy goes), ``held`` (a
    device is fetching the copy right now; run again later) or ``skipped``
    (left alone: the device let it go or the user watched it, the row is
    cancelled or expired, or an encode is running on it).
    """

    trip_id: int
    stale: int = 0
    repended: int = 0
    cleared: int = 0
    held: int = 0
    skipped: int = 0


def leaves_alone(row: TripEpisode, *, watched: bool = False) -> bool:
    """Whether a stale copy's row is skipped rather than re-made. Pure.

    A cancelled or expired row is history; a delivered row with
    ``released_at`` is one the device already deleted; a delivered row whose
    user has completed the episode (``watched``) has no reason to be fetched
    again. None is put back.
    """
    if row.state in _ENDED:
        return True
    if row.state is TripEpisodeState.DELIVERED:
        return row.released_at is not None or watched
    return False


async def _source_heights(session: AsyncSession, episode_ids: list[int]) -> dict[int, int]:
    """``episode → rendition height``: the source's own height, where known."""
    if not episode_ids:
        return {}
    rows = await session.execute(
        select(Rendition.episode_id, Rendition.height).where(
            Rendition.episode_id.in_(episode_ids), Rendition.height.is_not(None)
        )
    )
    return {episode_id: int(height) for episode_id, height in rows.all() if height is not None}


async def _stale_episodes(
    session: AsyncSession, settings: Settings, episode_ids: list[int]
) -> dict[int, OfflineCopy]:
    """The copies of ``episode_ids`` made otherwise, re-read from the database."""
    if not episode_ids:
        return {}
    copies = (
        await session.scalars(
            select(OfflineCopy)
            .where(OfflineCopy.episode_id.in_(episode_ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    heights = await _source_heights(session, episode_ids)
    options = offline_options(settings)
    return {
        copy.episode_id: copy
        for copy in copies
        if made_otherwise(copy, options, source_height=heights.get(copy.episode_id))
    }


async def _copyless(session: AsyncSession, episode_ids: list[int]) -> set[int]:
    """Which of ``episode_ids`` have no ``offline_copies`` row at all."""
    if not episode_ids:
        return set()
    having = set(
        (
            await session.scalars(
                select(OfflineCopy.episode_id).where(OfflineCopy.episode_id.in_(episode_ids))
            )
        ).all()
    )
    return set(episode_ids) - having


def delivered_before_cutoff(row: TripEpisode, cutoff: datetime) -> bool:
    """Whether a copyless row was delivered before ``cutoff`` (still held). Pure.

    The settle deletes a delivered copy an hour after the confirmation, row
    and file, so nothing records how the copy on the device was made; the
    operator names the time the settings changed instead.
    """
    return (
        row.state is TripEpisodeState.DELIVERED
        and row.delivered_at is not None
        and row.delivered_at < cutoff
    )


async def remake_stale(
    session: AsyncSession,
    settings: Settings,
    *,
    trip: Trip,
    now: datetime,
    delivered_before: datetime | None = None,
) -> RemakeReport:
    """Re-make ``trip``'s stale copies (module docstring). Flushed, not committed.

    ``delivered_before``, when given, also re-pends a ``delivered`` row
    delivered before that time whose episode has **no** copy row any more
    (the settle took it): the device holds a copy made the old way, and
    nothing on the server can say so. The same skips apply (released,
    watched); a row whose episode has a *current* copy row is left alone
    whenever it was delivered — the ETag rule on the device decides. Without
    it, a copyless row is never touched.

    :class:`~arc.services.trips.create.TripConflict` (``trip_not_active``)
    unless the trip is active. The report counts the named trip's rows; every
    other trip touched gets a log line of its own.
    """
    # Discovery, unlocked: which episodes, and which active trips hold them.
    own = list(
        (
            await session.scalars(
                select(TripEpisode.episode_id).where(TripEpisode.trip_id == trip.id)
            )
        ).all()
    )
    first_pass = set(await _stale_episodes(session, settings, own))
    if delivered_before is not None:
        first_pass |= await _copyless(session, own)
    trip_ids = {trip.id}
    if first_pass:
        trip_ids |= set(
            (
                await session.scalars(
                    select(TripEpisode.trip_id)
                    .join(Trip, Trip.id == TripEpisode.trip_id)
                    .where(
                        TripEpisode.episode_id.in_(list(first_pass)),
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
                .where(TripEpisode.trip_id.in_(active))
                .order_by(TripEpisode.trip_id, TripEpisode.episode_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    episode_ids = sorted({row.episode_id for row in rows})
    if episode_ids:
        await session.execute(
            select(Episode.id)
            .where(Episode.id.in_(episode_ids))
            .order_by(Episode.id)
            .with_for_update()
        )

    # Decided again under the locks: only the named trip's episodes count.
    stale = await _stale_episodes(session, settings, sorted(set(own)))
    copyless = await _copyless(session, sorted(set(own))) if delivered_before is not None else set()
    by_episode: dict[int, list[TripEpisode]] = defaultdict(list)
    for row in rows:
        if row.episode_id in stale or row.episode_id in copyless:
            by_episode[row.episode_id].append(row)
    watched: dict[int, frozenset[int]] = {}
    for trip_id in active:
        user_id = trips[trip_id].user_id
        if user_id not in watched:
            watched[user_id] = await completed_episode_ids(
                session, user_id=user_id, episode_ids=sorted(set(stale) | copyless)
            )

    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    removed = False
    for episode_id, copy in sorted(stale.items()):
        holders = by_episode.get(episode_id, [])
        for row in holders:
            counts[row.trip_id]["stale"] += 1
        live = [
            row
            for row in holders
            if not leaves_alone(row, watched=episode_id in watched[trips[row.trip_id].user_id])
        ]
        for row in holders:
            if row not in live:
                counts[row.trip_id]["skipped"] += 1
        if not live:
            continue
        if fetch_hold(copy.last_served_at, None, now=now) is not None:
            for row in live:
                counts[row.trip_id]["held"] += 1
            continue
        await drop_queued_copies(session, [episode_id], reason=REASON_REMAKE)
        if not await delete_copy(session, settings, episode_id, reason=REASON_REMAKE):
            # A running encode holds it; it is not stopped mid-flight.
            for row in live:
                counts[row.trip_id]["skipped"] += 1
            continue
        removed = True
        for row in live:
            if row.state is TripEpisodeState.DELIVERED:
                repend(row, available_at=now)
                counts[row.trip_id]["repended"] += 1
            else:
                row.available_at = now
                counts[row.trip_id]["cleared"] += 1

    # The settle took these copies; the operator's time says they were old.
    if delivered_before is not None:
        for episode_id in sorted(copyless):
            for row in by_episode.get(episode_id, []):
                if not delivered_before_cutoff(row, delivered_before):
                    continue
                counts[row.trip_id]["stale"] += 1
                if leaves_alone(row, watched=episode_id in watched[trips[row.trip_id].user_id]):
                    counts[row.trip_id]["skipped"] += 1
                    continue
                repend(row, available_at=now)
                counts[row.trip_id]["repended"] += 1
                removed = True

    if removed:
        await enqueue_compute_wants(session)
    await session.flush()
    for trip_id, tally in sorted(counts.items()):
        log.info(
            "trip copies re-made with the current settings",
            extra={
                "trip_id": trip_id,
                "user_id": trips[trip_id].user_id,
                "named": trip_id == named.id,
                **tally,
            },
        )
    mine = counts.get(named.id, {})
    return RemakeReport(
        trip_id=named.id,
        stale=mine.get("stale", 0),
        repended=mine.get("repended", 0),
        cleared=mine.get("cleared", 0),
        held=mine.get("held", 0),
        skipped=mine.get("skipped", 0),
    )


__all__ = [
    "REASON_REMAKE",
    "RemakeReport",
    "delivered_before_cutoff",
    "leaves_alone",
    "remake_stale",
]
