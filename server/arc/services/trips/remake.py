"""Re-making a trip's copies with the current encode settings (FR-A12, FR-P6).

An admin's ``python -m arc.cli remake-copies`` (owner, 2026-10-28: the copies
went from 720p H.264 CRF 26 to 1080p HEVC CRF 28, and the trip under way was
to follow). A copy is **stale** when the ``settings_key`` its row recorded
differs from the one a copy made now would record
(:func:`~arc.services.media.offline.current_settings_key`, the encode's own
computation). A copy row with no key yet (queued, never started) is not
stale: whatever makes it will use the current settings.

For each ``trip_episodes`` row of the trip whose episode has a stale copy:

* a ``cancelled`` or ``expired`` row, or a ``delivered`` one whose device has
  already let it go (``released_at`` set — deleted once watched, FR-S8,
  owner 2026-10-08), is **skipped**: never re-pend an episode the device gave
  up;
* otherwise the stale copy is removed the way the settle removes one — a
  *pending* ``offline_encode`` dropped
  (:func:`~arc.services.trips.cancel.drop_queued_copies`), then file and row
  (:func:`~arc.services.retention.delete.delete_copy`). A *running* encode is
  left alone, as everywhere else: the copy counts as **skipped** and nothing
  on its row changes;
* then a ``delivered`` row goes back to ``pending`` with its stamps cleared
  (:func:`~arc.services.trips.deliver.repend`, "again"'s own shape) —
  **repended** — and a ``pending`` row has its ``available_at`` cleared, so
  the copy hook stamps it when the new copy is ready — **cleared**.

One ``compute_wants`` is queued when anything was removed. With no copy row
left, the reconciler queues the copy again from a source still on disk
(:func:`~arc.services.acquisition.wants._requeue_trip_copies`), or wants and
fetches a trip-only episode again — its source went when the first copy was
made (owner decision 4) — after which the linker queues the copy. The
device, seeing the new copy's ETag on an episode it holds, replaces its own
(``client/src/offline/downloads.ts`` ``adoptTrip``).

Locks in the order every trip writer takes them
(:func:`~arc.services.trips.deliver._row`): the trip ``FOR UPDATE``, its
rows, then the episodes. Flushed, not committed: the caller commits. Nothing
here touches a want, a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, OfflineCopy, Trip, TripEpisode, TripEpisodeState, TripState
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.media.offline import current_settings_key
from arc.services.retention.delete import delete_copy
from arc.services.trips.cancel import drop_queued_copies
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict
from arc.services.trips.deliver import repend

log = logging.getLogger(__name__)

#: What a removed stale copy is logged as, and the reason on a dropped job.
REASON_REMAKE = "trip copy re-made with the current encode settings"


@dataclass(frozen=True, slots=True)
class RemakeReport:
    """What :func:`remake_stale` found and did for one trip.

    ``stale`` counts the trip's episodes whose copy was made with other
    settings; each of them is then exactly one of ``repended`` (a delivered
    row back to pending), ``cleared`` (a pending row's ``available_at``
    cleared) or ``skipped`` (left alone: the device let it go, the row is
    cancelled or expired, or an encode is running on it).
    """

    trip_id: int
    stale: int = 0
    repended: int = 0
    cleared: int = 0
    skipped: int = 0


def leaves_alone(row: TripEpisode) -> bool:
    """Whether a stale copy's row is skipped rather than re-made. Pure.

    A cancelled or expired row is history, and a delivered row with
    ``released_at`` is one the device already deleted: none is put back.
    """
    if row.state in (TripEpisodeState.CANCELLED, TripEpisodeState.EXPIRED):
        return True
    return row.state is TripEpisodeState.DELIVERED and row.released_at is not None


async def remake_stale(
    session: AsyncSession, settings: Settings, *, trip: Trip, now: datetime
) -> RemakeReport:
    """Re-make ``trip``'s stale copies (module docstring). Flushed, not committed.

    :class:`~arc.services.trips.create.TripConflict` (``trip_not_active``)
    unless the trip is active.
    """
    locked = await session.scalar(
        select(Trip)
        .where(Trip.id == trip.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None or locked.state is not TripState.ACTIVE:
        raise TripConflict(TRIP_NOT_ACTIVE)
    rows = list(
        (
            await session.scalars(
                select(TripEpisode)
                .where(TripEpisode.trip_id == locked.id)
                .order_by(TripEpisode.episode_id)
                .with_for_update()
            )
        ).all()
    )
    ids = [row.episode_id for row in rows]
    if ids:
        await session.execute(
            select(Episode.id).where(Episode.id.in_(ids)).order_by(Episode.id).with_for_update()
        )
    copies = (
        {
            copy.episode_id: copy
            for copy in (
                await session.scalars(select(OfflineCopy).where(OfflineCopy.episode_id.in_(ids)))
            ).all()
        }
        if ids
        else {}
    )

    stale = repended = cleared = skipped = 0
    for row in rows:
        copy = copies.get(row.episode_id)
        if copy is None or copy.settings_key is None:
            continue
        if copy.settings_key == await current_settings_key(session, settings, row.episode_id):
            continue
        stale += 1
        if leaves_alone(row):
            skipped += 1
            continue
        await drop_queued_copies(session, [row.episode_id], reason=REASON_REMAKE)
        if not await delete_copy(session, settings, row.episode_id, reason=REASON_REMAKE):
            # A running encode holds it; it is not stopped mid-flight.
            skipped += 1
            continue
        if row.state is TripEpisodeState.DELIVERED:
            repend(row, available_at=None)
            repended += 1
        else:
            row.available_at = None
            cleared += 1

    if repended or cleared:
        await enqueue_compute_wants(session)
    await session.flush()
    report = RemakeReport(
        trip_id=locked.id, stale=stale, repended=repended, cleared=cleared, skipped=skipped
    )
    log.info(
        "trip copies re-made with the current settings",
        extra={
            "trip_id": locked.id,
            "user_id": locked.user_id,
            "stale": stale,
            "repended": repended,
            "cleared": cleared,
            "skipped": skipped,
            "at": now.isoformat(),
        },
    )
    return report


__all__ = ["REASON_REMAKE", "RemakeReport", "leaves_alone", "remake_stale"]
